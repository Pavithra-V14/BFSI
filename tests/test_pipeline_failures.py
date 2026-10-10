"""Mocked failures, time budgets, the per-session lock and the start-up rules (spec 4, 11.1, 16)."""

import asyncio
import threading
import time
from dataclasses import replace

import httpx
import pytest
import redis

from app.accounts.budget import BudgetDecision
from app.cache.store import RateDecision
from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from app.guardrails.scan import StoredQuery
from app.memory import store
from app.pipeline import messages
from app.pipeline.config import NODE_LIMITS, PipelineConfig
from app.pipeline.persist import PersistQueue
from app.pipeline.startup import MaskerHolder
from app.pipeline.state import Request
from app.pipeline.trace import Span
from app.store_errors import StoreRejected
from tests.pipeline_rig import PipelineRig, masker_holder

QUESTION = "What is the waiting period for illness claims?"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def rig(tmp_path):
    made = PipelineRig(tmp_path)
    yield made
    made.close()


def stages(rig):
    return [s.stage for s in rig.spans]


def unavailable(result, code=None):
    assert result.kind == "unavailable" and result.text == messages.UNAVAILABLE
    if code:
        assert result.reason == code


def nothing_stored(rig):
    assert rig.db.query_one("SELECT COUNT(*) FROM messages")[0] == 0
    assert rig.db.query_one("SELECT COUNT(*) FROM sessions")[0] == 0
    assert len(rig.deps.queue) == 0


# ---- the stores and providers failing
# -----------------------------------------------------------------
def test_qdrant_down_is_temporarily_unavailable_with_no_ungrounded_answer(rig):
    class Down:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise httpx.ConnectError("connection refused")

            return fail

    rig.deps.qdrant = Down()
    result = run(rig.ask(QUESTION))
    unavailable(result, "vector_store_unavailable")
    assert rig.gateway.calls_of("complete", what="answer") == []  # never a guess from memory
    nothing_stored(rig)


def test_a_qdrant_that_refuses_us_is_a_settings_error_not_an_outage(rig):
    class Refuses:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise StoreRejected("vector store", "settings")

            return fail

    rig.deps.qdrant = Refuses()
    result = run(rig.ask(QUESTION))
    unavailable(result, "vector_store_rejected")
    assert "vector_store_rejected" in rig.event_names


def test_upstash_down_skips_the_cache_and_answers_normally(rig):
    class Down:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise redis.exceptions.ConnectionError("down")

            return fail

    rig.rig.cache._client = Down()
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))
    second = run(rig.ask(QUESTION, user=user))
    assert first.kind == second.kind == "answer" and not first.cached and not second.cached
    span = next(s for s in rig.spans if s.stage == "cache_lookup")
    assert span.extra["cache"] == "unavailable"
    assert len(rig.gateway.calls_of("complete", what="answer")) == 2


def test_both_embedding_keys_failing_is_temporarily_unavailable(rig):
    rig.gateway.embed_error = EmbeddingUnavailable([("gemini", "rate_limited")])
    result = run(rig.ask(QUESTION))
    unavailable(result, "embedding_unavailable")
    assert rig.gateway.calls_of("rerank") == [] and rig.gateway.calls_of("complete") == []
    nothing_stored(rig)


def test_all_answer_providers_failing_is_temporarily_unavailable(rig):
    rig.gateway.complete_error = AllProvidersFailed([("groq", "rate_limited")])
    unavailable(run(rig.ask(QUESTION)), "providers_unavailable")


@pytest.mark.parametrize("reply", ["this is not json", {"citations": []}, {"answer": 5}, "[1, 2]"])
def test_malformed_generator_output_gets_one_retry_and_then_unavailable(rig, reply):
    rig.gateway.answer_reply = reply
    result = run(rig.ask(QUESTION))
    unavailable(result, "unreadable_answer")
    assert len(rig.gateway.calls_of("complete", what="answer")) == 2  # one retry only


