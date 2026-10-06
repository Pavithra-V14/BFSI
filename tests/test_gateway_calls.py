"""Chat completions through the gateway: rotation, limits, retries, breaker, fallback."""

import pytest

from app.gateway.config import DEFAULT_LIMITS, DEFAULT_PLANS
from app.gateway.errors import AllProvidersFailed, ContextWindowExceeded
from app.gateway.keys import fingerprint
from app.gateway.limiter import Limits
from tests.helpers import MESSAGES, FakeBackend, fake_key, lit, make_config, make_gateway, ok, run

GROQ_MODEL = "groq/openai/gpt-oss-120b"
GROQ_20B = "groq/openai/gpt-oss-20b"
GEMINI_MODEL = "gemini/gemini-3.8-flash"
GROQ_KEY_1, GROQ_KEY_2 = fake_key("groq", 1), fake_key("groq", 2)


def provider_of(model: str) -> str:
    return model.split("/")[0]


def fail_providers(*names: str, error="ServiceUnavailableError"):
    """Handler that fails for the named providers and answers for the others."""

    def handler(model, key, n):
        return lit(error) if provider_of(model) in names else ok(f"answer from {model}")

    return handler


def test_answer_tier_uses_groq_first_and_records_everything():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and result.model == "openai/gpt-oss-120b"
    assert result.key_fingerprint == fingerprint(GROQ_KEY_1)
    assert (result.input_tokens, result.output_tokens) == (10, 5)  # provider-reported usage
    # The pilot runs on free plans: billed is $0, and the list price stays as the shadow cost.
    assert result.cost_billed == 0.0 and result.cost_source == "free_plan"
    assert result.shadow_cost > 0 and result.fallback_reason is None
    assert backend.calls == [(GROQ_MODEL, GROQ_KEY_1)]


def test_a_paid_plan_bills_the_reported_cost_and_a_free_plan_bills_zero():
    paid = make_config(plans={**DEFAULT_PLANS, "GROQ": "paid"})
    result = run(make_gateway(paid, backend=FakeBackend()).complete(MESSAGES))
    assert result.cost_billed == 0.001 and result.cost_source == "litellm"
    assert result.shadow_cost > 0
    free = make_config(plans={**DEFAULT_PLANS, "GROQ": "free"})
    result = run(make_gateway(free, backend=FakeBackend()).complete(MESSAGES))
    assert (result.cost_billed, result.cost_source) == (0.0, "free_plan")
    assert result.shadow_cost == pytest.approx(
        run(make_gateway(paid, backend=FakeBackend()).complete(MESSAGES)).shadow_cost
    )  # the shadow cost is the same on either plan: always the list price


def test_each_provider_has_its_own_plan():
    config = make_config(plans={**DEFAULT_PLANS, "GROQ": "paid", "GEMINI": "free"})

    def handler(model, key, n):
        return lit("ServiceUnavailableError") if provider_of(model) == "groq" else ok()

    result = run(make_gateway(config, backend=FakeBackend(handler)).complete(MESSAGES))
    assert result.decider == "gemini" and result.cost_billed == 0.0
    assert result.cost_source == "free_plan"


def test_calls_rotate_across_the_keys_of_a_pool():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    run(gateway.complete(MESSAGES))
    run(gateway.complete(MESSAGES))
    run(gateway.complete(MESSAGES))
    assert [key for _, key in backend.calls] == [GROQ_KEY_1, GROQ_KEY_2, GROQ_KEY_1]


def test_a_429_cools_down_only_that_key_and_the_call_moves_to_the_next_key():
    def handler(model, key, n):
        return lit("RateLimitError") if key == GROQ_KEY_1 else ok()

    backend = FakeBackend(handler)
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and result.key_fingerprint == fingerprint(GROQ_KEY_2)
    assert result.fallback_reason is None  # no provider fallback happened
    assert gateway.health()["groq"]["state"] != "circuit_open"
    run(gateway.complete(MESSAGES))
    run(gateway.complete(MESSAGES))
    assert [key for _, key in backend.calls].count(GROQ_KEY_1) == 1  # still cooling
    gateway._clock.advance(61)
    run(gateway.complete(MESSAGES))
    assert GROQ_KEY_1 in [key for _, key in backend.calls][-2:]


