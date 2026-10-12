"""The load tools (spec 16): they cannot reach a production store, a real-provider run cannot start
without the single-use ticket of its own plan and day, the report says what a local run cannot
show and judges a step the way it says it does, and the stub server really is the API with
everything behind it replaced."""

import ast
import csv
import io
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from load import plan, real_server, report, seed, stub_server
from scripts import load_estimate

ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "load"
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    """Every ticket, marker and users file of a test goes to its own folder."""
    monkeypatch.setattr(plan, "OUT_DIR", tmp_path / "load_out")
    return tmp_path / "load_out"


# ===================================================================================== the plan
def test_the_default_plan_starts_at_ten_users_and_steps_up():
    steps = plan.parse_steps(None)
    assert steps[0].users == 10
    assert [s.users for s in steps] == sorted({s.users for s in steps})
    assert plan.parse_steps("  ") == steps


@pytest.mark.parametrize(
    "text",
    ["x", "10", "10x5", "20x60", "10x60,10x60", "10x60,5x60", "10x60,100000x60", "10xmany"],
)
def test_a_bad_plan_is_refused_with_a_plain_message(text):
    with pytest.raises(plan.PlanError) as caught:
        plan.parse_steps(text)
    assert "LOAD_STEPS" in str(caught.value) or "step" in str(caught.value)


def test_a_step_shorter_than_the_percentile_window_is_refused():
    with pytest.raises(plan.PlanError):
        plan.parse_steps("10x19")
    assert plan.parse_steps("10x20")[0].seconds == 20


def test_the_stub_mode_needs_no_code():
    mode, steps = plan.check_start({"LOAD_MODE": "stub", "LOAD_STEPS": "10x30,20x30"})
    assert mode == "stub" and [s.users for s in steps] == [10, 20]


def test_an_unknown_mode_is_refused():
    with pytest.raises(plan.PlanError):
        plan.check_start({"LOAD_MODE": "production"})


# ================================================================================ the host name
@pytest.mark.parametrize(
    "host", ["http://127.0.0.1:8099", "http://localhost:8000", "127.0.0.1", "http://[::1]:8099"]
)
def test_a_loopback_host_is_accepted(host):
    assert plan.is_loopback(host)


@pytest.mark.parametrize(
    "host",
    [
        "https://localhost.evil.example",
        "http://x-127.0.0.1.onrender.com",
        "http://127.0.0.1.evil.example:8099",
        "https://bfsi.onrender.com",
        "http://10.0.0.5:8099",
        "",
    ],
)
def test_a_host_that_only_contains_a_loopback_name_is_refused(host):
    assert not plan.is_loopback(host)


# ============================================================================== real isolation
LOCAL_ENV = {"QDRANT_URL": "https://q.example.cloud:6333", "LOAD_COLLECTION": "documents_eval"}


def test_a_cloud_vector_store_is_allowed_for_an_eval_collection_only():
    assert plan.real_isolation_problems(LOCAL_ENV) == []
    assert plan.real_isolation_problems({**LOCAL_ENV, "LOAD_COLLECTION": "documents"})
    assert plan.real_isolation_problems({**LOCAL_ENV, "LOAD_COLLECTION": "Documents_eval"})
    assert plan.real_isolation_problems({**LOCAL_ENV, "LOAD_COLLECTION": "my_docs_eval"})


def test_a_cloud_database_and_a_cloud_cache_are_refused_by_name():
    for name, value in (
        ("DATABASE_URL", "postgresql://user@db.example.cloud/main"),
        ("UPSTASH_REDIS_URL", "rediss://default@r.example.cloud:6379"),
        ("EVAL_DATABASE_URL", "postgresql://user@db.example.cloud/eval"),
    ):
        problems = plan.real_isolation_problems({**LOCAL_ENV, name: value})
        assert any(name in line for line in problems), name
        assert "example.cloud" not in " ".join(problems), "names only, never the address"


def test_a_local_database_and_cache_are_not_a_problem():
    env = {**LOCAL_ENV, "DATABASE_URL": "sqlite:///x.db", "UPSTASH_REDIS_URL": "redis://localhost"}
    assert plan.real_isolation_problems(env) == []


