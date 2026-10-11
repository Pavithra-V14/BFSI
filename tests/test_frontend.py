"""The Streamlit frontend (Phase 11): plain-text rendering, the API client, the waiting states, the
chat and session flows, and the admin sections. Everything runs against a fake API (no network)."""

import httpx
import pytest

import frontend.api as frontend_api
from frontend import messages, progress, render
from frontend.api import ApiClient
from frontend.config import ConfigError, Settings, load_settings
from tests.frontend_fixtures import (
    FAKE_TOKEN,
    MESSAGE,
    OTHER_MESSAGE,
    SESSION,
    FakeApi,
    answer_body,
    error,
    html_bodies,
    json_response,
    page_text,
    signed_in,
    signed_out,
)

QUESTION = "What is the waiting period for illness claims?"


def ask(at, text=QUESTION):
    at.chat_input[0].set_value(text).run()
    assert not at.exception, at.exception
    return at


def button(at, label):
    found = [b for b in at.button if b.label == label]
    assert found, [b.label for b in at.button]
    return found[0]


# ================================================================================ config ====
def test_the_default_is_the_local_api_and_nothing_is_an_admin_machine():
    settings = load_settings({})
    assert settings.api_base_url == "http://127.0.0.1:8000"
    assert settings.admin_machine is False and settings.on_host is False


def test_the_api_address_is_read_and_a_trailing_slash_dropped():
    assert load_settings({"API_BASE_URL": "https://api.example.test/"}).api_base_url == (
        "https://api.example.test"
    )


@pytest.mark.parametrize("value", ["ftp://x.test", "not a url", "//x.test", "http:///x"])
def test_a_bad_api_address_is_refused_naming_the_variable_and_never_its_value(value):
    with pytest.raises(ConfigError) as caught:
        load_settings({"API_BASE_URL": value})
    assert "API_BASE_URL" in str(caught.value) and value not in str(caught.value)


def test_the_admin_machine_switch_is_ignored_on_the_host():
    assert load_settings({"ADMIN_MACHINE": "true"}).admin_machine is True
    assert load_settings({"ADMIN_MACHINE": "true", "RENDER": "true"}).admin_machine is False
    assert load_settings({"ADMIN_MACHINE": "false"}).admin_machine is False


