"""Phase 11 hardening round: labels from API text, one place for an expired sign-in, an interrupted
question, the settings file, the notices on the hidden-answers list, widget keys that carry an id,
a failed sign-out, and the table download."""

import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import frontend.api as frontend_api
from frontend import messages, render
from frontend.api import ApiClient, SessionExpired
from frontend.config import Settings, config_file_found, load_settings
from frontend.views import admin
from tests.frontend_fixtures import (
    FAKE_TOKEN,
    MESSAGE,
    FakeApi,
    answer_body,
    error,
    html_bodies,
    json_response,
    page_text,
    signed_in,
    signed_out,
)
from tests.test_frontend import QUESTION, admin_api, ask, button, open_section, sessions_api

ROOT = Path(__file__).resolve().parents[1]

# What makes Streamlit label text into something else: an address, a formula, a shortcode, a colour,
# a markdown link, an entity, an e-mail address.
LABEL_SYNTAX = re.compile(r"://|www\.|\$|:\w+:|:\w+\[|@|&\w+;|&#|\]\(|\*\*")
HOSTILE = (
    "see https://evil.example/x www.evil.example $x$ :warning: :red[boo] a@evil.example "
    "&amp; [t](https://evil.example) **bold**"
)


def every_label(at) -> list[str]:
    """The text of every widget label, option and caption on the page."""
    found: list[str] = []
    for group in (at.button, at.checkbox, at.radio, at.selectbox, at.expander, at.text_input):
        for widget in group:
            found.append(str(getattr(widget, "label", "")))
            found.extend(str(o) for o in getattr(widget, "options", []) or [])
    found.extend(str(c.value) for c in at.caption)
    found.extend(str(t.label) for t in at.tabs)
    return found


def assert_clean(at):
    labels = every_label(at)
    assert labels
    for label in labels:
        assert not LABEL_SYNTAX.search(label), label


# ============================================================================== labels ====
@pytest.mark.parametrize(
    "text",
    ["https://evil.example/x", "www.evil.example", "$x$", ":warning:", "a@b.example", "&amp;"],
)
def test_safe_text_leaves_no_address_formula_shortcode_or_entity(text):
    shown = render.safe_text(f"see {text} now")
    assert not LABEL_SYNTAX.search(shown), shown
    assert not LABEL_SYNTAX.search(render.label_text(text))


def test_safe_text_keeps_ordinary_words():
    assert render.safe_text("Waiting period for illness") == "Waiting period for illness"


def test_session_titles_with_hostile_text_are_clean_in_the_sidebar(monkeypatch):
    api = FakeApi()
    sessions_api(api, [HOSTILE, "Waiting period"])
    at = signed_in(api, monkeypatch).run()
    assert not at.exception, at.exception
    assert_clean(at)
    assert "Waiting period" in [b.label for b in at.sidebar.button]


def test_hostile_names_in_every_admin_widget_are_clean(monkeypatch):
    api = admin_api()
    api.routes[("GET", "/admin/users")] = json_response(
        200,
        {
            "total": 2,
            "limit": 50,
            "offset": 0,
            "users": [
                {"id": "u1", "username": "boss", "role": "admin", "is_active": True},
                {"id": "u3", "username": HOSTILE, "role": "customer", "is_active": True},
            ],
        },
    )
    api.routes[("GET", "/admin/entities")] = json_response(
        200,
        {
            "total": 1,
            "limit": 20,
            "offset": 0,
            "entities": [
                {
                    "id": "e1",
                    "text": HOSTILE,
                    "entity_type": "person_in_policy",
                    "doc_id": "doc-a",
                    "status": "pending",
                    "approved_at": None,
                },
            ],
        },
    )
    api.routes[("GET", "/admin/shadow/answers")] = json_response(
        200,
        {
            "total": 1,
            "limit": 20,
            "offset": 0,
            "answers": [
                {
                    "message_id": "m",
                    "question": HOSTILE,
                    "answer": HOSTILE,
                    "citations": [],
                    "grounded": True,
                    "source_removed": False,
                    "kind": "answer",
                    "user": "h",
                },
            ],
        },
    )
    at = signed_in(
        api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
    ).run()
    for section in ("Users", "Entities", "Rollout"):
        open_section(at, section)
        assert_clean(at)


