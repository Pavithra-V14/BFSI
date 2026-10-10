"""The chat endpoint (Phase 10): the pipeline behind the API, with its rollout stage (shadow mode),
its error mapping, its single rate-limit count and the connection that stays open for a write."""

import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import LazyDatabase
from app.api.rollout import SHADOW_NOTICE, WITHDRAWN_NOTICE
from app.api.routes import chat as chat_module
from app.pipeline import messages as pipeline_messages
from app.pipeline.config import NODE_LIMITS, PipelineConfig
from app.pipeline.startup import MaskerHolder
from app.vector.documents import COLLECTION
from tests.api_chat_fixtures import QUESTION, ChatEnv
from tests.pipeline_rig import masker_holder

SHADOW_KEYS = {"answer_visible", "kind", "text", "session_id"}


@pytest.fixture
def chat_env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path)
    yield made
    made.close()


def stored(env, session_id):
    return env.rows(
        "SELECT role, content, grounded, shadowed FROM messages WHERE session_id = ? ORDER BY seq",
        (session_id,),
    )


# ---- an answer ----------
def test_a_chat_returns_a_cited_answer_and_stores_the_turn(chat_env):
    user_id, token = chat_env.user_token("asha_rao")
    response = chat_env.chat(token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer_visible"] is True and body["kind"] == "answer"
    assert "30 days" in body["text"] and body["citations"]
    assert body["citations"][0]["doc_id"] == "doc-a"
    assert set(body) == {
        "answer_visible", "kind", "text", "reason", "citations", "warnings", "confidence",
        "degraded", "disclaimer", "session_id", "message_id",
    }  # fmt: skip
    assert "correlation_id" not in body and "tokens" not in body and "audit" not in body
    rows = stored(chat_env, body["session_id"])
    assert [r[0] for r in rows] == ["user", "assistant"] and rows[1][2] == 1
    owner = chat_env.rows("SELECT user_id FROM sessions WHERE id = ?", (body["session_id"],))
    assert owner == [(user_id,)]


def test_a_chat_with_a_session_id_resumes_that_session(chat_env):
    _, token = chat_env.user_token("asha_rao")
    first = chat_env.chat(token).json()
    second = chat_env.chat(token, QUESTION + " again", session_id=first["session_id"])
    assert second.status_code == 200 and second.json()["session_id"] == first["session_id"]
    assert len(stored(chat_env, first["session_id"])) == 4


def test_the_answer_text_is_plain_text_with_no_markup_and_no_link(tmp_path, fast_hashing):
    from tests.pipeline_rig import FakeGateway

    gateway = FakeGateway()
    gateway.answer_reply = {
        "answer": "A waiting period of 30 days applies to illness claims. "
        "<b>See</b> <a href='http://evil.example/x'>this page</a> or https://evil.example/y now.",
        "citations": [{"chunk_id": "S1"}],
    }
    env = ChatEnv(tmp_path, gateway=gateway)
    try:
        _, token = env.user_token("asha_rao")
        text = env.chat(token).json()["text"]
        assert "<" not in text and ">" not in text and "evil.example" not in text
        assert "http" not in text.lower()
    finally:
        env.close()


# ---- how the pipeline's result kinds become responses ----------
def test_while_the_masker_loads_the_answer_is_503_starting_up_with_a_retry_time(chat_env):
    gate = threading.Event()
    holder = MaskerHolder(lambda: (gate.wait(10), masker_holder().masker)[1])
    holder.start()
    chat_env.rig.deps.masker = holder
    try:
        _, token = chat_env.user_token("asha_rao")
        response = chat_env.chat(token)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "starting_up"
        assert response.headers["Retry-After"] == "5"
        assert chat_env.rig.gateway.model_calls() == 0
    finally:
        gate.set()
        holder.wait(10)


def test_a_failed_dependency_is_503_service_unavailable_with_the_fixed_text(chat_env):
    from app.gateway.errors import EmbeddingUnavailable

    chat_env.rig.gateway.embed_error = EmbeddingUnavailable([("gemini", "rate_limited")])
    _, token = chat_env.user_token("asha_rao")
    response = chat_env.chat(token)
    assert response.status_code == 503
    error = response.json()["error"]
    assert (
        error["code"] == "service_unavailable" and error["message"] == pipeline_messages.UNAVAILABLE
    )


def test_a_second_question_in_a_busy_session_is_409(chat_env):
    user_id, token = chat_env.user_token("asha_rao")
    first = chat_env.chat(token).json()
    assert chat_env.rig.deps.locks.try_acquire(user_id, first["session_id"])  # a turn is running
    try:
        response = chat_env.chat(token, QUESTION, session_id=first["session_id"])
    finally:
        chat_env.rig.deps.locks.release(user_id, first["session_id"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "busy"


def test_over_the_daily_budget_is_429_and_no_model_is_called(chat_env):
    _, token = chat_env.user_token("asha_rao")
    chat_env.limits.check_budget = lambda *a, **k: SimpleNamespace(allowed=False)
    response = chat_env.chat(token)
    assert response.status_code == 429 and response.json()["error"]["code"] == "over_budget"
    assert chat_env.rig.gateway.model_calls() == 0


def test_a_chat_is_counted_once_against_the_per_user_limit(chat_env):
    """The route is not metered by the API's dependency; the pipeline's first stage counts it."""
    _, token = chat_env.user_token("asha_rao")
    calls = []
    real = chat_env.limits.check_rate

    def counting(user_id):
        calls.append(user_id)
        return real(user_id)

    chat_env.limits.check_rate = counting
    assert chat_env.chat(token).status_code == 200
    assert len(calls) == 1


def test_the_21st_chat_in_a_minute_is_429_with_a_retry_time(chat_env):
    _, token = chat_env.user_token("asha_rao")
    for number in range(20):
        response = chat_env.chat(token, f"{QUESTION} number {number}")
        assert response.status_code == 200, (number, response.text)
    response = chat_env.chat(token, QUESTION + " one too many")
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_requests"
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.parametrize(
    "session_id",
    ["00000000-0000-4000-8000-000000000000", "not-a-session", "../../etc/passwd", "x" * 64],
)
def test_a_missing_or_malformed_session_is_the_same_404(chat_env, session_id):
    _, token = chat_env.user_token("asha_rao")
    response = chat_env.chat(token, session_id=session_id)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not found."}}
    assert chat_env.rig.gateway.model_calls() == 0


def test_a_malformed_session_id_never_reaches_the_pipeline(chat_env, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("a malformed session id must be refused before the pipeline")

    monkeypatch.setattr(chat_env.rig.pipeline, "answer", forbidden)
    _, token = chat_env.user_token("asha_rao")
    response = chat_env.chat(token, session_id="not-a-uuid")
    assert response.status_code == 404


def test_someone_elses_session_is_the_same_404_and_is_left_untouched(chat_env):
    _, owner = chat_env.user_token("asha_rao")
    _, other = chat_env.user_token("ravi_k")
    session_id = chat_env.chat(owner).json()["session_id"]
    before = stored(chat_env, session_id)
    response = chat_env.chat(other, "Tell me more", session_id=session_id)
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "Not found."}}
    assert stored(chat_env, session_id) == before


def test_a_chat_needs_a_signed_in_user(chat_env):
    response = chat_env.client.post("/chat", json={"message": QUESTION})
    assert response.status_code == 401
    assert chat_env.rig.gateway.model_calls() == 0


def test_the_body_is_checked_and_an_unknown_field_is_refused(chat_env):
    _, token = chat_env.user_token("asha_rao")
    for body in (
        {},
        {"message": ""},
        {"message": "x" * 8001},
        {"message": QUESTION, "role": "admin"},
        {"message": QUESTION, "session_id": 5},
    ):
        response = chat_env.client.post("/chat", json=body, headers=chat_env.bearer(token))
        assert response.status_code == 422, body
    assert chat_env.rig.gateway.model_calls() == 0


def test_without_a_pipeline_chat_is_503(tmp_path, fast_hashing):
    env = ChatEnv(tmp_path, pipeline=False)
    try:
        _, token = env.user_token("asha_rao")
        response = env.chat(token)
        assert response.status_code == 503
    finally:
        env.close()


# ---- the rollout stage: shadow mode ----------
@pytest.mark.parametrize(
    ("stage", "role", "visible"),
    [
        ("shadow", "customer", False),
        ("shadow", "staff", False),
        ("shadow", "admin", True),
        ("staff_beta", "customer", False),
        ("staff_beta", "staff", True),
        ("staff_beta", "admin", True),
        ("open", "customer", True),
        ("open", "staff", True),
        ("open", "admin", True),
    ],
)
def test_who_sees_answers_at_each_stage(chat_env, stage, role, visible):
    chat_env.set_stage_in_database(stage)
    _, token = chat_env.user_token("asha_rao", role)
    body = chat_env.chat(token).json()
    assert body["answer_visible"] is visible
    if not visible:
        # the fixed notice and nothing else: no answer text, citations, scores or reason
        assert set(body) == SHADOW_KEYS
        assert body["kind"] == "shadow_notice" and body["text"] == SHADOW_NOTICE
        assert "30 days" not in str(body)
    else:
        assert "30 days" in body["text"] and body["citations"]
    # the turn was generated and stored either way, and flagged when hidden
    assert chat_env.rig.gateway.calls_of("complete", what="answer")
    rows = stored(chat_env, body["session_id"])
    assert rows[1][1].startswith("A waiting period of 30 days")  # the real, masked answer
    assert bool(rows[1][3]) is (not visible)


def test_a_missing_stage_row_means_shadow(tmp_path, fast_hashing):
    env = ChatEnv(tmp_path, stage=None)
    try:
        _, token = env.user_token("asha_rao")
        assert env.chat(token).json()["answer_visible"] is False
    finally:
        env.close()


def test_a_stage_that_cannot_be_read_fails_closed_to_shadow(chat_env):
    chat_env.execute("DROP TABLE app_settings")
    _, token = chat_env.user_token("asha_rao")
    body = chat_env.chat(token).json()
    assert body["answer_visible"] is False and body["text"] == SHADOW_NOTICE
    assert any(name == "rollout_stage_unreadable" for name, _ in chat_env.app.state.services.events)


def test_an_unknown_stage_value_is_shadow(chat_env):
    chat_env.set_stage_in_database("everyone")
    _, token = chat_env.user_token("asha_rao")
    assert chat_env.chat(token).json()["answer_visible"] is False


def test_the_stage_is_cached_for_a_few_seconds_and_a_change_through_the_api_is_at_once(
    tmp_path, fast_hashing
):
    env = ChatEnv(tmp_path, stage="shadow", rollout_cache_seconds=60.0)
    try:
        _, customer = env.user_token("asha_rao")
        _, admin = env.user_token("boss", "admin")
        assert env.chat(customer).json()["answer_visible"] is False
        env.set_stage_in_database("open")  # changed behind the cache: not seen yet
        assert env.chat(customer, QUESTION + " b").json()["answer_visible"] is False
        response = env.client.put(
            "/admin/shadow", json={"stage": "open"}, headers=env.bearer(admin)
        )
        assert response.status_code == 200
        assert env.chat(customer, QUESTION + " c").json()["answer_visible"] is True
    finally:
        env.close()


# ---- the connection stays open while a timed-out write finishes ----------
def test_the_connection_is_closed_at_once_when_no_write_is_running():
    closed = []
    db = SimpleNamespace(close=lambda: closed.append(1))
    calls = []
    result = SimpleNamespace(db_busy=False, on_db_idle=lambda fn: calls.append(fn))
    chat_module._close_when_idle(db, result)
    assert closed == [1] and calls == []


def test_the_connection_stays_open_until_the_running_write_has_ended():
    closed, waiting = [], []
    db = SimpleNamespace(close=lambda: closed.append(1))
    result = SimpleNamespace(db_busy=True, on_db_idle=lambda fn: waiting.append(fn))
    chat_module._close_when_idle(db, result)
    assert closed == [] and len(waiting) == 1  # not closed yet
    waiting[0]()  # the write ended
    assert closed == [1]


def test_a_chat_whose_write_timed_out_keeps_its_connection_until_the_write_ends(
    chat_env, monkeypatch
):
    from app.memory import store

    gate = threading.Event()
    real = store.add_user_message

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_user_message", slow)
    chat_env.rig.deps.config = PipelineConfig(
        node_limits=dict(NODE_LIMITS) | {"persist": (0.3, 0.1)}
    )
    closes = []
    real_close = LazyDatabase.close

    def recording_close(self):
        closes.append(time.monotonic())
        real_close(self)

    monkeypatch.setattr(LazyDatabase, "close", recording_close)
    _, token = chat_env.user_token("asha_rao")
    started = len(closes)
    response = chat_env.chat(token)
    assert response.status_code == 200  # the user still gets the answer
    during = len(closes) - started
    gate.set()
    for _ in range(100):  # the write finishes, then the connection is closed
        if len(closes) > started + during:
            break
        time.sleep(0.05)
    assert len(closes) > started + during  # one more close happened AFTER the write ended


def test_the_answer_carries_the_id_of_its_stored_assistant_message(chat_env):
    _, token = chat_env.user_token("asha_rao")
    body = chat_env.chat(token).json()
    stored_ids = chat_env.rows(
        "SELECT id FROM messages WHERE session_id = ? AND role = 'assistant'", (body["session_id"],)
    )
    assert stored_ids == [(body["message_id"],)]
    # and it is the id feedback takes
    rated = chat_env.client.post(
        f"/messages/{body['message_id']}/feedback",
        json={"rating": "up"},
        headers=chat_env.bearer(token),
    )
    assert rated.status_code == 200


def test_a_hidden_answer_carries_no_message_id(chat_env):
    chat_env.set_stage_in_database("shadow")
    _, token = chat_env.user_token("asha_rao")
    body = chat_env.chat(token).json()
    assert set(body) == SHADOW_KEYS  # no message id for an answer the user may not see


def test_the_message_id_is_null_while_the_transcript_write_has_not_ended(chat_env, monkeypatch):
    from app.memory import store

    gate = threading.Event()
    real = store.add_user_message

    def slow(*args, **kwargs):
        gate.wait(10)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_user_message", slow)
    chat_env.rig.deps.config = PipelineConfig(
        node_limits=dict(NODE_LIMITS) | {"persist": (0.3, 0.1)}
    )
    _, token = chat_env.user_token("asha_rao")
    response = chat_env.chat(token)
    gate.set()
    assert response.status_code == 200
    assert response.json()["message_id"] is None  # queued: the frontend disables thumbs


# ---- the start-up check ----------
def test_the_start_up_check_runs_once_and_warns_with_counts(chat_env):
    ids = list(chat_env.rig.rig.points("doc-a"))
    chat_env.rig.rig.qdrant.set_payload(COLLECTION, payload={"sparse_version": 0}, points=ids[:1])
    with TestClient(chat_env.app, raise_server_exceptions=False):
        pass  # the lifespan ran the check
    events = [(n, f) for n, f in chat_env.app.state.services.events]
    assert ("sparse_version_mismatch", {"out_of_date": 1, "total": len(ids)}) in events


def test_the_start_up_check_is_quiet_when_every_chunk_is_current(chat_env):
    with TestClient(chat_env.app, raise_server_exceptions=False):
        pass
    names = [n for n, _ in chat_env.app.state.services.events]
    assert "sparse_version_mismatch" not in names and "sparse_check_unavailable" not in names


# ---- on resume ----------
def test_a_hidden_answer_stays_hidden_from_a_non_admin_after_the_stage_opens(chat_env):
    chat_env.set_stage_in_database("shadow")
    _, customer = chat_env.user_token("asha_rao")
    _, admin = chat_env.user_token("boss", "admin")
    session_id = chat_env.chat(customer).json()["session_id"]
    chat_env.set_stage_in_database("open")
    chat_env.chat(customer, QUESTION + " again", session_id=session_id)
    resumed = chat_env.client.get(f"/sessions/{session_id}", headers=chat_env.bearer(customer))
    answers = [m for m in resumed.json()["messages"] if m["role"] == "assistant"]
    assert answers[0]["text"] == SHADOW_NOTICE and answers[0]["citations"] == []
    assert answers[0]["notice"] == "shadowed" and answers[0]["grounded"] is None
    assert "30 days" in answers[1]["text"]  # the one generated after the stage opened
    # the owner of the session is the customer: an admin has no right to it here
    assert (
        chat_env.client.get(f"/sessions/{session_id}", headers=chat_env.bearer(admin)).status_code
        == 404
    )


def test_a_removed_source_shows_a_notice_instead_of_the_answer(chat_env):
    _, token = chat_env.user_token("asha_rao")
    session_id = chat_env.chat(token).json()["session_id"]
    chat_env.execute(
        "UPDATE messages SET source_removed = 1 WHERE session_id = ? AND role = 'assistant'",
        (session_id,),
    )
    resumed = chat_env.client.get(f"/sessions/{session_id}", headers=chat_env.bearer(token)).json()
    answer = [m for m in resumed["messages"] if m["role"] == "assistant"][0]
    assert answer["text"] == WITHDRAWN_NOTICE and answer["citations"] == []
    assert answer["source_removed"] is True and answer["notice"] == "source_removed"
    assert "30 days" not in str(resumed)
