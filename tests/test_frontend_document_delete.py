"""The Documents screen's Delete button (Change round 1, step 4): one button beside each document
on the admin machine only, a confirmation that needs the id typed again, and a plain result."""

from tests.frontend_fixtures import html_bodies, json_response, signed_in
from tests.test_frontend import admin_api, open_section

LOCAL = {"ADMIN_MACHINE": "true"}
DELETE = ("POST", "/admin/documents/doc-a/delete")


def documents_page(monkeypatch, api=None):
    api = api or admin_api()
    at = signed_in(api, monkeypatch, role="admin", username="boss", env=LOCAL).run()
    return api, open_section(at, "Documents")


def press(at, label):
    [button] = [b for b in at.button if b.label == label]
    button.click().run()
    assert not at.exception, at.exception
    return at


def test_each_document_has_a_delete_button_and_nothing_is_asked_before_it_is_pressed(monkeypatch):
    api, at = documents_page(monkeypatch)
    assert [b.label for b in at.button if b.label == "Delete"] == ["Delete"]
    assert not at.text_input and api.calls(*DELETE) == []


def test_pressing_delete_asks_for_the_id_and_the_delete_stays_off_until_it_matches(monkeypatch):
    api, at = documents_page(monkeypatch)
    press(at, "Delete")
    assert [t.label for t in at.text_input] == ["Type the document id to confirm"]
    go = [b for b in at.button if b.label == "Delete permanently"][0]
    assert go.disabled
    at.text_input[0].set_value("doc").run()
    assert [b for b in at.button if b.label == "Delete permanently"][0].disabled
    at.text_input[0].set_value("doc-a").run()
    assert not [b for b in at.button if b.label == "Delete permanently"][0].disabled
    assert api.calls(*DELETE) == []  # nothing was sent yet


def test_cancel_closes_the_panel_without_a_call(monkeypatch):
    api, at = documents_page(monkeypatch)
    press(at, "Delete")
    press(at, "Cancel")
    assert not at.text_input and api.calls(*DELETE) == []


def test_a_confirmed_delete_sends_the_id_the_typed_id_and_the_reason_and_says_so(monkeypatch):
    api = admin_api()
    api.routes[DELETE] = json_response(
        200,
        {"doc_id": "doc-a", "deleted": True, "timed_out": False, "lines": ["removed 3 chunk(s)"]},
    )
    api, at = documents_page(monkeypatch, api)
    press(at, "Delete")
    at.selectbox[0].set_value("outdated").run()
    at.text_input[0].set_value("doc-a").run()
    press(at, "Delete permanently")
    assert api.calls(*DELETE)[0][2] == {"confirm_id": "doc-a", "reason": "outdated"}
    page = " ".join(html_bodies(at))
    assert "Document deleted" in page and "removed 3 chunk" in page
    assert not at.text_input  # the panel is closed


def test_a_delete_that_did_not_finish_says_to_press_it_again(monkeypatch):
    api = admin_api()
    api.routes[DELETE] = json_response(
        200,
        {
            "doc_id": "doc-a",
            "deleted": False,
            "timed_out": False,
            "lines": ["delete incomplete: run the same command again to finish it"],
        },
    )
    api, at = documents_page(monkeypatch, api)
    press(at, "Delete")
    at.text_input[0].set_value("doc-a").run()
    press(at, "Delete permanently")
    page = " ".join(html_bodies(at))
    assert "did not finish" in page and "safe to repeat" in page


def test_an_error_from_the_service_is_shown_and_the_panel_stays_open(monkeypatch):
    from tests.frontend_fixtures import error

    api = admin_api()
    api.routes[DELETE] = error(
        409, "admin_machine_only", "Documents are deleted on the admin machine only."
    )
    api, at = documents_page(monkeypatch, api)
    press(at, "Delete")
    at.text_input[0].set_value("doc-a").run()
    press(at, "Delete permanently")
    assert at.text_input  # still open: nothing was deleted
    assert not at.exception