def test_a_caption_built_from_a_number_or_text_goes_through_the_same_neutraliser(monkeypatch):
    api = admin_api()
    api.routes[("GET", "/admin/alerts")] = json_response(
        200, {"banner": False, "critical": [], "events": [], "window": "$7$ https://x.example"}
    )
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    open_section(at, "Alerts")
    assert not at.exception, at.exception  # a count that is not a number is read as 0
    assert_clean(at)


def test_render_caption_neutralises_whatever_it_is_given(monkeypatch):
    shown = []
    monkeypatch.setattr(render.st, "caption", lambda text, *a, **k: shown.append(text))
    render.caption(HOSTILE)
    assert len(shown) == 1 and not LABEL_SYNTAX.search(shown[0])


# ============================================================================== table cells ====
@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\t=1"])
def test_a_cell_that_starts_like_a_formula_gets_an_apostrophe(value):
    assert admin.cell(value) == "'" + value


@pytest.mark.parametrize("value", ["boss", "10", None, "a=b", ""])
def test_an_ordinary_cell_is_unchanged(value):
    assert admin.cell(value) == ("" if value is None else str(value))


def test_the_users_table_holds_the_prefixed_cell(monkeypatch):
    api = admin_api()
    api.routes[("GET", "/admin/users")] = json_response(
        200,
        {
            "total": 1,
            "limit": 50,
            "offset": 0,
            "users": [
                {
                    "id": "u3",
                    "username": "=cmd|' /C calc'!A0",
                    "role": "customer",
                    "is_active": True,
                }
            ],
        },
    )
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    open_section(at, "Users")
    shown = at.dataframe[0].value.to_string()
    assert "'=cmd" in shown


# ========================================================= one place for an expired token ====
def expire(api, method, path):
    api.routes[(method, path)] = error(401, "not_signed_in", "Please sign in again.")


def assert_signed_out(at):
    assert not at.exception, at.exception
    assert "fx_token" not in at.session_state
    assert [t for t in at.text_input if t.label == "Username"]  # the sign-in page
    assert "Please sign in again." in page_text(at)
    assert FAKE_TOKEN not in page_text(at)


def test_an_expired_token_on_the_chats_list_signs_out(monkeypatch):
    api = FakeApi()
    expire(api, "GET", "/sessions")
    assert_signed_out(signed_in(api, monkeypatch).run())


def test_an_expired_token_on_resume_signs_out(monkeypatch):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    expire(api, "GET", f"/sessions/{rows[0]['id']}")
    at = signed_in(api, monkeypatch).run()
    button(at, "Waiting period").click().run()
    assert_signed_out(at)