# =========================================================================== plain text ====
HOSTILE = [
    "<script>alert(1)</script>",
    "[click here](https://evil.example/x)",
    "https://evil.example/path?next=a@irdai.gov.in",
    "<img src=x onerror=alert(1)>",
    "&lt;b&gt; &amp;lt; &#60;script&#62;",
    "<a href='javascript:alert(1)'>x</a>",
    "**bold** __bold__ `code`",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_plain_never_leaves_markup_or_a_link(text):
    shown = render.plain(text)
    assert "<" not in shown and ">" not in shown
    card = render.message_card("Assistant", text, footnote=text)
    assert card.count(shown) == 2  # the answer and the footnote carry only the escaped form
    outside = card.replace(shown, "")  # what is left is the frontend's own, constant markup
    for fragment in ("<script", "<img", "<a ", "href=", "onerror", "javascript:"):
        assert fragment not in outside


def test_plain_removes_control_and_direction_characters():
    assert render.plain("a‮b​c\x00d") == "abcd"


def test_a_button_label_has_no_markdown_and_is_short():
    label = render.label_text("[Open](https://evil.example) **now** `x` <b>y</b>\nsecond line")
    for character in "[]()*`<>":
        assert character not in label
    assert "\n" not in label
    assert len(render.label_text("w" * 500)) <= render.MAX_LABEL
    assert render.label_text("   ") == "Untitled chat"


def test_the_disclaimer_is_split_off_as_a_footnote_only_when_the_service_says_so():
    text = "Answer.\n\nThis is general information, not advice."
    assert render.split_disclaimer(text, True) == (
        "Answer.",
        "This is general information, not advice.",
    )
    assert render.split_disclaimer(text, False) == (text, "")
    assert render.split_disclaimer("One paragraph only.", True) == ("One paragraph only.", "")


def test_a_citation_label_names_the_document_clause_and_page():
    assert render.citation_label({"doc_id": "doc-a", "clause": "4.2", "page": 7}) == (
        "doc-a · clause 4.2 · page 7"
    )
    assert render.citation_label({"doc_id": "doc-a", "page": 7, "page_end": 9}).endswith("page 7-9")


# ================================================================================ client ====
def client_for(handler, token=None):
    return ApiClient(Settings(api_base_url="http://api.test"), token)


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        monkeypatch.setattr(frontend_api, "TRANSPORT", httpx.MockTransport(handler))

    return install


def test_a_reply_carries_the_status_and_the_json_body(transport):
    transport(lambda request: json_response(200, {"a": 1}))
    reply = client_for(None).live()
    assert reply.ok and reply.data == {"a": 1}


def test_the_token_goes_in_the_header_and_nowhere_else(transport):
    seen = []

    def handler(request):
        seen.append(request)
        return json_response(200, {})

    transport(handler)
    client = client_for(handler, FAKE_TOKEN)
    client.me()
    client.chat("hello", SESSION)
    for request in seen:
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        assert FAKE_TOKEN not in str(request.url) and FAKE_TOKEN.encode() not in request.content
    assert FAKE_TOKEN not in repr(client) and FAKE_TOKEN not in str(client)


def test_a_refusal_has_a_code_a_plain_message_and_a_retry_time(transport):
    transport(lambda r: error(429, "too_many_requests", "Slow down.", **{"Retry-After": "12"}))
    reply = client_for(None, FAKE_TOKEN).chat("x", None)
    assert reply.kind == "error" and reply.code == "too_many_requests"
    assert reply.message == "Slow down." and reply.retry_after == 12


def test_an_unreachable_service_is_waking_not_an_error(transport):
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    transport(refuse)
    reply = client_for(None).live()
    assert reply.waking and reply.kind == "waking"


def test_a_gateway_page_from_the_host_is_waking_but_the_apis_own_503_is_not(transport):
    transport(lambda r: httpx.Response(502, text="<html>Bad gateway</html>"))
    assert client_for(None).live().waking
    transport(lambda r: error(503, "starting_up", "The service is starting up."))
    reply = client_for(None).live()
    assert reply.kind == "error" and reply.code == "starting_up"


def test_a_slow_answer_is_a_timeout_not_waking(transport):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    transport(slow)
    assert client_for(None).live().kind == "timeout"


def test_a_page_with_no_body_or_a_bad_body_does_not_break_the_client(transport):
    transport(lambda r: httpx.Response(500, text="oops"))
    reply = client_for(None).live()
    assert reply.kind == "error" and reply.code == "http_error"


# ========================================================================= waiting states ====
def test_the_stage_text_follows_the_time_and_claims_no_real_step():
    assert progress.stage_for(0) == "Checking your question"
    assert progress.stage_for(5) == "Searching the documents"
    assert progress.stage_for(13) == "Writing the answer"
    assert "longer than usual" in progress.stage_for(60)
    for _, text in messages.PROGRESS_STAGES:  # generic names, no claim about a real step
        assert "step" not in text.lower() and "%" not in text


class Slot:
    def __init__(self):
        self.shown = []
        self.cleared = 0

    def html(self, text):
        self.shown.append(text)

    def empty(self):
        self.cleared += 1


def test_the_waiting_state_shows_an_elapsed_timer_and_ends_with_the_reply():
    import threading

    from frontend.api import Reply

    release = threading.Event()

    def work():
        release.wait(5)
        return Reply("ok", 200, {"x": 1})

    slot = Slot()
    timer = threading.Timer(0.3, release.set)
    timer.start()
    reply = progress.wait_with_progress(work, slot, tick=0.05)
    assert reply.ok and slot.cleared == 1
    assert slot.shown and "Checking your question" in slot.shown[0]
    assert " s<" in slot.shown[0]  # the timer


def test_waiting_for_a_waking_service_ends_when_it_answers():
    answers = iter([False, False, True])

    class Client:
        def live(self):
            from frontend.api import Reply

            return Reply("ok" if next(answers) else "waking")

    ticks = iter(range(100))
    slot = Slot()
    awake = progress.wait_until_awake(
        Client(),
        slot,
        max_seconds=60,
        poll_seconds=3,
        clock=lambda: next(ticks),
        sleep=lambda s: None,
    )
    assert awake is True and slot.cleared == 1
    assert all("waking up" in shown for shown in slot.shown)


def test_waiting_for_a_service_that_never_wakes_gives_up():
    class Client:
        def live(self):
            from frontend.api import Reply

            return Reply("waking")

    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    awake = progress.wait_until_awake(
        Client(), Slot(), max_seconds=10, poll_seconds=3, clock=clock, sleep=sleep
    )
    assert awake is False


# ====================================================================== sign in, sign out ====
def test_signing_in_stores_the_token_in_the_session_and_shows_the_chat(monkeypatch):
    api = FakeApi()
    at = signed_out(api, monkeypatch).run()
    assert not at.exception and at.text_input[0].label == "Username"
    at.text_input[0].set_value("asha_rao")
    at.text_input[1].set_value("a long password 123")
    button(at, "Sign in").click().run()
    assert not at.exception, at.exception
    assert at.session_state["fx_token"] == FAKE_TOKEN
    assert at.session_state["fx_user"]["username"] == "asha_rao"
    # the token is never part of anything rendered, and the password is not echoed either
    shown = page_text(at)
    assert FAKE_TOKEN not in shown and "a long password 123" not in shown
    login = api.calls("POST", "/auth/login")[0]
    assert login[2] == {"username": "asha_rao", "password": "a long password 123"}


def test_a_failed_sign_in_says_so_in_plain_words_and_echoes_nothing(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/auth/login")] = error(
        401, "invalid_credentials", "Wrong username or password."
    )
    at = signed_out(api, monkeypatch).run()
    at.text_input[0].set_value("asha_rao")
    at.text_input[1].set_value("wrong password 1")
    button(at, "Sign in").click().run()
    assert "Could not sign in" in page_text(at)
    assert "wrong password 1" not in page_text(at) and "fx_token" not in at.session_state


def test_a_new_account_is_created_as_a_customer_account(monkeypatch):
    api = FakeApi()
    at = signed_out(api, monkeypatch).run()
    at.text_input[2].set_value("new_user")
    at.text_input[3].set_value("new@example.test")
    at.text_input[4].set_value("another long password 9")
    button(at, "Create account").click().run()
    assert "Account created" in page_text(at)
    body = api.calls("POST", "/auth/register")[0][2]
    assert set(body) == {"username", "email", "password"}  # no role is ever sent


def test_signing_out_revokes_the_token_and_clears_the_session(monkeypatch):
    api = FakeApi()
    at = signed_in(api, monkeypatch).run()
    button(at, "Sign out").click().run()
    assert api.calls("POST", "/auth/logout")
    assert "fx_token" not in at.session_state and "fx_messages" not in at.session_state


def test_a_token_the_service_no_longer_accepts_goes_back_to_sign_in(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = error(401, "not_signed_in", "Please sign in again.")
    at = ask(signed_in(api, monkeypatch).run())
    assert "fx_token" not in at.session_state
    assert "Please sign in again." in page_text(at)


# ==================================================================================== chat ====
def test_an_answer_is_shown_with_its_sources_and_thumbs(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert "A waiting period of 30 days applies." in bodies
    assert "doc-a · clause 4.2 · page 7" in bodies
    assert {b.label for b in at.button} >= {"👍 Helpful", "👎 Not helpful"}
    chat_call = api.calls("POST", "/chat")[0]
    assert chat_call[2] == {"message": QUESTION}  # a new session: no session id sent
    assert at.session_state["fx_session_id"] == SESSION


def test_the_next_question_continues_the_same_session(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    ask(at, "And for accidents?")
    assert api.calls("POST", "/chat")[1][2] == {
        "message": "And for accidents?",
        "session_id": SESSION,
    }


def test_model_text_is_shown_escaped_never_as_markup_or_a_link(monkeypatch):
    hostile = "<script>alert(1)</script> [x](https://evil.example) https://evil.example/a"
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(200, answer_body(text=hostile))
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert "&lt;script&gt;" in bodies and "<script" not in bodies
    assert "href=" not in bodies and "<a " not in bodies
    assert not at.markdown or all("evil.example" not in m.value for m in at.markdown)


def test_warnings_low_confidence_and_reduced_mode_are_shown_as_words(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(
        200,
        answer_body(
            warnings=["Some figures could not be verified: 30."],
            confidence="low",
            degraded=["lexical_rerank", "something_new"],
        ),
    )
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert "Some figures could not be verified: 30." in bodies
    assert messages.LOW_CONFIDENCE_TITLE in bodies
    assert messages.DEGRADED_TEXT["lexical_rerank"] in bodies
    assert messages.DEGRADED_DEFAULT in bodies
    assert bodies.count("Reduced mode") == 2 and bodies.count("Check this") >= 2


def test_the_disclaimer_is_a_footnote_with_the_services_own_words(monkeypatch):
    text = "The premium is 5,000 a year.\n\nThis is general information, not advice."
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(200, answer_body(text=text, disclaimer=True))
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert '<div class="fx-footnote">This is general information, not advice.</div>' in bodies


def test_a_clarifying_reply_is_shown_as_an_assistant_message_with_the_box_left_open(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(
        200,
        answer_body(
            kind="clarify",
            text="I understood this is about a health policy. Which cover do you mean?",
            citations=[],
            message_id=OTHER_MESSAGE,
        ),
    )
    at = ask(signed_in(api, monkeypatch).run())
    assert "Which cover do you mean?" in "\n".join(html_bodies(at))
    assert at.chat_input  # the question box is still there to answer it
    assert not any(b.label == "👍 Helpful" for b in at.button)  # nothing to rate on a question


def test_shadow_mode_shows_the_fixed_notice_and_nothing_of_the_answer(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(
        200,
        {
            "answer_visible": False,
            "kind": "shadow_notice",
            "text": "The assistant is in a test period. Answers are not shown yet.",
            "session_id": SESSION,
        },
    )
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert messages.TEST_PERIOD_TITLE in bodies and "test period" in bodies
    assert "fx-chip" not in bodies and "Helpful" not in str([b.label for b in at.button])


def test_without_a_message_id_the_thumbs_are_off_with_a_short_note(monkeypatch):
    api = FakeApi()
    api.routes[("POST", "/chat")] = json_response(200, answer_body(message_id=None))
    at = ask(signed_in(api, monkeypatch).run())
    assert not any(b.label == "👍 Helpful" for b in at.button)
    assert messages.NO_FEEDBACK_YET in "\n".join(c.value for c in at.caption)


def test_a_thumb_is_sent_for_that_answers_message_and_thanked_once(monkeypatch):
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run())
    button(at, "👍 Helpful").click().run()
    assert api.calls("POST", f"/messages/{MESSAGE}/feedback")[0][2] == {"rating": "up"}
    assert messages.FEEDBACK_THANKS in "\n".join(c.value for c in at.caption)
    assert not any(b.label == "👍 Helpful" for b in at.button)  # one thumb per answer


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (429, "too_many_requests", "too quickly"),
        (429, "over_budget", "usage limit"),
        (409, "busy", "still being answered"),
        (503, "starting_up", "starting up"),
        (503, "service_unavailable", "temporarily unavailable"),
    ],
)
def test_each_refusal_is_shown_in_plain_words(monkeypatch, status, code, expected):
    api = FakeApi()
    api.routes[("POST", "/chat")] = error(status, code, "From the service.", **{"Retry-After": "7"})
    at = ask(signed_in(api, monkeypatch).run())
    shown = "\n".join(html_bodies(at))
    assert expected in shown and "Could not answer" in shown
    assert "Traceback" not in shown and "status" not in shown.lower().split("could not")[0]


def test_a_slow_answer_is_explained_not_shown_as_a_crash(monkeypatch):
    api = FakeApi()

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    api.routes[("POST", "/chat")] = slow
    at = ask(signed_in(api, monkeypatch).run())
    assert messages.TIMEOUT_TEXT in "\n".join(html_bodies(at))


# ===================================================================== cold start ====
def test_a_service_that_is_waking_up_is_waited_for_and_the_question_is_asked_again(monkeypatch):
    api = FakeApi()
    attempts = {"chat": 0}

    def chat(request):
        attempts["chat"] += 1
        if attempts["chat"] == 1:
            raise httpx.ConnectError("asleep", request=request)
        return json_response(200, answer_body())

    api.routes[("POST", "/chat")] = chat
    monkeypatch.setattr(progress.time, "sleep", lambda s: None)
    at = ask(signed_in(api, monkeypatch).run())
    assert attempts["chat"] == 2
    assert api.calls("GET", "/live")  # it asked whether the service was up before retrying
    assert "A waiting period of 30 days applies." in "\n".join(html_bodies(at))
    assert "Could not answer" not in "\n".join(html_bodies(at))


def test_a_service_that_does_not_wake_gives_a_calm_message_not_an_error(monkeypatch):
    api = FakeApi()

    def asleep(request):
        raise httpx.ConnectError("asleep", request=request)

    api.routes[("POST", "/chat")] = asleep
    api.routes[("GET", "/live")] = asleep
    monkeypatch.setattr(progress.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10_000, 30))
    monkeypatch.setattr(progress.time, "monotonic", lambda: next(ticks))
    at = ask(signed_in(api, monkeypatch).run())
    bodies = "\n".join(html_bodies(at))
    assert messages.WAKING_TITLE in bodies and messages.WAKING_GAVE_UP in bodies
    assert "Could not answer" not in bodies


# ====================================================================== sessions ====
def sessions_api(api, titles):
    rows = [
        {
            "id": f"{i:08d}-1111-4111-8111-111111111111",
            "title": title,
            "created_at": "2026-10-09T10:00:00+00:00",
            "last_active_at": "2026-10-09T10:00:00+00:00",
            "status": "active",
        }
        for i, title in enumerate(titles, start=1)
    ]
    api.routes[("GET", "/sessions")] = json_response(
        200, {"total": len(rows), "limit": 20, "offset": 0, "sessions": rows}
    )
    return rows


def test_my_chats_are_listed_by_title_with_no_markup_in_the_labels(monkeypatch):
    api = FakeApi()
    sessions_api(api, ["Waiting period", "[click](https://evil.example) **now**"])
    at = signed_in(api, monkeypatch).run()
    labels = [b.label for b in at.sidebar.button]
    assert "Waiting period" in labels
    assert all("](" not in label and "**" not in label for label in labels)


def test_a_chat_is_resumed_with_its_notices_and_a_withdrawn_source_flag(monkeypatch):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    session_id = rows[0]["id"]

    def transcript(request):
        return json_response(
            200,
            {
                "id": session_id,
                "title": "Waiting period",
                "status": "active",
                "total": 3,
                "messages": [
                    {"id": "m1", "role": "user", "text": "Old question", "citations": []},
                    {
                        "id": "m2",
                        "role": "assistant",
                        "text": "This answer cited a document that has since been withdrawn.",
                        "citations": [],
                        "grounded": True,
                        "source_removed": True,
                        "notice": "source_removed",
                    },
                    {
                        "id": "m3",
                        "role": "assistant",
                        "text": "The assistant is in a test period.",
                        "citations": [],
                        "grounded": None,
                        "source_removed": False,
                        "notice": "shadowed",
                    },
                ],
            },
        )

    api.routes[("GET", f"/sessions/{session_id}")] = transcript
    at = signed_in(api, monkeypatch).run()
    button(at, "Waiting period").click().run()
    bodies = "\n".join(html_bodies(at))
    assert "Old question" in bodies
    assert "Source withdrawn" in bodies and "has since been withdrawn" in bodies
    assert messages.TEST_PERIOD_TITLE in bodies
    assert at.session_state["fx_session_id"] == session_id


UNVERIFIED = "This answer could not be fully verified when it was given."


def resumed_answer(monkeypatch, grounded, text="An older answer.", citations=None):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    session_id = rows[0]["id"]
    api.routes[("GET", f"/sessions/{session_id}")] = json_response(
        200,
        {
            "id": session_id,
            "title": "Waiting period",
            "status": "active",
            "total": 2,
            "messages": [
                {"id": "m1", "role": "user", "text": "Old question", "citations": []},
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": text,
                    "citations": citations or [],
                    "grounded": grounded,
                    "source_removed": False,
                    "notice": None,
                },
            ],
        },
    )
    at = signed_in(api, monkeypatch).run()
    button(at, "Waiting period").click().run()
    assert not at.exception, at.exception
    return "\n".join(html_bodies(at))


def test_the_unverified_notice_uses_the_approved_wording():
    assert messages.UNVERIFIED_TEXT == UNVERIFIED


def test_a_resumed_answer_that_was_not_grounded_shows_the_unverified_notice(monkeypatch):
    bodies = resumed_answer(monkeypatch, False)
    assert UNVERIFIED in bodies and messages.UNVERIFIED_TITLE in bodies
    assert "An older answer." in bodies  # the answer itself is still shown


def test_a_resumed_grounded_answer_shows_no_unverified_notice(monkeypatch):
    citations = [{"doc_id": "doc-a", "clause": "4.2", "page": 7}]
    bodies = resumed_answer(monkeypatch, True, citations=citations)
    assert UNVERIFIED not in bodies and "doc-a · clause 4.2 · page 7" in bodies


def test_an_answer_with_no_grounded_flag_shows_no_unverified_notice(monkeypatch):
    assert UNVERIFIED not in resumed_answer(monkeypatch, None)


def test_a_hidden_resumed_answer_shows_the_test_period_notice_and_not_the_unverified_one(
    monkeypatch,
):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    session_id = rows[0]["id"]
    api.routes[("GET", f"/sessions/{session_id}")] = json_response(
        200,
        {
            "id": session_id,
            "title": "Waiting period",
            "status": "active",
            "total": 1,
            "messages": [
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "The assistant is in a test period.",
                    "citations": [],
                    "grounded": None,
                    "source_removed": False,
                    "notice": "shadowed",
                },
            ],
        },
    )
    at = signed_in(api, monkeypatch).run()
    button(at, "Waiting period").click().run()
    bodies = "\n".join(html_bodies(at))
    assert messages.TEST_PERIOD_TITLE in bodies and UNVERIFIED not in bodies


def test_the_unverified_text_is_escaped_like_any_other_text():
    shown = render.answer_extras({"grounded": False})
    assert UNVERIFIED in shown and "<script" not in shown


def test_deleting_a_chat_asks_first_and_then_deletes_it(monkeypatch):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    session_id = rows[0]["id"]
    api.routes[("DELETE", f"/sessions/{session_id}")] = httpx.Response(204)
    at = signed_in(api, monkeypatch).run()
    button(at, "Delete this chat").click().run()
    assert not api.calls("DELETE", f"/sessions/{session_id}")  # nothing yet: it asked first
    assert "Delete this chat?" in page_text(at)
    button(at, "Yes, delete").click().run()
    assert len(api.calls("DELETE", f"/sessions/{session_id}")) == 1


def test_keeping_a_chat_deletes_nothing(monkeypatch):
    api = FakeApi()
    rows = sessions_api(api, ["Waiting period"])
    at = signed_in(api, monkeypatch).run()
    button(at, "Delete this chat").click().run()
    button(at, "Keep").click().run()
    assert not api.calls("DELETE", f"/sessions/{rows[0]['id']}")


def test_delete_all_my_chats_asks_first(monkeypatch):
    api = FakeApi()
    sessions_api(api, ["One", "Two"])
    api.routes[("DELETE", "/sessions")] = json_response(200, {"deleted": 2})
    at = signed_in(api, monkeypatch).run()
    button(at, "Delete all my chats").click().run()
    assert not api.calls("DELETE", "/sessions")
    button(at, "Yes, delete").click().run()
    assert len(api.calls("DELETE", "/sessions")) == 1


# ================================================================================ admin ====
SECTIONS = [
    "Chat",
    "Documents",
    "Entities",
    "Review queue",
    "Users",
    "Alerts",
    "Dashboards",
    "Rollout",
    "Evaluation",
]


def admin_api():
    api = FakeApi()
    api.routes.update(
        {
            ("GET", "/admin/documents"): json_response(
                200,
                {
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                    "documents": [{"doc_id": "doc-a", "chunks_active": 3}],
                },
            ),
            ("GET", "/admin/entities"): json_response(
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
                            "id": "e2",
                            "text": "Rahul Sharma",
                            "entity_type": "person_in_policy",
                            "doc_id": "doc-a",
                            "status": "pending",
                            "approved_at": None,
                        },
                    ],
                },
            ),
            ("GET", "/admin/review"): json_response(
                200,
                {
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                    "items": [
                        {
                            "id": "r1",
                            "kind": "refused",
                            "status": "open",
                            "created_at": "2026-10-09T09:00:00+00:00",
                            "overdue": True,
                            "label": None,
                            "details": {"question": "a masked question"},
                        },
                    ],
                },
            ),
            ("POST", "/admin/review/r1/label"): json_response(200, {"id": "r1"}),
            ("GET", "/admin/users"): json_response(
                200,
                {
                    "total": 2,
                    "limit": 50,
                    "offset": 0,
                    "users": [
                        {
                            "id": "u1",
                            "username": "boss",
                            "role": "admin",
                            "is_active": True,
                            "created_at": "2026-10-01",
                        },
                        {
                            "id": "u3",
                            "username": "ravi",
                            "role": "customer",
                            "is_active": True,
                            "created_at": "2026-10-02",
                        },
                    ],
                },
            ),
            ("PUT", "/admin/users/u3/role"): json_response(200, {"id": "u3", "role": "staff"}),
            ("GET", "/admin/shadow"): json_response(
                200, {"stage": "shadow", "stages": ["shadow", "staff_beta", "open"]}
            ),
            ("PUT", "/admin/shadow"): json_response(
                200, {"stage": "open", "previous_stage": "shadow"}
            ),
            ("GET", "/admin/shadow/answers"): json_response(
                200, {"total": 0, "limit": 20, "offset": 0, "answers": []}
            ),
            ("PUT", "/admin/entities/e2"): json_response(200, {"id": "e2", "status": "approved"}),
        }
    )
    return api