def test_the_default_collection_is_the_evaluation_one():
    assert plan.collection_from({}) == "documents_eval"
    assert plan.real_isolation_problems({}) == []


def test_a_real_run_in_the_locust_window_refuses_a_cloud_database():
    env = {
        "LOAD_MODE": "real",
        "LOAD_STEPS": "10x20",
        "LOAD_CONFIRM": "AAAA-1111",
        "DATABASE_URL": "postgresql://user@db.example.cloud/main",
    }
    with pytest.raises(plan.PlanError) as caught:
        plan.check_start(env)
    assert "DATABASE_URL" in str(caught.value)


def test_a_real_run_without_a_code_is_refused_in_the_locust_window():
    with pytest.raises(plan.PlanError) as caught:
        plan.check_start({"LOAD_MODE": "real", "LOAD_STEPS": "10x20"})
    assert "LOAD_CONFIRM" in str(caught.value) and "load_estimate.py" in str(caught.value)


# =================================================================================== the ticket
def make_ticket(out_dir, steps="10x20", now=NOW, estimate=None):
    parsed = plan.parse_steps(steps)
    estimate = estimate or {"batches": [["a"]], "total_requests": 5, "total_tokens": 50}
    return parsed, plan.issue_ticket(estimate, parsed, now=now, folder=out_dir)


def test_a_fresh_ticket_is_used_once_and_only_once(out_dir):
    steps, ticket = make_ticket(out_dir)
    used = plan.consume_ticket(ticket.code.lower(), steps, now=NOW + timedelta(minutes=5),
                               folder=out_dir)  # fmt: skip
    assert used.used is True
    with pytest.raises(plan.PlanError) as caught:
        plan.consume_ticket(ticket.code, steps, now=NOW + timedelta(minutes=6), folder=out_dir)
    assert "already used" in str(caught.value)


def test_a_missing_ticket_is_refused(out_dir):
    with pytest.raises(plan.PlanError) as caught:
        plan.consume_ticket("AAAA-1111", plan.parse_steps("10x20"), now=NOW, folder=out_dir)
    assert "no ticket" in str(caught.value)


def test_a_missing_or_wrong_code_is_refused_and_does_not_use_the_ticket_up(out_dir):
    steps, ticket = make_ticket(out_dir)
    for given in ("", "ZZZZ-0000", ticket.code[:-1]):
        with pytest.raises(plan.PlanError):
            plan.consume_ticket(given, steps, now=NOW, folder=out_dir)
    assert plan.consume_ticket(ticket.code, steps, now=NOW, folder=out_dir).used


def test_a_ticket_older_than_thirty_minutes_is_refused(out_dir):
    from app.eval import constants as c

    steps, ticket = make_ticket(out_dir)
    late = NOW + timedelta(minutes=c.CONFIRMATION_MINUTES, seconds=1)
    with pytest.raises(plan.PlanError) as caught:
        plan.consume_ticket(ticket.code, steps, now=late, folder=out_dir)
    assert "older than" in str(caught.value)
    just_in_time = NOW + timedelta(minutes=c.CONFIRMATION_MINUTES - 1)
    assert plan.consume_ticket(ticket.code, steps, now=just_in_time, folder=out_dir).used


def test_a_ticket_from_yesterday_is_refused_even_inside_the_window(out_dir):
    steps, ticket = make_ticket(out_dir, now=datetime(2026, 10, 11, 23, 50, tzinfo=UTC))
    with pytest.raises(plan.PlanError) as caught:
        plan.consume_ticket(ticket.code, steps, now=datetime(2026, 10, 12, 0, 5, tzinfo=UTC),
                            folder=out_dir)  # fmt: skip
    assert "another day" in str(caught.value)


def test_the_ticket_belongs_to_its_own_plan(out_dir):
    _steps, ticket = make_ticket(out_dir, steps="10x20")
    with pytest.raises(plan.PlanError) as caught:
        plan.consume_ticket(ticket.code, plan.parse_steps("10x20,20x20"), now=NOW, folder=out_dir)
    assert "plan" in str(caught.value)


