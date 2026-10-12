"""Chaos tests of the gateway (spec 16: "providers forced to fail; breakers trip and recover").

Every test drives a whole schedule through the real gateway on a fake clock and fake providers:
nothing sleeps, nothing touches a network or a key. What is checked is the behaviour over time:
a breaker trips only when the pool is really failing, it is not tripped by slowness alone, it
admits one probe after its open period and closes again on success, a 429 with Retry-After keeps
that key out until the time is over, and the caller's budget is never exceeded."""

import httpx
import pytest

from app.gateway.breaker import CLOSED, HALF_OPEN, OPEN
from app.gateway.config import BREAKER_OPEN_SECONDS, RATE_LIMIT_FLOOR_SECONDS
from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from tests.helpers import (
    MESSAGES,
    FakeBackend,
    FakeClock,
    FakeDecisionClient,
    fake_key,
    lit,
    make_config,
    make_gateway,
    ok,
    run,
)
from tests.test_gateway_decisions import QUESTIONS, STATE, chat_answer, jev_response
from tests.test_gateway_embed_rerank import DOCS, cohere_json, transport
from tests.test_gateway_round3 import rate_limited

pytestmark = pytest.mark.chaos

KEYS = [fake_key("groq", i) for i in range(1, 5)]


def provider_of(model: str) -> str:
    return model.split("/")[0]


class TimedBackend(FakeBackend):
    """A provider that takes `seconds(call_number)` per call on the fake clock, and is cut at the
    timeout the gateway asked for (the way a real client is): a call that would take longer than
    its timeout spends exactly the timeout and fails with a timeout error."""

    def __init__(self, clock, seconds, handler=None):
        super().__init__(handler)
        self.clock = clock
        self.seconds = seconds
        self.starts: list[float] = []

    async def complete(self, *, model, messages, api_key, max_tokens, timeout, **kwargs):
        number = len(self.calls) + 1
        self.starts.append(self.clock.now())
        wanted = self.seconds(number) if callable(self.seconds) else self.seconds
        if wanted > timeout:
            self.clock.advance(timeout)
            self.calls.append((model, api_key))
            self.timeouts.append(timeout)
            raise lit("Timeout")
        self.clock.advance(wanted)
        return await super().complete(
            model=model,
            messages=messages,
            api_key=api_key,
            max_tokens=max_tokens,
            timeout=timeout,
            **kwargs,
        )


def states(gateway, tier="answer"):
    return {name: gateway._breaker(name, tier).state for name in ("groq", "gemini", "openrouter")}


# ==== every provider fails, then recovers ====
def test_every_provider_failing_trips_every_breaker_and_the_open_period_heals_it():
    clock = FakeClock()
    failing = [True]
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if failing[0] else ok("back again"))
    gateway = make_gateway(backend=backend, clock=clock)
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))
    assert set(states(gateway).values()) == {OPEN}
    asked = len(backend.calls)

    failing[0] = False  # the providers are back, but the breakers have not been told yet
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))
    assert len(backend.calls) == asked, "an open breaker makes no call at all"

    clock.advance(BREAKER_OPEN_SECONDS + 31)  # the open period and the key cooldowns are over
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and result.text == "back again"
    assert states(gateway)["groq"] == CLOSED


def test_a_failed_probe_opens_the_breaker_again_and_admits_one_probe_only():
    clock = FakeClock()
    backend = FakeBackend(lambda m, k, n: lit("Timeout"))
    gateway = make_gateway(backend=backend, clock=clock)
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))
    clock.advance(BREAKER_OPEN_SECONDS + 31)
    assert states(gateway)["groq"] == HALF_OPEN
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))  # the probe goes out and fails
    assert states(gateway)["groq"] == OPEN
    before = len(backend.calls)
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))
    assert len(backend.calls) == before, "no second probe until the next open period"


def test_a_pool_that_recovers_one_provider_at_a_time_answers_from_the_first_that_is_back():
    clock = FakeClock()
    healthy = {"groq": False, "gemini": False, "openrouter": False}
    backend = FakeBackend(
        lambda m, k, n: ok(f"from {provider_of(m)}") if healthy[provider_of(m)] else lit("Timeout")
    )
    gateway = make_gateway(backend=backend, clock=clock)
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES))
    clock.advance(BREAKER_OPEN_SECONDS + 31)
    healthy["openrouter"] = True  # only the last one is back
    assert run(gateway.complete(MESSAGES)).decider == "openrouter"
    healthy["gemini"] = True
    clock.advance(BREAKER_OPEN_SECONDS + 31)
    # groq is probed first and fails again; the call still ends on a provider that works
    assert run(gateway.complete(MESSAGES)).decider == "gemini"


