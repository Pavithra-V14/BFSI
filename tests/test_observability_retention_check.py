"""The retention task must run (Phase 15): a warning when no `retention_sweep` row is seven days
old or newer. The task writes one row on every real run, also when it deleted nothing, so the row
is the proof that the scheduled task ran; a dry run writes none."""

from datetime import UTC, datetime, timedelta

import pytest

from app.accounts.audit import write_audit
from app.db.connection import connect
from app.db.migrate import run
from app.memory import retention
from app.observability import alerts as alerts_module
from app.observability import service
from tests.api_fixtures import MIGRATIONS

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


class Collect:
    def __init__(self):
        self.raised: list[tuple[str, str]] = []

    def raise_rule(self, name, severity, count=1):
        self.raised.append((name, severity))


@pytest.fixture
def parts(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    collector = Collect()
    observability = service.Observability(
        db_factory=lambda: connect(url),
        alerts=collector,
        now=lambda: NOW,  # type: ignore[arg-type]
    )
    db = connect(url)
    yield observability, collector, db
    db.close()


def audit_row(db, action, days_ago):
    with db.transaction(write=True):
        write_audit(
            db,
            actor="cli:test",
            action=action,
            target="x",
            details={},
            now=NOW - timedelta(days=days_ago),
        )


def test_a_fresh_sweep_row_means_no_warning(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 1)
    assert observability.check_retention_once() is False and collector.raised == []


def test_a_sweep_row_four_days_old_means_no_warning_a_weekend_included(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 4)  # Friday's run seen on Tuesday morning: still quiet
    assert observability.check_retention_once() is False and collector.raised == []


def test_no_sweep_row_for_more_than_seven_days_is_a_warning(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 8)
    assert observability.check_retention_once() is True
    assert collector.raised == [("retention_overdue", alerts_module.WARNING)]


def test_exactly_seven_days_is_not_yet_overdue(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 7)
    assert observability.check_retention_once() is False


def test_other_audit_rows_never_stand_in_for_the_sweep_row(parts):
    """Sign-ins write audit rows all day. A newer login row must not hide an old sweep row: the
    mutation that looks at the newest audit row of ANY action silences the alert for good."""
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 8)
    for days_ago in (0, 0, 1, 2):
        audit_row(db, "login", days_ago)
    audit_row(db, "audit_purge", 1)  # a different retention action is not the sweep either
    assert observability.check_retention_once() is True
    assert collector.raised == [("retention_overdue", alerts_module.WARNING)]


def test_the_newest_of_several_sweep_rows_decides(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 20)
    audit_row(db, "retention_sweep", 15)
    audit_row(db, "retention_sweep", 1)
    assert observability.check_retention_once() is False and collector.raised == []


def test_several_old_sweep_rows_and_no_new_one_is_a_warning(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 30)
    audit_row(db, "retention_sweep", 9)
    assert observability.check_retention_once() is True and len(collector.raised) == 1


def test_a_new_deployment_is_not_blamed_before_seven_days_have_passed(parts):
    observability, collector, db = parts
    audit_row(db, "login", 5)  # the service is five days old and has never swept
    assert observability.check_retention_once() is False and collector.raised == []


def test_a_service_older_than_seven_days_that_never_swept_is_a_warning(parts):
    observability, collector, db = parts
    audit_row(db, "login", 9)
    assert observability.check_retention_once() is True
    assert collector.raised[0][0] == "retention_overdue"


def test_an_empty_audit_log_raises_nothing(parts):
    observability, collector, _db = parts
    assert observability.check_retention_once() is False and collector.raised == []


def test_a_run_that_deleted_nothing_still_writes_its_row_and_a_dry_run_does_not(parts):
    _observability, _collector, db = parts
    retention.sweep(db, now=NOW, actor="cli:test", dry_run=True)
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == 0
    retention.sweep(db, now=NOW, actor="cli:test")
    rows = db.query_all("SELECT action, details FROM audit_log")
    assert [r[0] for r in rows] == ["retention_sweep"] and '"sessions": 0' in rows[0][1]


def test_the_check_interval_and_the_overdue_limit_are_the_documented_ones():
    from app.observability.service import RETENTION_CHECK_SECONDS, RETENTION_OVERDUE_DAYS

    assert RETENTION_CHECK_SECONDS == 3_600.0 and RETENTION_OVERDUE_DAYS == 7


def test_the_background_thread_really_calls_the_check(tmp_path, monkeypatch):
    """Not only a thread of the right name: the loop must call `check_retention_once`."""
    import threading

    url = f"sqlite:///{tmp_path / 'loop.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    manager = alerts_module.AlertManager(store=None, background=False)
    observability = service.Observability(db_factory=lambda: connect(url), alerts=manager)
    called = threading.Event()
    monkeypatch.setattr(service, "RETENTION_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(service, "ROLLUP_SECONDS", 3600.0)
    monkeypatch.setattr(service, "TRACE_SECONDS", 3600.0)
    monkeypatch.setattr(observability, "check_retention_once", lambda: called.set() or False)
    observability.start()
    try:
        assert called.wait(timeout=5), "the retention check was never called by the loop"
    finally:
        observability.stop()


def test_a_sweep_that_fails_half_way_writes_no_row_and_deletes_nothing(parts, monkeypatch):
    """A row written for a run that did not finish would make the task look healthy."""
    _observability, _collector, db = parts
    old = NOW - timedelta(days=120)
    with db.transaction(write=True):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES ('u1', 'asha', 'asha@example.test', 'x')"
        )
        for session_id in ("s1", "s2"):
            db.execute(
                "INSERT INTO sessions (id, user_id, last_active_at) VALUES (?, 'u1', ?)",
                (session_id, old.strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
    real = retention.delete_session
    seen = []

    def second_one_fails(raw, session_id):
        seen.append(session_id)
        if len(seen) == 2:
            raise RuntimeError("database went away")
        return real(raw, session_id)

    monkeypatch.setattr(retention, "delete_session", second_one_fails)
    with pytest.raises(RuntimeError):
        retention.sweep(db, now=NOW, actor="cli:test")
    assert db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'retention_sweep'")[0] == 0
    assert db.query_one("SELECT COUNT(*) FROM sessions")[0] == 2, "all or nothing"


def test_the_service_runs_the_check_in_its_own_background_thread(tmp_path):
    """Not only the function: `start()` must register it, or the warning never fires."""
    url = f"sqlite:///{tmp_path / 'b.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    manager = alerts_module.AlertManager(store=None, background=False)
    observability = service.Observability(db_factory=lambda: connect(url), alerts=manager)
    observability.start()
    try:
        names = {t.name for t in observability._threads}
        assert {"rollup-writer", "trace-deleter", "retention-check"} <= names
    finally:
        observability.stop()
