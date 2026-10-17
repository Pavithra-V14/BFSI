"""The Maintenance screen's purge (Change round 1, step 6): only on the admin machine, a button,
then a red panel that needs a tick and the phrase typed in full, and the result in plain words."""

from tests.frontend_fixtures import error, html_bodies, json_response, signed_in
from tests.test_frontend import admin_api, open_section

LOCAL = {"ADMIN_MACHINE": "true"}
PLAN = ("GET", "/admin/maintenance/purge-plan")
PURGE = ("POST", "/admin/maintenance/purge")
PHRASE = "DELETE ALL DATA"


def plan_body(**counts):
    base = {
        "chunks": 40, "chats": 3, "messages": 9, "feedback": 2, "users": 5,
        "review_items": 1, "alert_rows": 4, "metric_rows": 12, "counter_rows": 6, "admins_kept": 1,
    }  # fmt: skip
    base.update(counts)
    return {
        "counts": base,
        "phrase": PHRASE,
        "warning": "Warning: this erases all indexed knowledge. This cannot be undone.",
        "kept": ["admin accounts", "the audit log"],
        "not_touched": ["Langfuse traces"],
    }


def page(monkeypatch, env=LOCAL, api=None):
    api = api or admin_api()
    api.routes[PLAN] = json_response(200, plan_body())
    at = signed_in(api, monkeypatch, role="admin", username="boss", env=env).run()
    return api, open_section(at, "Maintenance")


def press(at, label):
    [button] = [b for b in at.button if b.label == label]
    button.click().run()
    assert not at.exception, at.exception
    return at


def open_panel(monkeypatch, api=None):
    api, at = page(monkeypatch, api=api)
    press(at, "Purge Database Data")
    return api, at


def text(at):
    return " ".join(html_bodies(at))


def test_on_the_host_there_is_no_purge_button_and_no_call(monkeypatch):
    api, at = page(monkeypatch, env={})
    assert not [b for b in at.button if "urge" in b.label]
    assert "admin machine" in " ".join(c.value for c in at.caption)
    assert api.calls(*PLAN) == [] and api.calls(*PURGE) == []


def test_on_the_admin_machine_only_the_button_shows_and_nothing_is_asked_yet(monkeypatch):
    api, at = page(monkeypatch)
    assert [b.label for b in at.button if b.label == "Purge Database Data"] == [
        "Purge Database Data"
    ]
    assert not at.text_input and not at.checkbox
    assert api.calls(*PLAN) == [] and api.calls(*PURGE) == []


def test_the_panel_shows_the_counts_the_warning_and_what_is_kept(monkeypatch):
    api, at = open_panel(monkeypatch)
    shown = text(at)
    assert "Erase everything?" in shown and "This cannot be undone" in shown
    table = at.dataframe[0].value.to_string()
    assert "document chunks" in table and "40" in table and "customer and staff accounts" in table
    captions = " ".join(c.value for c in at.caption)
    assert "audit log" in captions and "Langfuse traces" in captions and "1 admin" in captions
    assert api.calls(*PURGE) == []


def test_the_erase_button_stays_off_until_the_box_is_ticked_and_the_phrase_is_typed(monkeypatch):
    api, at = open_panel(monkeypatch)
    go = lambda: [b for b in at.button if b.label == "Erase everything"][0]  # noqa: E731
    assert go().disabled
    at.text_input[0].set_value(PHRASE).run()
    assert go().disabled  # typed, but not ticked
    at.text_input[0].set_value("delete all data").run()
    at.checkbox[0].check().run()
    assert go().disabled  # ticked, but the phrase is wrong
    at.text_input[0].set_value(PHRASE).run()
    assert not go().disabled
    assert api.calls(*PURGE) == []


def test_cancel_closes_the_panel_without_a_call(monkeypatch):
    api, at = open_panel(monkeypatch)
    press(at, "Cancel")
    assert not at.text_input and [b for b in at.button if b.label == "Purge Database Data"]
    assert api.calls(*PURGE) == []


def confirm_and_erase(at):
    at.checkbox[0].check().run()
    at.text_input[0].set_value(PHRASE).run()
    press(at, "Erase everything")


def test_a_confirmed_purge_sends_the_phrase_and_says_so(monkeypatch):
    api = admin_api()
    api.routes[PURGE] = json_response(
        200, {"erased": True, "timed_out": False, "lines": ["erased: 40 chunks", "done"]}
    )
    api, at = open_panel(monkeypatch, api)
    confirm_and_erase(at)
    assert api.calls(*PURGE)[0][2] == {"phrase": PHRASE}
    shown = text(at)
    assert "The data was erased" in shown and "erased 40 chunks" in shown
    assert not at.text_input  # the panel is closed again


def test_a_purge_that_did_not_finish_says_to_repeat_it(monkeypatch):
    api = admin_api()
    api.routes[PURGE] = json_response(
        200,
        {
            "erased": False,
            "timed_out": False,
            "lines": ["purge incomplete: run the same command again to finish it"],
        },
    )
    api, at = open_panel(monkeypatch, api)
    confirm_and_erase(at)
    shown = text(at)
    assert "did not finish" in shown and "safe to repeat" in shown


def test_an_error_from_the_service_is_shown_and_the_panel_stays_open(monkeypatch):
    api = admin_api()
    api.routes[PURGE] = error(
        409, "admin_machine_only", "Data is erased on the admin machine only."
    )
    api, at = open_panel(monkeypatch, api)
    confirm_and_erase(at)
    assert at.text_input  # still open: nothing was erased
    assert "Not erased" in text(at)


def test_a_plan_that_cannot_be_loaded_shows_no_erase_button(monkeypatch):
    api = admin_api()
    api.routes[PLAN] = error(503, "service_unavailable", "The document store is not available.")
    api, at = page(monkeypatch, api=api)
    api.routes[PLAN] = error(503, "service_unavailable", "The document store is not available.")
    press(at, "Purge Database Data")
    assert not [b for b in at.button if b.label == "Erase everything"]
    assert "Could not load" in text(at)
