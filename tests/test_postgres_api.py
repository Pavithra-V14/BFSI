"""The queries of the Phase 10 API on a real Postgres. Runs only in CI, on the throw-away service
container (TEST_POSTGRES_URL); skipped everywhere else (and never run on this machine).

Written without being run against Postgres: the first CI run is its first run. It covers the new
queries: the session and message pages, the user page, the review queue (list, overdue, label), the
allow-list review, the rollout stage (read, insert, update), the shadowed flag on a message and the
list of hidden answers, and the one-thumb-per-message unique index.
"""

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.accounts import users
from app.accounts.tokens import utc_text
from app.api.rollout import Rollout
from app.db import entities, feedback, review_queue
from app.db.connection import connect
from app.db.migrate import run
from app.memory import store
from tests.stored import stored_answer, stored_query

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
STAMP = utc_text(NOW)  # the same text form the routes write (app.accounts.tokens.utc_text)


@pytest.fixture
def url():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    url = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(url, MIGRATIONS, log=lambda line: None)
    yield url
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(url):
    database = connect(url)
    yield database
    database.close()


def new_user(db, role="customer"):
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", "h", role),
    )
    db.raw.commit()
    return user_id


def test_session_and_message_pages(db):
    user = new_user(db)
    for _ in range(3):
        store.create_session(db, user, now=NOW)
    page, total = store.list_sessions_page(db, user, limit=2, offset=0)
    assert total == 3 and len(page) == 2
    rest, _ = store.list_sessions_page(db, user, limit=2, offset=2)
    assert len(rest) == 1
    session_id = page[0].id
    store.add_user_message(db, user, session_id, stored_query("a question"), now=NOW)
    store.add_assistant_message(
        db, user, session_id, stored_answer("an answer"), citations=[], grounded=True, now=NOW,
        shadowed=True,
    )  # fmt: skip
    info, messages, count = store.transcript(db, user, session_id, limit=10, offset=0)
    assert info.id == session_id and count == 2
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[1].shadowed is True and messages[0].shadowed is False
    with pytest.raises(store.SessionNotFound):
        store.transcript(db, new_user(db), session_id, limit=10, offset=0)


def test_the_user_page_and_the_activation_rules(db):
    admin = new_user(db, "admin")
    customer = new_user(db)
    accounts, total = users.list_users(db, limit=1, offset=0)
    assert total == 2 and len(accounts) == 1
    actor = users.get_by_id(db, admin)
    assert (
        users.set_active_by_id(db, actor=actor, target_id=customer, active=False, now=NOW) is False
    )
    with pytest.raises(users.RoleChangeRefused):
        users.set_active_by_id(db, actor=actor, target_id=admin, active=False, now=NOW)


def test_the_review_queue_list_label_and_overdue(db):
    admin = new_user(db, "admin")
    item = str(uuid.uuid4())
    db.execute(
        "INSERT INTO review_queue (id, kind, status, details, created_at) "
        "VALUES (?, 'refused', 'open', ?, ?)",
        (item, json.dumps({"question": "q"}), "2026-10-01T09:00:00+00:00"),
    )
    db.raw.commit()
    items, total = review_queue.list_items(
        db, status="open", kind=None, limit=10, offset=0, now=NOW
    )
    assert total == 1 and items[0].overdue is True
    with db.transaction(write=True):
        assert review_queue.label_item(
            db, item_id=item, label="other", status="labelled", user_id=admin, now=NOW
        )
    items, _ = review_queue.list_items(
        db, status="labelled", kind="refused", limit=10, offset=0, now=NOW
    )
    assert items[0].label == "other" and items[0].labelled_by == admin


def test_entities_list_and_review(db):
    admin = new_user(db, "admin")
    entity = str(uuid.uuid4())
    db.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, 'doc-a', 'Rahul', 'person_in_policy', 'pending', 'customer')",
        (entity,),
    )
    db.raw.commit()
    rows, total = entities.list_entities(db, doc_id="doc-a", status="pending", limit=10, offset=0)
    assert total == 1 and rows[0].id == entity
    with db.transaction(write=True):
        assert entities.set_status(db, entity_id=entity, status="approved", user_id=admin, now=NOW)
    rows, _ = entities.list_entities(db, doc_id=None, status="approved", limit=10, offset=0)
    assert rows[0].approved_by == admin and rows[0].approved_at


