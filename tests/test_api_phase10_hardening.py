"""Phase 10 hardening round: the rules the audit found missing or weak.

Ids are checked by format only (the same 404 for any bad, foreign or missing id, headers included);
a chat is counted once at the route's entry, also when it is refused before the pipeline; the
connection is closed only after a running write has ended, also on a cancellation; a hidden answer
reveals nothing, also on the admin list and for a withdrawn source; one thumb and one review item
per answer even when two requests arrive together; alert events of every part reach the banner;
entity review is opt-in and keeps the single-word protection.
"""

import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace

import fakeredis
import pytest

from app.api import rollout as rollout_module
from app.api.deps import LazyDatabase
from app.api.rollout import SHADOW_NOTICE, WITHDRAWN_NOTICE
from app.api.routes import admin_ops
from app.api.routes import chat as chat_module
from app.api.security import MAX_BODY_BYTES
from app.cache.store import CacheStore, RateDecision
from app.db import entities as entity_store
from app.db.connection import Database, connect
from app.pipeline import persist
from app.pipeline.config import NODE_LIMITS, PipelineConfig
from app.pipeline.locks import WriteGuard
from app.pipeline.startup import MaskerHolder
from app.pipeline.state import Request as PipelineRequest
from app.vector.documents import COLLECTION
from tests.api_chat_fixtures import QUESTION, ChatEnv

MISSING = "00000000-0000-4000-8000-000000000000"
VARYING_HEADERS = {"date"}


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path)
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def shape(response):
    """What a client can see of a response, apart from its timestamp."""
    headers = {
        k.lower(): v for k, v in response.headers.items() if k.lower() not in VARYING_HEADERS
    }
    return response.status_code, response.json(), headers


def count_rate_checks(env, monkeypatch):
    calls = []
    real = env.limits.check_rate

    def counted(user_id):
        calls.append(user_id)
        return real(user_id)

    monkeypatch.setattr(env.limits, "check_rate", counted)
    return calls


def add_entity(env, text="Rahul Sharma", entity_type="person_in_policy"):
    entity = str(uuid.uuid4())
    env.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, 'doc-a', ?, ?, 'pending', 'customer')",
        (entity, text, entity_type),
    )
    return entity


# ======================================================================= ids: the same 404 =====
BAD_IDS = ["x", "a" * 64, "a" * 65, "b" * 300, "not-a-uuid", "../etc", "%20"]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_a_bad_id_of_any_length_is_the_same_404_as_a_missing_id_on_every_user_route(env, bad):
    _, token = env.user_token("asha_rao")
    headers = env.bearer(token)

    def calls(identifier):
        return [
            env.client.get(f"/sessions/{identifier}", headers=headers),
            env.client.delete(f"/sessions/{identifier}", headers=headers),
            env.client.post(
                f"/messages/{identifier}/feedback", json={"rating": "up"}, headers=headers
            ),
            env.chat(token, session_id=identifier),
        ]

    for got, expected in zip(calls(bad), calls(MISSING), strict=True):
        assert got.status_code == 404, (bad, got.text)
        assert shape(got) == shape(expected)


@pytest.mark.parametrize("bad", ["a" * 65, "b" * 300])
def test_a_long_id_is_the_same_404_on_the_admin_routes(tmp_path, fast_hashing, bad):
    env = ChatEnv(tmp_path, entity_review_enabled=True)
    try:
        headers = env.bearer(env.user_token("boss", "admin")[1])

        def calls(identifier):
            return [
                env.client.put(
                    f"/admin/users/{identifier}/active", json={"active": False}, headers=headers
                ),
                env.client.put(
                    f"/admin/users/{identifier}/role", json={"role": "staff"}, headers=headers
                ),
                env.client.post(
                    f"/admin/review/{identifier}/label", json={"label": "other"}, headers=headers
                ),
                env.client.put(
                    f"/admin/entities/{identifier}", json={"status": "approved"}, headers=headers
                ),
            ]

        for got, expected in zip(calls(bad), calls(MISSING), strict=True):
            assert got.status_code == 404, got.text
            assert shape(got) == shape(expected)
    finally:
        env.close()


