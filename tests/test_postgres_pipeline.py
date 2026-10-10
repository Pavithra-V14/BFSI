"""The pipeline's persistence and review queries on a real Postgres. Runs only in CI, on the
throw-away service container (TEST_POSTGRES_URL); skipped everywhere else.

Written without being run against Postgres (none is installed on this machine): the first CI run
is its first run. It covers the new queries of Phase 8: the review-queue de-duplication (a join with
the sessions of the user, a day boundary on a TIMESTAMPTZ column and two LIKE tests on the JSON
text) and its insert, and the whole write of a turn (session, unclear count, messages, review row).
The same tests run on SQLite in `tests/test_pipeline_persist.py`.
"""

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.migrate import run
from app.memory import store
from app.pipeline import persist
from app.pipeline.persist import PendingTurn, question_hash, write_turn
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


@pytest.fixture
def db():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    url = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(url, MIGRATIONS, log=lambda line: None)
    database = connect(url)
    yield database
    database.close()
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


def new_user(db):
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, ?)",
        (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", "h"),
    )
    db.raw.commit()
    return user_id


def job(user_id, session_id=None, **overrides):
    question = stored_query("Who won the cricket match?")
    values = dict(
        user_id=user_id,
        session_id=session_id,
        question=question,
        answer=stored_answer("I can only help with our documents."),
        ended_answer=None,
        fixed=True,
        grounded=False,
        citations=[],
        history_excluded=True,
        unclear=None,
        review_reason="out_of_domain",
        review_kind="refused",
        question_hash=question_hash(question.text),
    )
    values.update(overrides)
    return PendingTurn(**values)


def review_rows(db):
    return db.query_all("SELECT kind, session_id, message_id, details FROM review_queue")


def test_a_whole_turn_is_written_and_the_review_row_is_linked_to_the_stored_reply(db):
    user = new_user(db)
    pending = job(user)
    assert write_turn(db, pending, NOW) is False
    state = store.load_session(db, user, pending.session_id)
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert state.messages[0].history_excluded is True and state.messages[1].grounded is False
    rows = review_rows(db)
    assert len(rows) == 1 and rows[0][0] == "refused" and rows[0][1] == pending.session_id
    assert rows[0][2] == pending.assistant_message_id
    assert json.loads(rows[0][3])["reason"] == "out_of_domain"


def test_the_same_question_by_the_same_user_on_the_same_day_is_queued_once(db):
    user = new_user(db)
    first, second = job(user), job(user)
    write_turn(db, first, NOW)
    write_turn(db, second, NOW + timedelta(hours=3))
    assert len(review_rows(db)) == 1


def test_another_day_another_user_another_reason_or_another_question_is_queued_again(db):
    user, other = new_user(db), new_user(db)
    write_turn(db, job(user), NOW)
    write_turn(db, job(user), NOW + timedelta(days=1))  # the next day
    write_turn(db, job(other), NOW)  # another user
    write_turn(db, job(user, review_reason="no_context"), NOW)  # another reason
    different = stored_query("Who won the football match?")
    write_turn(db, job(user, question=different, question_hash=question_hash(different.text)), NOW)
    assert len(review_rows(db)) == 5


def test_the_unclear_count_and_the_fixed_ending_are_written_on_postgres(db):
    user = new_user(db)
    first = job(user, unclear=True, review_reason=None, history_excluded=False,
                ended_answer=stored_answer("Fixed ending."))  # fmt: skip
    write_turn(db, first, NOW)
    second = job(user, session_id=first.session_id, unclear=True, review_reason=None,
                 history_excluded=False, ended_answer=stored_answer("Fixed ending."))  # fmt: skip
    assert write_turn(db, second, NOW + timedelta(minutes=1)) is True
    kinds = [r[0] for r in review_rows(db)]
    assert kinds == ["two_unclear_turns"]
    last = store.load_session(db, user, first.session_id).messages[-1]
    assert last.content == "Fixed ending." and last.grounded is False


def test_a_retried_write_does_not_repeat_a_step(db, monkeypatch):
    user = new_user(db)
    pending = job(user, review_reason=None)
    real = persist.store.add_fixed_reply
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return real(*args, **kwargs)

    monkeypatch.setattr(persist.store, "add_fixed_reply", fail_once)
    with pytest.raises(RuntimeError):
        write_turn(db, pending, NOW)
    write_turn(db, pending, NOW)
    state = store.load_session(db, user, pending.session_id)
    assert [m.role for m in state.messages] == ["user", "assistant"]