def test_breaker_trips_only_when_all_keys_are_limited_then_recovers():
    backend = FakeBackend(fail_providers("groq", error="RateLimitError"))
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "gemini" and result.fallback_reason == "groq: rate_limited"
    assert gateway.health()["groq"]["state"] == "circuit_open"
    groq_calls = [c for c in backend.calls if provider_of(c[0]) == "groq"]
    assert len(groq_calls) == 2  # both keys were tried before the provider was given up
    run(gateway.complete(MESSAGES))
    assert [c for c in backend.calls if provider_of(c[0]) == "groq"] == groq_calls  # skipped
    result = run(gateway.complete(MESSAGES))
    assert result.fallback_reason == "groq: circuit_open"
    gateway._clock.advance(31)  # open period over, but both keys still cool for 60 s
    backend.handler = lambda model, key, n: ok()
    result = run(gateway.complete(MESSAGES))
    assert result.fallback_reason == "groq: circuit_open"  # the probe is not wasted
    gateway._clock.advance(30)  # now a key is usable: one probe is allowed
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and gateway.health()["groq"]["state"] == "healthy"


def test_timeouts_retry_with_backoff_then_fall_back():
    backend = FakeBackend(fail_providers("groq", error="Timeout"))
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    groq_calls = [c for c in backend.calls if provider_of(c[0]) == "groq"]
    assert len(groq_calls) == 3  # three attempts per provider
    assert gateway._clock.slept == [0.375, 0.75]  # backoff with jitter, base 0.5 s
    assert result.decider == "gemini" and result.fallback_reason == "groq: timeout"
    assert gateway.health()["groq"]["state"] == "circuit_open"


def test_a_transient_error_is_retried_and_can_succeed():
    def handler(model, key, n):
        return lit("ServiceUnavailableError") if n == 1 else ok()

    gateway = make_gateway(backend=FakeBackend(handler))
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and len(gateway._clock.slept) == 1


def test_bad_request_is_not_retried_and_does_not_trip_the_breaker():
    backend = FakeBackend(fail_providers("groq", error="BadRequestError"))
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert len([c for c in backend.calls if provider_of(c[0]) == "groq"]) == 1
    assert result.decider == "gemini" and result.fallback_reason == "groq: bad_request"
    assert gateway.health()["groq"]["state"] != "circuit_open"


def test_context_window_error_from_a_provider_is_not_retried_or_fallen_back():
    backend = FakeBackend(fail_providers("groq", error="ContextWindowExceededError"))
    gateway = make_gateway(backend=backend)
    with pytest.raises(ContextWindowExceeded):
        run(gateway.complete(MESSAGES))
    assert len(backend.calls) == 1


def test_prompt_larger_than_the_smallest_window_is_refused_before_any_call():
    backend = FakeBackend()
    config = make_config(context_overrides={GEMINI_MODEL: 50})
    gateway = make_gateway(config, backend=backend)
    with pytest.raises(ContextWindowExceeded):
        run(gateway.complete([{"role": "user", "content": "word " * 100}]))
    assert backend.calls == []
    run(gateway.complete([{"role": "user", "content": "word " * 5}], max_tokens=20))
    assert len(backend.calls) == 1


def test_auth_failure_cools_the_key_and_the_next_key_is_used():
    def handler(model, key, n):
        return lit("AuthenticationError") if key == GROQ_KEY_1 else ok()

    backend = FakeBackend(handler)
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.key_fingerprint == fingerprint(GROQ_KEY_2)
    run(gateway.complete(MESSAGES))
    gateway._clock.advance(600)  # an auth failure cools the key for much longer than a 429
    run(gateway.complete(MESSAGES))
    assert [key for _, key in backend.calls].count(GROQ_KEY_1) == 1


def test_every_provider_failing_raises_all_providers_failed_in_chain_order():
    gateway = make_gateway(backend=FakeBackend(fail_providers("groq", "gemini", "openrouter")))
    with pytest.raises(AllProvidersFailed) as caught:
        run(gateway.complete(MESSAGES))
    assert [p for p, _ in caught.value.skipped] == ["groq", "gemini", "openrouter"]


def test_the_offline_mock_is_off_by_default_and_labelled_when_on():
    failing = FakeBackend(fail_providers("groq", "gemini", "openrouter"))
    with pytest.raises(AllProvidersFailed):
        run(make_gateway(backend=failing).complete(MESSAGES))
    config = make_config(enable_mock=True)
    result = run(make_gateway(config, backend=failing).complete(MESSAGES))
    assert result.is_mock and result.decider == "mock" and "mock" in result.text
    assert result.fallback_reason.startswith("groq:")


def test_json_mode_parses_fenced_json_and_invalid_json_moves_to_the_next_provider():
    fenced = ok('Here you go:\n```json\n{"a": 1, "b": {"c": "}"}}\n```')
    result = run(
        make_gateway(backend=FakeBackend(lambda m, k, n: fenced)).complete(MESSAGES, json_mode=True)
    )
    assert result.parsed == {"a": 1, "b": {"c": "}"}}

    def handler(model, key, n):
        return ok("not json at all") if provider_of(model) == "groq" else ok('{"x": 2}')

    result = run(make_gateway(backend=FakeBackend(handler)).complete(MESSAGES, json_mode=True))
    assert result.decider == "gemini" and result.parsed == {"x": 2}
    assert result.fallback_reason == "groq: invalid_output"


