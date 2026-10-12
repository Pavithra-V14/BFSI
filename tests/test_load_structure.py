"""The load tools (spec 16): they cannot reach a production store, a real-provider run cannot start
without the code of its own plan, the report says what a stubbed run cannot show, and the stub
server really is the API with everything behind it replaced."""

import ast
import csv
import io
import json
import secrets
from pathlib import Path

import pytest

from load import plan, report, stub_server
from scripts import load_estimate

ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "load"


# ===================================================================================== the plan
def test_the_default_plan_starts_at_ten_users_and_steps_up():
    steps = plan.parse_steps(None)
    assert steps[0].users == 10
    assert [s.users for s in steps] == sorted({s.users for s in steps})
    assert plan.parse_steps("  ") == steps


@pytest.mark.parametrize(
    "text",
    ["", "x", "10", "10x5", "20x60", "10x60,10x60", "10x60,5x60", "10x60,100000x60", "10xmany"],
)
def test_a_bad_plan_is_refused_with_a_plain_message(text):
    if text == "":
        return  # empty is the default plan, tested above
    with pytest.raises(plan.PlanError) as caught:
        plan.parse_steps(text)
    assert "LOAD_STEPS" in str(caught.value) or "step" in str(caught.value)


def test_the_stub_mode_needs_no_code():
    mode, steps = plan.check_start({"LOAD_MODE": "stub", "LOAD_STEPS": "10x10,20x10"})
    assert mode == "stub" and [s.users for s in steps] == [10, 20]


def real_env(**extra):
    return {
        "LOAD_MODE": "real",
        "LOAD_STEPS": "10x10",
        "GROQ_API_KEYS": "g" + secrets.token_hex(8),
        "GEMINI_API_KEYS": "m" + secrets.token_hex(8),
        **extra,
    }


def test_a_real_run_cannot_start_without_the_code():
    with pytest.raises(plan.PlanError) as caught:
        plan.check_start(real_env())
    assert "LOAD_CONFIRM" in str(caught.value)
    assert "load_estimate.py" in str(caught.value), "the message tells where the code comes from"


def test_a_real_run_cannot_start_with_a_wrong_code():
    with pytest.raises(plan.PlanError):
        plan.check_start(real_env(LOAD_CONFIRM="AAAA-0000"))


def test_the_code_belongs_to_its_own_plan_and_starts_that_plan_only():
    from app.eval.cases import load_cases
    from app.gateway import GatewayConfig

    env = real_env()
    config = GatewayConfig.from_env(env)
    cases = load_cases()[1]
    short = plan.parse_steps("10x10")
    longer = plan.parse_steps("10x10,20x10")
    code = plan.confirmation(plan.real_estimate(short, config, cases), short)
    other = plan.confirmation(plan.real_estimate(longer, config, cases), longer)
    assert code != other
    assert plan.check_start({**env, "LOAD_CONFIRM": code.lower()})[0] == "real"
    with pytest.raises(plan.PlanError):
        plan.check_start({**env, "LOAD_STEPS": "10x10,20x10", "LOAD_CONFIRM": code})


def test_an_unknown_mode_is_refused():
    with pytest.raises(plan.PlanError):
        plan.check_start({"LOAD_MODE": "production"})


def test_the_estimate_script_prints_the_code_and_makes_no_call():
    lines = []
    status = load_estimate.main(["--steps", "10x10"], real_env(), out=lines.append)
    shown = "\n".join(lines)
    assert status == 0 and "LOAD_CONFIRM=" in shown and "explicit yes" in shown
    assert "requests" in shown


def test_the_estimate_script_refuses_on_render_and_a_bad_plan():
    lines = []
    assert load_estimate.main([], {**real_env(), "RENDER": "true"}, out=lines.append) == 1
    assert load_estimate.main(["--steps", "5x5"], real_env(), out=lines.append) == 1


# ================================================================================ the stub server
def test_the_stub_server_listens_on_this_machine_only():
    with pytest.raises(SystemExit) as caught:
        stub_server.check_local("0.0.0.0", {})
    assert "this machine only" in str(caught.value)
    stub_server.check_local("127.0.0.1", {})


def test_the_stub_server_refuses_a_remote_store_address_in_the_shell():
    env = {"DATABASE_URL": "postgresql://user@db.example.com/main"}
    with pytest.raises(SystemExit) as caught:
        stub_server.check_local("127.0.0.1", env)
    assert "DATABASE_URL" in str(caught.value) and "db.example.com" not in str(caught.value)


