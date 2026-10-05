"""Migration runner. SQLite runs everywhere; Postgres tests run only in CI."""

import os
import sqlite3
import uuid

import pytest

from app.db import migrate
from app.db.migrate import MigrationError, checksum_of, run

CREATE_A = "CREATE TABLE a (id INTEGER PRIMARY KEY);\n"
CREATE_B = "CREATE TABLE b (id INTEGER PRIMARY KEY);\nINSERT INTO b (id) VALUES (1);\n"


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "migrations"
    path.mkdir()
    return path


@pytest.fixture
def url(tmp_path):
    return "sqlite:///" + (tmp_path / "test.db").as_posix()


def write(folder, name, sql):
    (folder / name).write_bytes(sql.encode("utf-8"))


def tables(url):
    conn = sqlite3.connect(url[len("sqlite:///") :])
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {name for (name,) in rows}
    finally:
        conn.close()


def recorded(url):
    conn = sqlite3.connect(url[len("sqlite:///") :])
    try:
        return conn.execute(
            "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()


def quiet(_message):
    pass


def test_applies_in_order_and_records_checksums(folder, url):
    write(folder, "0002_b.sql", CREATE_B)
    write(folder, "0001_a.sql", CREATE_A)
    assert run(url, folder, log=quiet) == ["0001_a.sql", "0002_b.sql"]
    assert {"a", "b", "schema_migrations"} <= tables(url)
    assert recorded(url) == [
        (1, "0001_a.sql", checksum_of(CREATE_A)),
        (2, "0002_b.sql", checksum_of(CREATE_B)),
    ]


def test_second_run_applies_nothing(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    run(url, folder, log=quiet)
    assert run(url, folder, log=quiet) == []


def test_only_new_files_are_applied_later(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    run(url, folder, log=quiet)
    write(folder, "0002_b.sql", CREATE_B)
    assert run(url, folder, log=quiet) == ["0002_b.sql"]


def test_changed_applied_file_is_refused(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    run(url, folder, log=quiet)
    write(folder, "0001_a.sql", CREATE_A + "CREATE TABLE sneaky (id INTEGER);\n")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        run(url, folder, log=quiet)


def test_line_ending_change_is_not_a_change(folder, url):
    write(folder, "0001_a.sql", "CREATE TABLE a (\n  id INTEGER PRIMARY KEY\n);\n")
    run(url, folder, log=quiet)
    write(folder, "0001_a.sql", "CREATE TABLE a (\r\n  id INTEGER PRIMARY KEY\r\n);\r\n")
    assert run(url, folder, log=quiet) == []


def test_missing_applied_file_is_refused(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    write(folder, "0002_b.sql", CREATE_B)
    run(url, folder, log=quiet)
    (folder / "0002_b.sql").unlink()
    with pytest.raises(MigrationError, match="file is missing"):
        run(url, folder, log=quiet)


def test_gap_in_numbers_is_refused(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    write(folder, "0003_c.sql", CREATE_B)
    with pytest.raises(MigrationError, match="none missing or used twice"):
        run(url, folder, log=quiet)


def test_duplicate_number_is_refused(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    write(folder, "0001_b.sql", CREATE_B)
    with pytest.raises(MigrationError, match="none missing or used twice"):
        run(url, folder, log=quiet)


def test_bad_file_name_is_refused(folder, url):
    write(folder, "1_a.sql", CREATE_A)
    with pytest.raises(MigrationError, match="bad migration file name"):
        run(url, folder, log=quiet)


def test_other_files_in_the_folder_are_ignored(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    write(folder, "README.md", "notes")
    write(folder, ".gitkeep", "")
    assert run(url, folder, log=quiet) == ["0001_a.sql"]


def test_empty_folder_is_fine(folder, url):
    assert run(url, folder, log=quiet) == []


def test_failing_file_rolls_back_and_keeps_earlier_ones(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    write(
        folder,
        "0002_bad.sql",
        "CREATE TABLE c (id INTEGER);\nINSERT INTO missing (id) VALUES (1);\n",
    )
    with pytest.raises(sqlite3.OperationalError):
        run(url, folder, log=quiet)
    assert "c" not in tables(url)
    assert [row[1] for row in recorded(url)] == ["0001_a.sql"]


def test_statement_without_semicolon_is_refused(folder, url):
    write(folder, "0001_a.sql", "CREATE TABLE a (id INTEGER)\n")
    with pytest.raises(MigrationError, match="does not end with"):
        run(url, folder, log=quiet)
    assert "a" not in tables(url)


def test_dry_run_changes_nothing(folder, url):
    write(folder, "0001_a.sql", CREATE_A)
    messages = []
    assert run(url, folder, dry_run=True, log=messages.append) == ["0001_a.sql"]
    assert messages == ["pending 0001_a.sql"]
    assert "schema_migrations" not in tables(url)


def test_remote_database_without_tls_is_refused_before_connecting(folder):
    write(folder, "0001_a.sql", CREATE_A)
    host = "ep-quiet-sky-123456.us-east-2.aws.neon.tech"
    for bad in (
        f"postgresql://user:pw@{host}/db",
        f"postgresql://user:pw@{host}/db?sslmode=prefer",
    ):
        with pytest.raises(MigrationError, match="TLS") as caught:
            run(bad, folder, log=quiet)
        assert host not in str(caught.value)


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_tls_modes_are_accepted_for_remote_hosts(mode):
    migrate.require_tls(f"postgresql://user:pw@db.remote.test/db?sslmode={mode}")


def test_local_postgres_needs_no_tls():
    migrate.require_tls("postgresql://user:pw@localhost:5432/db")


def test_unsupported_url_is_refused_without_echoing_it(folder):
    with pytest.raises(MigrationError, match="unsupported database URL") as caught:
        run("mysql://user:pw@db.remote.test/db", folder, log=quiet)
    assert "db.remote.test" not in str(caught.value)


def test_cli_applies_and_reports(folder, url, monkeypatch, capsys):
    write(folder, "0001_a.sql", CREATE_A)
    monkeypatch.setenv("TEST_MIGRATE_URL", url)
    assert migrate.main(["--dir", str(folder), "--url-env", "TEST_MIGRATE_URL"]) == 0
    assert "applied 0001_a.sql" in capsys.readouterr().out
    assert migrate.main(["--dir", str(folder), "--url-env", "TEST_MIGRATE_URL"]) == 0
    assert "nothing to apply" in capsys.readouterr().out


def test_cli_without_url_variable_exits_2(folder, monkeypatch, capsys):
    monkeypatch.delenv("TEST_MIGRATE_URL", raising=False)
    assert migrate.main(["--dir", str(folder), "--url-env", "TEST_MIGRATE_URL"]) == 2
    assert "TEST_MIGRATE_URL" in capsys.readouterr().err


def test_cli_errors_do_not_print_the_url(folder, monkeypatch, capsys):
    write(folder, "0001_a.sql", CREATE_A)
    monkeypatch.setenv("TEST_MIGRATE_URL", "postgresql://user:pw@db.remote.test/db")
    assert migrate.main(["--dir", str(folder), "--url-env", "TEST_MIGRATE_URL"]) == 1
    captured = capsys.readouterr()
    assert "db.remote.test" not in captured.out + captured.err


# --- Postgres: runs only where TEST_POSTGRES_URL is set (the CI service container) ---

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
needs_postgres = pytest.mark.skipif(
    not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
)


@pytest.fixture
def pg_url():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    yield f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.mark.postgres
@needs_postgres
def test_postgres_applies_in_order_and_is_idempotent(folder, pg_url):
    write(folder, "0001_a.sql", CREATE_A)
    write(folder, "0002_b.sql", CREATE_B)
    assert run(pg_url, folder, log=quiet) == ["0001_a.sql", "0002_b.sql"]
    assert run(pg_url, folder, log=quiet) == []


@pytest.mark.postgres
@needs_postgres
def test_postgres_failing_file_rolls_back(folder, pg_url):
    import psycopg

    write(folder, "0001_a.sql", CREATE_A)
    write(
        folder,
        "0002_bad.sql",
        "CREATE TABLE c (id INTEGER);\nINSERT INTO missing (id) VALUES (1);\n",
    )
    with pytest.raises(psycopg.errors.UndefinedTable):
        run(pg_url, folder, log=quiet)
    with psycopg.connect(pg_url, autocommit=True) as conn:
        names = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        c_exists = conn.execute("SELECT to_regclass('c') IS NOT NULL").fetchone()[0]
    assert names == {1}
    assert not c_exists


@pytest.mark.postgres
@needs_postgres
def test_postgres_changed_file_is_refused(folder, pg_url):
    write(folder, "0001_a.sql", CREATE_A)
    run(pg_url, folder, log=quiet)
    write(folder, "0001_a.sql", CREATE_A + "-- edited\n")
    with pytest.raises(MigrationError, match="changed after it was applied"):
        run(pg_url, folder, log=quiet)
