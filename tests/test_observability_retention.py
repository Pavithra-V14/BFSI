"""The owner-run purge of old audit, alert and hourly metric rows (spec 12.1, 30 days: pilot).

Not automatic: nothing in the API process calls it; the retention command does, when the owner runs
it. 30 days is a pilot value to revisit in Phase 16."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.db.connection import connect
from app.db.migrate import run
from app.memory import retention
from tests.api_fixtures import MIGRATIONS

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    connection = connect(url)
    yield connection
    connection.close()


def audit_at(db, days_old, action="login"):
    with db.transaction(write=True):
        write_audit(
            db,
            actor="system",
            action=action,
            target=None,
            details={},
            now=NOW - timedelta(days=days_old),
        )


def alert_at(db, days_old):
    db.execute(
        "INSERT INTO alert_events (id, name, severity, event_count, notified, created_at) "
        "VALUES (?, 'graph_failed', 'critical', 1, ?, ?)",
        (f"a{days_old}-{id(db)}", False, utc_text(NOW - timedelta(days=days_old))),
    )


def rollup_at(db, period, days_old, metric="requests"):
    db.execute(
        "INSERT INTO metric_rollups (id, period, period_start, role, metric, value) "
        "VALUES (?, ?, ?, 'all', ?, 1)",
        (f"r{period}{days_old}{metric}", period, utc_text(NOW - timedelta(days=days_old)), metric),
    )


def counts(db):
    one = lambda sql: db.query_one(sql)[0]  # noqa: E731
    return (
        one("SELECT COUNT(*) FROM audit_log"),
        one("SELECT COUNT(*) FROM alert_events"),
        one("SELECT COUNT(*) FROM metric_rollups WHERE period = 'hour'"),
        one("SELECT COUNT(*) FROM metric_rollups WHERE period = 'day'"),
    )


def test_only_rows_older_than_30_days_go(db):
    for days in (1, 29, 31, 90):
        audit_at(db, days)
        alert_at(db, days)
        rollup_at(db, "hour", days)
    rollup_at(db, "day", 90)  # a daily row is counts only and is kept
    db.raw.commit()
    result = retention.purge_old_rows(db, now=NOW, actor="cli:test")
    assert (result.audit_rows, result.alert_rows, result.hourly_rows) == (2, 2, 2)
    audit, alerts, hourly, daily = counts(db)
    assert (alerts, hourly, daily) == (2, 2, 1)
    assert audit == 2 + 1  # the two young rows and the purge's own row


def test_the_purge_writes_one_audit_row_of_counts_only(db):
    audit_at(db, 40)
    alert_at(db, 40)
    db.raw.commit()
    retention.purge_old_rows(db, now=NOW, actor="cli:test")
    rows = db.query_all("SELECT actor, action, target, details FROM audit_log")
    assert len(rows) == 1
    actor, action, target, details = rows[0]
    assert (actor, action, target) == ("cli:test", "audit_purge", "audit_log")
    assert details == '{"alerts": 1, "audit": 1, "days": 30, "eval_samples": 0, "hourly": 0}'


def test_the_purge_counts_the_evaluation_samples_it_was_told_about(db):
    retention.purge_old_rows(db, now=NOW, actor="cli:test", eval_samples=4)
    (details,) = db.query_one("SELECT details FROM audit_log")
    assert '"eval_samples": 4' in details


def test_nothing_to_purge_writes_no_audit_row(db):
    audit_at(db, 2)
    db.raw.commit()
    result = retention.purge_old_rows(db, now=NOW, actor="cli:test")
    assert (result.audit_rows, result.alert_rows, result.hourly_rows) == (0, 0, 0)
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == 1


def test_a_dry_run_counts_and_changes_nothing_not_even_an_audit_row(db):
    audit_at(db, 40)
    alert_at(db, 40)
    db.raw.commit()
    before = counts(db)
    result = retention.purge_old_rows(db, now=NOW, actor="cli:test", dry_run=True)
    assert result.dry_run and result.audit_rows == 1 and result.alert_rows == 1
    assert counts(db) == before


def test_the_cutoff_is_exactly_the_number_of_days(db):
    audit_at(db, 30)  # exactly 30 days old is on the line: kept (not "older than")
    audit_at(db, 31)
    db.raw.commit()
    assert retention.purge_old_rows(db, now=NOW, actor="cli:test").audit_rows == 1


def test_the_command_runs_the_sweep_and_the_purge_and_prints_counts_only(
    db, tmp_path, monkeypatch, capsys
):
    audit_at(db, 45)
    db.raw.commit()
    url = f"sqlite:///{tmp_path / 'r.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LANGFUSE_USER_HASH_KEY", raising=False)
    assert retention.main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert "deleted 0 chats" in out and "1 audit rows" in out and "older than 30 days" in out


def test_the_command_dry_run_says_would_delete(db, tmp_path, monkeypatch, capsys):
    audit_at(db, 45)
    db.raw.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    retention.main(["--dry-run"], now=NOW)
    assert "would delete 1 audit rows" in capsys.readouterr().out


def test_the_value_is_the_pilot_one_and_the_purge_is_not_called_by_the_service():
    assert retention.AUDIT_RETENTION_DAYS == 30
    called_from = []
    for path in (ROOT / "app").rglob("*.py"):
        if path.name == "retention.py":
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "purge_old_rows":
                called_from.append(str(path.relative_to(ROOT)))
            if isinstance(node, ast.ImportFrom) and any(
                a.name == "purge_old_rows" for a in node.names
            ):
                called_from.append(str(path.relative_to(ROOT)))
    assert called_from == []  # owner-run only, never automatic


def _eval_stub(monkeypatch, result):
    def fake(now, *, dry_run):
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(retention, "_purge_eval_samples", fake)


def test_a_failing_sample_purge_never_hides_or_undoes_the_main_parts(
    db, tmp_path, monkeypatch, capsys
):
    audit_at(db, 45)
    db.raw.commit()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.delenv("LANGFUSE_USER_HASH_KEY", raising=False)
    _eval_stub(monkeypatch, RuntimeError("postgresql://user:pw@host/db is down"))
    assert retention.main([], now=NOW) == 0
    out = capsys.readouterr().out
    assert "1 audit rows" in out, "the audit sweep ran first and is reported"
    assert "evaluation samples: not purged (RuntimeError)" in out
    assert "postgresql" not in out and "pw@host" not in out
    assert counts(db)[0] == 1  # only the purge's own audit row is left


def test_the_sample_purge_writes_its_own_audit_row_of_counts(db, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.delenv("LANGFUSE_USER_HASH_KEY", raising=False)
    _eval_stub(monkeypatch, 3)
    assert retention.main([], now=NOW) == 0
    assert "deleted 3 masked evaluation samples" in capsys.readouterr().out
    row = db.query_one(
        "SELECT target, details FROM audit_log WHERE action = 'audit_purge' ORDER BY created_at"
    )
    assert row[0] == "eval_samples" and '"eval_samples": 3' in row[1]
