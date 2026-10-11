"""The evaluation command line (Phase 13): nothing is called without the confirmation code, the
output holds names and numbers only, and Render is refused."""

import json

import pytest

from app.eval import cli, store
from app.eval.cases import load_cases
from tests.eval_rig import EvalRig

# Made up from pieces: long enough to be noticed if one ever reached the output.
SECRETS = {
    name: "test-" + name.lower().replace("_", "-") + "-" + "q" * 12
    for name in (
        "EVAL_GROQ_API_KEYS",
        "EVAL_GEMINI_API_KEYS",
        "EVAL_OPENROUTER_API_KEYS",
        "JEV_OPENROUTER_API_KEY",
    )
}
PRODUCTION_ONLY = {"GROQ_API" + "_KEYS": "production-" + "x" * 12}


class Output:
    def __init__(self):
        self.lines = []

    def __call__(self, text):
        self.lines.append(str(text))

    @property
    def text(self):
        return "\n".join(self.lines)


def run_cli(args, env=None, factory=None):
    out = Output()
    calls = []

    def default_factory(e):
        calls.append(1)
        raise AssertionError("no service may be built for this command")

    code = cli.main(
        [*args],
        dict(SECRETS if env is None else env),
        services_factory=factory or default_factory,
        out=out,
    )
    return code, out, calls


@pytest.fixture(autouse=True)
def workdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "WORKDIR", tmp_path / "work")
    return tmp_path


def db_args(tmp_path):
    return ["--db", str(tmp_path / "e.db")]


# ============================================================================ the basics ====
def test_check_passes_and_names_what_the_owner_must_read(workdir):
    code, out, _calls = run_cli(["check"])
    assert code == 0
    assert "60 cases, 20 active" in out.text and "20 active cases" in out.text


def test_migrate_creates_the_tables_and_a_dry_run_changes_nothing(workdir):
    code, out, _ = run_cli(["migrate", "--dry-run", *db_args(workdir)])
    assert code == 0 and "pending 0001_evaluation.sql" in out.text
    code, out, _ = run_cli(["migrate", *db_args(workdir)])
    assert code == 0 and "applied 0001_evaluation.sql" in out.text
    code, out, _ = run_cli(["migrate", *db_args(workdir)])
    assert "0 file(s) applied" in out.text  # safe to repeat


def test_cases_sync_writes_the_set_and_is_safe_to_repeat(workdir):
    run_cli(["migrate", *db_args(workdir)])
    for _ in range(2):
        code, out, _ = run_cli(["cases-sync", *db_args(workdir)])
        assert code == 0 and "60 cases of dataset 2026-10-v1" in out.text
    db = store.open_db(store.sqlite_url(workdir / "e.db"))
    assert len(store.list_cases(db)) == 60 and len(store.list_cases(db, active_only=True)) == 20
    db.close()


def test_the_production_view_needs_its_address_set(workdir):
    code, _out, _ = run_cli(["migrate", "--production"], env=dict(SECRETS))
    assert code == 1