# ===================================================================== slow but successful
# The per-call timeout is a setting (GATEWAY_TIMEOUT_SECONDS, default 10 s, spec 5): a call that
# is slow but ANSWERS within it is a success and never counts against the provider; a call that
# runs past it is cut and counts as a timeout failure, like any other.
def test_a_call_under_the_default_per_call_timeout_never_trips_the_breaker():
    clock = FakeClock()
    backend = TimedBackend(clock, 8.0)  # slow, but inside the 10 s default
    gateway = make_gateway(backend=backend, clock=clock)
    for _ in range(6):
        result = run(gateway.complete(MESSAGES, timeout_s=30.0))
        assert result.decider == "groq" and result.fallback_reason is None
    assert states(gateway) == {"groq": CLOSED, "gemini": CLOSED, "openrouter": CLOSED}
    assert gateway.health()["groq"]["state"] == "healthy"


def test_a_call_over_the_default_per_call_timeout_is_a_timeout_failure_and_trips_the_breakers():
    clock = FakeClock()
    backend = TimedBackend(clock, 15.0)  # past the 10 s default: cut at 10 s, every time
    gateway = make_gateway(backend=backend, clock=clock)
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES, timeout_s=300.0))
    assert backend.timeouts and max(backend.timeouts) <= 10.0 + 1e-9, "nothing ran past the limit"
    # the fake clock has moved on by the time of the three timeouts: a breaker that opened first
    # may already be half open, but none of them is closed again
    assert CLOSED not in states(gateway).values(), "a timeout counts like any other failure"
    assert gateway.health()["groq"]["state"] == "circuit_open" or states(gateway)["groq"] != CLOSED


@pytest.mark.parametrize("seconds", [15.0, 25.0])
def test_the_same_slow_call_is_a_success_when_the_timeout_setting_is_thirty(seconds):
    clock = FakeClock()
    backend = TimedBackend(clock, seconds)
    gateway = make_gateway(make_config(timeout_seconds=30.0), backend=backend, clock=clock)
    for _ in range(6):
        result = run(gateway.complete(MESSAGES, timeout_s=30.0))
        assert result.decider == "groq" and result.fallback_reason is None
    assert states(gateway) == {"groq": CLOSED, "gemini": CLOSED, "openrouter": CLOSED}


def test_a_slow_failure_leaves_the_next_attempt_only_the_time_that_is_left():
    clock = FakeClock()
    backend = TimedBackend(clock, 20.0, lambda m, k, n: lit("ServiceUnavailableError"))
    gateway = make_gateway(make_config(timeout_seconds=30.0), backend=backend, clock=clock)
    started = clock.now()
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES, timeout_s=30.0))
    assert clock.now() - started <= 30.0 + 0.2, "the caller's budget is never exceeded"
    assert len(backend.timeouts) >= 2
    for start, timeout in zip(backend.starts, backend.timeouts, strict=True):
        assert timeout <= max(0.1, started + 30.0 - start) + 1e-9, "each call got the time left"
    assert backend.timeouts[1] < backend.timeouts[0], "the second attempt was given less"


def test_slow_failures_inside_a_budget_end_as_unavailable_and_trip_nothing():
    clock = FakeClock()
    backend = TimedBackend(clock, 40.0)  # slower than any budget: every call is cut and fails
    gateway = make_gateway(make_config(timeout_seconds=30.0), backend=backend, clock=clock)
    started = clock.now()
    with pytest.raises(AllProvidersFailed):
        run(gateway.complete(MESSAGES, timeout_s=12.0))
    assert clock.now() - started <= 12.0 + 0.2
    # the budget ran out: nothing says the providers are down, so no breaker is open
    assert OPEN not in states(gateway).values()


# ============================================================================ 429 with Retry-After
def test_a_key_that_was_told_to_wait_stays_out_until_the_time_is_over():
    clock = FakeClock()
    first = {"limited": True}

    def handler(model, key, n):
        if provider_of(model) == "groq" and key == KEYS[0] and first["limited"]:
            return rate_limited("120")
        return ok(f"answered by {key[-6:]}")

    backend = FakeBackend(handler)
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend, clock=clock)
    run(gateway.complete(MESSAGES))
    first["limited"] = False
    seen = []
    for _ in range(4):
        run(gateway.complete(MESSAGES))
        seen.append(backend.calls[-1][1])
    assert KEYS[0] not in seen, "the limited key is not used while it cools down"
    clock.advance(100)  # well past the 60 s default, still inside the 120 s the header asked for
    for _ in range(6):
        run(gateway.complete(MESSAGES))
        assert backend.calls[-1][1] != KEYS[0], "Retry-After 120 is honoured past the default"
    clock.advance(21)
    keys_used = set()
    for _ in range(8):
        run(gateway.complete(MESSAGES))
        keys_used.add(backend.calls[-1][1])
    assert KEYS[0] in keys_used, "and it is used again once the time is over"
    assert states(gateway)["groq"] == CLOSED