def test_someone_elses_session_and_message_look_exactly_like_missing_ones_headers_included(env):
    _, owner = env.user_token("asha_rao")
    _, other = env.user_token("ravi_kumar")
    first = env.chat(owner).json()
    session_id = first["session_id"]
    message_id = env.rows(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant'", (session_id,)
    )[0][0]
    headers = env.bearer(other)
    for foreign, missing in [
        (
            env.client.get(f"/sessions/{session_id}", headers=headers),
            env.client.get(f"/sessions/{MISSING}", headers=headers),
        ),
        (
            env.client.post(
                f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=headers
            ),
            env.client.post(
                f"/messages/{MISSING}/feedback", json={"rating": "down"}, headers=headers
            ),
        ),
        (
            env.client.delete(f"/sessions/{session_id}", headers=headers),
            env.client.delete(f"/sessions/{MISSING}", headers=headers),
        ),
        (env.chat(other, session_id=session_id), env.chat(other, session_id=MISSING)),
    ]:
        assert foreign.status_code == 404
        assert shape(foreign) == shape(missing)
    # nothing of the owner's changed
    assert env.rows("SELECT COUNT(*) FROM feedback_entries") == [(0,)]
    assert env.rows("SELECT COUNT(*) FROM review_queue") == [(0,)]
    assert env.client.get(f"/sessions/{session_id}", headers=env.bearer(owner)).status_code == 200


# ============================================================ one rate-limit count per chat =====
def test_a_normal_chat_is_counted_exactly_once(env, monkeypatch):
    _, token = env.user_token("asha_rao")
    calls = count_rate_checks(env, monkeypatch)
    assert env.chat(token).status_code == 200
    assert len(calls) == 1
    assert env.chat(token, QUESTION + " again").status_code == 200
    assert len(calls) == 2


def test_a_malformed_session_id_is_counted_once(env, monkeypatch):
    _, token = env.user_token("asha_rao")
    calls = count_rate_checks(env, monkeypatch)
    assert env.chat(token, session_id="nope").status_code == 404
    assert len(calls) == 1


def test_a_busy_session_is_counted_once(env, monkeypatch):
    user_id, token = env.user_token("asha_rao")
    session_id = env.chat(token).json()["session_id"]
    assert env.rig.deps.locks.try_acquire(user_id, session_id)
    calls = count_rate_checks(env, monkeypatch)
    response = env.chat(token, session_id=session_id)
    assert response.status_code == 409
    assert len(calls) == 1
    env.rig.deps.locks.release(user_id, session_id)


def test_a_chat_while_the_service_is_starting_up_is_counted_once(env, monkeypatch):
    _, token = env.user_token("asha_rao")
    env.rig.deps.masker = MaskerHolder(lambda: None)  # never started: not ready, not failed
    calls = count_rate_checks(env, monkeypatch)
    response = env.chat(token)
    assert response.status_code == 503 and response.json()["error"]["code"] == "starting_up"
    assert len(calls) == 1


def test_a_chat_when_the_masker_failed_is_counted_once(env, monkeypatch):
    _, token = env.user_token("asha_rao")
    holder = MaskerHolder(lambda: None)
    holder.failed = True
    env.rig.deps.masker = holder
    calls = count_rate_checks(env, monkeypatch)
    assert env.chat(token).status_code == 503
    assert len(calls) == 1


def test_a_chat_with_no_pipeline_is_counted_once(tmp_path, fast_hashing, monkeypatch):
    env = ChatEnv(tmp_path, pipeline=False)
    try:
        _, token = env.user_token("asha_rao")
        calls = count_rate_checks(env, monkeypatch)
        assert env.chat(token).status_code == 503
        assert len(calls) == 1
    finally:
        env.close()


def test_a_chat_over_the_limit_is_refused_at_the_entry_before_anything_is_done(env, monkeypatch):
    _, token = env.user_token("asha_rao")
    monkeypatch.setattr(env.limits, "check_rate", lambda user_id: RateDecision(False, 21, 20, 30))
    response = env.chat(token)
    assert response.status_code == 429 and response.headers["Retry-After"] == "30"
    assert response.json()["error"]["code"] == "too_many_requests"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(0,)]  # the pipeline never ran
    assert env.rows("SELECT COUNT(*) FROM messages") == [(0,)]


