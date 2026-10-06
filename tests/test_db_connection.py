"""The small database handle used by the API (one SQL, Postgres and SQLite)."""

import sqlite3

import pytest

from app.db.connection import Database, connect
from app.db.migrate import MigrationError
from app.store_errors import DatabaseUnavailable


@pytest.fixture
def sqlite_db(tmp_path):
    db = connect(f"sqlite:///{tmp_path / 'c.db'}")
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)")
    yield db
    db.close()


def test_sqlite_is_opened_with_foreign_keys_on(sqlite_db):
    assert sqlite_db.query_one("PRAGMA foreign_keys") == (1,)


def test_query_helpers_return_tuples(sqlite_db):
    sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("a",))
    sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("b",))
    assert sqlite_db.query_one("SELECT name FROM t WHERE name = ?", ("a",)) == ("a",)
    assert sqlite_db.query_one("SELECT name FROM t WHERE name = ?", ("zzz",)) is None
    assert sqlite_db.query_all("SELECT name FROM t ORDER BY name") == [("a",), ("b",)]


def test_a_transaction_commits_on_success(sqlite_db):
    with sqlite_db.transaction():
        sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("a",))
    assert sqlite_db.query_all("SELECT name FROM t") == [("a",)]


def test_a_transaction_rolls_back_on_any_error(sqlite_db):
    with pytest.raises(RuntimeError):
        with sqlite_db.transaction():
            sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("a",))
            raise RuntimeError("boom")
    assert sqlite_db.query_all("SELECT name FROM t") == []


def test_a_transaction_rolls_back_when_the_database_refuses_a_row(sqlite_db):
    sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("a",))
    with pytest.raises(sqlite3.IntegrityError):
        with sqlite_db.transaction():
            sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("b",))
            sqlite_db.execute("INSERT INTO t (name) VALUES (?)", ("a",))  # duplicate
    assert sqlite_db.query_all("SELECT name FROM t") == [("a",)]


def test_a_write_transaction_takes_the_write_lock_at_once(tmp_path):
    url = f"sqlite:///{tmp_path / 'lock.db'}"
    first, second = connect(url), connect(url)
    first.execute("CREATE TABLE t (id INTEGER)")
    second.raw.execute("PRAGMA busy_timeout = 100")
    try:
        with first.transaction(write=True):
            with pytest.raises(sqlite3.OperationalError):  # "database is locked"
                with second.transaction(write=True):
                    pass
    finally:
        first.close()
        second.close()


def test_a_read_transaction_does_not_block_another_reader(tmp_path):
    url = f"sqlite:///{tmp_path / 'read.db'}"
    first, second = connect(url), connect(url)
    first.execute("CREATE TABLE t (id INTEGER)")
    try:
        with first.transaction():
            with second.transaction():
                assert second.query_all("SELECT * FROM t") == []
    finally:
        first.close()
        second.close()


def test_a_connection_may_be_used_from_another_thread(sqlite_db):
    import threading

    result = []
    thread = threading.Thread(target=lambda: result.append(sqlite_db.query_one("SELECT 1")))
    thread.start()
    thread.join()
    assert result == [(1,)]  # the route's worker thread is not the thread that opened it


# ---- Postgres placeholders (no server needed: a stand-in connection records the SQL) ----------


class Recorder:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return self

    def commit(self):
        self.sql.append(("COMMIT", ()))

    def rollback(self):
        self.sql.append(("ROLLBACK", ()))


def test_question_marks_become_percent_s_for_postgres():
    raw = Recorder()
    Database(raw, "postgres").execute("SELECT * FROM t WHERE a = ? AND b = ?", (1, 2))
    assert raw.sql == [("SELECT * FROM t WHERE a = %s AND b = %s", (1, 2))]


def test_a_literal_percent_is_escaped_for_postgres():
    raw = Recorder()
    Database(raw, "postgres").execute("SELECT * FROM t WHERE name LIKE 'a%' AND id = ?", (1,))
    assert raw.sql[0][0] == "SELECT * FROM t WHERE name LIKE 'a%%' AND id = %s"


def test_sqlite_sql_is_left_alone():
    raw = Recorder()
    Database(raw, "sqlite").execute("SELECT ? WHERE 1 LIKE '%'", (1,))
    assert raw.sql[0][0] == "SELECT ? WHERE 1 LIKE '%'"


def test_a_postgres_transaction_commits_and_rolls_back_through_the_driver():
    raw = Recorder()
    db = Database(raw, "postgres")
    with db.transaction():
        db.execute("SELECT 1")
    assert raw.sql[-1][0] == "COMMIT"
    with pytest.raises(RuntimeError):
        with db.transaction(write=True):
            raise RuntimeError("boom")
    assert raw.sql[-1][0] == "ROLLBACK"


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        Database(Recorder(), "mysql")


# ---- connecting ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["mysql://x", "http://x", "file.db", "sqlite:file.db"])
def test_an_unsupported_url_is_refused_without_showing_it(url):
    with pytest.raises(MigrationError) as caught:
        connect(url)
    assert url not in str(caught.value) or url == "file.db"


def test_a_remote_postgres_url_without_tls_is_refused_and_never_shown():
    url = "postgresql://someone:not-real@db.example.test/app"
    with pytest.raises(MigrationError) as caught:
        connect(url)
    assert "sslmode" in str(caught.value)
    assert "someone" not in str(caught.value) and "db.example.test" not in str(caught.value)


def test_a_postgres_that_cannot_be_reached_is_database_unavailable_with_no_address():
    # Nothing listens on port 1 of this machine: a real connection failure, no network used.
    with pytest.raises(DatabaseUnavailable) as caught:
        connect("postgresql://someone:not-real@127.0.0.1:1/app")
    assert str(caught.value) == "database is temporarily unavailable"
    assert "127.0.0.1" not in str(caught.value) and caught.value.__context__ is None
