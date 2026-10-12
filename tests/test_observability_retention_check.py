"""The retention task must run (Phase 15): a warning when no `retention_sweep` row is three days
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


def test_no_sweep_row_for_more_than_three_days_is_a_warning(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 4)
    assert observability.check_retention_once() is True
    assert collector.raised == [("retention_overdue", alerts_module.WARNING)]


def test_exactly_three_days_is_not_yet_overdue(parts):
    observability, collector, db = parts
    audit_row(db, "retention_sweep", 3)
    assert observability.check_retention_once() is False


def test_a_new_deployment_is_not_blamed_before_three_days_have_passed(parts):
    observability, collector, db = parts
    audit_row(db, "login", 1)  # the service is one day old and has never swept
    assert observability.check_retention_once() is False and collector.raised == []


def test_a_service_older_than_three_days_that_never_swept_is_a_warning(parts):
    observability, collector, db = parts
    audit_row(db, "login", 5)
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


def test_the_check_runs_in_the_background_loop():
    from app.observability.service import RETENTION_CHECK_SECONDS, RETENTION_OVERDUE_DAYS

    assert RETENTION_CHECK_SECONDS == 3_600.0 and RETENTION_OVERDUE_DAYS == 3


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
