"""The Postgres schema (spec 12.1), applied to SQLite for unit tests.

The same SQL files run on Postgres in CI (tests/test_postgres_schema.py). Foreign keys are
switched on for every connection here, because SQLite ignores them unless asked.
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from app.db.chats import (
    ForeignKeysOff,
    clear_review_details_for_session,
    delete_session,
    ensure_foreign_keys,
)
from app.db.identity import normalise_email, normalise_username
from app.db.migrate import _Sqlite, load_migrations, run

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"

# table -> column -> (declared type, NOT NULL). Every column the planned schema has, nothing else.
SPEC = {
    "users": {
        "id": ("TEXT", True),
        "username": ("TEXT", True),
        "email": ("TEXT", True),
        "password_hash": ("TEXT", True),
        "role": ("TEXT", True),
        "created_at": ("TIMESTAMPTZ", True),
        "is_active": ("BOOLEAN", True),  # migration 0004 [P]
    },
    "revoked_tokens": {
        "jti": ("TEXT", True),
        "user_id": ("TEXT", True),
        "revoked_at": ("TIMESTAMPTZ", True),
        "expires_at": ("TIMESTAMPTZ", True),
    },
    "sessions": {
        "id": ("TEXT", True),
        "user_id": ("TEXT", True),
        "title": ("TEXT", False),
        "created_at": ("TIMESTAMPTZ", True),
        "summary_text": ("TEXT", False),
        "summary_upto_message_id": ("TEXT", False),
        "session_facts": ("TEXT", True),
        "last_active_at": ("TIMESTAMPTZ", True),
        "status": ("TEXT", True),
    },
    "messages": {
        "id": ("TEXT", True),
        "session_id": ("TEXT", True),
        "seq": ("INTEGER", True),
        "role": ("TEXT", True),
        "content": ("TEXT", True),
        "citations": ("TEXT", True),
        "grounded": ("BOOLEAN", False),
        "source_removed": ("BOOLEAN", True),
        "created_at": ("TIMESTAMPTZ", True),
        "history_excluded": ("BOOLEAN", True),  # migration 0005 [P]
        "shadowed": ("BOOLEAN", True),  # migration 0006 [P]
        "fixed_reply": ("BOOLEAN", True),  # migration 0008 [P]
    },
    "alert_events": {  # migration 0009 [P]
        "id": ("TEXT", True),
        "name": ("TEXT", True),
        "severity": ("TEXT", True),
        "event_count": ("INTEGER", True),
        "notified": ("BOOLEAN", True),
        "created_at": ("TIMESTAMPTZ", True),
    },
    "metric_rollups": {  # migration 0009 [P]
        "id": ("TEXT", True),
        "period": ("TEXT", True),
        "period_start": ("TIMESTAMPTZ", True),
        "role": ("TEXT", True),
        "metric": ("TEXT", True),
        "value": ("DOUBLE PRECISION", True),
    },
    "trace_deletions": {  # migration 0009 [P]
        "id": ("TEXT", True),
        "session_hash": ("TEXT", True),
        "attempts": ("INTEGER", True),
        "alerted": ("BOOLEAN", True),
        "created_at": ("TIMESTAMPTZ", True),
        "last_attempt_at": ("TIMESTAMPTZ", False),
    },
    "key_usage_daily": {  # migration 0011 [P]
        "id": ("TEXT", True),
        "day": ("TEXT", True),
        "provider": ("TEXT", True),
        "fingerprint": ("TEXT", True),
        "calls": ("INTEGER", True),
        "tokens": ("INTEGER", True),
    },
    "withdrawn_documents": {  # migration 0010 [P]
        "doc_id": ("TEXT", True),
        "withdrawn_at": ("TIMESTAMPTZ", True),
        "reason_code": ("TEXT", True),
    },
    "app_settings": {  # migration 0006 [P]
        "key": ("TEXT", True),
        "value": ("TEXT", True),
        "updated_by": ("TEXT", False),
        "updated_at": ("TIMESTAMPTZ", True),
    },
    "audit_log": {
        "id": ("TEXT", True),
        "actor": ("TEXT", True),
        "action": ("TEXT", True),
        "target": ("TEXT", False),
        "details": ("TEXT", True),
        "created_at": ("TIMESTAMPTZ", True),
    },
    # Exactly the columns spec 12.1 lists.
    "allowlist_entities": {
        "id": ("TEXT", True),
        "doc_id": ("TEXT", True),
        "text": ("TEXT", True),
        "entity_type": ("TEXT", True),
        "status": ("TEXT", True),
        "approved_by": ("TEXT", False),
        "approved_at": ("TIMESTAMPTZ", False),
        "access_role": ("TEXT", True),
    },
    "feedback_entries": {
        "id": ("TEXT", True),
        "message_id": ("TEXT", True),
        "user_id": ("TEXT", True),
        "rating": ("TEXT", True),
        "created_at": ("TIMESTAMPTZ", True),
    },
    "review_queue": {
        "id": ("TEXT", True),
        "kind": ("TEXT", True),
        "status": ("TEXT", True),
        "session_id": ("TEXT", False),
        "message_id": ("TEXT", False),
        "doc_id": ("TEXT", False),
        "details": ("TEXT", True),
        "label": ("TEXT", False),
        "labelled_by": ("TEXT", False),
        "labelled_at": ("TIMESTAMPTZ", False),
        "created_at": ("TIMESTAMPTZ", True),
    },
}

PRIMARY_KEYS = {
    "users": "id",
    "revoked_tokens": "jti",
    "sessions": "id",
    "messages": "id",
    "audit_log": "id",
    "allowlist_entities": "id",
    "feedback_entries": "id",
    "review_queue": "id",
    "app_settings": "key",
    "alert_events": "id",
    "metric_rollups": "id",
    "trace_deletions": "id",
    "withdrawn_documents": "doc_id",
    "key_usage_daily": "id",
}

# Every default the schema sets. A column not listed here has no default.
DEFAULTS = {
    ("users", "role"): "'customer'",
    ("users", "created_at"): "CURRENT_TIMESTAMP",
    ("users", "is_active"): "TRUE",
    ("revoked_tokens", "revoked_at"): "CURRENT_TIMESTAMP",
    ("sessions", "created_at"): "CURRENT_TIMESTAMP",
    ("sessions", "session_facts"): "'{}'",
    ("sessions", "last_active_at"): "CURRENT_TIMESTAMP",
    ("sessions", "status"): "'active'",
    ("messages", "citations"): "'[]'",
    ("messages", "source_removed"): "FALSE",
    ("messages", "history_excluded"): "FALSE",
    ("messages", "shadowed"): "FALSE",
    ("messages", "fixed_reply"): "FALSE",
    ("alert_events", "event_count"): "1",
    ("alert_events", "notified"): "FALSE",
    ("alert_events", "created_at"): "CURRENT_TIMESTAMP",
    ("metric_rollups", "value"): "0",
    ("trace_deletions", "attempts"): "0",
    ("trace_deletions", "alerted"): "FALSE",
    ("trace_deletions", "created_at"): "CURRENT_TIMESTAMP",
    ("withdrawn_documents", "withdrawn_at"): "CURRENT_TIMESTAMP",
    ("key_usage_daily", "calls"): "0",
    ("key_usage_daily", "tokens"): "0",
    ("app_settings", "updated_at"): "CURRENT_TIMESTAMP",
    ("messages", "created_at"): "CURRENT_TIMESTAMP",
    ("audit_log", "details"): "'{}'",
    ("audit_log", "created_at"): "CURRENT_TIMESTAMP",
    ("allowlist_entities", "status"): "'pending'",
    ("feedback_entries", "created_at"): "CURRENT_TIMESTAMP",
    ("review_queue", "status"): "'open'",
    ("review_queue", "details"): "'{}'",
    ("review_queue", "created_at"): "CURRENT_TIMESTAMP",
}

# (table, column) -> (parent table, what happens when the parent row is deleted)
FOREIGN_KEYS = {
    ("revoked_tokens", "user_id"): ("users", "CASCADE"),
    ("sessions", "user_id"): ("users", "CASCADE"),
    ("messages", "session_id"): ("sessions", "CASCADE"),
    ("allowlist_entities", "approved_by"): ("users", "SET NULL"),
    ("feedback_entries", "message_id"): ("messages", "CASCADE"),
    ("feedback_entries", "user_id"): ("users", "CASCADE"),
    ("review_queue", "session_id"): ("sessions", "SET NULL"),
    ("review_queue", "message_id"): ("messages", "SET NULL"),
    ("review_queue", "labelled_by"): ("users", "SET NULL"),
    ("app_settings", "updated_by"): ("users", "SET NULL"),
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


def test_the_eleven_migrations_apply_cleanly_in_order_and_are_recorded(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    quiet = lambda line: None  # noqa: E731
    applied = run(url, MIGRATIONS, log=quiet)
    assert applied == [
        "0001_accounts.sql",
        "0002_sessions_messages.sql",
        "0003_audit_allowlist_feedback_review.sql",
        "0004_users_active.sql",
        "0005_messages_history_excluded.sql",
        "0006_rollout_feedback_settings.sql",
        "0007_review_thumbs_down_unique.sql",
        "0008_messages_fixed_reply.sql",
        "0009_observability.sql",
        "0010_withdrawn_documents.sql",
        "0011_key_usage_daily.sql",
    ]
    assert run(url, MIGRATIONS, log=quiet) == []  # nothing left to apply
    assert [m.version for m in load_migrations(MIGRATIONS)] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]


def test_the_sql_stays_portable_between_postgres_and_sqlite():
    forbidden = ("SERIAL", "JSONB", "IDENTITY", "NOW()", "UUID PRIMARY KEY", "TRIGGER")
    for migration in load_migrations(MIGRATIONS):
        # Comments may talk about triggers; only the statements count.
        statements = "\n".join(line.split("--", 1)[0] for line in migration.sql.splitlines())
        text = statements.upper()
        for word in forbidden:
            assert word not in text, f"{migration.filename} uses {word}"
        # ALTER is allowed only as ADD COLUMN with a constant default, which both databases accept.
        for statement in (part.strip() for part in text.split(";")):
            if "ALTER " in statement:
                assert "ADD COLUMN" in statement and "DEFAULT" in statement, migration.filename


def test_migration_0004_adds_the_active_flag_and_existing_users_stay_active(tmp_path):
    path = tmp_path / "upgrade.db"
    old = tmp_path / "old"
    old.mkdir()
    for migration in load_migrations(MIGRATIONS)[:3]:
        (old / migration.filename).write_text(migration.sql, encoding="utf-8")
    url = f"sqlite:///{path}"
    run(url, old, log=lambda line: None)  # a database as it was before this phase
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) "
        "VALUES ('u', 'asha', 'a@e.test', 'h')"
    )
    conn.commit()
    conn.close()
    assert run(url, MIGRATIONS, log=lambda line: None) == [
        "0004_users_active.sql",
        "0005_messages_history_excluded.sql",
        "0006_rollout_feedback_settings.sql",
        "0007_review_thumbs_down_unique.sql",
        "0008_messages_fixed_reply.sql",
        "0009_observability.sql",
        "0010_withdrawn_documents.sql",
        "0011_key_usage_daily.sql",
    ]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT is_active FROM users").fetchall() == [(1,)]
    conn.close()


def test_migration_0005_adds_the_history_flag_and_existing_messages_stay_in_history(tmp_path):
    path = tmp_path / "upgrade5.db"
    old = tmp_path / "old5"
    old.mkdir()
    for migration in load_migrations(MIGRATIONS)[:4]:
        (old / migration.filename).write_text(migration.sql, encoding="utf-8")
    url = f"sqlite:///{path}"
    run(url, old, log=lambda line: None)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES ('u', 'a', 'a@e.test', 'h')"
    )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES ('s', 'u')")
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) "
        "VALUES ('m', 's', 1, 'user', 'x')"
    )
    conn.commit()
    conn.close()
    assert run(url, MIGRATIONS, log=lambda line: None) == [
        "0005_messages_history_excluded.sql",
        "0006_rollout_feedback_settings.sql",
        "0007_review_thumbs_down_unique.sql",
        "0008_messages_fixed_reply.sql",
        "0009_observability.sql",
        "0010_withdrawn_documents.sql",
        "0011_key_usage_daily.sql",
    ]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT history_excluded FROM messages").fetchall() == [(0,)]
    conn.close()


def test_migration_0006_adds_the_shadow_flag_the_settings_table_and_one_thumb_per_message(tmp_path):
    path = tmp_path / "upgrade6.db"
    old = tmp_path / "old6"
    old.mkdir()
    for migration in load_migrations(MIGRATIONS)[:5]:
        (old / migration.filename).write_text(migration.sql, encoding="utf-8")
    url = f"sqlite:///{path}"
    run(url, old, log=lambda line: None)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES ('u', 'a', 'a@e.test', 'h')"
    )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES ('s', 'u')")
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) "
        "VALUES ('m', 's', 1, 'assistant', 'x')"
    )
    conn.commit()
    conn.close()
    assert run(url, MIGRATIONS, log=lambda line: None) == [
        "0006_rollout_feedback_settings.sql",
        "0007_review_thumbs_down_unique.sql",
        "0008_messages_fixed_reply.sql",
        "0009_observability.sql",
        "0010_withdrawn_documents.sql",
        "0011_key_usage_daily.sql",
    ]
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT shadowed FROM messages").fetchall() == [(0,)]  # visible as before
    assert conn.execute("SELECT COUNT(*) FROM app_settings").fetchone() == (0,)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES ('f','m','u','up')"
    )
    with pytest.raises(sqlite3.IntegrityError):  # a second thumb of the same user: refused
        conn.execute(
            "INSERT INTO feedback_entries (id, message_id, user_id, rating) "
            "VALUES ('g', 'm', 'u', 'down')"
        )
    conn.close()


def test_migration_0007_removes_duplicate_thumbs_down_items_and_allows_only_one_per_answer(
    tmp_path,
):
    path = tmp_path / "upgrade7.db"
    old = tmp_path / "old7"
    old.mkdir()
    for migration in load_migrations(MIGRATIONS)[:6]:
        (old / migration.filename).write_text(migration.sql, encoding="utf-8")
    url = f"sqlite:///{path}"
    run(url, old, log=lambda line: None)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES ('u', 'a', 'a@e.test', 'h')"
    )
    conn.execute("INSERT INTO sessions (id, user_id) VALUES ('s', 'u')")
    conn.execute(
        "INSERT INTO messages (id, session_id, seq, role, content) "
        "VALUES ('m', 's', 1, 'assistant', 'x')"
    )
    for item in ("r1", "r2"):  # a double click before this migration made two items
        conn.execute(
            "INSERT INTO review_queue (id, kind, message_id) VALUES (?, 'thumbs_down', 'm')",
            (item,),
        )
    conn.execute("INSERT INTO review_queue (id, kind, message_id) VALUES ('x1', 'refused', 'm')")
    conn.execute("INSERT INTO review_queue (id, kind, message_id) VALUES ('x2', 'refused', 'm')")
    conn.commit()
    conn.close()
    assert run(url, MIGRATIONS, log=lambda line: None) == [
        "0007_review_thumbs_down_unique.sql",
        "0008_messages_fixed_reply.sql",
        "0009_observability.sql",
        "0010_withdrawn_documents.sql",
        "0011_key_usage_daily.sql",
    ]
    conn = sqlite3.connect(path)
    kept = conn.execute("SELECT id FROM review_queue WHERE kind = 'thumbs_down'").fetchall()
    assert kept == [("r1",)]  # the first one stays
    assert conn.execute("SELECT COUNT(*) FROM review_queue WHERE kind = 'refused'").fetchone() == (
        2,
    )  # other kinds may repeat
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO review_queue (id, kind, message_id) VALUES ('r3', 'thumbs_down', 'm')"
        )
    conn.close()


@pytest.mark.parametrize("table", sorted(SPEC))
def test_every_column_has_the_planned_type_and_null_rule(db, table):
    found = {
        row[1]: (row[2].upper(), bool(row[3])) for row in db.execute(f"PRAGMA table_info({table})")
    }
    assert found == SPEC[table]  # exactly these columns, with these types and NOT NULL flags


@pytest.mark.parametrize("table", sorted(SPEC))
def test_every_default_is_the_planned_one_and_nothing_else_has_a_default(db, table):
    found = {row[1]: row[4] for row in db.execute(f"PRAGMA table_info({table})")}
    expected = {name: DEFAULTS.get((table, name)) for name in SPEC[table]}
    assert found == expected


@pytest.mark.parametrize("table", sorted(SPEC))
def test_each_table_has_the_planned_single_column_primary_key(db, table):
    keys = [row[1] for row in db.execute(f"PRAGMA table_info({table})") if row[5]]
    assert keys == [PRIMARY_KEYS[table]]


def test_the_foreign_keys_and_their_delete_rules_are_exactly_the_planned_ones(db):
    found = {}
    for table in SPEC:
        for row in db.execute(f"PRAGMA foreign_key_list({table})"):
            found[(table, row[3])] = (row[2], row[6])  # parent table, ON DELETE rule
    assert found == FOREIGN_KEYS
    # No foreign key on purpose: the audit log keeps history, the summary pointer cannot block.
    assert ("audit_log", "actor") not in found
    assert ("sessions", "summary_upto_message_id") not in found


def test_uniqueness_rules_are_exactly_the_planned_ones(db):
    unique = {}
    for table in SPEC:
        for index in db.execute(f"PRAGMA index_list({table})"):
            if index[2] and index[3] == "u":  # unique, created by a UNIQUE constraint
                columns_ = tuple(row[2] for row in db.execute(f"PRAGMA index_info({index[1]})"))
                unique.setdefault(table, set()).add(columns_)
    assert unique == {"users": {("username",), ("email",)}, "messages": {("session_id", "seq")}}


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


def test_username_and_email_are_unique(db):
    add_user(db, "asha")
    with pytest.raises(sqlite3.IntegrityError):
        add_user(db, "asha")  # same username
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, 'other', "
            "'asha@example.test', 'h')",
            (new_id(),),
        )  # same email


def test_the_database_does_not_fold_case_the_application_does(db):
    # No CHECK on case: lower() differs between Postgres and SQLite. The database stores what it
    # is given, so the application must normalise first (app/db/identity.py).
    add_user(db, "asha")
    add_user(db, "Asha")  # accepted as a different value, which is why normalising is required
    assert count(db, "users") == 2


def test_normalising_before_storing_makes_case_and_spacing_variants_collide(db):
    def register(name, email):
        db.execute(
            "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, 'h')",
            (new_id(), normalise_username(name), normalise_email(email)),
        )

    register("  Asha ", "Asha@Example.test")
    with pytest.raises(sqlite3.IntegrityError):
        register("ASHA", "other@example.test")  # the same username after the rule
    with pytest.raises(sqlite3.IntegrityError):
        register("ravi", " ASHA@EXAMPLE.TEST ")  # the same email after the rule
    assert db.execute("SELECT username, email FROM users").fetchall() == [
        ("asha", "asha@example.test")
    ]


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
        "feedback_entries_message_user",
        "review_queue_thumbs_down_message",
    } <= names


# ---- deleting a chat clears the review items' text (spec 8.4, 13.6) --------------------------


def add_review(db, session_id=None, message_id=None, details='{"snippet": "masked text"}', **extra):
    rid = new_id()
    db.execute(
        "INSERT INTO review_queue (id, kind, session_id, message_id, details, label) "
        "VALUES (?, 'low_confidence', ?, ?, ?, ?)",
        (rid, session_id, message_id, details, extra.get("label")),
    )
    return rid


def details_of(db, rid):
    return db.execute("SELECT details FROM review_queue WHERE id = ?", (rid,)).fetchone()[0]


def test_deleting_a_chat_clears_the_details_of_every_linked_review_item_but_keeps_the_rows(db):
    user = add_user(db)
    sid = add_session(db, user)
    mid = add_message(db, sid, 1, "assistant")
    by_session = add_review(db, session_id=sid, label="wrong clause")
    by_message = add_review(db, message_id=mid)
    by_both = add_review(db, session_id=sid, message_id=mid)
    assert delete_session(db, sid) is True
    assert count(db, "sessions") == 0 and count(db, "messages") == 0
    assert count(db, "review_queue") == 3  # the rows stay
    for rid in (by_session, by_message, by_both):
        assert details_of(db, rid) == "{}"  # the text is gone
    row = db.execute(
        "SELECT session_id, message_id, kind, status, label FROM review_queue WHERE id = ?",
        (by_session,),
    ).fetchone()
    assert row == (None, None, "low_confidence", "open", "wrong clause")  # links cleared, rest kept


def test_other_chats_and_unlinked_review_items_keep_their_details(db):
    user = add_user(db)
    gone, kept = add_session(db, user), add_session(db, user)
    gone_message = add_message(db, gone, 1)
    kept_message = add_message(db, kept, 1)
    other_chat = add_review(db, session_id=kept)
    other_message = add_review(db, message_id=kept_message)
    unlinked = add_review(db)  # for example a failed ingest page: no chat at all
    linked = add_review(db, message_id=gone_message)
    delete_session(db, gone)
    assert details_of(db, linked) == "{}"
    for rid in (other_chat, other_message, unlinked):
        assert details_of(db, rid) == '{"snippet": "masked text"}'
    assert count(db, "sessions") == 1 and count(db, "messages") == 1


def test_clearing_reports_how_many_items_it_cleared(db):
    sid = add_session(db, add_user(db))
    add_review(db, session_id=sid)
    add_review(db, session_id=sid)
    add_review(db)
    assert clear_review_details_for_session(db, sid) == 2
    assert clear_review_details_for_session(db, "no-such-session") == 0


def test_deleting_a_session_that_does_not_exist_changes_nothing(db):
    kept = add_review(db)
    assert delete_session(db, "no-such-session") is False
    assert details_of(db, kept) == '{"snippet": "masked text"}'


def test_the_clearing_and_the_delete_roll_back_together(db):
    sid = add_session(db, add_user(db))
    rid = add_review(db, session_id=sid)
    db.commit()
    delete_session(db, sid)
    db.rollback()  # the caller owns the transaction: nothing was committed
    assert count(db, "sessions") == 1 and details_of(db, rid) == '{"snippet": "masked text"}'


def test_feedback_on_the_deleted_chat_goes_with_it(db):
    user = add_user(db)
    sid = add_session(db, user)
    mid = add_message(db, sid, 1, "assistant")
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'down')",
        (new_id(), mid, user),
    )
    delete_session(db, sid)
    assert count(db, "feedback_entries") == 0


# ---- SQLite foreign keys are switched on by our code, not left to luck (CLAUDE.md) ---------------


@pytest.fixture
def plain_db(tmp_path):
    """A connection that did NOT turn foreign keys on: SQLite's default, which ignores them."""
    path = tmp_path / "plain.db"
    run(f"sqlite:///{path}", MIGRATIONS, log=lambda line: None)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA foreign_keys").fetchone() == (0,)
    yield conn
    conn.close()


