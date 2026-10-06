"""The same migration files on a real Postgres. Runs only in CI, on the throw-away service
container (TEST_POSTGRES_URL); skipped everywhere else, so it never runs on the admin machine.

These tests were written without being run against Postgres (none is installed on this machine).
The first CI run is their first run.
"""

import os
import uuid
from pathlib import Path

import pytest

from app.db.migrate import run

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]


@pytest.fixture
def conn():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    url = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(url, MIGRATIONS, log=lambda line: None)
    with psycopg.connect(url, autocommit=True) as connection:
        yield connection
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def new_id() -> str:
    return str(uuid.uuid4())


def test_the_tables_exist_and_a_chat_round_trips(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )
    }
    assert {
        "users",
        "revoked_tokens",
        "sessions",
        "messages",
        "audit_log",
        "allowlist_entities",
        "feedback_entries",
        "review_queue",
    } <= tables
    user, sid = new_id(), new_id()
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (%s, 'asha', "
        "'asha@example.test', 'hash')",
        (user,),
    )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s)", (sid, user))
    for seq in (2, 1):
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, content) "
            "VALUES (%s, %s, %s, 'user', 'masked')",
            (new_id(), sid, seq),
        )
    rows = conn.execute("SELECT seq FROM messages WHERE session_id = %s ORDER BY seq", (sid,))
    assert [row[0] for row in rows] == [1, 2]


def test_checks_uniqueness_and_cascades_hold_on_postgres(conn):
    import psycopg

    user, sid = new_id(), new_id()
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (%s, 'asha', "
        "'asha@example.test', 'hash')",
        (user,),
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) "
            "VALUES (%s, 'x', 'x@example.test', 'h', 'superuser')",
            (new_id(),),
        )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s)", (sid, user))
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) "
        "VALUES (%s, %s, 1, 'user', 'masked')",
        (new_id(), sid),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, content) "
            "VALUES (%s, %s, 1, 'user', 'again')",
            (new_id(), sid),
        )
    conn.execute(
        "INSERT INTO audit_log (id, actor, action) VALUES (%s, %s, 'login')", (new_id(), user)
    )
    conn.execute("DELETE FROM users WHERE id = %s", (user,))
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1


def test_deleting_a_chat_clears_review_details_on_postgres_and_keeps_the_row(conn):
    from app.db.chats import delete_session

    user, sid, mid, rid = new_id(), new_id(), new_id(), new_id()
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (%s, 'asha', "
        "'asha@example.test', 'hash')",
        (user,),
    )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s)", (sid, user))
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) "
        "VALUES (%s, %s, 1, 'assistant', 'masked')",
        (mid, sid),
    )
    conn.execute(
        "INSERT INTO review_queue (id, kind, message_id, details) "
        "VALUES (%s, 'low_confidence', %s, '{\"snippet\": \"masked\"}')",
        (rid, mid),
    )
    assert delete_session(conn, sid) is True
    row = conn.execute(
        "SELECT details, message_id, kind FROM review_queue WHERE id = %s", (rid,)
    ).fetchone()
    assert row == ("{}", None, "low_confidence")
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_a_text_primary_key_cannot_be_null_on_postgres_either(conn):
    import psycopg

    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash) "
            "VALUES (NULL, 'x', 'x@example.test', 'h')"
        )