def test_the_rollout_stage_is_read_changed_and_defaults_to_shadow(db):
    admin = new_user(db, "admin")
    rollout = Rollout(cache_seconds=0.0)
    assert rollout.stage(db) == "shadow"
    assert rollout.set_stage(db, stage="staff_beta", actor_id=admin, now=NOW) == "shadow"
    assert rollout.stage(db) == "staff_beta"
    assert rollout.set_stage(db, stage="open", actor_id=admin, now=NOW) == "staff_beta"
    assert rollout.stage(db) == "open"


def test_one_thumb_per_user_per_message_is_enforced_by_the_index(db):
    user = new_user(db)
    session_id = store.create_session(db, user, now=NOW)
    store.add_user_message(db, user, session_id, stored_query("q"), now=NOW)
    answer = store.add_assistant_message(
        db, user, session_id, stored_answer("a"), citations=[], grounded=True, now=NOW
    )
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'up')",
        (str(uuid.uuid4()), answer.id, user),
    )
    db.raw.commit()
    with pytest.raises(Exception):  # noqa: B017  (psycopg.errors.UniqueViolation)
        db.execute(
            "INSERT INTO feedback_entries (id, message_id, user_id, rating) "
            "VALUES (?, ?, ?, 'down')",
            (str(uuid.uuid4()), answer.id, user),
        )
    db.raw.rollback()


def test_one_thumbs_down_item_per_answer_is_enforced_and_the_insert_is_a_no_op_the_second_time(db):
    user = new_user(db)
    session_id = store.create_session(db, user, now=NOW)
    store.add_user_message(db, user, session_id, stored_query("q"), now=NOW)
    answer = store.add_assistant_message(
        db, user, session_id, stored_answer("a"), citations=[], grounded=True, now=NOW
    )
    for _ in range(2):
        with db.transaction(write=True):
            feedback.add_thumbs_down_item(
                db, message_id=answer.id, session_id=session_id, question="q", stamp=STAMP
            )
    count = db.query_one("SELECT COUNT(*) FROM review_queue WHERE kind = 'thumbs_down'")
    assert count == (1,)
    with pytest.raises(Exception):  # noqa: B017  (psycopg.errors.UniqueViolation)
        db.execute(
            "INSERT INTO review_queue (id, kind, message_id) VALUES (?, 'thumbs_down', ?)",
            (str(uuid.uuid4()), answer.id),
        )
    db.raw.rollback()


def test_two_requests_at_the_same_moment_leave_one_thumb_and_one_item(url):
    import threading

    setup = connect(url)
    user = new_user(setup)
    session_id = store.create_session(setup, user, now=NOW)
    store.add_user_message(setup, user, session_id, stored_query("q"), now=NOW)
    answer = store.add_assistant_message(
        setup, user, session_id, stored_answer("a"), citations=[], grounded=True, now=NOW
    )
    setup.close()
    barrier, errors = threading.Barrier(6), []

    def click(rating):
        conn = connect(url)
        try:
            barrier.wait(10)
            with conn.transaction(write=True):
                feedback.upsert_rating(
                    conn, message_id=answer.id, user_id=user, rating=rating, stamp=STAMP
                )
                feedback.add_thumbs_down_item(
                    conn, message_id=answer.id, session_id=session_id, question="q", stamp=STAMP
                )
        except Exception as error:  # a lost race would show here
            errors.append(type(error).__name__)
        finally:
            conn.close()

    threads = [threading.Thread(target=click, args=("down" if i % 2 else "up",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    check = connect(url)
    try:
        assert errors == []
        assert check.query_one("SELECT COUNT(*) FROM feedback_entries") == (1,)
        assert check.query_one("SELECT COUNT(*) FROM review_queue WHERE kind = 'thumbs_down'") == (
            1,
        )
    finally:
        check.close()


def test_two_first_stage_changes_at_the_same_moment_both_succeed(url):
    import threading

    setup = connect(url)
    admin = new_user(setup, "admin")
    setup.close()
    barrier, errors = threading.Barrier(4), []

    def change(stage):
        conn = connect(url)
        try:
            barrier.wait(10)
            Rollout(cache_seconds=0.0).set_stage(conn, stage=stage, actor_id=admin, now=NOW)
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            conn.close()

    stages = ["open", "staff_beta", "open", "shadow"]
    threads = [threading.Thread(target=change, args=(stage,)) for stage in stages]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    check = connect(url)
    try:
        assert errors == []
        assert check.query_one("SELECT COUNT(*) FROM app_settings") == (1,)
    finally:
        check.close()