def _chat(conn):
    user = add_user(conn)
    sid = add_session(conn, user)
    mid = add_message(conn, sid, 1, "assistant")
    conn.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'down')",
        (new_id(), mid, user),
    )
    return sid


def test_without_the_pragma_a_plain_delete_leaves_the_messages_behind(plain_db):
    sid = _chat(plain_db)
    plain_db.commit()
    plain_db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    assert count(plain_db, "messages") == 1  # the cascade did not fire: the chat text survives


def test_delete_session_switches_foreign_keys_on_so_the_cascade_fires(plain_db):
    sid = _chat(plain_db)
    plain_db.commit()  # no transaction is open: the pragma can take effect
    assert delete_session(plain_db, sid) is True
    assert count(plain_db, "messages") == 0 and count(plain_db, "feedback_entries") == 0
    assert plain_db.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_delete_session_refuses_rather_than_half_deleting_when_the_pragma_cannot_apply(plain_db):
    sid = _chat(plain_db)  # the inserts left a transaction open: SQLite ignores the pragma now
    assert plain_db.in_transaction
    with pytest.raises(ForeignKeysOff, match="PRAGMA foreign_keys = ON"):
        delete_session(plain_db, sid)
    assert count(plain_db, "sessions") == 1 and count(plain_db, "messages") == 1  # nothing deleted


def test_ensure_foreign_keys_leaves_a_connection_that_already_has_them_alone(db):
    ensure_foreign_keys(db)
    assert db.execute("PRAGMA foreign_keys").fetchone() == (1,)


def test_ensure_foreign_keys_does_nothing_for_a_postgres_connection():
    class FakePostgres:
        def execute(self, *args):
            raise AssertionError("no pragma may be sent to Postgres")

    ensure_foreign_keys(FakePostgres())


def test_the_migration_runner_opens_sqlite_with_foreign_keys_on(tmp_path):
    runner = _Sqlite(f"sqlite:///{tmp_path / 'm.db'}")
    try:
        assert runner.conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    finally:
        runner.close()
