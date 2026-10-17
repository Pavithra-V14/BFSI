"""The admin page's Delete button (Change round 1, step 4): the route runs the delete command on the
admin machine only, after the document id is typed again; it audits the request, and a command that
did not finish is reported, never hidden."""

import pytest

from app.api.commands import CommandResult, _clean
from tests.api_chat_fixtures import ChatEnv

DOC = "doc-a"  # the document the chat test environment stores


class Runner:
    """Stands in for the real command: records what it was asked and answers as told."""

    def __init__(self, code=0, lines=None, timed_out=False):
        self.calls = []
        self.result = CommandResult(
            code, lines if lines is not None else ["removed 12 chunk(s)"], timed_out
        )

    def __call__(self, args):
        self.calls.append(list(args))
        return self.result


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, observability=True)
    made.runner = Runner()  # type: ignore[attr-defined]
    made.app.state.services.ingest_runner = made.runner  # type: ignore[attr-defined]
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def post(env, token, doc_id=DOC, **body):
    body.setdefault("confirm_id", doc_id)
    return env.client.post(
        f"/admin/documents/{doc_id}/delete", json=body, headers=env.bearer(token)
    )


# ---- who and where -----------------------------------------------------------------------
def test_only_an_admin_may_delete(env):
    for role in ("customer", "staff"):
        token = env.user_token(f"user_{role}", role)[1]
        assert post(env, token).status_code == 403
    assert post(env, "not-a-token").status_code == 401
    assert env.runner.calls == []


def test_it_is_refused_on_the_host(env, admin):
    env.app.state.services.on_render = True
    response = post(env, admin)
    assert response.status_code == 409 and response.json()["error"]["code"] == "admin_machine_only"
    assert env.runner.calls == []


# ---- the typed confirmation --------------------------------------------------------------
def test_a_wrong_or_missing_confirmation_runs_nothing(env, admin):
    for typed in ("doc", "DOC-A", "x"):
        response = post(env, admin, confirm_id=typed)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "confirmation_mismatch"
    assert (
        env.client.post(
            f"/admin/documents/{DOC}/delete", json={}, headers=env.bearer(admin)
        ).status_code
        == 422
    )
    assert env.runner.calls == []


def test_a_bad_id_a_bad_reason_or_an_extra_field_is_refused(env, admin):
    assert post(env, admin, doc_id="Bad_ID", confirm_id="Bad_ID").status_code == 422
    assert post(env, admin, reason="because I said so").status_code == 422
    assert post(env, admin, extra="field").status_code == 422
    assert env.runner.calls == []


def test_a_document_that_is_not_stored_is_not_found_and_nothing_runs(env, admin):
    assert post(env, admin, doc_id="no-such-doc").status_code == 404
    assert env.runner.calls == []


# ---- the run ------------------------------------------------------------------------------
def test_the_command_is_started_with_the_id_the_reason_and_the_typed_confirmation(env, admin):
    response = post(env, admin, reason="outdated")
    assert response.status_code == 200
    assert response.json() == {
        "doc_id": DOC,
        "deleted": True,
        "timed_out": False,
        "lines": ["removed 12 chunk(s)"],
    }
    assert env.runner.calls == [
        ["delete", "--doc-id", DOC, "--reason", "outdated", "--confirm-id", DOC]
    ]


def test_the_default_reason_is_other(env, admin):
    post(env, admin)
    assert env.runner.calls[0][4] == "other"


def test_the_request_is_audited_with_who_what_and_why_and_no_text(env, admin):
    post(env, admin, reason="compliance")
    rows = env.rows(
        "SELECT actor, action, target, details FROM audit_log WHERE action = ?",
        ("document_delete_request",),
    )
    assert len(rows) == 1
    actor, action, target, details = rows[0]
    assert target == DOC and "compliance" in details and actor


def test_a_command_that_did_not_finish_is_reported_and_raises_an_alert_event(env, admin):
    env.runner.result = CommandResult(1, ["delete incomplete: run the same command again"])
    body = post(env, admin).json()
    assert body["deleted"] is False and "run the same command again" in body["lines"][0]
    names = [name for name, _ in env.app.state.services.events]
    assert "document_delete_failed" in names


def test_a_timeout_is_reported_as_one(env, admin):
    env.runner.result = CommandResult(-1, ["the command did not finish in time"], timed_out=True)
    body = post(env, admin).json()
    assert body["deleted"] is False and body["timed_out"] is True


# ---- what comes back from the command is cleaned ------------------------------------------
def test_the_output_is_cut_to_a_few_short_lines_without_long_tokens():
    noisy = "\n".join(f"line {n}" for n in range(30)) + "\n" + "k" * 60 + "\n" + "x " * 200
    lines = _clean(noisy)
    assert len(lines) <= 8 and all(len(line) <= 200 for line in lines)
    assert not any("k" * 40 in line for line in lines)