def test_a_truncated_answer_and_reasoning_tokens_are_reported():
    raw = ok("", finish_reason="length", reasoning_tokens=400, completion=400)
    result = run(make_gateway(backend=FakeBackend(lambda m, k, n: raw)).complete(MESSAGES))
    assert result.truncated and result.reasoning_tokens == 400 and result.text == ""


def test_the_limiter_blocks_a_provider_whose_keys_are_used_up():
    limits = {**DEFAULT_LIMITS, "GROQ": Limits(rpm=1)}
    gateway = make_gateway(make_config(limits=limits), backend=FakeBackend())
    assert run(gateway.complete(MESSAGES)).decider == "groq"
    assert run(gateway.complete(MESSAGES)).decider == "groq"  # second key
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "gemini" and result.fallback_reason == "groq: all_keys_limited"
    assert any(name == "all_keys_exhausted" for name, _ in gateway.events)


def test_a_request_that_can_never_fit_a_key_skips_that_provider():
    limits = {**DEFAULT_LIMITS, "GROQ": Limits(tpm=50)}
    gateway = make_gateway(make_config(limits=limits), backend=FakeBackend())
    result = run(gateway.complete(MESSAGES, max_tokens=400))
    assert result.decider == "gemini" and result.fallback_reason == "groq: too_large_for_key"


def test_tokens_are_reserved_at_max_tokens_and_corrected_afterwards():
    limits = {**DEFAULT_LIMITS, "GROQ": Limits(tpm=1000)}
    config = make_config(limits=limits, groq_keys=[GROQ_KEY_1])
    gateway = make_gateway(config, backend=FakeBackend(lambda m, k, n: ok(prompt=10, completion=5)))
    for _ in range(5):  # each call reserves about 400 tokens, but uses only 15
        assert run(gateway.complete(MESSAGES, max_tokens=400)).decider == "groq"


def test_decide_tier_completion_skips_jev_and_starts_with_groq_20b():
    backend = FakeBackend()
    result = run(make_gateway(backend=backend).complete(MESSAGES, tier="decide"))
    assert backend.calls[0][0] == GROQ_20B and result.decider == "groq"
    assert result.model == "openai/gpt-oss-20b"


def test_only_provider_limits_a_call_to_one_provider():
    backend = FakeBackend()
    result = run(make_gateway(backend=backend).complete(MESSAGES, only_provider="gemini"))
    assert result.decider == "gemini" and [m for m, _ in backend.calls] == [GEMINI_MODEL]
    failing = FakeBackend(fail_providers("gemini"))
    with pytest.raises(AllProvidersFailed) as caught:
        run(make_gateway(backend=failing).complete(MESSAGES, only_provider="gemini"))
    assert [p for p, _ in caught.value.skipped] == ["gemini"]


def test_a_callers_time_budget_shortens_the_per_call_timeout():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    run(gateway.complete(MESSAGES))
    run(gateway.complete(MESSAGES, timeout_s=2.0))
    assert backend.timeouts[0] == 10.0 and backend.timeouts[1] <= 2.0


def test_stats_show_the_share_of_each_decider():
    backend = FakeBackend()
    gateway = make_gateway(backend=backend)
    run(gateway.complete(MESSAGES))
    backend.handler = fail_providers("groq")
    run(gateway.complete(MESSAGES))
    gateway._breakers.clear()
    assert gateway.stats.shares("answer") == {"groq": 0.5, "gemini": 0.5}
    gateway._clock.advance(3_601)
    assert gateway.stats.shares("answer") == {}
    assert gateway.stats.shares("answer", window_seconds=86_400) == {"groq": 0.5, "gemini": 0.5}


def test_free_model_cost_is_billed_zero_but_has_a_shadow_cost():
    def handler(model, key, n):
        if provider_of(model) == "openrouter":
            return ok("free", cost_usd=0.0, cost_source="free_model")
        return lit("ServiceUnavailableError")

    gateway = make_gateway(backend=FakeBackend(handler))
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "openrouter" and result.cost_billed == 0.0
    assert result.cost_source == "free_model" and result.shadow_cost > 0
    assert result.shadow_source == "default_reference"


def test_only_four_keys_per_provider_are_accepted():
    from app.gateway.config import GatewayConfig
    from app.gateway.keys import ConfigError

    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"GROQ_API_KEYS": "a,b,c,d,e"})
