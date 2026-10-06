"""Fourth hardening round: every key is tried on 429, the reasoning retry's edge cases (window
error, "off", little time left), honest cost labels, and a store URL without a scheme."""

import pytest

from app.gateway.breaker import CLOSED
from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from tests.helpers import MESSAGES, FakeBackend, lit, make_config, make_gateway, ok, run
from tests.test_gateway_round2 import (
    KEYS,
    empty_length,
    groq_events,
    provider_of,
    reasoning_backend,
)
from tests.test_gateway_round3 import (
    KEY_A,
    failing_embeddings,
    groq_calls,
    rate_limited,
    vectors_for,
)

RETRY_OK = ok("The waiting period is 30 days.", completion=50)


# ---- 1. a 429 tries every key of the pool before the provider is given up on ------------------


def test_three_rate_limited_keys_do_not_stop_the_fourth_from_answering():
    def handler(model, key, n):
        if provider_of(model) == "groq" and key != KEYS[3]:
            return rate_limited("0")
        return ok("answered by the last key")

    backend = FakeBackend(handler)
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and result.text == "answered by the last key"
    assert groq_calls(backend) == 4 and result.fallback_reason is None
    assert gateway._breaker("groq", "answer").state == CLOSED  # one key was fine


def test_all_four_keys_rate_limited_falls_to_the_next_provider_and_trips_the_breaker():
    backend = FakeBackend(
        lambda m, k, n: rate_limited("0") if provider_of(m) == "groq" else ok("from gemini")
    )
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 4 and result.decider == "gemini"
    assert gateway.health()["groq"]["state"] == "circuit_open"  # every key is cooling


def test_other_retryable_errors_keep_the_three_attempt_cap():
    backend = FakeBackend(
        lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok("from gemini")
    )
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)
    run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 3  # timeouts: three attempts, the fourth key is not tried


def test_a_mix_of_429_and_a_timeout_stops_at_three_attempts():
    errors = {KEYS[0]: rate_limited("0"), KEYS[1]: lit("Timeout"), KEYS[2]: lit("Timeout")}
    backend = FakeBackend(
        lambda m, k, n: errors.get(k, ok("late")) if provider_of(m) == "groq" else ok("gemini")
    )
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 3 and result.decider == "gemini"


# ---- 3. a context-window error from the retry is "no room" ------------------------------------


def test_a_window_error_from_the_retry_falls_through_instead_of_aborting():
    def handler(model, key, n):
        if provider_of(model) != "groq":
            return ok("a fine answer")
        return empty_length() if n == 1 else lit("ContextWindowExceededError")

    backend = FakeBackend(handler)
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    result = run(gateway.complete(MESSAGES))  # no exception reaches the caller
    assert result.decider == "gemini" and result.fallback_reason == "groq: truncated"
    assert groq_calls(backend) == 2
    assert any(name == "retry_skipped_window" for name, _ in gateway.events)
    assert gateway._breaker("groq", "answer").state == CLOSED
    assert sum(e.tokens for e in groq_events(gateway)) == 410  # the first call still counts


def test_the_first_calls_own_window_error_still_aborts_as_before():
    backend = FakeBackend(lambda m, k, n: lit("ContextWindowExceededError"))
    with pytest.raises(Exception) as caught:
        run(make_gateway(backend=backend).complete(MESSAGES))
    assert not isinstance(
        caught.value, AllProvidersFailed
    )  # a prompt that is too big is not retried


# ---- 5. "off" means no reasoning setting on the retry either ----------------------------------


def test_with_effort_off_the_retry_sends_no_effort_either():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    config = make_config(groq_keys=KEYS[:1], reasoning_effort_answer=None)
    result = run(make_gateway(config, backend=backend).complete(MESSAGES))
    groq = [r for r in backend.requests if provider_of(r["model"]) == "groq"]
    assert result.decider == "groq" and [r["effort"] for r in groq] == [None, None]


def test_with_effort_set_the_retry_still_uses_low():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    config = make_config(groq_keys=KEYS[:1], reasoning_effort_answer="high")
    run(make_gateway(config, backend=backend).complete(MESSAGES))
    assert [r["effort"] for r in backend.requests[:2]] == ["high", "low"]


# ---- 6. no retry when the caller has little time left -----------------------------------------