def test_a_customer_sees_no_admin_sections_and_makes_no_admin_call(monkeypatch):
    api = admin_api()
    at = signed_in(api, monkeypatch, role="customer").run()
    assert not at.radio
    assert not [r for r in api.requests if r[1].startswith("/admin")]


def test_an_admin_has_every_section_and_the_alerts_banner_data_is_fetched(monkeypatch):
    api = admin_api()
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    assert list(at.radio[0].options) == SECTIONS
    assert api.calls("GET", "/admin/alerts")


def test_a_critical_alert_shows_a_banner_with_the_event_names(monkeypatch):
    api = admin_api()
    api.routes[("GET", "/admin/alerts")] = json_response(
        200,
        {"banner": True, "critical": ["masker_load_failed"], "events": [], "window": 1},
    )
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    bodies = "\n".join(html_bodies(at))
    assert "masker_load_failed" in bodies and 'role="alert"' in bodies


def open_section(at, name):
    at.radio[0].set_value(name).run()
    assert not at.exception, at.exception
    return at


def test_documents_show_status_and_commands_to_copy_only_on_the_admin_machine(monkeypatch):
    api = admin_api()
    host = open_section(
        signed_in(api, monkeypatch, role="admin", username="boss").run(), "Documents"
    )
    assert not host.code and "done on the admin machine" in "\n".join(c.value for c in host.caption)
    api = admin_api()
    local = open_section(
        signed_in(
            api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
        ).run(),
        "Documents",
    )
    commands = [c.value for c in local.code]
    assert any("app.ingest ingest" in c for c in commands)
    assert any("app.ingest delete" in c for c in commands)
    # the page never runs anything: no button starts those commands
    assert not [
        b for b in local.button if "ingest" in b.label.lower() or "delete" in b.label.lower()
    ]