def test_the_code_changes_with_the_plan_the_numbers_and_the_day():
    steps = plan.parse_steps("10x20")
    estimate = {"batches": [["a"]], "total_requests": 5, "total_tokens": 50}
    base = plan.confirmation(estimate, steps, "2026-10-12")
    assert base != plan.confirmation(estimate, steps, "2026-10-13")
    assert base != plan.confirmation(estimate, plan.parse_steps("10x30"), "2026-10-12")
    assert base != plan.confirmation({**estimate, "total_tokens": 51}, steps, "2026-10-12")


def test_the_real_server_marker_binds_the_locust_side_to_the_consumed_ticket(out_dir):
    steps, ticket = make_ticket(out_dir)
    with pytest.raises(plan.PlanError):
        plan.check_marker(ticket.code, steps, "http://127.0.0.1:8099", folder=out_dir)  # none yet
    plan.write_marker(ticket, 8099, now=NOW, folder=out_dir)
    plan.check_marker(ticket.code, steps, "http://127.0.0.1:8099", folder=out_dir)
    for given, host, shape in (
        ("ZZZZ-0000", "http://127.0.0.1:8099", steps),
        (ticket.code, "http://127.0.0.1:8000", steps),
        (ticket.code, "http://127.0.0.1:8099", plan.parse_steps("10x20,20x20")),
    ):
        with pytest.raises(plan.PlanError):
            plan.check_marker(given, shape, host, folder=out_dir)


# ================================================================================= the estimate
def real_env(**extra):
    return {
        "GROQ_API_KEYS": "g" + secrets.token_hex(8),
        "GEMINI_API_KEYS": "m" + secrets.token_hex(8),
        **extra,
    }


def test_the_estimate_script_prints_the_ticket_truthfully_and_makes_no_call(out_dir):
    lines = []
    status = load_estimate.main(["--steps", "10x20"], real_env(), out=lines.append)
    shown = "\n".join(lines)
    assert status == 0, shown
    assert "LOAD_CONFIRM=" in shown and "explicit yes" in shown
    assert "THIS plan" in shown and "today (UTC" in shown and "works ONCE" in shown
    assert "30 minutes" in shown
    assert "writes to this machine only" in shown and "only reads the test collection" in shown
    assert plan.ticket_path().is_file()


def test_the_estimate_script_makes_no_ticket_for_a_plan_over_the_daily_limits(out_dir):
    lines = []
    status = load_estimate.main([], real_env(), out=lines.append)  # the default plan: too big
    assert status == 2 and "no ticket is made" in "\n".join(lines)
    assert not plan.ticket_path().exists()


def test_the_estimate_script_refuses_on_render_and_a_bad_plan(out_dir):
    lines = []
    assert load_estimate.main([], {**real_env(), "RENDER": "true"}, out=lines.append) == 1
    assert load_estimate.main(["--steps", "5x5"], real_env(), out=lines.append) == 1
    assert not plan.ticket_path().exists()


# ============================================================================ the real server
def test_the_real_server_listens_on_this_machine_only():
    with pytest.raises(SystemExit) as caught:
        real_server.check_start(LOCAL_ENV, "0.0.0.0")
    assert "this machine only" in str(caught.value)
    real_server.check_start(LOCAL_ENV, "127.0.0.1")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DATABASE_URL", "postgresql://user@db.example.cloud/main"),
        ("UPSTASH_REDIS_URL", "rediss://default@r.example.cloud:6379"),
        ("LOAD_COLLECTION", "documents"),
    ],
)
def test_the_real_server_refuses_a_cloud_store_and_a_non_eval_collection(name, value):
    with pytest.raises(SystemExit) as caught:
        real_server.check_start({**LOCAL_ENV, name: value}, "127.0.0.1")
    text = str(caught.value)
    assert text.startswith("refused:") and "example.cloud" not in text