def test_a_valid_second_try_is_used(rig):
    rig.gateway.answer_reply = [
        "not json",
        {
            "answer": "A waiting period of 30 days applies to illness claims.",
            "citations": [{"chunk_id": "S1"}],
        },  # fmt: skip
    ]
    result = run(rig.ask(QUESTION))
    assert result.kind == "answer" and len(rig.gateway.calls_of("complete", what="answer")) == 2


def test_the_retry_is_not_started_when_there_is_no_time_for_it(rig):
    rig.gateway.answer_reply = "not json"
    rig.gateway.delay["answer"] = 0.5
    limits = {name: (limit, 0.05) for name, (limit, _) in NODE_LIMITS.items()}
    limits["generate"] = (12.0, 0.6)  # a call (or its retry) needs 0.6 s left to be started
    rig.deps.config = PipelineConfig(budget_seconds=1.0, node_limits=limits)
    result = run(rig.ask(QUESTION))
    unavailable(result)
    assert len(rig.gateway.calls_of("complete", what="answer")) == 1  # no room for a second call


def test_a_failed_rerank_or_a_slow_one_falls_back_to_lexical_and_the_request_goes_on(rig):
    rig.gateway.rerank_error = RuntimeError("rerank service down")
    failed = run(rig.ask(QUESTION))
    assert failed.kind == "answer" and "lexical_rerank" in failed.degraded
    assert messages.DEGRADED_RERANK in failed.warnings
    rig.gateway.rerank_error = None
    rig.gateway.delay["rerank"] = 5
    limits = dict(NODE_LIMITS) | {"rerank": (0.2, 0.1)}
    rig.deps.config = PipelineConfig(node_limits=limits)
    slow = run(rig.ask(QUESTION + " twice"))
    assert slow.kind == "answer" and "lexical_rerank" in slow.degraded


def test_a_failed_or_slow_triage_applies_the_safe_default_and_goes_on(rig):
    rig.gateway.delay["decide"] = 5
    limits = dict(NODE_LIMITS) | {"triage": (0.2, 0.1)}
    rig.deps.config = PipelineConfig(node_limits=limits)
    result = run(rig.ask(QUESTION))
    assert result.kind == "answer"
    assert next(s for s in rig.spans if s.stage == "triage").status == "timeout"


# ---- time
# ---------------------------------------------------------------------------------------------
def test_each_node_gets_the_smaller_of_its_timeout_and_the_time_left(rig):
    rig.deps.config = PipelineConfig(budget_seconds=3.0)
    run(rig.ask(QUESTION))
    decide_timeout = rig.gateway.calls_of("decide")[0]["timeout_s"]
    embed_timeout = rig.gateway.calls_of("embed")[0]["timeout_s"]
    assert 2.0 < decide_timeout <= 3.0  # the 8 s node limit is cut to the time left
    assert embed_timeout <= decide_timeout and embed_timeout <= 3.0
    rig.deps.config = PipelineConfig(budget_seconds=30.0)
    run(rig.ask(QUESTION + " once more"))
    assert rig.gateway.calls_of("decide")[1]["timeout_s"] == pytest.approx(8.0, abs=0.01)
    assert rig.gateway.calls_of("complete", what="answer")[-1]["timeout_s"] <= 12.0


def test_a_call_that_cannot_finish_is_never_started(rig):
    rig.gateway.delay["decide"] = 0.8  # triage eats most of a 1 s budget
    rig.deps.config = PipelineConfig(budget_seconds=1.0)
    result = run(rig.ask(QUESTION))
    unavailable(result, "no_time")
    assert rig.gateway.calls_of("embed") == []  # embed needs 1.5 s and only ~0.2 s were left
    assert rig.gateway.calls_of("complete") == []
    assert result.kind == "unavailable"


def test_a_node_that_runs_out_its_own_timeout_fails_that_stage_only(rig):
    rig.gateway.delay["embed"] = 5
    limits = dict(NODE_LIMITS) | {"embed": (0.2, 0.1)}
    rig.deps.config = PipelineConfig(node_limits=limits)
    result = run(rig.ask(QUESTION))
    unavailable(result, "timeout")
    span = next(s for s in rig.spans if s.stage == "embed")
    assert span.status == "timeout" and span.error_code == "timeout"


