# ruff: noqa: F811
"""The transcript write is ONE transaction (Change round 1): one commit for the whole turn, far
fewer statements than before, all or nothing, and a message-number clash retries the whole write."""

from datetime import timedelta

import pytest

from app.db.connection import Database
from app.memory import store
from app.pipeline import persist
from app.pipeline.persist import PendingTurn, write_turn
from tests.stored import stored_answer, stored_query
from tests.test_pipeline_persist import NOW, db, job, new_user, review_rows  # noqa: F401


class Count:
    """Counts the statements and the commits a block of code makes."""

    def __init__(self, monkeypatch):
        self.statements = 0
        self.commits = 0
        execute, end = Database.execute, Database._end

        def counted_execute(this, sql, params=()):
            self.statements += 1
            return execute(this, sql, params)

        def counted_end(this, how):
            if how == "commit":
                self.commits += 1
            return end(this, how)

        monkeypatch.setattr(Database, "execute", counted_execute)
        monkeypatch.setattr(Database, "_end", counted_end)


def answer_job(user, **overrides):
    values = dict(
        answer=stored_answer("A waiting period of 30 days applies."),
        fixed=False,
        grounded=True,
        citations=[{"doc_id": "doc-a", "clause": "2", "page": 1}],
        history_excluded=False,
        review_reason=None,
        product_line="insurance",
    )
    values.update(overrides)
    return job(user, **values)


def test_a_whole_turn_is_one_commit_and_a_handful_of_statements(db, monkeypatch):  # noqa: F811
    user = new_user(db)
    pending = answer_job(user, new_session=True, session_id="11111111-1111-4111-8111-111111111111")
    counted = Count(monkeypatch)
    write_turn(db, pending, NOW)
    assert counted.commits == 1  # before: one commit for every step of the turn
    assert counted.statements <= 12, counted.statements  # before: about twenty
    state = store.load_session(db, user, pending.session_id)
    assert [m.role for m in state.messages] == ["user", "assistant"]
    assert [m.seq for m in state.messages] == [1, 2]


def test_a_follow_up_turn_is_one_commit_too(db, monkeypatch):  # noqa: F811
    user = new_user(db)
    first = answer_job(user)
    write_turn(db, first, NOW)
    second = answer_job(user, session_id=first.session_id, review_reason="low_confidence")
    counted = Count(monkeypatch)
    write_turn(db, second, NOW + timedelta(minutes=1))
    assert counted.commits == 1
    state = store.load_session(db, user, first.session_id)
    assert [m.seq for m in state.messages] == [1, 2, 3, 4]


def test_a_failure_in_the_middle_leaves_nothing_and_the_retry_writes_the_turn_once(
    db,
    monkeypatch,  # noqa: F811
):
    user = new_user(db)
    pending = answer_job(user, new_session=True, session_id="22222222-2222-4222-8222-222222222222")
    real = persist.update_facts
    calls = {"n": 0}

    def blip(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return real(*args, **kwargs)

    monkeypatch.setattr(persist, "update_facts", blip)
    with pytest.raises(RuntimeError):
        write_turn(db, pending, NOW)
    assert db.query_one("SELECT COUNT(*) FROM sessions")[0] == 0  # the session was rolled back too
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 0
    assert "committed" not in pending.done and pending.assistant_message_id is None
    write_turn(db, pending, NOW)
    assert db.query_one("SELECT COUNT(*) FROM sessions")[0] == 1
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 2  # once, not twice


def test_a_job_with_no_session_id_gets_none_back_when_its_write_is_rolled_back(
    db,
    monkeypatch,  # noqa: F811
):
    user = new_user(db)
    pending = job(user)  # no session id yet, a review row to write
    monkeypatch.setattr(persist, "_write_review", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        write_turn(db, pending, NOW)
    assert pending.session_id is None and pending.assistant_message_id is None
    assert db.query_one("SELECT COUNT(*) FROM sessions")[0] == 0


def test_a_committed_job_is_not_written_again(db):  # noqa: F811
    user = new_user(db)
    pending = answer_job(user)
    write_turn(db, pending, NOW)
    write_turn(db, pending, NOW)  # a queue entry that the live thread already wrote
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 2


def test_the_review_row_is_linked_to_the_session_and_the_stored_reply(db):  # noqa: F811
    user = new_user(db)
    pending = job(user)
    write_turn(db, pending, NOW)
    (row,) = review_rows(db)
    assert row[1] == pending.session_id and row[2] == pending.assistant_message_id


# ---- a message number taken by another writer -------------------------------------------------
def test_a_clash_on_the_message_number_retries_the_whole_write(db, monkeypatch):  # noqa: F811
    user = new_user(db)
    first = answer_job(user)
    write_turn(db, first, NOW)
    second = answer_job(user, session_id=first.session_id)
    real = store._next_seq
    calls = {"n": 0}

    def stale(db_, session_id):
        calls["n"] += 1
        return 1 if calls["n"] == 1 else real(db_, session_id)  # the number of the first turn

    monkeypatch.setattr(store, "_next_seq", stale)
    monkeypatch.setattr(persist.time, "sleep", lambda seconds: None)
    write_turn(db, second, NOW + timedelta(minutes=1))
    state = store.load_session(db, user, first.session_id)
    assert [m.seq for m in state.messages] == [1, 2, 3, 4]
    assert calls["n"] >= 2  # the first try clashed, the next one read a fresh number


def test_inside_a_larger_transaction_a_clash_is_raised_not_retried_in_place(db):  # noqa: F811
    user = new_user(db)
    first = answer_job(user)
    write_turn(db, first, NOW)
    with db.transaction(write=True):
        with pytest.raises(Exception) as caught:
            store.add_user_message(
                db, user, first.session_id, stored_query("again"), now=NOW, seq=1, owned=True
            )
    assert store.is_unique_violation(caught.value)


def test_the_hints_are_not_a_way_round_the_checks(db):  # noqa: F811
    user, other = new_user(db), new_user(db)
    first = answer_job(user)
    write_turn(db, first, NOW)
    with pytest.raises(store.SessionNotFound):  # someone else's session: still refused
        store.add_user_message(db, other, first.session_id, stored_query("hi there"), now=NOW)
    assert db.query_one("SELECT COUNT(*) FROM messages")[0] == 2


def test_the_pending_turn_type_is_unchanged_for_the_queue():
    assert {"done", "ended", "assistant_message_id", "new_session"} <= set(
        PendingTurn.__dataclass_fields__
    )
