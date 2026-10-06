"""Small migration runner (spec sections 12.1 and 17.4).

Numbered SQL files (NNNN_name.sql) are applied in order, one transaction per file.
The runner records each applied file and its checksum in the schema_migrations
table, refuses to run if an applied file was changed or removed, and never
prints a connection URL.

Databases: a plain standard Postgres URL through psycopg 3 (TLS required for any
remote host), and SQLite for local tests. There is no provider-specific code. The
runner uses only features that survive a connection pooler (one transaction per
file, no session variables, no advisory locks, no prepared statements), but
migrations should still use the provider's direct, non-pooled connection URL.

Limits: every file runs inside a transaction, so statements that cannot run in a
transaction (for example CREATE INDEX CONCURRENTLY) are not supported.

Run: uv run python -m app.db.migrate [--dry-run]
The URL is read from the DATABASE_URL environment variable (see --url-env).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.isolation import is_local_host, url_hosts

FILE_PATTERN = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
TABLE = "schema_migrations"
TLS_MODES = ("require", "verify-ca", "verify-full")
CREATE_TABLE = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "version INTEGER PRIMARY KEY, "
    "filename TEXT NOT NULL, "
    "checksum TEXT NOT NULL, "
    "applied_at TEXT NOT NULL)"
)
INSERT_ROW = (
    f"INSERT INTO {TABLE} (version, filename, checksum, applied_at) VALUES ({{}}, {{}}, {{}}, {{}})"
)


class MigrationError(Exception):
    """A problem the owner can fix. The message never contains a connection URL."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    sql: str
    checksum: str


def checksum_of(text: str) -> str:
    """SHA-256 of the file text with line endings normalised (Windows or Linux)."""
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations(directory: Path | str) -> list[Migration]:
    folder = Path(directory)
    if not folder.is_dir():
        raise MigrationError(f"migrations folder not found: {folder}")
    found: list[Migration] = []
    for path in sorted(folder.glob("*.sql")):
        match = FILE_PATTERN.match(path.name)
        if not match:
            raise MigrationError(
                f"bad migration file name '{path.name}' "
                "(use NNNN_name.sql: four digits, then lower-case letters, digits, underscores)"
            )
        text = path.read_bytes().decode("utf-8-sig")
        found.append(Migration(int(match.group(1)), path.name, text, checksum_of(text)))
    found.sort(key=lambda migration: migration.version)
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                "migration numbers must run 0001, 0002, ... with none missing or used twice "
                f"(problem at '{migration.filename}')"
            )
    return found


def pending_migrations(
    migrations: list[Migration], applied: dict[int, tuple[str, str]]
) -> list[Migration]:
    by_version = {migration.version: migration for migration in migrations}
    for version, (filename, checksum) in sorted(applied.items()):
        current = by_version.get(version)
        if current is None:
            raise MigrationError(
                f"migration {version:04d} ('{filename}') was applied before, "
                "but its file is missing"
            )
        if current.filename != filename:
            raise MigrationError(f"migration {version:04d} was applied as '{filename}' and renamed")
        if current.checksum != checksum:
            raise MigrationError(
                f"migration '{filename}' was changed after it was applied; "
                "add a new numbered file instead of editing an applied one"
            )
    if sorted(applied) != list(range(1, len(applied) + 1)):
        raise MigrationError("applied migrations are not a continuous run starting at 0001")
    return [migration for migration in migrations if migration.version not in applied]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sqlite_statements(sql: str) -> list[str]:
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    leftover = [
        line for line in buffer.splitlines() if line.strip() and not line.strip().startswith("--")
    ]
    if leftover:
        raise MigrationError("a statement does not end with ';'")
    return statements


class _Sqlite:
    def __init__(self, url: str) -> None:
        if not url.lower().startswith("sqlite:///"):
            raise MigrationError("a SQLite URL looks like sqlite:///path/to/file.db")
        # isolation_level=None: this class sends BEGIN and COMMIT itself.
        self.conn = sqlite3.connect(url[len("sqlite:///") :], isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys = ON")  # CLAUDE.md: every SQLite connection

    def read_applied(self) -> dict[int, tuple[str, str]]:
        table = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE,)
        ).fetchone()
        if not table:
            return {}
        rows = self.conn.execute(f"SELECT version, filename, checksum FROM {TABLE}").fetchall()
        return {version: (filename, checksum) for version, filename, checksum in rows}

    def apply(self, migration: Migration) -> bool:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(CREATE_TABLE)
            if conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE version = ?", (migration.version,)
            ).fetchone():
                conn.execute("ROLLBACK")
                return False
            for statement in _sqlite_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                INSERT_ROW.format("?", "?", "?", "?"),
                (migration.version, migration.filename, migration.checksum, _now()),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.conn.close()


