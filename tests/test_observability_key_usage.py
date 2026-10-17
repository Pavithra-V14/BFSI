"""The saved per-key daily counts: added up in `key_usage_daily`, written by the rollup pass,
put back when a save fails, and loaded again after a restart."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.migrate import run as migrate
from app.observability import key_usage
from app.observability.alerts import AlertManager, AlertStore, config_from_env
from app.observability.service import Observability

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
DAY = "2026-10-17"


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'keys.db'}"
    migrate(url, MIGRATIONS, log=lambda line: None)
    connection = connect(url)
    yield connection
    connection.close()


class Source:
    """What the gateway offers: take, restore and seed."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.seeded = None

    def take_key_usage(self):
        taken, self.rows = self.rows, []
        return taken

    def restore_key_usage(self, taken):
        self.rows = list(taken) + self.rows

    def seed_key_usage(self, rows):
        self.seeded = rows


def observability(tmp_path, db_factory):
    store = AlertStore(db_factory)
    manager = AlertManager(store=store, sender=None, config=config_from_env({}))
    return Observability(db_factory=db_factory, alerts=manager)


def test_the_counts_of_a_day_add_up_across_saves(db):
    key_usage.add_usage(db, [(DAY, "groq", "aaaaaaaa", 2, 30), (DAY, "groq", "bbbbbbbb", 1, 5)])
    key_usage.add_usage(db, [(DAY, "groq", "aaaaaaaa", 3, 40)])
    assert key_usage.load_day(db, DAY) == {"groq": {"aaaaaaaa": (5, 70), "bbbbbbbb": (1, 5)}}
    assert key_usage.load_day(db, "2026-10-16") == {}


def test_the_rollup_pass_saves_what_the_gateway_counted(tmp_path, db):
    path = tmp_path / "keys.db"
    obs = observability(tmp_path, lambda: connect(f"sqlite:///{path}"))
    obs.key_source = Source([(DAY, "groq", "aaaaaaaa", 4, 99)])
    assert obs.save_key_usage_once() == 1
    assert obs.save_key_usage_once() == 0  # nothing new, no database work
    assert key_usage.load_day(db, DAY) == {"groq": {"aaaaaaaa": (4, 99)}}


def test_a_failed_save_puts_the_counts_back_for_the_next_pass(tmp_path):
    def broken():
        raise RuntimeError("database down")

    obs = observability(tmp_path, broken)
    source = Source([(DAY, "groq", "aaaaaaaa", 4, 99)])
    obs.key_source = source
    with pytest.raises(RuntimeError):
        obs.save_key_usage_once()
    assert source.rows == [(DAY, "groq", "aaaaaaaa", 4, 99)]


def test_a_restart_loads_today_s_saved_counts_into_the_gateway(tmp_path, db):
    path = tmp_path / "keys.db"
    key_usage.add_usage(db, [("2026-10-17", "groq", "aaaaaaaa", 7, 700)])
    obs = Observability(
        db_factory=lambda: connect(f"sqlite:///{path}"),
        alerts=observability(tmp_path, lambda: connect(f"sqlite:///{path}")).alerts,
        now=lambda: datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
    )
    obs.key_source = Source([])
    obs.seed_key_usage()
    assert obs.key_source.seeded == {"groq": {"aaaaaaaa": (7, 700)}}


def test_without_a_gateway_nothing_is_saved_or_seeded(tmp_path):
    obs = observability(tmp_path, lambda: (_ for _ in ()).throw(RuntimeError("never called")))
    assert obs.save_key_usage_once() == 0
    obs.seed_key_usage()