def test_the_real_server_needs_the_vector_store_setting():
    with pytest.raises(SystemExit) as caught:
        real_server.check_start({}, "127.0.0.1")
    assert "QDRANT_URL" in str(caught.value)


def test_the_cloud_collection_is_only_read():
    from qdrant_client import QdrantClient, models

    client = QdrantClient(":memory:")
    client.create_collection("documents_eval", vectors_config=models.VectorParams(size=4,
                             distance=models.Distance.COSINE))  # fmt: skip
    wrapped = real_server.ReadOnlyQdrant(client)
    assert wrapped.collection_exists("documents_eval") is True
    assert wrapped.count("documents_eval").count == 0
    for call in (
        lambda: wrapped.upsert("documents_eval", points=[]),
        lambda: wrapped.delete("documents_eval", points_selector=[1]),
        lambda: wrapped.set_payload("documents_eval", payload={}, points=[1]),
        lambda: wrapped.update_vectors("documents_eval", points=[]),
        lambda: wrapped.create_collection("x", vectors_config={}),
        lambda: wrapped.delete_collection("documents_eval"),
        lambda: wrapped.create_payload_index("documents_eval", "f", "keyword"),
        lambda: wrapped.upload_points("documents_eval", points=[]),
        lambda: wrapped.batch_update_points("documents_eval", update_operations=[]),
        lambda: wrapped.delete_payload("documents_eval", keys=["x"], points=[1]),
        lambda: wrapped.overwrite_payload("documents_eval", payload={}, points=[1]),
        lambda: wrapped.create_snapshot("documents_eval"),
    ):
        with pytest.raises((real_server.ReadOnlyViolation, AttributeError)):
            call()


# ================================================================================ the stub server
def test_the_stub_server_listens_on_this_machine_only():
    with pytest.raises(SystemExit) as caught:
        stub_server.check_local("0.0.0.0", {})
    assert "this machine only" in str(caught.value)
    with pytest.raises(SystemExit):
        stub_server.check_local("localhost.evil.example", {})
    stub_server.check_local("127.0.0.1", {})


def test_the_stub_server_refuses_a_remote_store_address_in_the_shell():
    env = {"DATABASE_URL": "postgresql://user@db.example.com/main"}
    with pytest.raises(SystemExit) as caught:
        stub_server.check_local("127.0.0.1", env)
    assert "DATABASE_URL" in str(caught.value) and "db.example.com" not in str(caught.value)


def test_the_users_are_made_in_the_local_database_and_can_log_in(tmp_path, out_dir):
    from starlette.testclient import TestClient

    app, rig = stub_server.build_app(tmp_path, latency_ms=0, users=12)
    try:
        names, password = seed.read_users_file(plan.users_path())
        assert len(names) == len(set(names)) == 12
        with TestClient(app) as client:
            for name in names[:8]:  # more than the per-address login limit would allow
                reply = client.post("/auth/login", json={"username": name, "password": password})
                assert reply.status_code == 200, reply.text
            headers = {"Authorization": "Bearer " + reply.json()["access_token"]}
            from app.eval.cases import load_cases

            question = next(
                c for c in load_cases()[1] if c.active and c.role == "customer"
                and c.category == "factual"
            ).turns[-1]  # fmt: skip
            answered = client.post("/chat", json={"message": question}, headers=headers)
            assert answered.status_code == 200, answered.text
    finally:
        rig.close()


def test_the_registration_limit_per_address_stays_on_in_the_stub(tmp_path, out_dir):
    from starlette.testclient import TestClient

    app, rig = stub_server.build_app(tmp_path, latency_ms=0, users=0)
    try:
        with TestClient(app) as client:
            password = secrets.token_urlsafe(18) + "Aa1"
            codes = []
            for index in range(8):
                body = {"username": f"reg{index}x", "email": f"reg{index}@load.invalid",
                        "password": password}  # fmt: skip
                codes.append(client.post("/auth/register", json=body).status_code)
        assert codes[0] == 201 and 429 in codes, codes  # the real limit is not switched off
    finally:
        rig.close()