def test_the_whole_request_cannot_run_past_the_budget(rig):
    rig.gateway.delay["answer"] = 5
    limits = dict(NODE_LIMITS) | {"generate": (30.0, 0.1)}
    rig.deps.config = PipelineConfig(budget_seconds=0.6, node_limits=limits)
    started = time.perf_counter()
    result = run(rig.ask(QUESTION))
    assert time.perf_counter() - started < 3.0
    unavailable(result)


# ---- Postgres down: the queue
# ----------------------------------------------------------------------------
def test_when_postgres_fails_the_answer_is_returned_and_the_masked_turn_is_queued(rig, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(store, "create_session", boom)
    user = rig.new_user()
    result = run(rig.ask("My name is Rahul Sharma. " + QUESTION, user=user))
    assert result.kind == "answer" and result.session_id is None
    assert "persist_queued" in rig.event_names and len(rig.deps.queue) == 1
    job = rig.deps.queue._jobs[0]
    assert type(job.question) is StoredQuery
    assert "Rahul" not in repr(job) and "[PERSON]" in job.question.text  # pass-2 masked text only
    monkeypatch.undo()  # Postgres is back: the next request writes the queued turn first
    run(rig.ask(QUESTION + " again", user=user))
    assert len(rig.deps.queue) == 0
    texts = [r[0] for r in rig.db.query_all("SELECT content FROM messages WHERE role = 'user'")]
    assert any("[PERSON]" in t for t in texts) and not any("Rahul" in t for t in texts)


def test_the_queue_is_bounded_and_says_so_when_it_drops_the_oldest():
    queue = PersistQueue(size=2)
    assert [queue.add(object()) for _ in range(3)] == [False, False, True]
    assert len(queue) == 2


def test_a_queue_flush_stops_at_the_first_failure_and_keeps_the_rest(rig, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(store, "create_session", boom)
    for n in range(2):
        run(rig.ask(QUESTION + f" number {n}"))
    assert len(rig.deps.queue) == 2
    assert rig.deps.queue.flush(rig.db, rig.deps.now()) == 0 and len(rig.deps.queue) == 2


def test_a_queue_write_is_idempotent_so_a_retry_never_duplicates(rig, monkeypatch):
    calls = {"n": 0}
    real = store.add_assistant_message

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "add_assistant_message", fail_once)
    run(rig.ask(QUESTION))
    assert len(rig.deps.queue) == 1
    rig.deps.queue.flush(rig.db, rig.deps.now())
    assert len(rig.deps.queue) == 0
    assert rig.db.query_one("SELECT COUNT(*) FROM messages WHERE role = 'user'")[0] == 1
    assert rig.db.query_one("SELECT COUNT(*) FROM messages WHERE role = 'assistant'")[0] == 1


# ---- stage 1
# ----------------------------------------------------------------------------------------------
class StubUsage:
    def __init__(self, rate_ok=True, budget_ok=True):
        self.rate_ok, self.budget_ok = rate_ok, budget_ok
        self.charged: list[tuple[str, int]] = []
        self.checked: list[tuple] = []

    def check_rate(self, user_id):
        self.checked.append(("rate", user_id))
        return RateDecision(self.rate_ok, 99, 20, None if self.rate_ok else 17)

    def check_budget(self, user_id, role, estimated_tokens=0):
        self.checked.append(("budget", role, estimated_tokens))
        return BudgetDecision(self.budget_ok, 1, 50_000)

    def charge(self, user_id, tokens):
        self.charged.append((user_id, tokens))
        return tokens


def test_over_the_rate_limit_is_refused_with_a_retry_time_before_any_model(rig):
    rig.deps.usage = StubUsage(rate_ok=False)
    result = run(rig.ask(QUESTION))
    assert (result.kind, result.retry_after, result.text) == (
        "rate_limited",
        17,
        messages.RATE_LIMITED,
    )
    assert rig.gateway.model_calls() == 0 and stages(rig) == ["authenticate", "respond"]
    nothing_stored(rig)


def test_over_the_daily_budget_is_refused_before_any_model_and_the_estimate_is_checked(rig):
    rig.deps.usage = StubUsage(budget_ok=False)
    result = run(rig.ask(QUESTION, role="staff"))
    assert result.kind == "over_budget" and rig.gateway.model_calls() == 0
    assert ("budget", "staff", 3_400) in rig.deps.usage.checked


def test_the_tokens_a_request_really_used_are_charged_to_the_user(rig):
    rig.deps.usage = StubUsage()
    user = rig.new_user()
    result = run(rig.ask(QUESTION, user=user))
    assert rig.deps.usage.charged == [(user, result.tokens)] and result.tokens > 100


def test_a_refusal_that_used_no_model_charges_nothing(rig):
    rig.deps.usage = StubUsage()
    run(rig.ask("Hello!"))
    assert rig.deps.usage.charged == []


# ---- the masker: start-up and fail closed
# ---------------------------------------------------------------------------
def test_until_the_masker_is_ready_the_answer_is_starting_up_and_nothing_runs(rig):
    gate = threading.Event()
    holder = MaskerHolder(lambda: (gate.wait(10), masker_holder().masker)[1])
    holder.start()
    rig.deps.masker = holder
    result = run(rig.ask(QUESTION))
    assert (result.kind, result.text) == ("starting_up", messages.STARTING_UP)
    assert rig.gateway.model_calls() == 0
    nothing_stored(rig)
    gate.set()
    assert holder.wait(10) and holder.ready
    assert run(rig.ask(QUESTION)).kind == "answer"  # and once it is ready, it answers


def test_a_masker_that_failed_to_load_means_unavailable_never_unmasked(rig):
    def broken():
        raise RuntimeError("model file missing")

    events = []
    holder = MaskerHolder(broken, on_event=lambda n, f: events.append((n, f)))
    holder.start()
    holder._thread.join(5)
    rig.deps.masker = holder
    unavailable(run(rig.ask(QUESTION)), "masker_failed")
    assert holder.failed and events == [("masker_load_failed", {"reason": "RuntimeError"})]
    assert rig.gateway.model_calls() == 0


def test_a_masker_that_raises_refuses_the_question_and_stores_nothing(rig, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("masking engine crashed")

    monkeypatch.setattr(rig.deps.masker.masker, "mask", boom)
    result = run(rig.ask(QUESTION))
    unavailable(result, "masker_failed")
    assert rig.gateway.model_calls() == 0
    nothing_stored(rig)


def test_a_masker_that_fails_when_making_the_stored_copy_stores_nothing(rig, monkeypatch):
    real = rig.deps.masker.masker.mask
    calls = {"n": 0}

    def fail_second(text, allow=()):
        calls["n"] += 1
        if calls["n"] >= 2:  # the local scan worked; the pass-2 storage copy fails
            raise RuntimeError("crash")
        return real(text, allow)

    monkeypatch.setattr(rig.deps.masker.masker, "mask", fail_second)
    result = run(rig.ask(QUESTION))
    assert result.kind == "answer"  # the user still gets the answer
    assert "persist_masking_failed" in rig.event_names
    nothing_stored(rig)  # but nothing unmasked is stored or queued


# ---- sessions: ownership and the per-session lock
# ------------------------------------------------------------------
def test_someone_elses_session_looks_exactly_like_a_missing_one(rig):
    owner = rig.new_user()
    first = run(rig.ask(QUESTION, user=owner))
    intruder = rig.new_user()
    theirs = run(rig.ask(QUESTION, user=intruder, session_id=first.session_id))
    import uuid

    missing = run(rig.ask(QUESTION, user=intruder, session_id=str(uuid.uuid4())))
    for result in (theirs, missing):
        assert (result.kind, result.text, result.reason) == (
            "session_not_found",
            messages.SESSION_NOT_FOUND,
            "not_found",
        )
    assert rig.deps.locks.held() == 0
    assert len(store.load_session(rig.db, owner, first.session_id).messages) == 2  # untouched


def test_a_second_question_in_a_session_that_is_being_answered_is_refused_as_busy(rig):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))
    rig.gateway.delay["decide"] = 0.3

    async def two():
        slow = asyncio.create_task(rig.ask(QUESTION, user=user, session_id=first.session_id))
        await asyncio.sleep(0.1)
        assert rig.deps.locks.held() == 1
        busy = await rig.ask("Another question here", user=user, session_id=first.session_id)
        return await slow, busy

    slow, busy = run(two())
    assert slow.kind == "answer" and (busy.kind, busy.text) == ("busy", messages.BUSY)
    assert rig.deps.locks.held() == 0
    assert (
        run(rig.ask(QUESTION + " later", user=user, session_id=first.session_id)).kind == "answer"
    )