def test_a_retry_after_of_zero_is_raised_to_the_floor_not_an_instant_retry():
    clock = FakeClock()
    calls_by_key = []

    def handler(model, key, n):
        calls_by_key.append(key)
        if provider_of(model) == "groq" and len(calls_by_key) == 1:
            return rate_limited("0")
        return ok("fine")

    gateway = make_gateway(
        make_config(groq_keys=KEYS[:1]), backend=FakeBackend(handler), clock=clock
    )
    first = run(gateway.complete(MESSAGES))
    assert first.decider == "gemini"  # one groq key, told to wait: the next provider answers
    pool = gateway._pools["groq"]
    assert len(pool.cooling_fingerprints()) == 1
    clock.advance(RATE_LIMIT_FLOOR_SECONDS - 1)
    assert len(pool.cooling_fingerprints()) == 1, "a header of 0 still means the floor, not now"
    clock.advance(2)
    assert pool.cooling_fingerprints() == set(), "and the key is free once the floor has passed"
    clock.advance(BREAKER_OPEN_SECONDS)  # the breaker opened because its only key was cooling
    assert run(gateway.complete(MESSAGES)).decider == "groq"


def test_every_key_rate_limited_opens_the_breaker_and_heals_when_the_time_is_over():
    clock = FakeClock()
    limited = [True]

    def handler(model, key, n):
        if provider_of(model) == "groq" and limited[0]:
            return rate_limited("20")
        return ok(f"from {provider_of(model)}")

    gateway = make_gateway(make_config(groq_keys=KEYS), backend=FakeBackend(handler), clock=clock)
    assert run(gateway.complete(MESSAGES)).decider == "gemini"
    assert states(gateway)["groq"] == OPEN
    limited[0] = False
    clock.advance(BREAKER_OPEN_SECONDS + 1)  # longer than the 20 s Retry-After and the open time
    assert run(gateway.complete(MESSAGES)).decider == "groq"
    assert states(gateway)["groq"] == CLOSED


# ==================================================================================== Jev
def test_jev_failing_repeatedly_is_skipped_while_open_and_probed_after_the_open_period():
    clock = FakeClock()
    healthy = [False]
    client = FakeDecisionClient(
        lambda call, n: jev_response() if healthy[0] else lit("Timeout", "slow")
    )
    gateway = make_gateway(decision_client=client, backend=FakeBackend(chat_answer), clock=clock)
    first = run(gateway.decide(QUESTIONS, STATE))
    assert first.decider == "groq" and (first.fallback_reason or "").startswith("jev:")
    asked = len(client.calls)
    for _ in range(3):  # while the breaker is open, every decision goes straight to Groq
        assert run(gateway.decide(QUESTIONS, STATE)).decider == "groq"
    assert len(client.calls) == asked
    healthy[0] = True
    clock.advance(BREAKER_OPEN_SECONDS + 1)
    assert run(gateway.decide(QUESTIONS, STATE)).decider == "jev"
    assert gateway.health()["jev"]["state"] == "healthy"


# ============================================================================== embeddings
def test_both_embedding_keys_failing_is_unavailable_and_recovers_after_the_cooldown():
    clock = FakeClock()
    failing = [True]
    backend = FakeBackend()
    from app.gateway.backend import RawEmbedding

    def handler(model, key, inputs, n):
        if failing[0]:
            return lit("Timeout")
        return RawEmbedding([[1.0] + [0.0] * 767 for _ in inputs], 3, 0.0001, "litellm")

    backend.embed_handler = handler
    gateway = make_gateway(backend=backend, clock=clock)
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["a clause"]))
    failing[0] = False
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["a clause"]))  # both keys are still cooling
    clock.advance(BREAKER_OPEN_SECONDS + 61)
    assert len(run(gateway.embed(["a clause"])).vectors) == 1


# =================================================================================== rerank
def test_the_rerank_chain_degrades_to_lexical_and_returns_to_cohere():
    clock = FakeClock()
    cohere_up = [False]

    def cohere(request):
        if not cohere_up[0]:
            return httpx.Response(503)
        return httpx.Response(200, json=cohere_json([(1, 0.9), (0, 0.4), (2, 0.1)]))

    def jina(request):
        return httpx.Response(429, headers={"retry-after": "30"})

    gateway = make_gateway(
        rerank_transport=transport({"cohere": cohere, "jina": jina}), clock=clock
    )
    degraded = run(gateway.rerank("waiting period", DOCS, top_n=2))
    assert degraded.reranker == "lexical" and degraded.degraded
    assert degraded.items, "the lexical fallback still orders the passages"
    cohere_up[0] = True
    clock.advance(BREAKER_OPEN_SECONDS + 1)
    healed = run(gateway.rerank("waiting period", DOCS, top_n=2))
    assert healed.reranker == "cohere" and not healed.degraded