class _Postgres:
    def __init__(self, url: str) -> None:
        import psycopg

        try:
            # prepare_threshold=None: no server-side prepared statements, so a
            # connection pooler cannot break the runner.
            self.conn = psycopg.connect(url, autocommit=False, prepare_threshold=None)
        except psycopg.OperationalError:
            raise MigrationError(
                "could not connect to the database (details are hidden so that no "
                "connection data is printed)"
            ) from None

    def read_applied(self) -> dict[int, tuple[str, str]]:
        exists = self.conn.execute("SELECT to_regclass(%s) IS NOT NULL", (TABLE,)).fetchone()[0]
        rows = []
        if exists:
            rows = self.conn.execute(f"SELECT version, filename, checksum FROM {TABLE}").fetchall()
        self.conn.rollback()
        return {version: (filename, checksum) for version, filename, checksum in rows}

    def apply(self, migration: Migration) -> bool:
        conn = self.conn
        try:
            conn.execute(CREATE_TABLE)
            # Held until COMMIT, so two runners cannot apply the same file.
            conn.execute(f"LOCK TABLE {TABLE} IN SHARE ROW EXCLUSIVE MODE")
            if conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE version = %s", (migration.version,)
            ).fetchone():
                conn.rollback()
                return False
            conn.execute(migration.sql)  # several statements, no parameters
            conn.execute(
                INSERT_ROW.format("%s", "%s", "%s", "%s"),
                (migration.version, migration.filename, migration.checksum, _now()),
            )
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise

    def close(self) -> None:
        self.conn.close()


def require_tls(url: str) -> None:
    """Remote databases must be reached over TLS; this machine needs no TLS."""
    try:
        hosts = url_hosts(url)
        mode = (parse_qs(urlsplit(url).query).get("sslmode") or [""])[0].lower()
    except ValueError:
        raise MigrationError("the database URL is malformed") from None
    if not hosts:
        raise MigrationError("the database URL names no host; use a full postgresql:// URL")
    if all(host.startswith("/") or is_local_host(host) for host in hosts):
        return
    if mode not in TLS_MODES:
        raise MigrationError(
            "remote databases need TLS: add sslmode=require (or verify-full) to the database URL"
        )


def _open(url: str) -> _Sqlite | _Postgres:
    scheme = url.split(":", 1)[0].lower()
    if scheme == "sqlite":
        return _Sqlite(url)
    if scheme in ("postgres", "postgresql"):
        require_tls(url)
        return _Postgres(url)
    raise MigrationError("unsupported database URL (use sqlite:///... or postgresql://...)")


def run(
    url: str,
    directory: Path | str = "migrations",
    *,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Apply pending migrations. Returns the file names applied (or pending, if dry_run)."""
    migrations = load_migrations(directory)
    database = _open(url)
    try:
        pending = pending_migrations(migrations, database.read_applied())
        if dry_run:
            for migration in pending:
                log(f"pending {migration.filename}")
            return [migration.filename for migration in pending]
        done: list[str] = []
        for migration in pending:
            if database.apply(migration):
                log(f"applied {migration.filename}")
                done.append(migration.filename)
        return done
    finally:
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.db.migrate", description="Apply numbered SQL migrations in order."
    )
    parser.add_argument("--dir", default="migrations", help="folder with NNNN_name.sql files")
    parser.add_argument(
        "--url-env",
        default="DATABASE_URL",
        help="name of the environment variable that holds the database URL "
        "(the URL itself is never taken from the command line)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="list the pending migrations and change nothing"
    )
    args = parser.parse_args(argv)

    url = os.environ.get(args.url_env, "").strip()
    if not url:
        print(f"error: the environment variable {args.url_env} is not set", file=sys.stderr)
        return 2
    try:
        done = run(url, args.dir, dry_run=args.dry_run)
    except MigrationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # a failing SQL statement: show its type and first line only
        first_line = (str(error).splitlines() or [""])[0]
        print(f"error: migration failed ({type(error).__name__}): {first_line}", file=sys.stderr)
        return 1
    if not done:
        print("nothing to apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