def test_the_pipeline_counts_a_request_itself_unless_the_caller_already_did(env, monkeypatch):
    user_id = env.make_user("direct_user")
    calls = count_rate_checks(env, monkeypatch)

    async def answer(counted):
        db = connect(env.url)
        try:
            return await env.rig.pipeline.answer(
                PipelineRequest(user_id, "customer", QUESTION, rate_counted=counted), db
            )
        finally:
            db.close()

    asyncio.run(answer(False))
    assert len(calls) == 1  # a direct caller is still counted by stage 1
    asyncio.run(answer(True))
    assert len(calls) == 1  # the route counted it: not again


# ======================================================= the connection and a cancellation =====
def test_two_releases_on_one_write_both_run_when_it_ends_in_the_order_they_were_given():
    guard, order = WriteGuard(), []

    class Future:
        def __init__(self):
            self.callbacks = []

        def add_done_callback(self, fn):
            self.callbacks.append(fn)

    future = Future()
    guard.track(future)
    guard.release_when_idle(lambda: order.append("lock"))
    guard.release_when_idle(lambda: order.append("connection"))
    assert order == []  # a write is running
    for callback in future.callbacks:
        callback(future)
    assert order == ["lock", "connection"]


def test_a_failing_release_does_not_stop_the_next_one():
    guard, order = WriteGuard(), []

    class Future:
        def add_done_callback(self, fn):
            self.fn = fn

    future = Future()
    guard.track(future)
    guard.release_when_idle(lambda: 1 / 0)
    guard.release_when_idle(lambda: order.append("second"))
    future.fn(future)
    assert order == ["second"]


def test_a_chat_with_a_write_still_running_releases_the_session_lock_and_closes_the_connection(
    env, monkeypatch
):
    """The lock release and the connection close both wait for the write (the second used to
    replace the first, which left the session locked)."""
    from app.memory import store

    gate = threading.Event()
    gate.set()  # the first chat writes at once
    real = store.add_user_message

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_user_message", slow)
    env.rig.deps.config = PipelineConfig(node_limits=dict(NODE_LIMITS) | {"persist": (0.3, 0.1)})
    closes = []
    real_close = LazyDatabase.close

    def recording_close(self):
        closes.append(1)
        real_close(self)

    monkeypatch.setattr(LazyDatabase, "close", recording_close)
    _, token = env.user_token("asha_rao")
    first = env.chat(token)
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    gate.clear()  # from now on the transcript write is stuck
    closed_before = len(closes)
    followup = env.chat(token, QUESTION + " b", session_id=session_id)
    assert followup.status_code == 200  # the answer is returned at once
    assert env.rig.deps.locks.held() == 1  # the write is still running: the session stays locked
    assert len(closes) == closed_before  # and the connection stays open
    gate.set()
    for _ in range(200):
        if env.rig.deps.locks.held() == 0 and len(closes) > closed_before:
            break
        time.sleep(0.05)
    assert env.rig.deps.locks.held() == 0  # the lock was released when the write ended
    assert len(closes) == closed_before + 1  # and the connection was closed then, once


def test_a_cancelled_chat_closes_its_connection_only_after_the_running_write_has_ended(
    env, monkeypatch
):
    user_id = env.make_user("asha_rao")
    started, gate = threading.Event(), threading.Event()
    real = persist.write_turn

    def slow_write(*args, **kwargs):
        started.set()
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(persist, "write_turn", slow_write)
    closed = []

    async def scenario():
        db = connect(env.url)
        task = asyncio.create_task(
            env.rig.pipeline.answer(
                PipelineRequest(user_id, "customer", QUESTION),
                db,
                close_db=lambda: (closed.append(time.monotonic()), db.close()),
            )
        )
        for _ in range(400):  # until the transcript write is running in its thread
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == []  # the write has not ended: the connection is still open
        gate.set()
        for _ in range(200):
            if closed:
                break
            await asyncio.sleep(0.02)
        assert len(closed) == 1

    asyncio.run(scenario())