def test_entities_are_listed_for_reading_and_changed_only_on_the_admin_machine(monkeypatch):
    api = admin_api()
    host = open_section(
        signed_in(api, monkeypatch, role="admin", username="boss").run(), "Entities"
    )
    assert not [b for b in host.button if b.label in ("Approve", "Reject")]
    api = admin_api()
    local = open_section(
        signed_in(
            api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
        ).run(),
        "Entities",
    )
    assert {b.label for b in local.button} >= {"Approve", "Reject"}


def test_the_admin_machine_switch_does_nothing_on_the_host(monkeypatch):
    api = admin_api()
    at = open_section(
        signed_in(
            api,
            monkeypatch,
            role="admin",
            username="boss",
            env={"ADMIN_MACHINE": "true", "RENDER": "true"},
        ).run(),
        "Entities",
    )
    assert not [b for b in at.button if b.label in ("Approve", "Reject")]


def test_approving_a_one_word_name_sends_the_confirmation_only_when_ticked(monkeypatch):
    api = admin_api()
    api.routes[("PUT", "/admin/entities/e1")] = error(
        409,
        "single_word_needs_confirmation",
        "A one-word name is approved only when you confirm it.",
    )
    at = open_section(
        signed_in(
            api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
        ).run(),
        "Entities",
    )
    button(at, "Approve").click().run()
    first = api.calls("PUT", "/admin/entities/e1")[0][2]
    assert first == {"status": "approved"}  # not ticked: no confirmation sent
    assert "confirm it" in page_text(at)
    at.checkbox[0].check().run()
    button(at, "Approve").click().run()
    assert api.calls("PUT", "/admin/entities/e1")[1][2] == {
        "status": "approved",
        "confirm_single_word": True,
    }