def test_the_stub_server_is_the_real_api_over_stand_ins(tmp_path):
    from starlette.testclient import TestClient

    app, rig = stub_server.build_app(tmp_path, latency_ms=0)
    try:
        with TestClient(app) as client:
            password = secrets.token_urlsafe(18) + "Aa1"
            body = {"username": "loadone", "email": "loadone@load.invalid", "password": password}
            assert client.post("/auth/register", json=body).status_code == 201
            login = client.post("/auth/login", json={"username": "loadone", "password": password})
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            question = next(
                c for c in __import__("app.eval.cases", fromlist=["x"]).load_cases()[1]
                if c.active and c.role == "customer" and c.category == "factual"
            ).turns[-1]  # fmt: skip
            reply = client.post("/chat", json={"message": question}, headers=headers)
            assert reply.status_code == 200, reply.text
            assert reply.json().get("text") or reply.json().get("kind")
            # the same address registers again: the per-address throttle is off in the stub
            for i in range(8):
                other = {
                    "username": f"loadx{i}",
                    "email": f"x{i}@load.invalid",
                    "password": password,
                }
                assert client.post("/auth/register", json=other).status_code == 201
    finally:
        rig.close()


# ====================================================================================== the report
def history(rows):
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=["Name", "User Count", "Requests/s", "Failures/s", "50%", "95%"]
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({"Name": "Aggregated", **row})
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def second(users, requests, failures, p95, p50=200):
    return {"User Count": users, "Requests/s": requests, "Failures/s": failures, "50%": p50,
            "95%": p95}  # fmt: skip


def test_the_report_names_the_first_step_that_broke_by_errors():
    rows = history(
        [second(10, 5, 0, 800)] * 30
        + [second(20, 10, 0, 1200)] * 30
        + [second(40, 20, 1, 2500)] * 30  # 5 % failed
        + [second(80, 30, 9, 9000)] * 30
    )
    steps = report.steps_from_rows(rows)
    assert [s.users for s in steps] == [10, 20, 40, 80]
    broke = report.first_break(steps)
    assert broke is not None and broke.users == 40
    text = "\n".join(report.render(steps))
    assert "first step that broke: 40 users" in text and "more than 1 %" in text


def test_the_report_names_a_step_that_broke_by_latency_alone():
    rows = history([second(10, 5, 0, 800)] * 30 + [second(20, 10, 0, 31_000)] * 30)
    broke = report.first_break(report.steps_from_rows(rows))
    assert broke is not None and broke.users == 20
    assert "p95 passed the 30 s" in "\n".join(report.render(report.steps_from_rows(rows)))


def test_a_run_that_did_not_break_says_so_with_its_top_step():
    steps = report.steps_from_rows(history([second(10, 5, 0, 800)] * 20))
    text = "\n".join(report.render(steps))
    assert "did not break up to 10 users" in text


def test_the_stub_report_says_it_is_concurrency_not_capacity_and_lists_the_blind_spots():
    text = "\n".join(
        report.render(report.steps_from_rows(history([second(10, 5, 0, 800)])), "stub")
    )
    assert "not host capacity" in text
    assert "Postgres connection ceiling" in text and "one database connection per chat" in text
    assert "Provider limits" in text and "Host capacity" in text


def test_a_failure_share_is_failures_over_requests():
    steps = report.steps_from_rows(history([second(10, 100, 2, 100)]))
    assert steps[0].error_rate == pytest.approx(0.02)


# ======================================================================== what the tools may import
def imports_of(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_the_load_tools_call_no_provider_directly():
    banned = {"litellm", "openai", "groq", "cohere", "anthropic", "google.genai", "httpx"}
    for path in LOAD.glob("*.py"):
        assert not {m.split(".")[0] for m in imports_of(path)} & banned, path.name


def test_only_the_locust_file_imports_locust_and_the_api_never_imports_the_load_tools():
    for path in LOAD.glob("*.py"):
        uses_locust = any(m.split(".")[0] == "locust" for m in imports_of(path))
        assert uses_locust == (path.name == "locustfile.py"), path.name
    for path in (ROOT / "app").rglob("*.py"):
        assert not any(m.split(".")[0] == "load" for m in imports_of(path)), path


def test_the_locust_file_checks_the_plan_before_any_user_exists():
    source = (LOAD / "locustfile.py").read_text("utf-8")
    assert "plan.check_start()" in source
    assert source.index("plan.check_start()") < source.index("class Customer")


def test_the_stub_plan_in_json_is_plain_numbers():
    assert json.dumps([s.users for s in plan.parse_steps(None)])
