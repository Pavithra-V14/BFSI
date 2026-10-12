"""The memory queries on a real Postgres. Runs only in CI, on the throw-away service container
(TEST_POSTGRES_URL); skipped everywhere else, so it never runs on the admin machine.

Written without being run against Postgres (none is installed on this machine): the first CI run
is its first run. Every query that Phase 7 added is exercised here: the session and message
inserts (booleans, NULL grounded, text times into TIMESTAMPTZ), the next message number and its
retry on a real unique violation between two connections, the ownership lookups, the boundary
lookup of a resume, the compare-and-set of the summary, the row lock of the facts and the unclear
count, the review-queue insert, the retention queries (a text time compared with TIMESTAMPTZ, the
IN list, the delete and the audit row) and the LIKE and sub-select that find summaries built from
a document.
"""

import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.migrate import run
from app.memory import retention, store
from app.memory.documents import clear_memory_for_document
from app.memory.facts import Entity, update_facts
from app.memory.messages import UNCLEAR_END
from app.memory.unclear import record_turn
from tests.stored import stored_answer, stored_query, stored_summary

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg_url():
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
def db(pg_url):
    database = connect(pg_url)
    yield database
    database.close()


def new_user(db):
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash) VALUES (?, ?, ?, ?)",
        (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", "h"),
    )
    db.raw.commit()
    return user_id


def save(db, user, session, text, upto, expected_upto=None, folded=()):
    return store.save_summary(
        db,
        user,
        session,
        stored_summary(text),
        upto,
        expected_upto=expected_upto,
        folded_ids=list(folded),
    )


def talk(db, user, session, count=1, citations=None, when=NOW):
    made = []
    for n in range(count):
        made.append(
            store.add_user_message(db, user, session, stored_query(f"question {n} here"), now=when)
        )
        made.append(
            store.add_assistant_message(
                db,
                user,
                session,
                stored_answer(f"answer {n} here"),
                citations=citations or [],
                grounded=True,
                now=when,
            )
        )
    return made