def test_an_error_before_anything_started_closes_the_connection_at_once(env, monkeypatch):
    user_id = env.make_user("asha_rao")
    closed = []

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(type(env.rig.pipeline), "_answer", boom)

    async def scenario():
        with pytest.raises(RuntimeError):
            await env.rig.pipeline.answer(
                PipelineRequest(user_id, "customer", QUESTION),
                None,
                close_db=lambda: closed.append(1),
            )

    asyncio.run(scenario())
    assert closed == [1]


def test_a_chat_cancelled_while_its_connection_is_being_opened_still_closes_it(env, monkeypatch):
    release, opened = threading.Event(), threading.Event()
    closed = []
    user = SimpleNamespace(id="u", role="customer")

    def slow_open(svc, authorization):
        opened.set()
        release.wait(10)
        return SimpleNamespace(user=user), SimpleNamespace(close=lambda: closed.append(1))

    monkeypatch.setattr(chat_module, "open_user_context", slow_open)

    async def scenario():
        request = SimpleNamespace(app=env.app)
        body = chat_module.ChatBody(message="hello")
        task = asyncio.create_task(chat_module.chat(body, request, "Bearer x"))
        for _ in range(200):
            if opened.is_set():
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == []  # the thread is still working
        release.set()
        for _ in range(200):
            if closed:
                break
            await asyncio.sleep(0.01)
        assert closed == [1]

    asyncio.run(scenario())


def test_a_cancelled_chat_leaves_the_closing_to_the_pipeline_which_waits_for_its_write(
    env, monkeypatch
):
    closed, seen = [], {}
    db = SimpleNamespace(close=lambda: closed.append("route"))
    user = SimpleNamespace(id="u", role="customer")
    monkeypatch.setattr(
        chat_module,
        "open_user_context",
        lambda svc, authorization: (SimpleNamespace(user=user), db),
    )

    class FakePipeline:
        async def answer(self, request, database, *, close_db=None):
            seen["close_db"] = close_db
            raise asyncio.CancelledError

    env.app.state.services.pipeline = FakePipeline()
    body = chat_module.ChatBody(message="hello")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(chat_module.chat(body, SimpleNamespace(app=env.app), "Bearer x"))
    assert closed == []  # the route did not close it itself
    assert seen["close_db"] is db.close  # it handed the closing to the pipeline


# ===================================================================== message length =====
def test_a_message_of_2000_characters_is_accepted_and_2001_is_refused(env):
    _, token = env.user_token("asha_rao")
    assert env.chat(token, "w" * 2000).status_code == 200
    refused = env.client.post("/chat", json={"message": "w" * 2001}, headers=env.bearer(token))
    assert refused.status_code == 422


def test_the_longest_message_in_its_worst_escaping_never_meets_the_body_limit(env):
    _, token = env.user_token("asha_rao")
    body = json.dumps({"message": "\U0001f600" * 2000}).encode()  # 12 bytes for each character
    assert 20_000 < len(body) < MAX_BODY_BYTES
    response = env.client.post(
        "/chat",
        content=body,
        headers={**env.bearer(token), "Content-Type": "application/json"},
    )
    assert response.status_code == 200, response.text  # not 413, not 422


# ============================================================ feedback and stage, two at once =====
def test_a_double_click_leaves_one_thumb_and_one_review_item(env):
    _, token = env.user_token("asha_rao")
    session_id = env.chat(token).json()["session_id"]
    message_id = env.rows(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant'", (session_id,)
    )[0][0]
    for _ in range(3):
        response = env.client.post(
            f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=env.bearer(token)
        )
        assert response.status_code == 200
    assert env.rows("SELECT rating FROM feedback_entries") == [("down",)]
    assert env.rows("SELECT COUNT(*) FROM review_queue WHERE kind = 'thumbs_down'") == [(1,)]