def test_seeding_uses_the_application_account_function_and_a_customer_role(tmp_path):
    from app.db.connection import connect
    from app.db.migrate import run as migrate

    url = f"sqlite:///{tmp_path / 's.db'}"
    migrate(url, ROOT / "migrations", log=lambda line: None)
    db = connect(url)
    try:
        names, password = seed.seed_users(db, 3)
        rows = db.query_all("SELECT username, role FROM users ORDER BY username")
    finally:
        db.close()
    assert [r[0] for r in rows] == sorted(names) and {r[1] for r in rows} == {"customer"}
    assert len(password) >= 10


# ====================================================================================== the report
def history(rows, windowed=True):
    out = io.StringIO()
    names = ["Timestamp", "User Count", "Type", "Name", "Requests/s", "Failures/s", "50%", "95%"]
    writer = csv.DictWriter(out, fieldnames=names)
    writer.writeheader()
    for row in rows:
        writer.writerow({"Type": "", "Name": "Aggregated", **row})
        if windowed:
            writer.writerow({**row, "Type": "POST", "Name": "chat"})
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def side(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=plan_columns())
    writer.writeheader()
    for row in rows:
        writer.writerow({"chat_ok": 0, "chat_rate_limited": 0, "chat_unavailable": 0,
                         "chat_other": 0, **row})  # fmt: skip
    return list(csv.DictReader(io.StringIO(out.getvalue())))


def plan_columns():
    return ["time", "target_users", "active_users", "chat_ok", "chat_rate_limited",
            "chat_unavailable", "chat_other"]  # fmt: skip


STEPS = plan.parse_steps("10x40,20x40,40x40")


def seconds(step_index, users, requests=5.0, failures=0.0, p95=800, p50=200, count=40, t0=1000):
    """One history row per second for one step of STEPS (40 s each)."""
    start = t0 + step_index * 40
    return [
        {"Timestamp": start + i, "User Count": users, "Requests/s": requests,
         "Failures/s": failures, "50%": p50, "95%": p95}
        for i in range(count)
    ]  # fmt: skip


def active(step_index, target, now_active, t0=1000, **counters):
    start = t0 + step_index * 40
    rows = []
    for i in range(40):
        cumulative = {k: int(v * (i + 1) / 40) for k, v in counters.items()}
        rows.append({"time": start + i, "target_users": target, "active_users": now_active,
                     **cumulative})  # fmt: skip
    return rows


def analysed(*parts, side_rows=(), windowed=True):
    rows = [r for part in parts for r in part]
    return report.analyse(history(rows, windowed), side(side_rows), STEPS)


def test_rows_are_assigned_to_steps_by_time_not_by_the_user_count():
    # while users are added the count passes through values that are not a step of the plan, and
    # a count can reach the next step's number early: the time of the row decides its step
    ramp = [{"Timestamp": 1000 + i, "User Count": 3 + i, "Requests/s": 1, "Failures/s": 0,
             "50%": 100, "95%": 300} for i in range(4)]  # fmt: skip
    early = [
        {"Timestamp": 1020 + i, "User Count": 20, "Requests/s": 10, "Failures/s": 5,
         "50%": 100, "95%": 300}
        for i in range(10)
    ]  # fmt: skip
    clean_first = [r for r in seconds(0, 10)[4:] if not 20 <= r["Timestamp"] - 1000 < 30]
    steps, _ = analysed(ramp, clean_first, early, seconds(1, 20), seconds(2, 40))
    assert [s.users for s in steps] == [10, 20, 40]
    assert report.first_break(steps).users == 10, "the failing rows belong to the first step"


def test_a_step_with_more_than_one_percent_failed_is_the_first_to_break():
    steps, windowed = analysed(
        seconds(0, 10),
        seconds(1, 20),
        seconds(2, 40, requests=20, failures=1),  # 5 %
    )
    assert windowed
    broke = report.first_break(steps)
    assert broke is not None and broke.users == 40
    assert "more than 1 % of requests failed" in "\n".join(report.render(steps))