def test_a_review_item_is_labelled_from_the_fixed_list(monkeypatch):
    api = admin_api()
    at = open_section(
        signed_in(api, monkeypatch, role="admin", username="boss").run(), "Review queue"
    )
    label_box = [s for s in at.selectbox if s.label == "Label"][0]
    assert list(label_box.options) == [messages.LABEL_TEXT[v] for v in messages.REVIEW_LABELS]
    label_box.set_value("wrong_answer").run()
    button(at, "Save label").click().run()
    assert api.calls("POST", "/admin/review/r1/label")[0][2] == {
        "label": "wrong_answer",
        "status": "labelled",
    }
    assert [b for b in at.button if b.disabled and "Phase 13" in b.label]  # eval button is a shell


def test_roles_are_changed_for_customers_and_staff_never_for_an_admin(monkeypatch):
    api = admin_api()
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Users")
    pick = [s for s in at.selectbox if s.label == "Account"][0]
    pick.set_value("u1").run()  # an admin account: no controls
    assert not [b for b in at.button if b.label == "Save role"]
    pick.set_value("u3").run()
    role = [s for s in at.selectbox if s.label == "Role"][0]
    assert list(role.options) == ["Customer", "Staff"]  # never "Admin"
    role.set_value("staff").run()
    button(at, "Save role").click().run()
    assert api.calls("PUT", "/admin/users/u3/role")[0][2] == {"role": "staff"}