def test_a_thumb_that_another_request_already_stored_is_changed_not_a_500(env):
    """The other request got there first: its feedback row and review item already exist."""
    user_id, token = env.user_token("asha_rao")
    session_id = env.chat(token).json()["session_id"]
    message_id = env.rows(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant'", (session_id,)
    )[0][0]
    env.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'up')",
        (str(uuid.uuid4()), message_id, user_id),
    )
    env.execute(
        "INSERT INTO review_queue (id, kind, message_id, session_id) "
        "VALUES (?, 'thumbs_down', ?, ?)",
        (str(uuid.uuid4()), message_id, session_id),
    )
    response = env.client.post(
        f"/messages/{message_id}/feedback", json={"rating": "down"}, headers=env.bearer(token)
    )
    assert response.status_code == 200, response.text
    assert env.rows("SELECT rating FROM feedback_entries") == [("down",)]
    assert env.rows("SELECT COUNT(*) FROM review_queue WHERE kind = 'thumbs_down'") == [(1,)]


def test_the_first_stage_change_does_not_fail_when_another_one_wrote_the_row_first(
    env, admin, monkeypatch
):
    """The stage is read as "no row yet", but the row exists by the time of the write."""
    env.set_stage_in_database("open")
    real = Database.query_one

    def blind(self, sql, params=()):
        if "SELECT value FROM app_settings" in sql:
            return None
        return real(self, sql, params)

    monkeypatch.setattr(Database, "query_one", blind)
    response = env.client.put(
        "/admin/shadow", json={"stage": "staff_beta"}, headers=env.bearer(admin)
    )
    monkeypatch.undo()
    assert response.status_code == 200, response.text
    assert env.rows("SELECT value FROM app_settings WHERE key = 'rollout_stage'") == [
        ("staff_beta",)
    ]
    assert env.rows("SELECT COUNT(*) FROM app_settings") == [(1,)]


# ========================================================================= who sees what =====
def test_a_deactivated_user_is_refused_on_chat_sessions_and_feedback(env, admin):
    user_id, token = env.user_token("asha_rao")
    session_id = env.chat(token).json()["session_id"]
    message_id = env.rows("SELECT id FROM messages WHERE role = 'assistant'")[0][0]
    env.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    headers = env.bearer(token)
    for response in [
        env.chat(token),
        env.client.get("/sessions", headers=headers),
        env.client.get(f"/sessions/{session_id}", headers=headers),
        env.client.delete(f"/sessions/{session_id}", headers=headers),
        env.client.delete("/sessions", headers=headers),
        env.client.post(f"/messages/{message_id}/feedback", json={"rating": "up"}, headers=headers),
    ]:
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "not_signed_in"
    assert env.rows("SELECT COUNT(*) FROM sessions") == [(1,)]  # nothing was deleted


def test_a_deactivated_admin_loses_the_admin_routes_at_once(env):
    admin_id, token = env.user_token("boss", "admin")
    assert env.client.get("/admin/alerts", headers=env.bearer(token)).status_code == 200
    env.execute("UPDATE users SET is_active = 0 WHERE id = ?", (admin_id,))
    assert env.client.get("/admin/alerts", headers=env.bearer(token)).status_code == 401
    assert (
        env.client.put(
            "/admin/shadow", json={"stage": "open"}, headers=env.bearer(token)
        ).status_code
        == 401
    )


def hidden_session(env, username="asha_rao", role="customer", stage="shadow"):
    env.set_stage_in_database(stage)
    user_id, token = env.user_token(username, role)
    session_id = env.chat(token).json()["session_id"]
    return user_id, token, session_id


def resume(env, token, session_id):
    response = env.client.get(f"/sessions/{session_id}", headers=env.bearer(token))
    assert response.status_code == 200, response.text
    return [m for m in response.json()["messages"] if m["role"] == "assistant"][0], response


def test_a_hidden_answer_of_a_withdrawn_document_shows_only_the_hidden_notice(env):
    _, token, session_id = hidden_session(env)
    env.execute("UPDATE messages SET source_removed = 1 WHERE role = 'assistant'")
    env.set_stage_in_database("open")
    answer, response = resume(env, token, session_id)
    assert answer["text"] == SHADOW_NOTICE and answer["notice"] == "shadowed"
    assert answer["citations"] == [] and answer["grounded"] is None
    assert answer["source_removed"] is False  # a hidden answer says nothing about its source
    assert WITHDRAWN_NOTICE not in response.text and "30 days" not in response.text