def test_the_retry_is_skipped_when_less_than_two_seconds_are_left():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    result = run(gateway.complete(MESSAGES, timeout_s=1.5))
    assert groq_calls(backend) == 1 and result.decider == "gemini"
    assert result.fallback_reason == "groq: truncated"
    assert any(name == "retry_skipped_no_time" for name, _ in gateway.events)
    assert [e.tokens for e in groq_events(gateway)] == [410]  # one lease only
    assert gateway._breaker("groq", "answer").state == CLOSED


def test_the_retry_goes_ahead_with_enough_time_or_no_deadline():
    for timeout in (30, None):
        backend = reasoning_backend(empty_length(), RETRY_OK)
        gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
        result = run(gateway.complete(MESSAGES, timeout_s=timeout))
        assert groq_calls(backend) == 2 and result.decider == "groq"


# ---- 7. cost labels stay honest when a call has no price --------------------------------------


def paid(**plans):
    return make_config(groq_keys=KEYS[:1], plans={"GROQ": "paid", "EMBEDDING": "paid", **plans})


def test_a_retry_with_one_unpriced_call_has_no_price_and_says_unknown():
    unpriced_first = empty_length()
    unpriced_first = ok(
        "",
        finish_reason="length",
        completion=400,
        reasoning_tokens=400,
        cost_usd=None,
        cost_source="unknown",
    )
    second = ok("A real answer.", completion=50, cost_usd=0.002, cost_source="litellm")
    backend = reasoning_backend(unpriced_first, second)
    result = run(make_gateway(paid(), backend=backend).complete(MESSAGES))
    assert result.decider == "groq" and result.cost_billed is None
    assert result.cost_source == "unknown"  # not the last call's "litellm"


def test_a_retry_with_both_calls_priced_adds_the_costs_and_keeps_the_label():
    first = ok(
        "",
        finish_reason="length",
        completion=400,
        reasoning_tokens=400,
        cost_usd=0.001,
        cost_source="litellm",
    )
    second = ok("A real answer.", completion=50, cost_usd=0.002, cost_source="litellm")
    result = run(make_gateway(paid(), backend=reasoning_backend(first, second)).complete(MESSAGES))
    assert result.cost_billed == pytest.approx(0.003) and result.cost_source == "litellm"


def test_a_multi_batch_embed_with_one_unpriced_batch_says_unknown_on_a_paid_plan():
    def handler(model, key, inputs, n):
        raw = vectors_for(inputs)
        if n == 1:  # the first batch has no price, the last one has
            return type(raw)(raw.vectors, raw.prompt_tokens, None, "unknown")
        return type(raw)(raw.vectors, raw.prompt_tokens, 0.5, "litellm")

    backend = FakeBackend()
    backend.embed_handler = handler
    result = run(make_gateway(paid(), backend=backend).embed([f"t{i}" for i in range(20)]))
    assert result.cost_billed is None and result.cost_source == "unknown"


def test_a_multi_batch_embed_with_every_batch_priced_adds_up():
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: type(vectors_for(inputs))(
        vectors_for(inputs).vectors, 1, 0.25, "litellm"
    )
    result = run(make_gateway(paid(), backend=backend).embed([f"t{i}" for i in range(20)]))
    assert result.cost_billed == pytest.approx(0.5) and result.cost_source == "litellm"


# ---- 8. a scheme-less store URL counts as local in the gateway too ----------------------------


def test_a_local_scheme_less_qdrant_address_still_allows_the_dev_embedding():
    gateway = make_gateway(
        backend=failing_embeddings(),
        allow_dev_embedding=True,
        env={"QDRANT_URL": "localhost:6333"},
    )
    assert run(gateway.embed(["x"])).is_dev_fallback


def test_a_remote_scheme_less_qdrant_address_refuses_it():
    gateway = make_gateway(
        backend=failing_embeddings(),
        allow_dev_embedding=True,
        env={"QDRANT_URL": "qdrant.example.test:6333"},
    )
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["x"]))
    refused = [fields for name, fields in gateway.events if name == "dev_embedding_refused"]
    assert refused == [{"variables": ["QDRANT_URL"]}]


def test_embedding_key_a_is_still_used_first():
    # A small guard that the shared helpers behave: key A is the first embedding key.
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: vectors_for(inputs)
    gateway = make_gateway(backend=backend)
    run(gateway.embed(["x"]))
    assert backend.embed_calls[0][1] == KEY_A