def test_the_rollout_stage_is_shown_and_changed(monkeypatch):
    api = admin_api()
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Rollout")
    assert "Shadow: only admins see answers" in "\n".join(html_bodies(at))
    stage = [s for s in at.selectbox if s.label == "Change to"][0]
    stage.set_value("open").run()
    button(at, "Change stage").click().run()
    assert api.calls("PUT", "/admin/shadow")[0][2] == {"stage": "open"}


def test_the_evaluation_section_is_an_honest_shell(monkeypatch):
    api = admin_api()
    at = open_section(
        signed_in(api, monkeypatch, role="admin", username="boss").run(), "Evaluation"
    )
    assert "Not available yet" in "\n".join(html_bodies(at))


def test_an_admin_refusal_is_shown_in_plain_words(monkeypatch):
    api = admin_api()
    api.routes[("GET", "/admin/users")] = error(403, "forbidden", "You do not have access to this.")
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Users")
    assert "You do not have access to this." in "\n".join(html_bodies(at))


# ====================================================================== nothing leaks ====
def test_the_token_is_in_no_rendered_element_through_a_whole_session(monkeypatch):
    api = admin_api()
    at = signed_in(
        api, monkeypatch, role="admin", username="boss", env={"ADMIN_MACHINE": "true"}
    ).run()
    ask(at)
    for section in SECTIONS[1:]:
        open_section(at, section)
        assert FAKE_TOKEN not in page_text(at), section
    assert FAKE_TOKEN not in page_text(at)


def test_the_question_box_stops_at_the_services_own_limit(monkeypatch):
    at = signed_in(FakeApi(), monkeypatch).run()
    assert at.chat_input[0].proto.max_chars == 2000


def test_the_question_and_the_token_are_not_logged(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    api = FakeApi()
    at = ask(signed_in(api, monkeypatch).run(), "My private question about claim 123")
    assert at.session_state["fx_token"] == FAKE_TOKEN
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert FAKE_TOKEN not in logged and "private question" not in logged