def test_the_admin_who_once_was_staff_sees_the_text_of_their_own_hidden_answer(env):
    user_id, token, session_id = hidden_session(env, "ravi_kumar", role="staff")
    first, _ = resume(env, token, session_id)
    assert first["text"] == SHADOW_NOTICE
    env.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))  # promoted since
    answer, _ = resume(env, token, session_id)
    assert "30 days" in answer["text"] and answer["notice"] is None and answer["citations"]
    message_id = env.rows("SELECT id FROM messages WHERE role = 'assistant'")[0][0]
    rated = env.client.post(
        f"/messages/{message_id}/feedback", json={"rating": "up"}, headers=env.bearer(token)
    )
    assert rated.status_code == 200  # an admin may rate a hidden answer
    env.execute("UPDATE messages SET source_removed = 1 WHERE role = 'assistant'")
    withdrawn, _ = resume(env, token, session_id)  # the withdrawn notice applies to admins too
    assert withdrawn["text"] == WITHDRAWN_NOTICE and withdrawn["citations"] == []
    assert withdrawn["source_removed"] is True


def test_in_the_staff_beta_staff_see_answers_and_customers_do_not_also_when_the_stage_moves(env):
    env.set_stage_in_database("staff_beta")
    _, customer = env.user_token("asha_rao")
    _, staff = env.user_token("ravi_kumar", "staff")
    customer_reply, staff_reply = env.chat(customer).json(), env.chat(staff).json()
    assert customer_reply["answer_visible"] is False and set(customer_reply) == {
        "answer_visible", "kind", "text", "session_id",
    }  # fmt: skip
    assert staff_reply["answer_visible"] is True and "30 days" in staff_reply["text"]
    customer_session, staff_session = customer_reply["session_id"], staff_reply["session_id"]
    for stage in ("open", "shadow"):
        env.set_stage_in_database(stage)
        env.app.state.services.rollout._value = None  # the cache must not hide the change
        hidden, _ = resume(env, customer, customer_session)
        assert hidden["text"] == SHADOW_NOTICE  # also once the stage is open
        shown, _ = resume(env, staff, staff_session)
        assert "30 days" in shown["text"]  # answered while visible: stays visible


def test_a_withdrawn_document_shows_the_notice_on_the_hidden_answers_list_too(env, admin):
    _, token, session_id = hidden_session(env)
    env.execute("UPDATE messages SET source_removed = 1 WHERE role = 'assistant'")
    response = env.client.get("/admin/shadow/answers", headers=env.bearer(admin))
    item = response.json()["answers"][0]
    assert item["answer"] == WITHDRAWN_NOTICE and item["citations"] == []
    assert item["source_removed"] is True
    assert "30 days" not in response.text


def test_the_hidden_answers_list_shows_no_flag_text_or_name_of_the_user(env, admin):
    _, token, session_id = hidden_session(env)
    response = env.client.get("/admin/shadow/answers", headers=env.bearer(admin))
    assert "asha_rao" not in response.text and "@example.test" not in response.text


# ===================================================================== the stage cache =====
def test_the_stage_is_cached_for_the_configured_time_and_an_api_change_applies_at_once(
    tmp_path, fast_hashing
):
    env = ChatEnv(tmp_path, stage="open", rollout_cache_seconds=30.0)
    try:
        clock = [100.0]
        env.app.state.services.rollout._clock = lambda: clock[0]
        _, customer = env.user_token("asha_rao")
        _, boss = env.user_token("boss", "admin")
        assert env.chat(customer).json()["answer_visible"] is True  # read and cached: open
        env.set_stage_in_database("shadow")  # changed behind the cache's back
        clock[0] += 10
        assert env.chat(customer).json()["answer_visible"] is True  # still the cached value
        clock[0] += 30  # the cache ran out
        assert env.chat(customer).json()["answer_visible"] is False
        # a change through the API replaces the cache at once, no waiting
        response = env.client.put("/admin/shadow", json={"stage": "open"}, headers=env.bearer(boss))
        assert response.status_code == 200
        assert env.chat(customer).json()["answer_visible"] is True
    finally:
        env.close()


