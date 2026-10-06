"""One small database handle for Postgres and SQLite (spec 2, 12.1).

SQL is written once with `?` placeholders and runs on both. SQLite (unit tests) is opened with
foreign keys on (CLAUDE.md invariant). Postgres needs TLS for any remote host and uses the plain
standard connection URL, one connection per request (no pool). Times are written by the
application as UTC ISO strings, which both databases accept for a TIMESTAMPTZ column.

Connection failures are mapped by app.store_errors: DatabaseUnavailable, or StoreRejected for a
refused credential or a wrong setting. No URL or password is ever shown.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.db.migrate import MigrationError, require_tls
from app.store_errors import call_database

SQLITE_BUSY_SECONDS = 10.0
POSTGRES_CONNECT_SECONDS = 10


class Database:
    """A connection plus the few things the app needs: query, transaction, close."""

    def __init__(self, connection: Any, kind: str) -> None:
        if kind not in ("sqlite", "postgres"):
            raise ValueError("kind must be sqlite or postgres")
        self.raw = connection
        self.kind = kind

    def _sql(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.raw.execute(self._sql(sql), params)

    def query_one(self, sql: str, params: tuple = ()) -> tuple | None:
        row = self.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None

    def query_all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return [tuple(row) for row in self.execute(sql, params).fetchall()]

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Database]:
        """Commit on success, roll back on any error.

        write=True takes the write lock at once on SQLite (BEGIN IMMEDIATE), so two writers that
        must not interleave are serialised. On Postgres the caller takes a table lock where it
        needs one (see app.accounts.users.set_role).
        """
        if self.kind == "sqlite":
            self.raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self
        except BaseException:
            self._end("rollback")
            raise
        else:
            self._end("commit")

    def _end(self, how: str) -> None:
        if self.kind == "postgres":
            getattr(self.raw, how)()
        elif self.raw.in_transaction:  # SQLite may already have ended it after a severe error
            self.raw.execute(how.upper())

    def close(self) -> None:
        self.raw.close()


def connect(url: str) -> Database:
    """Open a database from a sqlite:///path or postgresql:// URL."""
    scheme = url.split(":", 1)[0].lower()
    if scheme == "sqlite":
        if not url.lower().startswith("sqlite:///"):
            raise MigrationError("a SQLite URL looks like sqlite:///path/to/file.db")
        # isolation_level=None: Database.transaction sends BEGIN and COMMIT itself. A connection
        # may be used from the worker thread that runs the route, so no thread check.
        connection = sqlite3.connect(
            url[len("sqlite:///") :],
            isolation_level=None,
            check_same_thread=False,
            timeout=SQLITE_BUSY_SECONDS,
        )
        connection.execute("PRAGMA foreign_keys = ON")  # CLAUDE.md: every SQLite connection
        return Database(connection, "sqlite")
    if scheme in ("postgres", "postgresql"):
        import psycopg

        require_tls(url)

        def open_connection() -> Any:
            # prepare_threshold=None: no server-side prepared statements, so a connection
            # pooler such as Neon's cannot break the app.
            connection = psycopg.connect(
                url,
                autocommit=False,
                prepare_threshold=None,
                connect_timeout=POSTGRES_CONNECT_SECONDS,
            )
            # Set explicitly, whatever the host's default is: the table lock in set_role and
            # set_active only protects the last-admin check at READ COMMITTED, where each
            # statement after the lock sees the rows committed before it.
            connection.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
            return connection

        return Database(call_database(open_connection), "postgres")
    raise MigrationError("unsupported database URL (use sqlite:///... or postgresql://...)")
