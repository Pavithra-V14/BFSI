"""The Postgres schema (spec 12.1), applied to SQLite for unit tests.

The same SQL files run on Postgres in CI (tests/test_postgres_schema.py). Foreign keys are
switched on for every connection here, because SQLite ignores them unless asked.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from app.db.migrate import load_migrations, run

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

TABLES = {
    "users": {"id", "username", "email", "password_hash", "role", "created_at"},
    "revoked_tokens": {"jti", "user_id", "revoked_at", "expires_at"},
    "sessions": {
        "id",
        "user_id",
        "title",
        "created_at",
        "summary_text",
        "summary_upto_message_id",
        "session_facts",
        "last_active_at",
        "status",
    },
    "messages": {
        "id",
        "session_id",
        "seq",
        "role",
        "content",
        "citations",
        "grounded",
        "source_removed",
        "created_at",
    },
    "audit_log": {"id", "actor", "action", "target", "details", "created_at"},
    # Exactly the columns spec 12.1 lists.
    "allowlist_entities": {
        "id",
        "doc_id",
        "text",
        "entity_type",
        "status",
        "approved_by",
        "approved_at",
        "access_role",
    },
    "feedback_entries": {"id", "message_id", "user_id", "rating", "created_at"},
    "review_queue": {
        "id",
        "kind",
        "status",
        "session_id",
        "message_id",
        "doc_id",
        "details",
        "label",
        "labelled_by",
        "labelled_at",
        "created_at",
    },
}


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "schema.db"
    run(f"sqlite:///{path}", MIGRATIONS, log=lambda line: None)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_user(conn, name="asha", role=None):
    uid = new_id()
    if role is None:
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, 'hash')",
            (uid, name, f"{name}@example.test"),
        )
    else:
        conn.execute(
            "INSERT INTO users (id, username, email, password_hash, role) "
            "VALUES (?, ?, ?, 'hash', ?)",
            (uid, name, f"{name}@example.test", role),
        )
    return uid


def add_session(conn, user_id, **extra):
    sid = new_id()
    conn.execute("INSERT INTO sessions (id, user_id) VALUES (?, ?)", (sid, user_id))
    for column, value in extra.items():
        conn.execute(f"UPDATE sessions SET {column} = ? WHERE id = ?", (value, sid))
    return sid


def add_message(conn, session_id, seq, role="user", mid=None):
    mid = mid or new_id()
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) VALUES (?, ?, ?, ?, 'masked')",
        (mid, session_id, seq, role),
    )
    return mid


def count(conn, table, where="1 = 1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


# ---- the migrations themselves --------------------------------------------------------


def test_the_three_migrations_apply_cleanly_in_order_and_are_recorded(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    quiet = lambda line: None  # noqa: E731
    applied = run(url, MIGRATIONS, log=quiet)
    assert applied == [
        "0001_accounts.sql",
        "0002_sessions_messages.sql",
        "0003_audit_allowlist_feedback_review.sql",
    ]
    assert run(url, MIGRATIONS, log=quiet) == []  # nothing left to apply
    assert [m.version for m in load_migrations(MIGRATIONS)] == [1, 2, 3]


def test_the_sql_stays_portable_between_postgres_and_sqlite():
    forbidden = ("SERIAL", "JSONB", "IDENTITY", "NOW()", "UUID PRIMARY KEY", "TRIGGER", "ALTER ")
    for migration in load_migrations(MIGRATIONS):
        # Comments may talk about triggers; only the statements count.
        statements = "\n".join(line.split("--", 1)[0] for line in migration.sql.splitlines())
        text = statements.upper()
        for word in forbidden:
            assert word not in text, f"{migration.filename} uses {word}"


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_table_has_exactly_the_planned_columns(db, table):
    assert columns(db, table) == TABLES[table]


def test_foreign_keys_are_really_enforced_in_these_tests(db):
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, "no-such-session", 1)


# ---- users ----------------------------------------------------------------------------


def test_a_new_account_is_a_customer_unless_a_role_is_given(db):
    uid = add_user(db)
    assert db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone() == ("customer",)


@pytest.mark.parametrize("role", ["customer", "staff", "admin"])
def test_the_three_roles_are_accepted(db, role):
    add_user(db, f"user_{role}", role)


@pytest.mark.parametrize("role", ["superuser", "Admin", "", "guest"])
def test_any_other_role_is_rejected(db, role):
    with pytest.raises(sqlite3.IntegrityError):
        add_user(db, "x", role)


def test_username_and_email_are_unique_and_stored_lower_case(db):
    add_user(db, "asha")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(db, "asha")  # same username
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, 'other', "
            "'asha@example.test', 'h')",
            (new_id(),),
        )  # same email
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, 'Mixed', "
            "'mixed@example.test', 'h')",
            (new_id(),),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, 'mixed', "
            "'Mixed@Example.test', 'h')",
            (new_id(),),
        )


# ---- sessions and messages ---------------------------------------------------------


def test_session_defaults_and_status_values(db):
    sid = add_session(db, add_user(db))
    row = db.execute(
        "SELECT status, session_facts, summary_text, summary_upto_message_id, last_active_at "
        "FROM sessions WHERE id = ?",
        (sid,),
    ).fetchone()
    assert row[:4] == ("active", "{}", None, None) and row[4]
    db.execute("UPDATE sessions SET status = 'closed' WHERE id = ?", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE sessions SET status = 'archived' WHERE id = ?", (sid,))


def test_message_defaults_and_flags(db):
    sid = add_session(db, add_user(db))
    mid = add_message(db, sid, 1, "assistant")
    row = db.execute(
        "SELECT grounded, source_removed, citations FROM messages WHERE id = ?", (mid,)
    ).fetchone()
    assert row == (None, 0, "[]")  # grounded unknown, not removed, no citations
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, sid, 2, "system")


def test_seq_is_required_and_unique_inside_a_session(db):
    user = add_user(db)
    first, second = add_session(db, user), add_session(db, user)
    add_message(db, first, 1)
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, first, 1)  # the same position twice in one session
    add_message(db, second, 1)  # the same number in another session is fine
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO messages (id, session_id, role, content) VALUES (?, ?, 'user', 'x')",
            (new_id(), first),
        )  # no seq at all


def test_order_follows_seq_not_the_ids(db):
    sid = add_session(db, add_user(db))
    # The ids sort in the opposite order to the real order, and the rows go in out of order.
    ids = {seq: f"id-{9 - seq}" for seq in (1, 2, 3, 4)}
    for seq in (3, 1, 4, 2):
        add_message(db, sid, seq, mid=ids[seq])
    ordered = [
        row[0]
        for row in db.execute("SELECT id FROM messages WHERE session_id = ? ORDER BY seq", (sid,))
    ]
    assert ordered == [ids[1], ids[2], ids[3], ids[4]]
    assert ordered != sorted(ordered)  # ordering by id would have given a different answer


def test_the_summary_points_at_a_message_id_and_the_buffer_is_the_messages_after_it(db):
    sid = add_session(db, add_user(db))
    ids = [add_message(db, sid, seq, mid=f"zz-{10 - seq}") for seq in range(1, 6)]
    db.execute("UPDATE sessions SET summary_upto_message_id = ? WHERE id = ?", (ids[2], sid))
    after = [
        row[0]
        for row in db.execute(
            "SELECT m.id FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE s.id = ? AND m.seq > "
            "(SELECT seq FROM messages WHERE id = s.summary_upto_message_id) "
            "ORDER BY m.seq",
            (sid,),
        )
    ]
    assert after == ids[3:]  # messages 4 and 5, found through seq


# ---- cascades really fire ------------------------------------------------------------


def build_chat(db):
    user = add_user(db)
    other = add_user(db, "ravi")
    sid = add_session(db, user)
    mid = add_message(db, sid, 1, "assistant")
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'down')",
        (new_id(), mid, other),
    )
    db.execute(
        "INSERT INTO review_queue (id, kind, session_id, message_id) VALUES (?, 'refused', ?, ?)",
        (new_id(), sid, mid),
    )
    db.execute(
        "INSERT INTO audit_log (id, actor, action, target) VALUES (?, ?, 'login', ?)",
        (new_id(), user, user),
    )
    db.execute(
        "INSERT INTO revoked_tokens (jti, user_id, expires_at) VALUES (?, ?, '2030-01-01')",
        (new_id(), user),
    )
    return user, other, sid, mid


def test_deleting_a_session_removes_its_messages_and_their_feedback(db):
    user, other, sid, mid = build_chat(db)
    db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    assert count(db, "messages") == 0
    assert count(db, "feedback_entries") == 0  # feedback on a deleted message goes with it
    # The review item stays, with its links cleared, and the audit log is untouched.
    row = db.execute("SELECT session_id, message_id FROM review_queue").fetchone()
    assert row == (None, None)
    assert count(db, "audit_log") == 1


def test_deleting_a_user_removes_their_sessions_messages_and_revoked_tokens(db):
    user, other, sid, mid = build_chat(db)
    db.execute("DELETE FROM users WHERE id = ?", (user,))
    assert count(db, "sessions") == 0 and count(db, "messages") == 0
    assert count(db, "revoked_tokens") == 0
    assert count(db, "audit_log") == 1  # the audit log has no foreign key: history stays


def test_deleting_the_approver_keeps_the_allow_list_entry(db):
    admin = add_user(db, "boss", "admin")
    entry = new_id()
    db.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, approved_by, "
        "access_role) VALUES (?, 'doc-1', 'IRDAI', 'regulator', 'approved', ?, 'customer')",
        (entry, admin),
    )
    db.execute("DELETE FROM users WHERE id = ?", (admin,))
    assert db.execute("SELECT approved_by FROM allowlist_entities").fetchone() == (None,)


def test_deleting_a_reviewer_keeps_the_review_item(db):
    reviewer = add_user(db, "boss", "admin")
    db.execute(
        "INSERT INTO review_queue (id, kind, labelled_by) VALUES (?, 'thumbs_down', ?)",
        (new_id(), reviewer),
    )
    db.execute("DELETE FROM users WHERE id = ?", (reviewer,))
    assert db.execute("SELECT labelled_by FROM review_queue").fetchone() == (None,)


# ---- the other tables ------------------------------------------------------------------


def test_allow_list_defaults_and_status_values(db):
    db.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, access_role) "
        "VALUES ('a', 'doc-1', 'Mr. Sharma', 'person', 'staff')"
    )
    assert db.execute("SELECT status FROM allowlist_entities").fetchone() == ("pending",)
    for status in ("approved", "rejected"):
        db.execute("UPDATE allowlist_entities SET status = ?", (status,))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE allowlist_entities SET status = 'maybe'")


def test_feedback_is_thumbs_up_or_down(db):
    user, other, sid, mid = build_chat(db)
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'up')",
        (new_id(), mid, user),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO feedback_entries (id, message_id, user_id, rating) "
            "VALUES (?, ?, ?, 'meh')",
            (new_id(), mid, user),
        )


@pytest.mark.parametrize(
    "kind", ["refused", "low_confidence", "thumbs_down", "two_unclear_turns", "failed_ingest_page"]
)
def test_the_five_review_kinds_are_accepted(db, kind):
    db.execute("INSERT INTO review_queue (id, kind) VALUES (?, ?)", (new_id(), kind))
    assert db.execute("SELECT status FROM review_queue").fetchone() == ("open",)


def test_other_review_kinds_are_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO review_queue (id, kind) VALUES (?, 'spam')", (new_id(),))


@pytest.mark.parametrize("status", ["open", "labelled", "added_to_eval", "dismissed"])
def test_the_four_review_statuses_are_accepted(db, status):
    db.execute(
        "INSERT INTO review_queue (id, kind, status) VALUES (?, 'refused', ?)", (new_id(), status)
    )


def test_other_review_statuses_are_rejected(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO review_queue (id, kind, status) VALUES (?, 'refused', 'closed')",
            (new_id(),),
        )


def test_the_audit_log_takes_inserts_and_the_expected_indexes_exist(db):
    db.execute(
        "INSERT INTO audit_log (id, actor, action) VALUES (?, 'system', 'retention')", (new_id(),)
    )
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {
        "audit_log_created_at",
        "sessions_user_last_active",
        "sessions_last_active",
        "revoked_tokens_expires_at",
        "allowlist_entities_doc_id",
        "review_queue_status_created",
        "feedback_entries_message_id",
    } <= names