def test_a_p95_over_the_budget_breaks_a_step_by_latency_alone():
    steps, _ = analysed(seconds(0, 10), seconds(1, 20, p95=31_000), seconds(2, 40))
    broke = report.first_break(steps)
    assert broke is not None and broke.users == 20
    assert "p95 passed the 30 s" in "\n".join(report.render(steps))


def test_unavailable_answers_break_a_step_even_when_locust_saw_no_failure():
    sides = (
        active(0, 10, 10, chat_ok=40)
        + active(1, 20, 20, chat_ok=70, chat_unavailable=30)  # 30 % temporarily unavailable
        + active(2, 40, 40, chat_ok=100)
    )
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40), side_rows=sides)
    broke = report.first_break(steps)
    assert broke is not None and broke.users == 20
    assert broke.unavailable_share == pytest.approx(0.3, abs=0.05)
    assert "temporarily unavailable" in "\n".join(report.render(steps))


def test_the_per_user_rate_limit_is_not_a_failure_and_is_counted_apart():
    sides = active(0, 10, 10, chat_ok=20, chat_rate_limited=20)
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40), side_rows=sides)
    assert steps[0].broken is False and steps[0].rate_limited >= 18


def test_a_step_with_fewer_than_eighty_percent_of_its_users_active_fails():
    sides = active(0, 10, 10, chat_ok=40) + active(1, 20, 12, chat_ok=40)  # 60 % active
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40), side_rows=sides)
    assert steps[0].active_share == pytest.approx(1.0)
    broke = report.first_break(steps)
    assert broke is not None and broke.users == 20
    assert "fewer than 80 %" in "\n".join(report.render(steps))


def test_exactly_eighty_percent_active_is_enough():
    sides = active(0, 10, 8, chat_ok=40)
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40), side_rows=sides)
    assert steps[0].broken is False


def test_a_history_without_per_endpoint_rows_is_reported_as_cumulative():
    steps, windowed = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40), windowed=False)
    assert windowed is False
    text = "\n".join(report.render(steps, windowed=windowed))
    assert "CUMULATIVE" in text and "--csv-full-history" in text


def test_a_run_that_did_not_break_says_so_with_its_top_step():
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40))
    assert "did not break up to 40 users" in "\n".join(report.render(steps))


def test_the_report_says_it_is_concurrency_not_capacity_and_lists_the_blind_spots():
    steps, _ = analysed(seconds(0, 10), seconds(1, 20), seconds(2, 40))
    text = "\n".join(report.render(steps, "stub"))
    assert "not host capacity" in text
    assert "Postgres connection ceiling" in text and "one database connection per chat" in text
    assert "Provider limits" in text and "Host capacity" in text


def test_a_failure_share_is_failures_over_requests():
    steps, _ = analysed(seconds(0, 10, requests=100, failures=2), [], [])
    assert steps[0].error_rate == pytest.approx(0.02)


def test_the_settle_seconds_are_left_out_so_the_previous_step_does_not_leak_in():
    slow_start = seconds(1, 20)
    for row in slow_start[:5]:
        row["95%"] = 90_000  # the window still holds the end of the step before
    steps, _ = analysed(seconds(0, 10), slow_start, seconds(2, 40))
    assert steps[1].p95_ms == 800 and not steps[1].broken


def test_a_run_with_no_rows_says_so():
    steps, _ = report.analyse([], [], STEPS)
    assert "no rows" in "\n".join(report.render(steps))


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


def test_the_locust_file_never_registers_and_counts_unavailable_as_a_failure():
    source = (LOAD / "locustfile.py").read_text("utf-8")
    assert "/auth/register" not in source, "accounts are made by the server, not the API"
    assert 'reply.failure("service_unavailable")' in source
    assert "plan.is_loopback(host)" in source and "check_marker" in source


def test_the_locust_file_and_the_report_name_the_full_history_flag():
    assert "--csv-full-history" in (LOAD / "locustfile.py").read_text("utf-8")
    assert "--csv-full-history" in (LOAD / "report.py").read_text("utf-8")
    assert "--csv-full-history" in (ROOT / "docs" / "OWNER_RUNS.md").read_text("utf-8")