# ============================================================================= estimate ====
def test_an_estimate_prints_the_numbers_and_the_code_and_calls_nothing(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, out, calls = run_cli(["estimate", "--set", "active", *db_args(workdir)])
    assert code == 0 and calls == []
    assert "requests:" in out.text and "judge model" in out.text and "25 %" in out.text
    assert "confirmation code" in out.text and "--confirm" in out.text
    assert not any(value in out.text for value in SECRETS.values())


def test_an_estimate_refuses_without_an_evaluation_key(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, _out, _ = run_cli(["estimate", *db_args(workdir)], env=PRODUCTION_ONLY)
    assert code == 1


def test_an_estimate_refuses_on_render(workdir):
    run_cli(["migrate", *db_args(workdir)])
    env = {**SECRETS, "RENDER": "true"}
    code, _out, calls = run_cli(["estimate", *db_args(workdir)], env=env)
    assert code == 1 and calls == []


# ==================================================================================== run ====
def make_run(workdir, set_name="active"):
    run_cli(["migrate", *db_args(workdir)])
    code, out, _ = run_cli(["estimate", "--set", set_name, "--no-judge", *db_args(workdir)])
    assert code == 0
    words = out.text.split()
    run_id = words[words.index("run") + 1]
    confirm = out.text.split("--confirm ")[1].split()[0]
    return run_id, confirm


def test_a_run_with_the_wrong_code_is_not_started_and_builds_no_service(workdir):
    run_id, _code = make_run(workdir)
    code, out, calls = run_cli(["run", run_id, "--confirm", "AAAA-0000", *db_args(workdir)])
    assert code == 2 and "not started" in out.text and calls == []
    db = store.open_db(store.sqlite_url(workdir / "e.db"))
    assert store.get_run(db, run_id)["status"] == "estimated"
    db.close()


def test_a_run_without_a_code_is_not_started(workdir):
    run_id, _code = make_run(workdir)
    code, _out, calls = run_cli(["run", run_id, *db_args(workdir)])
    assert code == 2 and calls == []


def test_a_run_with_the_right_code_runs_one_batch_and_prints_numbers_only(workdir, tmp_path):
    (tmp_path / "rig").mkdir()
    rig = EvalRig(tmp_path / "rig")
    try:
        run_id, confirm = make_run(workdir)

        def factory(env):
            from app.gateway.config import GatewayConfig

            return cli.Services(
                rig.gateway, rig.pipeline, rig.qdrant, rig.db, GatewayConfig.for_evaluation(env)
            )

        code, out, _ = run_cli(
            ["run", run_id, "--confirm", confirm, "--no-judge", "--pace", "0", *db_args(workdir)],
            factory=factory,
        )
        assert code == 0, out.text
        assert f"run {run_id}: " in out.text and "requests" in out.text
        assert "What is the" not in out.text  # no question text
        assert not any(value in out.text for value in SECRETS.values())
        db = store.open_db(store.sqlite_url(workdir / "e.db"))
        run = store.get_run(db, run_id)
        db.close()
        assert run["status"] in ("finished", "paused") and run["finished_items"] > 0
    finally:
        rig.close()


def test_a_run_on_render_is_refused_before_anything_else(workdir):
    run_id, confirm = make_run(workdir)
    env = {**SECRETS, "RENDER": "true"}
    code, _out, calls = run_cli(["run", run_id, "--confirm", confirm, *db_args(workdir)], env=env)
    assert code == 1 and calls == []


def test_an_unknown_run_is_a_plain_error(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, _out, _ = run_cli(["run", "0" * 32, "--confirm", "AAAA-0000", *db_args(workdir)])
    assert code == 1


# ============================================================================== the rest ====
def test_status_lists_the_runs(workdir):
    run_id, _code = make_run(workdir)
    code, out, _ = run_cli(["status", *db_args(workdir)])
    assert code == 0 and run_id in out.text and "estimated" in out.text
    code, out, _ = run_cli(["status", run_id, *db_args(workdir)])
    assert code == 0 and "0/" in out.text


def test_calibrate_prints_its_estimate_and_stops_without_the_code(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, out, calls = run_cli(["calibrate", *db_args(workdir)])
    assert code == 0 and calls == [] and "calibrate --confirm" in out.text
    code, out, calls = run_cli(["calibrate", "--confirm", "AAAA-0000", *db_args(workdir)])
    assert code == 2 and calls == [] and "not started" in out.text


def test_judge_samples_with_none_waiting_says_so_and_calls_nothing(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, out, calls = run_cli(["judge-samples", *db_args(workdir)])
    assert code == 0 and "no unjudged samples" in out.text and calls == []


def test_judge_samples_needs_the_code_for_the_exact_estimate(workdir):
    run_cli(["migrate", *db_args(workdir)])
    db = store.open_db(store.sqlite_url(workdir / "e.db"))
    row = {
        "role": "customer", "result_kind": "answer", "question": "q", "answer": "a",
        "contexts": ["p"], "cheap_checks": {},
    }  # fmt: skip
    for _ in range(3):
        store.insert_sample(db, row)
    db.close()
    code, out, calls = run_cli(["judge-samples", *db_args(workdir)])
    assert code == 0 and calls == [] and "--confirm" in out.text
    code, _out, calls = run_cli(["judge-samples", "--confirm", "AAAA-0000", *db_args(workdir)])
    assert code == 2 and calls == []


def test_drift_reports_numbers_without_a_model_call(workdir):
    run_cli(["migrate", *db_args(workdir)])
    code, out, calls = run_cli(["drift", *db_args(workdir)])
    assert code == 0 and calls == [] and "alerts: none" in out.text
    assert "too few samples" in out.text


def test_a_command_that_fails_shows_its_class_and_no_text(workdir, capsys):
    def boom(env):
        raise RuntimeError("the secret " + "value " + "must not appear")

    run_id, confirm = make_run(workdir)
    code, _out, _ = run_cli(
        ["run", run_id, "--confirm", confirm, "--no-judge", *db_args(workdir)], factory=boom
    )
    err = capsys.readouterr().err
    assert code == 1 and "RuntimeError" in err and "must not appear" not in err


def test_the_case_file_loads_and_the_help_names_every_command():
    version, cases = load_cases()
    assert version and len(cases) == 60
    for command in ("estimate", "run", "calibrate", "judge-samples", "drift", "repair"):
        assert command in cli.__doc__
    assert json.dumps(cli.__doc__)