def test_the_lock_is_released_after_a_normal_answer_a_refusal_and_a_missing_session(rig):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))
    assert rig.deps.locks.held() == 0
    run(rig.ask("Hello!", user=user, session_id=first.session_id))
    assert rig.deps.locks.held() == 0


def test_the_lock_is_released_after_a_timeout(rig):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))
    rig.gateway.delay["answer"] = 5
    limits = dict(NODE_LIMITS) | {"generate": (30.0, 0.1)}
    rig.deps.config = PipelineConfig(budget_seconds=0.6, node_limits=limits)
    result = run(rig.ask(QUESTION + " slow", user=user, session_id=first.session_id))
    unavailable(result)
    assert rig.deps.locks.held() == 0


def test_the_lock_is_released_after_a_cancellation(rig):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))
    rig.gateway.delay["decide"] = 5

    async def cancelled():
        task = asyncio.create_task(rig.ask(QUESTION, user=user, session_id=first.session_id))
        await asyncio.sleep(0.15)
        assert rig.deps.locks.held() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return rig.deps.locks.held()

    assert run(cancelled()) == 0


def test_the_lock_is_released_after_an_unexpected_error_and_the_user_gets_a_fixed_answer(rig):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))

    async def explode(*args, **kwargs):
        raise RuntimeError("a bug inside the graph with secret words")

    rig.pipeline.graph.ainvoke = explode
    result = run(rig.ask(QUESTION, user=user, session_id=first.session_id))
    unavailable(result, "RuntimeError")
    assert "secret" not in repr(result) and "graph_failed" in rig.event_names
    assert rig.deps.locks.held() == 0