def test_a_chat_round_trips_with_booleans_nulls_and_text_times(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    made = talk(db, user, session, citations=[{"doc_id": "doc-a", "clause": "2.1", "page": 3}])
    state = store.load_session(db, user, session)
    assert [m.seq for m in state.messages] == [1, 2]
    assert state.messages[0].grounded is None and state.messages[1].grounded is True
    assert state.messages[1].citations == ({"doc_id": "doc-a", "clause": "2.1", "page": 3},)
    assert state.messages[1].source_removed is False and state.info.title == "question 0 here"
    assert [s.id for s in store.list_sessions(db, user)] == [session]
    assert made[0].id != made[1].id


def test_another_users_session_is_a_missing_one_here_too(db):
    owner, intruder = new_user(db), new_user(db)
    session = store.create_session(db, owner, now=NOW)
    talk(db, owner, session)
    missing = str(uuid.uuid4())
    for target in (session, missing):
        with pytest.raises(store.SessionNotFound):
            store.load_session(db, intruder, target)
        with pytest.raises(store.SessionNotFound):
            update_facts(db, intruder, target, topic="claims")  # the FOR UPDATE lookup
        with pytest.raises(store.SessionNotFound):
            record_turn(db, intruder, target, unclear=True, now=NOW)
        assert store.delete_own_session(db, intruder, target) is False
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 2


def test_two_connections_that_pick_the_same_number_collide_on_the_constraint_and_retry(
    pg_url, monkeypatch
):
    first, second = connect(pg_url), connect(pg_url)
    try:
        user = new_user(first)
        session = store.create_session(first, user, now=NOW)
        barrier = threading.Barrier(2, timeout=20)
        real = store._next_seq
        waited, calls = set(), []

        def meet(database, session_id):
            number = real(database, session_id)
            calls.append(number)
            if threading.get_ident() not in waited:
                waited.add(threading.get_ident())
                barrier.wait()
            return number

        monkeypatch.setattr(store, "_next_seq", meet)
        results, errors = [], []

        def write(database, text):
            try:
                message = store.add_user_message(
                    database, user, session, stored_query(text), now=NOW
                )
                results.append(message.seq)
            except Exception as error:  # reported below
                errors.append(error)

        threads = [
            threading.Thread(target=write, args=(first, "first writer here")),
            threading.Thread(target=write, args=(second, "second writer here")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors, errors
        assert sorted(results) == [1, 2] and len(calls) == 3
    finally:
        first.close()
        second.close()


def test_free_running_writers_never_share_a_number(pg_url):
    seed = connect(pg_url)
    user = new_user(seed)
    session = store.create_session(seed, user, now=NOW)
    seed.close()
    errors = []

    def write(count):
        database = connect(pg_url)
        try:
            for n in range(count):
                store.add_user_message(
                    database, user, session, stored_query(f"message number {n}"), now=NOW
                )
        except Exception as error:
            errors.append(error)
        finally:
            database.close()

    threads = [threading.Thread(target=write, args=(10,)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(120)
    assert not errors, errors
    check = connect(pg_url)
    numbers = [r[0] for r in check.query_all("SELECT seq FROM messages ORDER BY seq")]
    check.close()
    assert numbers == list(range(1, 31))


def test_the_summary_compare_and_set_and_the_boundary_lookup(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    made = talk(db, user, session, count=3)
    assert save(db, user, session, "first", made[1].id, expected_upto=None)
    assert not save(db, user, session, "stale", made[3].id, expected_upto=None)
    assert save(db, user, session, "second", made[3].id, expected_upto=made[1].id)
    state = store.load_session(db, user, session)
    assert state.summary_text == "second"
    assert [m.id for m in state.messages] == [made[4].id, made[5].id]  # only after the boundary


def test_the_facts_and_the_unclear_count_use_a_row_lock_and_the_review_queue(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    update_facts(
        db, user, session, product_line="Health insurance", entities=[Entity("irdai", ("doc-a",))]
    )
    assert record_turn(db, user, session, unclear=True, now=NOW).count == 1
    ended = record_turn(db, user, session, unclear=True, now=NOW)
    assert ended.ended and ended.message == UNCLEAR_END
    rows = db.query_all("SELECT kind, status, session_id, details FROM review_queue")
    assert len(rows) == 1 and rows[0][:3] == ("two_unclear_turns", "open", session)
    assert json.loads(rows[0][3]) == {"unclear_turns": 2}
    facts = store.load_session(db, user, session).facts
    assert facts == {
        "entities": [{"name": "irdai", "doc_ids": ["doc-a"]}],
        "product_line": "Health insurance",
    }


def test_a_user_deletes_their_own_chat(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    talk(db, user, session)
    assert store.delete_own_session(db, user, session) is True
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 0
    assert store.delete_own_session(db, user, session) is False
    other = store.create_session(db, user, now=NOW)
    store.close_session(db, user, other)
    assert store.load_session(db, user, other).info.status == "closed"


def aged(db, user, days):
    session = store.create_session(db, user, now=NOW - timedelta(days=days))
    talk(db, user, session, when=NOW - timedelta(days=days))
    return session


def test_the_retention_queries_compare_text_times_with_the_timestamp_column(db):
    user = new_user(db)
    young, old = aged(db, user, 89), aged(db, user, 91)
    dry = retention.sweep(db, now=NOW, actor="cli:test", dry_run=True)
    assert (dry.sessions, dry.messages) == (1, 2) and dry.dry_run
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == 0
    result = retention.sweep(db, now=NOW, actor="cli:test")
    assert (result.sessions, result.messages) == (1, 2)
    assert {r[0] for r in db.query_all("SELECT id FROM sessions")} == {young}
    rows = db.query_all("SELECT action, details FROM audit_log")
    assert len(rows) == 1 and rows[0][0] == "retention_sweep"
    assert json.loads(rows[0][1]) == {"days": 90, "messages": 2, "sessions": 1}
    assert old not in {r[0] for r in db.query_all("SELECT id FROM sessions")}
    assert retention.sweep(db, now=NOW, actor="cli:test").sessions == 0
    # Phase 15: every real run writes its own row, also one that deleted nothing (it is the proof
    # that the scheduled task ran); the second run is the second row, with zero sessions
    rows = db.query_all("SELECT action, details FROM audit_log ORDER BY created_at, id")
    assert [r[0] for r in rows] == ["retention_sweep", "retention_sweep"]
    assert sorted(json.loads(r[1])["sessions"] for r in rows) == [0, 1]


def test_summaries_built_from_a_document_are_found_with_like_and_a_subselect(db):
    user = new_user(db)
    built, other, later = (store.create_session(db, user, now=NOW) for _ in range(3))
    for session, doc in ((built, "doc-x"), (other, "doc-y")):
        made = talk(db, user, session, citations=[{"doc_id": doc}])
        save(db, user, session, "A summary.", made[1].id, expected_upto=None)
    made = talk(db, user, later, count=2, citations=[])
    extra = store.add_assistant_message(
        db,
        user,
        later,
        stored_answer("late"),
        citations=[{"doc_id": "doc-x"}],
        grounded=True,
        now=NOW,
    )
    save(db, user, later, "A summary.", made[1].id, expected_upto=None)
    assert extra.seq > 4  # cited only after the boundary
    assert clear_memory_for_document(db, "doc-x", actor="cli:test", now=NOW).summaries == 1
    summaries = {r[0]: r[1] for r in db.query_all("SELECT id, summary_text FROM sessions")}
    assert summaries[built] is None and summaries[other] == "A summary."
    assert summaries[later] == "A summary."
    rows = db.query_all("SELECT action, target, details FROM audit_log")
    assert rows == [
        ("memory_clear", "doc-x", json.dumps({"entities": 0, "summaries": 1}, sort_keys=True))
    ]
    assert clear_memory_for_document(db, "doc-x", actor="cli:test", now=NOW).summaries == 0


# ---- the Phase 7 hardening queries -------------------------------------------------------------


def idle(db):
    from psycopg.pq import TransactionStatus

    return db.raw.info.transaction_status == TransactionStatus.IDLE


def test_no_read_leaves_a_transaction_open_on_postgres(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    talk(db, user, session)
    for read in (
        lambda: store.list_sessions(db, user),
        lambda: store.load_session(db, user, session),
        lambda: store.owned_session(db, user, session),
        lambda: store._next_seq(db, session),
    ):
        read()
        assert idle(db)  # autocommit is off: a read outside transaction() would leave this open
    with pytest.raises(store.SessionNotFound):
        store.load_session(db, user, str(uuid.uuid4()))
    assert idle(db)


def test_no_transaction_is_open_while_the_model_is_called(db):
    import asyncio

    from app.guardrails.masking import Masker
    from app.memory.config import MemoryConfig
    from app.memory.history import prepare_history
    from tests.helpers import WordCounter

    class Quiet:
        def analyze(self, **kwargs):
            return []

    class Gateway:
        def __init__(self, fail):
            self.fail, self.seen = fail, []

        async def complete(self, messages, *, tier, max_tokens, max_wait_s):
            self.seen.append(idle(db))
            if self.fail:
                raise RuntimeError("down")
            return type("Result", (), {"text": "A summary of the chat."})()

    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    talk(db, user, session, count=8)
    for gateway in (Gateway(fail=True), Gateway(fail=False)):
        asyncio.run(
            prepare_history(
                db,
                gateway,
                user_id=user,
                session_id=session,
                config=MemoryConfig(buffer_tokens=15),
                counter=WordCounter(),
                masker=Masker(analyzer=Quiet()),
            )
        )
        assert gateway.seen == [True]  # no transaction while the model worked
        assert idle(db)  # and none left behind afterwards


def test_a_save_is_refused_when_a_folded_message_was_flagged_and_the_boundary_must_match(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    other = store.create_session(db, user, now=NOW)
    made = talk(db, user, session)
    foreign = talk(db, user, other)
    with pytest.raises(ValueError, match="not in this session"):
        save(db, user, session, "A summary.", foreign[0].id)
    db.execute("UPDATE messages SET source_removed = ? WHERE id = ?", (True, made[1].id))
    db.raw.commit()
    assert not save(db, user, session, "A summary.", made[1].id, folded=[made[1].id])  # FOR UPDATE
    assert save(db, user, session, "A summary.", made[1].id, folded=[made[0].id])


def test_a_closed_session_refuses_writes_and_a_role_change_ends_the_users_sessions(db):
    from app.accounts.users import create_admin, create_customer, set_role

    boss = create_admin(
        db, username="boss", email="boss@example.test", password_hash="h", now=NOW, actor="cli"
    )
    member = create_customer(
        db, username="asha", email="asha@example.test", password_hash="h", now=NOW
    )
    session = store.create_session(db, member.id, now=NOW)
    made = talk(db, member.id, session)
    save(db, member.id, session, "A summary.", made[1].id)
    update_facts(db, member.id, session, topic="claims", entities=[Entity("irdai", ("doc-a",))])
    assert set_role(db, actor=boss, target_id=member.id, new_role="staff", now=NOW)
    state = store.load_session(db, member.id, session)
    assert state.info.status == "closed"
    assert (state.summary_text, state.summary_upto_message_id, state.facts) == (None, None, {})
    with pytest.raises(store.SessionNotFound):
        store.add_user_message(db, member.id, session, stored_query("one more question"), now=NOW)
    with pytest.raises(store.SessionNotFound):
        save(db, member.id, session, "A summary.", made[1].id)


def test_the_facts_entities_of_a_deleted_document_are_found_with_like_and_removed(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    update_facts(
        db,
        user,
        session,
        topic="claims",
        entities=[Entity("irdai", ("doc-x", "doc-y")), Entity("rahul sharma", ("doc-x",))],
    )
    cleared = clear_memory_for_document(db, "doc-x", actor="cli:test", now=NOW)
    assert (cleared.summaries, cleared.entities) == (0, 1)
    facts = store.load_session(db, user, session).facts
    assert facts["entities"] == [{"name": "irdai", "doc_ids": ["doc-y"]}]
    assert facts["topic"] == "claims"
    assert clear_memory_for_document(db, "doc-x", actor="cli:test", now=NOW).entities == 0


def test_the_sweep_rechecks_the_last_active_time_under_a_row_lock(db, monkeypatch):
    user = new_user(db)
    stale = aged(db, user, 120)
    real = retention.expired_session_ids

    def read_then_resume(database, moment, days=retention.RETENTION_DAYS):
        ids = real(database, moment, days)
        store.add_user_message(database, user, stale, stored_query("I am back here"), now=NOW)
        return ids

    monkeypatch.setattr(retention, "expired_session_ids", read_then_resume)
    result = retention.sweep(db, now=NOW, actor="cli:test")
    assert (result.sessions, result.messages) == (0, 0)
    assert db.query_one("SELECT COUNT(*) FROM sessions")[0] == 1
    # Phase 15: the run finished and deleted nothing: its row says so (zero sessions, zero messages)
    rows = db.query_all("SELECT action, details FROM audit_log")
    assert [r[0] for r in rows] == ["retention_sweep"]
    assert json.loads(rows[0][1])["sessions"] == 0 and json.loads(rows[0][1])["messages"] == 0


def test_last_active_never_moves_backwards_with_greatest_and_a_naive_time_is_refused(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    newer = NOW + timedelta(hours=1)
    store.add_user_message(db, user, session, stored_query("a newer question"), now=newer)
    late = store.list_sessions(db, user)[0].last_active_at
    older = NOW - timedelta(days=1)
    store.add_user_message(db, user, session, stored_query("a slow older one"), now=older)
    assert store.list_sessions(db, user)[0].last_active_at == late
    with pytest.raises(ValueError, match="timezone-aware"):
        store.create_session(db, user, now=datetime(2026, 10, 7, 12, 0))


def test_a_history_excluded_question_and_a_fixed_reply_round_trip(db):
    user = new_user(db)
    session = store.create_session(db, user, now=NOW)
    flagged = stored_query("a flagged question")
    store.add_user_message(db, user, session, flagged, now=NOW, history_excluded=True)
    store.add_fixed_reply(db, user, session, stored_answer("Hello! How can I help?"), now=NOW)
    state = store.load_session(db, user, session)
    assert [m.history_excluded for m in state.messages] == [True, False]
    assert state.messages[1].grounded is False and state.messages[1].citations == ()