def test_an_expired_token_on_feedback_signs_out(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    expire(api, "POST", f"/messages/{MESSAGE}/feedback")
    button(at, "👍 Helpful").click().run()
    assert_signed_out(at)


@pytest.mark.parametrize(
    ("section", "path"),
    [
        ("Users", "/admin/users"),
        ("Documents", "/admin/documents"),
        ("Review queue", "/admin/review"),
        ("Entities", "/admin/entities"),
        ("Rollout", "/admin/shadow"),
        ("Alerts", "/admin/alerts"),
    ],
)
def test_an_expired_token_on_an_admin_call_signs_out(monkeypatch, section, path):
    api = admin_api()
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    expire(api, "GET", path)
    at.radio[0].set_value(section).run()
    assert_signed_out(at)


def test_an_expired_token_on_the_banner_signs_out(monkeypatch):
    api = admin_api()
    expire(api, "GET", "/admin/alerts")
    assert_signed_out(signed_in(api, monkeypatch, role="admin", username="boss").run())


def test_the_client_raises_for_every_call_made_with_a_token(monkeypatch):
    api = FakeApi()
    expire(api, "GET", "/auth/me")
    monkeypatch.setattr(frontend_api, "TRANSPORT", api.transport)
    with pytest.raises(SessionExpired):
        ApiClient(Settings(api_base_url="http://api.test"), FAKE_TOKEN).me()
    # a call made without a token (the sign-in itself) never raises it
    assert ApiClient(Settings(api_base_url="http://api.test")).me().code == "not_signed_in"


def test_signing_out_drops_every_fx_key(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    for key in ("fx_user_pick", "fx_section", "fx_pending", "fx_rated", "fx_confirm", "fx_zzz"):
        at.session_state[key] = "x"
    button(at, "Sign out").click().run()
    # what is left are the empty boxes of the sign-in page that was drawn after signing out
    left = [k for k in at.session_state.keys() if str(k).startswith("fx_")]
    assert set(left) <= {"fx_reg_user", "fx_reg_email", "fx_reg_pass"}, left
    assert all(not at.session_state[k] for k in left)


def test_the_next_person_on_the_same_server_session_sees_nothing_of_the_last(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    button(at, "Sign out").click().run()
    shown = page_text(at)
    assert "waiting period" not in shown.lower() and "asha_rao" not in shown
    assert "fx_messages" not in at.session_state and "fx_session_id" not in at.session_state


# ==================================================================== a failed sign-out ====
def test_a_failed_sign_out_drops_the_token_here_and_says_so(monkeypatch):
    api = FakeApi()

    def unreachable(request):
        raise httpx.ConnectError("down", request=request)

    api.routes[("POST", "/auth/logout")] = unreachable
    at = signed_in(api, monkeypatch).run()
    button(at, "Sign out").click().run()
    assert "fx_token" not in at.session_state
    assert messages.LOGOUT_FAILED_TITLE in "\n".join(html_bodies(at))


def test_a_clean_sign_out_says_nothing_extra(monkeypatch):
    at = signed_in(FakeApi(), monkeypatch).run()
    button(at, "Sign out").click().run()
    assert messages.LOGOUT_FAILED_TITLE not in "\n".join(html_bodies(at))


# ===================================================================== double submit ====
def test_an_interrupted_question_is_removed_and_the_new_one_is_not_sent(monkeypatch):
    api = FakeApi()
    at = signed_in(api, monkeypatch).run()
    at.session_state["fx_messages"] = [{"role": "user", "text": "The first question"}]
    at.session_state["fx_pending"] = True  # the run that was waiting for it was cut short
    at.chat_input[0].set_value("A second question").run()
    assert not at.exception, at.exception
    assert not api.calls("POST", "/chat")  # not sent: the earlier one may still be answered
    bodies = "\n".join(html_bodies(at))
    assert messages.INTERRUPTED_TITLE in bodies
    assert "The first question" not in bodies and "A second question" not in bodies
    assert "fx_pending" not in at.session_state
    # and the next question goes through
    at.chat_input[0].set_value("A third question").run()
    assert len(api.calls("POST", "/chat")) == 1


def test_a_normal_question_leaves_no_pending_flag_and_no_interrupted_notice(monkeypatch):
    at = ask(signed_in(FakeApi(), monkeypatch).run())
    assert "fx_pending" not in at.session_state
    assert messages.INTERRUPTED_TITLE not in "\n".join(html_bodies(at))
    assert QUESTION in "\n".join(html_bodies(at))  # and the question is still on the page


def test_the_flag_is_cleared_when_the_service_gives_an_error(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = error(409, "busy", "Busy.")
    at = ask(signed_in(api, monkeypatch).run())
    assert "fx_pending" not in at.session_state
    assert messages.INTERRUPTED_TITLE not in "\n".join(html_bodies(at))


# ================================================================== Retry-After, waking ====
def test_a_rate_limit_shows_how_long_to_wait(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = error(429, "too_many_requests", "Slow.", **{"Retry-After": "7"})
    at = ask(signed_in(api, monkeypatch).run())
    assert "about 7 seconds" in "\n".join(html_bodies(at))


def test_a_dropped_connection_is_not_treated_as_waking_and_is_not_asked_again(monkeypatch):
    api = FakeApi()

    def dropped(request):
        raise httpx.RemoteProtocolError("closed", request=request)

    api.routes[("POST", "/chat")] = dropped
    at = ask(signed_in(api, monkeypatch).run())
    assert len(api.calls("POST", "/chat")) == 1 and not api.calls("GET", "/live")
    assert messages.CONNECTION_ERROR in "\n".join(html_bodies(at))


@pytest.mark.parametrize("status", [502, 503, 504])
def test_only_the_hosts_gateway_pages_count_as_waking(monkeypatch, status):
    api = FakeApi()
    api.routes[("GET", "/live")] = httpx.Response(status, text="<html>gateway</html>")
    monkeypatch.setattr(frontend_api, "TRANSPORT", api.transport)
    assert ApiClient(Settings()).live().waking
    api.routes[("GET", "/live")] = httpx.Response(500, text="oops")
    assert not ApiClient(Settings()).live().waking


# ==================================================================== the settings file ====
def test_the_settings_file_is_found_from_the_project_folder_and_not_from_another(tmp_path):
    assert config_file_found(ROOT) is True
    assert config_file_found(tmp_path) is False


def test_streamlit_really_reads_the_settings_file_from_the_folder_it_starts_in(tmp_path):
    code = (
        "import streamlit.config as c;"
        "print(c.get_option('server.enableXsrfProtection'),"
        "c.get_option('client.showErrorDetails'),"
        "c.get_option('browser.gatherUsageStats'))"
    )
    here = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert here.stdout.split()[-3:] == ["True", "none", "False"], here.stderr[-300:]
    elsewhere = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=120
    )
    # the same options from another folder: the file is NOT read, so the details come back
    assert elsewhere.stdout.split()[-2] != "none"


def test_a_start_from_another_folder_shows_the_warning(monkeypatch, tmp_path):
    at = signed_out(FakeApi(), monkeypatch)
    monkeypatch.chdir(tmp_path)
    at.run()
    assert messages.CONFIG_MISSING_TITLE in "\n".join(html_bodies(at))


def test_a_start_from_the_project_folder_shows_no_warning(monkeypatch):
    at = signed_out(FakeApi(), monkeypatch)
    monkeypatch.chdir(ROOT)
    at.run()
    assert messages.CONFIG_MISSING_TITLE not in "\n".join(html_bodies(at))


# ==================================================================== plain http address ====
@pytest.mark.parametrize(
    ("address", "insecure"),
    [
        ("http://api.example.test", True),
        ("http://203.0.113.5:8000", True),
        ("https://api.example.test", False),
        ("http://127.0.0.1:8000", False),
        ("http://localhost:8000", False),
        ("http://[::1]:8000", False),
    ],
)
def test_plain_http_to_another_host_is_flagged(address, insecure):
    assert load_settings({"API_BASE_URL": address}).insecure_api is insecure


def test_the_default_address_is_not_flagged():
    assert load_settings({}).insecure_api is False


def test_an_insecure_address_shows_a_warning_on_the_sign_in_page(monkeypatch):
    at = signed_out(FakeApi(), monkeypatch, env={"API_BASE_URL": "http://api.example.test"}).run()
    assert messages.INSECURE_API_TITLE in "\n".join(html_bodies(at))
    at = signed_out(FakeApi(), monkeypatch, env={"API_BASE_URL": "https://api.example.test"}).run()
    assert messages.INSECURE_API_TITLE not in "\n".join(html_bodies(at))


# ========================================================================= fixed replies ====
def resumed(monkeypatch, *, kind, grounded):
    api = FakeApi()
    rows = sessions_api(api, ["Greeting"])
    session_id = rows[0]["id"]
    api.routes[("GET", f"/sessions/{session_id}")] = json_response(
        200,
        {
            "id": session_id,
            "title": "Greeting",
            "status": "active",
            "total": 2,
            "messages": [
                {"id": "m1", "role": "user", "text": "Hello!", "citations": [], "kind": None},
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "Hello! How can I help?",
                    "citations": [],
                    "grounded": grounded,
                    "source_removed": False,
                    "notice": None,
                    "kind": kind,
                },
            ],
        },
    )
    at = signed_in(api, monkeypatch).run()
    button(at, "Greeting").click().run()
    assert not at.exception, at.exception
    return at


def test_a_resumed_fixed_reply_has_no_unverified_notice_and_no_thumbs(monkeypatch):
    at = resumed(monkeypatch, kind="fixed_reply", grounded=False)
    assert messages.UNVERIFIED_TEXT not in "\n".join(html_bodies(at))
    assert not [b for b in at.button if b.label in ("👍 Helpful", "👎 Not helpful")]
    assert "Hello! How can I help?" in "\n".join(html_bodies(at))


def test_a_resumed_generated_answer_that_failed_verification_has_the_notice_and_thumbs(monkeypatch):
    at = resumed(monkeypatch, kind="answer", grounded=False)
    assert messages.UNVERIFIED_TEXT in "\n".join(html_bodies(at))
    assert [b for b in at.button if b.label == "👍 Helpful"]


def test_an_older_service_without_the_marker_is_treated_as_an_answer(monkeypatch):
    at = resumed(monkeypatch, kind=None, grounded=False)
    assert messages.UNVERIFIED_TEXT in "\n".join(html_bodies(at))


# =============================================================== hidden-answers notices ====
def shadow_api(**item):
    api = admin_api()
    base = {
        "message_id": "m1",
        "question": "A masked question",
        "answer": "The stored answer text.",
        "citations": [{"doc_id": "doc-a", "clause": "4.2", "page": 7}],
        "grounded": True,
        "source_removed": False,
        "kind": "answer",
        "user": "h",
    }
    base.update(item)
    api.routes[("GET", "/admin/shadow/answers")] = json_response(
        200, {"total": 1, "limit": 20, "offset": 0, "answers": [base]}
    )
    return api


def rollout_bodies(monkeypatch, **item):
    at = signed_in(shadow_api(**item), monkeypatch, role="admin", username="boss").run()
    open_section(at, "Rollout")
    return "\n".join(html_bodies(at))


def test_a_hidden_answer_that_was_not_verified_shows_the_notice(monkeypatch):
    bodies = rollout_bodies(monkeypatch, grounded=False)
    assert messages.UNVERIFIED_TEXT in bodies and "The stored answer text." in bodies


def test_a_hidden_verified_answer_shows_no_notice_and_keeps_its_sources(monkeypatch):
    bodies = rollout_bodies(monkeypatch, grounded=True)
    assert messages.UNVERIFIED_TEXT not in bodies and "doc-a · clause 4.2 · page 7" in bodies


def test_a_hidden_fixed_reply_gets_no_unverified_notice(monkeypatch):
    bodies = rollout_bodies(monkeypatch, grounded=False, kind="fixed_reply", citations=[])
    assert messages.UNVERIFIED_TEXT not in bodies


def test_a_hidden_answer_of_a_withdrawn_source_has_the_title_and_no_sources(monkeypatch):
    bodies = rollout_bodies(
        monkeypatch,
        source_removed=True,
        citations=[],
        answer="This answer cited a document that has since been withdrawn.",
    )
    assert "Source withdrawn" in bodies and "has since been withdrawn" in bodies
    assert "fx-chip" not in bodies


# ========================================================================== never self ====
def users_api(me_role):
    api = admin_api()
    api.routes[("GET", "/admin/users")] = json_response(
        200,
        {
            "total": 4,
            "limit": 50,
            "offset": 0,
            "users": [
                {"id": "u1", "username": "boss", "role": "admin", "is_active": True},
                {"id": "u3", "username": "ravi", "role": me_role, "is_active": True},
                {"id": "u4", "username": "meera", "role": "staff", "is_active": True},
                {"id": "u5", "username": "tara", "role": "customer", "is_active": True},
            ],
        },
    )
    return api


def pick(at, user_id):
    [s for s in at.selectbox if s.label == "Account"][0].set_value(user_id).run()


def test_no_role_or_switch_controls_for_my_own_account_even_when_it_is_not_an_admin_row(
    monkeypatch,
):
    """The rule "never self" on its own: the signed-in id is a row the service lists as a plain
    customer (the page's idea of the role is behind), and still gets no controls."""
    api = users_api("customer")
    at = signed_in(api, monkeypatch, role="admin", username="ravi", user_id="u3").run()
    open_section(at, "Users")
    pick(at, "u3")
    assert "Your own account cannot be changed here." in "\n".join(c.value for c in at.caption)
    assert not [b for b in at.button if b.label in ("Save role", "Switch off", "Switch on")]


def test_no_controls_for_another_admins_account(monkeypatch):
    api = users_api("customer")
    at = signed_in(api, monkeypatch, role="admin", username="boss", user_id="u9").run()
    open_section(at, "Users")
    pick(at, "u1")  # an admin who is not me
    assert "Admin accounts cannot be changed here." in "\n".join(c.value for c in at.caption)
    assert not [b for b in at.button if b.label in ("Save role", "Switch off", "Switch on")]


# ============================================================== widget keys carry the id ====
def entity_api():
    api = admin_api()
    api.routes[("PUT", "/admin/entities/e1")] = json_response(200, {"id": "e1"})
    api.routes[("GET", "/admin/entities")] = json_response(
        200,
        {
            "total": 2,
            "limit": 20,
            "offset": 0,
            "entities": [
                {
                    "id": "e1",
                    "text": "Sharma",
                    "entity_type": "person_in_policy",
                    "doc_id": "doc-a",
                    "status": "pending",
                    "approved_at": None,
                },
                {
                    "id": "e3",
                    "text": "Verma",
                    "entity_type": "person_in_policy",
                    "doc_id": "doc-a",
                    "status": "pending",
                    "approved_at": None,
                },
            ],
        },
    )
    api.routes[("PUT", "/admin/entities/e3")] = json_response(200, {"id": "e3"})
    return api


def test_a_ticked_confirmation_is_not_carried_to_the_next_entry(monkeypatch):
    api = entity_api()
    at = signed_in(
        api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
    ).run()
    open_section(at, "Entities")
    at.checkbox[0].check().run()
    assert at.checkbox[0].value is True
    [s for s in at.selectbox if s.label == "Entry"][0].set_value("e3").run()
    assert at.checkbox[0].value is False  # a fresh entry: a fresh, unticked confirmation
    button(at, "Approve").click().run()
    assert api.calls("PUT", "/admin/entities/e3")[0][2] == {"status": "approved"}


def test_a_role_picked_for_one_account_is_not_carried_to_another(monkeypatch):
    api = users_api("customer")
    api.routes[("PUT", "/admin/users/u5/role")] = json_response(200, {"id": "u5"})
    at = signed_in(api, monkeypatch, role="admin", username="boss", user_id="u1").run()
    open_section(at, "Users")
    pick(at, "u5")  # a customer
    [s for s in at.selectbox if s.label == "Role"][0].set_value("staff").run()
    pick(at, "u4")  # already staff: shows its own role
    assert [s for s in at.selectbox if s.label == "Role"][0].value == "staff"
    pick(at, "u3")  # a customer: shows customer, not the "staff" chosen for u5
    assert [s for s in at.selectbox if s.label == "Role"][0].value == "customer"
    button(at, "Save role").click().run()
    assert api.calls("PUT", "/admin/users/u3/role")[0][2] == {"role": "customer"}


class Cut(BaseException):
    """Stands for the run being cut short while the service call is in flight."""


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_run_cut_short_while_waiting_leaves_the_flag_and_the_next_run_cleans_up(monkeypatch):
    api = FakeApi()

    def cut(request):
        raise Cut

    api.routes[("POST", "/chat")] = cut
    at = signed_in(api, monkeypatch).run()
    at.chat_input[0].set_value(QUESTION).run()
    # the question is on the page with no answer and the flag says an answer was awaited
    assert at.session_state["fx_pending"] is True
    assert at.session_state["fx_messages"][-1] == {"role": "user", "text": QUESTION}
    api.routes[("POST", "/chat")] = json_response(200, answer_body())
    at.run()  # the page comes back (a reload, or another action)
    assert "fx_pending" not in at.session_state
    assert [m for m in at.session_state["fx_messages"] if m.get("text") == QUESTION] == []
    assert messages.INTERRUPTED_TITLE in "\n".join(html_bodies(at))
