"""The purge screen's routes (Change round 1, step 6): admin only, the admin machine only, the
phrase typed in full, an audit row for the request, and the command's outcome reported as it is."""

import pytest

from app.api.commands import CommandResult
from app.maintenance.purge import PHRASE
from tests.api_chat_fixtures import ChatEnv


class Runner:
    def __init__(self, code=0, lines=None, timed_out=False):
        self.calls = []
        self.result = CommandResult(code, lines if lines is not None else ["done"], timed_out)

    def __call__(self, args):
        self.calls.append(list(args))
        return self.result


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, observability=True)
    made.runner = Runner()  # type: ignore[attr-defined]
    made.app.state.services.maintenance_runner = made.runner  # type: ignore[attr-defined]
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def plan(env, token):
    return env.client.get("/admin/maintenance/purge-plan", headers=env.bearer(token))


def erase(env, token, phrase=PHRASE, **body):
    body["phrase"] = phrase
    return env.client.post("/admin/maintenance/purge", json=body, headers=env.bearer(token))


# ---- who and where -------------------------------------------------------------------------
def test_only_an_admin_may_see_the_plan_or_erase(env):
    for role in ("customer", "staff"):
        token = env.user_token(f"user_{role}", role)[1]
        assert plan(env, token).status_code == 403 and erase(env, token).status_code == 403
    assert plan(env, "not-a-token").status_code == 401
    assert erase(env, "not-a-token").status_code == 401
    assert env.runner.calls == []


def test_both_are_refused_on_the_host(env, admin):
    env.app.state.services.on_render = True
    for response in (plan(env, admin), erase(env, admin)):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "admin_machine_only"
    assert env.runner.calls == []


# ---- the plan ------------------------------------------------------------------------------
def test_the_plan_has_counts_the_phrase_the_warning_and_what_is_kept(env, admin):
    env.make_user("asha_rao")
    body = plan(env, admin).json()
    assert body["phrase"] == PHRASE and "cannot be undone" in body["warning"]
    counts = body["counts"]
    assert counts["users"] >= 1 and counts["admins_kept"] == 1 and counts["chunks"] > 0
    assert any("audit log" in kept for kept in body["kept"])
    assert "Langfuse traces" in body["not_touched"]
    assert env.runner.calls == []  # looking is not erasing


# ---- erasing -------------------------------------------------------------------------------
def test_a_wrong_or_missing_phrase_runs_nothing(env, admin):
    for typed in ("delete all data", "DELETE ALL", "yes", "x"):
        response = erase(env, admin, phrase=typed)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "confirmation_mismatch"
    assert (
        env.client.post("/admin/maintenance/purge", json={}, headers=env.bearer(admin)).status_code
        == 422
    )
    assert erase(env, admin, extra="field").status_code == 422
    assert env.runner.calls == []
    assert env.rows("SELECT COUNT(*) FROM audit_log WHERE action = 'purge_all_request'") == [(0,)]


def test_the_right_phrase_audits_the_request_and_runs_the_command_once(env, admin):
    response = erase(env, admin, phrase="  " + PHRASE + " ")
    assert response.status_code == 200
    assert response.json() == {"erased": True, "timed_out": False, "lines": ["done"]}
    (call,) = env.runner.calls
    assert call[:3] == ["purge", "--confirm-phrase", PHRASE] and call[3] == "--actor"
    assert call[4].startswith("admin:") and len(call) == 5
    rows = env.rows(
        "SELECT actor, action, target FROM audit_log WHERE action = 'purge_all_request'"
    )
    assert len(rows) == 1 and rows[0][2] == "database" and rows[0][0]


def test_a_purge_that_did_not_finish_is_reported_and_raises_an_alert_event(env, admin):
    env.runner.result = CommandResult(1, ["purge incomplete: run the same command again"])
    body = erase(env, admin).json()
    assert body["erased"] is False and "run the same command again" in body["lines"][0]
    assert "purge_failed" in [name for name, _ in env.app.state.services.events]


def test_a_timeout_is_reported_as_one(env, admin):
    env.runner.result = CommandResult(-1, ["the command did not finish in time"], timed_out=True)
    body = erase(env, admin).json()
    assert body["erased"] is False and body["timed_out"] is True


def test_the_real_runner_is_the_maintenance_module_and_nothing_else():
    import inspect

    from app.api import commands

    assert commands.MODULES == {"app.ingest", "app.maintenance"}
    assert "app.maintenance" in inspect.getsource(commands.run_maintenance_command)
    with pytest.raises(ValueError):
        commands.run_module("os", ["system"])