def test_the_default_cache_time_is_a_few_seconds():
    assert 1 <= rollout_module.CACHE_SECONDS <= 10


# ================================================================================ entities =====
def test_entity_review_is_off_unless_switched_on_and_changes_nothing_when_off(
    tmp_path, fast_hashing
):
    env = ChatEnv(tmp_path)  # entity_review_enabled defaults to off
    try:
        admin = env.user_token("boss", "admin")[1]
        entity = add_entity(env)
        response = env.client.put(
            f"/admin/entities/{entity}", json={"status": "approved"}, headers=env.bearer(admin)
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "entity_review_disabled"
        assert env.client.get("/admin/entities", headers=env.bearer(admin)).status_code == 200
        assert env.rows("SELECT status FROM allowlist_entities") == [("pending",)]
    finally:
        env.close()


def test_the_host_refuses_entity_review_even_when_it_is_switched_on(tmp_path, fast_hashing):
    env = ChatEnv(tmp_path, entity_review_enabled=True, on_render=True)
    try:
        admin = env.user_token("boss", "admin")[1]
        entity = add_entity(env)
        response = env.client.put(
            f"/admin/entities/{entity}", json={"status": "approved"}, headers=env.bearer(admin)
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_machine_only"
        assert env.rows("SELECT status FROM allowlist_entities") == [("pending",)]
    finally:
        env.close()


def test_a_one_word_name_is_approved_only_when_confirmed_and_may_be_rejected_freely(
    tmp_path, fast_hashing
):
    env = ChatEnv(tmp_path, entity_review_enabled=True)
    try:
        admin = env.user_token("boss", "admin")[1]
        headers = env.bearer(admin)
        one_word, two_words, other = (
            add_entity(env, "Sharma"),
            add_entity(env),
            add_entity(env, "Bharosa", entity_type="product_term"),
        )
        refused = env.client.put(
            f"/admin/entities/{one_word}", json={"status": "approved"}, headers=headers
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "single_word_needs_confirmation"
        assert env.rows("SELECT status FROM allowlist_entities WHERE id = ?", (one_word,)) == [
            ("pending",)
        ]
        assert env.rows("SELECT COUNT(*) FROM audit_log WHERE action = 'entity_review'") == [(0,)]
        for entity in (two_words, other):  # no confirmation needed for these
            approved = env.client.put(
                f"/admin/entities/{entity}", json={"status": "approved"}, headers=headers
            )
            assert approved.status_code == 200
        confirmed = env.client.put(
            f"/admin/entities/{one_word}",
            json={"status": "approved", "confirm_single_word": True},
            headers=headers,
        )
        assert confirmed.status_code == 200
        rejected = add_entity(env, "Verma")
        assert (
            env.client.put(
                f"/admin/entities/{rejected}", json={"status": "rejected"}, headers=headers
            ).status_code
            == 200
        )
        details = env.rows(
            "SELECT details FROM audit_log WHERE action = 'entity_review' AND target = ?",
            (one_word,),
        )
        assert json.loads(details[0][0])["single_word_confirmed"] is True
    finally:
        env.close()


@pytest.mark.parametrize(
    ("entity_type", "text", "expected"),
    [
        ("person_in_policy", "Sharma", True),
        ("person_in_policy", "Rahul Sharma", False),
        ("person_in_policy", "Dr. Sharma", False),
        ("product_term", "Bharosa", False),
        ("person_in_policy", "", False),
    ],
)
def test_the_cli_review_and_the_api_use_one_single_word_rule(entity_type, text, expected):
    from app.ingest.review import Group, Row

    assert entity_store.is_single_word_person(entity_type, text) is expected
    group = Group(head=text, kind=entity_type)
    assert group.single_word(Row("id", text, entity_type)) is expected


# ======================================================== a payload that is not a number =====
def test_a_document_version_that_is_not_a_number_does_not_break_the_list(env, admin):
    ids = list(env.rig.rig.points("doc-a"))
    env.rig.rig.qdrant.set_payload(COLLECTION, payload={"doc_version": "abc"}, points=ids[:1])
    response = env.client.get("/admin/documents", headers=env.bearer(admin))
    assert response.status_code == 200, response.text
    assert response.json()["documents"][0]["document_version"] == 1  # the other chunks still say 1
    single = env.client.get("/admin/documents/doc-a", headers=env.bearer(admin))
    assert single.status_code == 200


# ================================================================ alerts from every part =====
@pytest.fixture(scope="module")
def wired():
    """The app as `create_app_from_env` builds it, with fakes in place of the network clients and a
    masker that fails to load. Nothing here touches a real service."""
    from qdrant_client import QdrantClient

    from app.api import main as api_main
    from app.pipeline import startup
    from tests.api_fixtures import AUDIT_KEY, JWT_SECRET

    created: dict = {}

    def fake_store(env, *, on_event=None):
        created["store_on_event"] = on_event
        return CacheStore(fakeredis.FakeRedis(), on_event=on_event)

    class FailingHolder(startup.MaskerHolder):
        def __init__(self, factory=None, **kwargs):
            def boom():
                raise RuntimeError("the model did not load")

            super().__init__(boom, **kwargs)

    env = {
        "JWT_SECRET_KEY": JWT_SECRET,
        "AUDIT_HASH_KEY": AUDIT_KEY,
        "DATABASE_URL": "sqlite:///unused.db",
        "UPSTASH_REDIS_URL": "redis://localhost:6379",
        "QDRANT_URL": "http://localhost:6333",
        "ENTITY_REVIEW_ENABLED": "true",
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(api_main, "store_from_env", fake_store)
        patch.setattr(startup, "MaskerHolder", FailingHolder)
        patch.setattr(
            "app.vector.documents.client_from_env", lambda env: QdrantClient(location=":memory:")
        )
        app = api_main.create_app_from_env(env)
    holder = app.state.services.pipeline.deps.masker
    for _ in range(200):  # the load fails in its thread
        if holder.failed:
            break
        time.sleep(0.02)
    return app


def components(app):
    svc = app.state.services
    return {
        "cache": svc.cache._on_event,
        "gateway": svc.gateway._on_event,
        "limits": svc.limits._on_event,
        "masker": svc.pipeline.deps.masker._on_event,
    }


def banner(app):
    return admin_ops.alerts(SimpleNamespace(app=app), None)


def test_the_environment_switch_for_entity_review_is_read(wired):
    assert wired.state.services.entity_review_enabled is True


def test_a_masker_that_failed_to_load_reaches_the_alerts_without_any_help(wired):
    names = [name for name, _ in wired.state.services.events]
    assert "masker_load_failed" in names
    assert "masker_load_failed" in banner(wired)["critical"]


@pytest.mark.parametrize("name", sorted(admin_ops.CRITICAL_ALERTS))
@pytest.mark.parametrize("part", ["cache", "gateway", "limits", "masker"])
def test_every_critical_alert_from_every_part_reaches_the_banner(wired, part, name):
    before = len(wired.state.services.events)
    components(wired)[part](name, {"count": 1})
    assert len(wired.state.services.events) == before + 1
    result = banner(wired)
    assert result["banner"] is True and name in result["critical"]


def test_the_pipeline_events_still_reach_the_alerts_once(wired):
    svc = wired.state.services
    before = [n for n, _ in svc.events].count("persist_queue_overflow")
    svc.pipeline.deps.on_event("persist_queue_overflow", {})
    assert [n for n, _ in svc.events].count("persist_queue_overflow") == before + 1


def test_an_event_that_arrives_before_the_app_exists_is_passed_on_when_it_is_bound():
    from app.api.main import EventRelay

    relay, seen = EventRelay(), []
    relay("early_one", {"n": 1})
    relay.bind(lambda name, fields: seen.append((name, fields)))
    relay("late_one", {"n": 2})
    assert seen == [("early_one", {"n": 1}), ("late_one", {"n": 2})]
