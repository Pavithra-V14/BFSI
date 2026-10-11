"""The queries of Phase 12 on a real Postgres. Runs only in CI, on the throw-away service container
(TEST_POSTGRES_URL); skipped everywhere else (and never run on this machine).

Written without being run against Postgres: the first CI run is its first run. It covers the new
queries: the alert rows (insert, count, recent, hour lookup), the rollup upserts (add and replace),
the daily counts and series (including the cast of the timestamp to text), the trace-deletion queue
and the owner-run purge.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.db.connection import connect
from app.db.migrate import run
from app.memory import retention
from app.observability import metrics, traces
from app.observability.alerts import AlertStore
from app.pipeline.langfuse_sink import LangfuseConfig

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
CONFIG = LangfuseConfig(
    "https://langfuse.acme-corp.org", "pk-public0001", "sk-secret0001", "k" * 40
)


@pytest.fixture
def url():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    target = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(target, MIGRATIONS, log=lambda line: None)
    yield target
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(url):
    database = connect(url)
    yield database
    database.close()


def test_alert_rows_are_written_counted_and_listed(url):
    store = AlertStore(lambda: connect(url))
    first = store.insert("graph_failed", "critical", 1, False, NOW)
    store.bump(first, 4)
    store.mark_notified(first)
    store.insert("jev_unavailable", "warning", 1, False, NOW - timedelta(hours=2))
    recent = store.recent(NOW - timedelta(hours=24))
    assert [(a["name"], a["count"], a["notified"]) for a in recent] == [
        ("graph_failed", 5, True),
        ("jev_unavailable", 1, False),
    ]
    assert store.notified_since(NOW - timedelta(hours=1)) == 1
    found = store.last_in_hour("graph_failed", NOW - timedelta(hours=1))
    assert found is not None and found[0] == first


def test_hourly_rows_are_added_to_and_daily_rows_are_replaced(db):
    hour = datetime(2026, 10, 11, 8, tzinfo=UTC)
    metrics.add_hourly(db, {(hour, "customer", "requests"): 3.0})
    metrics.add_hourly(db, {(hour, "customer", "requests"): 2.0})
    total = metrics.hourly_totals(db, hour, hour + timedelta(hours=1))
    assert total[("customer", "requests")] == 5.0
    metrics.set_daily(db, NOW, {("customer", "thumbs_up"): 3.0})
    metrics.set_daily(db, NOW, {("customer", "thumbs_up"): 7.0})
    (item,) = [d for d in metrics.daily_series(db, 2, now=NOW) if d["day"] == "2026-10-11"]
    assert item["roles"]["customer"]["thumbs_up"] == 7.0
    assert item["roles"]["customer"]["requests"] == 5.0  # the hour summed into its day


def test_the_daily_counts_run_on_postgres(db):
    user = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role) "
        "VALUES (?, 'asha', 'a@example.test', 'h', 'customer')",
        (user,),
    )
    session = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
        (session, user, utc_text(NOW), utc_text(NOW)),
    )
    db.execute(
        "INSERT INTO messages (id, session_id, seq, role, content, created_at) "
        "VALUES (?, ?, 1, 'user', 'masked', ?)",
        (str(uuid.uuid4()), session, utc_text(NOW)),
    )
    db.raw.commit()
    values = metrics.daily_from_tables(db, NOW)
    assert values[("customer", "questions")] == 1.0
    assert values[("customer", "active_users")] == 1.0
    assert values[("customer", "chats_started")] == 1.0


def test_the_trace_queue_works_on_postgres(db, monkeypatch):
    traces.enqueue(db, ["h1", "h2"], NOW)
    traces.enqueue(db, ["h1"], NOW)  # the unique index makes this a no-op
    assert traces.pending_count(db) == 2

    present = {"h1": ["t1"], "h2": ["t2"]}

    def handler(request):
        if request.method == "GET":
            ids = present.get(request.url.params["sessionId"], [])
            return httpx.Response(200, json={"data": [{"id": i} for i in ids]})
        trace_id = request.url.path.rsplit("/", 1)[1]
        for ids in present.values():
            if trace_id in ids:
                ids.remove(trace_id)
        return httpx.Response(200, json={})

    monkeypatch.setattr(traces, "TRANSPORT", httpx.MockTransport(handler))
    deleter = traces.TraceDeleter.from_config(CONFIG)
    first = traces.process_pending(db, deleter, NOW)
    # the traces are gone, but a row waits for a clean look at least 10 minutes after it was queued
    assert first.removed_chats == 0 and traces.pending_count(db) == 2
    later = NOW + timedelta(seconds=traces.GRACE_SECONDS + 60)
    result = traces.process_pending(db, deleter, later)
    assert result.removed_chats == 2 and traces.pending_count(db) == 0


def test_the_purge_runs_on_postgres(db):
    with db.transaction(write=True):
        write_audit(
            db,
            actor="system",
            action="login",
            target=None,
            details={},
            now=NOW - timedelta(days=45),
        )
    db.execute(
        "INSERT INTO alert_events (id, name, severity, event_count, notified, created_at) "
        "VALUES (?, 'graph_failed', 'critical', 1, ?, ?)",
        (str(uuid.uuid4()), False, utc_text(NOW - timedelta(days=45))),
    )
    db.raw.commit()
    result = retention.purge_old_rows(db, now=NOW, actor="cli:test")
    assert (result.audit_rows, result.alert_rows) == (1, 1)
    assert db.query_one("SELECT COUNT(*) FROM alert_events")[0] == 0