def test_a_node_that_raises_is_a_stage_error_with_a_code_and_the_lock_is_released(rig, monkeypatch):
    user = rig.new_user()
    first = run(rig.ask(QUESTION, user=user))

    def boom(*args, **kwargs):
        raise ValueError("bad value with secret words")

    monkeypatch.setattr("app.pipeline.answer.build_context_block", boom)
    result = run(rig.ask(QUESTION + " more", user=user, session_id=first.session_id))
    unavailable(result, "ValueError")
    assert rig.deps.locks.held() == 0
    failed = [s for s in rig.spans if s.status == "error"]
    assert failed and all("secret" not in repr(s) for s in failed)


def test_a_new_session_takes_no_lock(rig):
    run(rig.ask(QUESTION))
    assert rig.deps.locks.held() == 0


def test_a_request_with_a_correlation_id_returns_it_and_spans_carry_no_text(rig):
    request = Request(
        user_id=rig.new_user(), role="customer", query=QUESTION, correlation_id="abc123"
    )
    result = run(rig.pipeline.answer(request, rig.db))
    assert result.correlation_id == "abc123"
    for span in rig.spans:
        assert isinstance(span, Span) and "waiting period" not in repr(span).lower()
    assert replace(rig.deps.config).budget_seconds == 30.0
