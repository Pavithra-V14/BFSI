"""Second hardening round: backoff cancellation, the 30 s key cool-down, config checks, bounded
events, Retry-After cap, malformed answers, and reasoning-token truncation."""

import asyncio
import json
import math

import litellm
import pytest

from app.gateway import backend as backend_module
from app.gateway import questions as qs
from app.gateway.breaker import CLOSED, HALF_OPEN
from app.gateway.clock import SystemClock
from app.gateway.config import (
    DEFAULT_LIMITS,
    ConfigError,
    GatewayConfig,
    validate_jev_model,
)
from app.gateway.decisions import ChoiceQuestion, parse_fallback_answers, read_jev_response
from app.gateway.errors import (
    AllProvidersFailed,
    InvalidOutput,
    SchemaSurprise,
    retry_after_seconds,
)
from app.gateway.gateway import EVENT_BUFFER
from app.gateway.keys import fingerprint
from app.gateway.limiter import Limits
from tests.helpers import (
    MESSAGES,
    FakeBackend,
    FakeDecisionClient,
    fake_key,
    lit,
    make_config,
    make_gateway,
    ok,
    run,
)

KEYS = [fake_key("groq", i) for i in range(1, 5)]


def provider_of(model: str) -> str:
    return model.split("/")[0]


def groq_events(gateway, index=0):
    return gateway._pools["groq"]._states[index].limiter._events


# ---- cancellation during a real backoff sleep, and any BaseException --------------------


def test_cancelling_during_the_backoff_sleep_releases_the_probe():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    clock = SystemClock()
    gateway = make_gateway(
        make_config(groq_keys=KEYS[:2]), backend=backend, clock=clock, rng=lambda: 1.0
    )
    breaker = gateway._breaker("groq", "answer")
    breaker._opened_at = clock.now() - 31  # half-open: the next call is the probe
    assert breaker.state == HALF_OPEN

    async def scenario():
        task = asyncio.create_task(gateway.complete(MESSAGES))
        for _ in range(200):  # wait for the first failure; the 0.5 s backoff sleep then starts
            if backend.calls:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.1)
        assert breaker._probing is True  # the probe is still held while we sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert breaker.state == HALF_OPEN and breaker._probing is False
    assert [e.tokens for e in groq_events(gateway)] == [0]
    assert len([c for c in backend.calls if provider_of(c[0]) == "groq"]) == 1  # no second try


class Boom(BaseException):
    """Not an Exception and not a cancellation."""


def test_any_baseexception_cleans_up_through_the_single_path():
    def handler(model, key, n):
        return Boom() if n == 1 else ok()

    config = make_config(groq_keys=KEYS[:1])
    gateway = make_gateway(config, backend=FakeBackend(handler))
    breaker = gateway._breaker("groq", "answer")
    breaker.record_failure()
    gateway._clock.advance(31)
    assert breaker.state == HALF_OPEN
    with pytest.raises(Boom):
        run(gateway.complete(MESSAGES))
    assert breaker._probing is False and [e.tokens for e in groq_events(gateway)] == [0]
    assert run(gateway.complete(MESSAGES)).decider == "groq"  # the probe works again


# ---- the 30 s per-key cool-down and the breaker ----------------------------------------


def test_a_dead_provider_with_four_keys_trips_on_the_second_call():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)
    first = run(gateway.complete(MESSAGES))
    groq_calls = [c for c in backend.calls if provider_of(c[0]) == "groq"]
    assert first.decider == "gemini" and len(groq_calls) == 3  # three attempts, three keys
    assert gateway._breaker("groq", "answer").state == CLOSED  # the fourth key never failed
    assert len(gateway._pools["groq"].cooling_fingerprints()) == 3  # but those three now cool

    second = run(gateway.complete(MESSAGES))
    assert second.decider == "gemini"
    assert gateway.health()["groq"]["state"] == "circuit_open"  # now every key is down
    calls_so_far = len(backend.calls)
    third = run(gateway.complete(MESSAGES))
    assert third.fallback_reason == "groq: circuit_open"
    assert len([c for c in backend.calls[calls_so_far:] if provider_of(c[0]) == "groq"]) == 0


def test_keys_come_back_after_the_thirty_second_cool_down():
    flaky = {"down": True}

    def handler(model, key, n):
        if provider_of(model) == "groq" and flaky["down"]:
            return lit("Timeout")
        return ok()

    gateway = make_gateway(make_config(groq_keys=KEYS[:2]), backend=FakeBackend(handler))
    run(gateway.complete(MESSAGES))
    assert gateway.health()["groq"]["state"] == "circuit_open"
    flaky["down"] = False
    gateway._clock.advance(31)  # keys and breaker both recover after 30 s
    assert run(gateway.complete(MESSAGES)).decider == "groq"


def test_a_provider_with_one_key_still_retries_three_times_in_one_call():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    run(gateway.complete(MESSAGES))
    assert len([c for c in backend.calls if provider_of(c[0]) == "groq"]) == 3
    assert gateway.health()["groq"]["state"] == "circuit_open"


def test_rerank_keys_cool_down_too():
    import httpx

    from tests.helpers import _no_network  # noqa: F401  (the guard stays in force elsewhere)

    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    gateway = make_gateway(rerank_transport=transport)
    run(gateway.rerank("claims", ["a clause"]))
    assert gateway._pools["cohere"].cooling_fingerprints() == {fingerprint(fake_key("cohere"))}


def test_the_backoff_does_not_overrun_the_callers_deadline():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=KEYS[:2]), backend=backend)
    result = run(gateway.complete(MESSAGES, timeout_s=0.3))  # the first backoff would be 0.375 s
    assert gateway._clock.slept == [] or sum(gateway._clock.slept) < 0.3
    assert result.fallback_reason == "groq: deadline" and result.decider == "gemini"
    assert gateway._breaker("groq", "answer").state == CLOSED  # not the provider's fault


# ---- decide() falls through on a context-window error ----------------------------------


def test_a_provider_context_window_error_does_not_abort_decide():
    from tests.helpers import NoJevClient  # noqa: F401

    client = FakeDecisionClient(lambda call, n: lit("ContextWindowExceededError"))
    backend = FakeBackend(lambda m, k, n: lit("ContextWindowExceededError"))
    gateway = make_gateway(decision_client=client, backend=backend)
    result = run(gateway.decide([qs.domain_relevance()], {"query": "hello there"}))
    assert result.needs_rules and result.decider == "rules"  # no exception reached the caller
    assert "context_window_exceeded" in result.fallback_reason


def test_decide_moves_on_to_the_next_decider_when_one_window_is_too_small():
    answer = {"domain_relevance": {"type": "noul", "noul": 0.9}}

    def handler(model, key, n):
        if provider_of(model) == "groq":
            return lit("ContextWindowExceededError")
        return ok(json.dumps(answer))

    client = FakeDecisionClient(lambda call, n: lit("ServiceUnavailableError"))
    gateway = make_gateway(decision_client=client, backend=FakeBackend(handler))
    result = run(gateway.decide([qs.domain_relevance()], {"query": "hello there"}))
    assert result.decider == "gemini" and "groq: context_window_exceeded" in result.fallback_reason


# ---- malformed answers are invalid output, not bugs -------------------------------------

CHOICE = ChoiceQuestion("product_line", "Which?", {"banking": "b", "insurance": "i"})


@pytest.mark.parametrize(
    "bad",
    [
        {"product_line": {"choice": ["banking"], "probabilities": {"banking": 1.0}}},
        {"product_line": {"choice": {"a": 1}, "probabilities": {"banking": 1.0}}},
        {"product_line": {"choice": 7, "probabilities": {"banking": 1.0}}},
        {"product_line": {"choice": "banking", "probabilities": ["x"]}},
        {"product_line": {"choice": "banking", "probabilities": {"banking": "high"}}},
        {"product_line": "banking"},
    ],
)
def test_malformed_fallback_json_is_invalid_output(bad):
    with pytest.raises(InvalidOutput):
        parse_fallback_answers(json.dumps(bad), [CHOICE])


def test_absurdly_nested_json_is_invalid_output_not_a_crash():
    text = '{"a":' * 5_000 + "1" + "}" * 5_000
    with pytest.raises(InvalidOutput):
        parse_fallback_answers(text, [CHOICE])


def test_a_chat_model_returning_a_list_as_choice_falls_back_without_an_internal_error():
    def handler(model, key, n):
        if provider_of(model) == "groq":
            bad = {"domain_relevance": {"type": "noul", "noul": [0.5]}}
            return ok(json.dumps(bad))
        return ok(json.dumps({"domain_relevance": {"type": "noul", "noul": 0.4}}))

    client = FakeDecisionClient(lambda call, n: lit("ServiceUnavailableError"))
    gateway = make_gateway(decision_client=client, backend=FakeBackend(handler))
    result = run(gateway.decide([qs.domain_relevance()], {"query": "hello there"}))
    assert result.decider == "gemini" and "groq: invalid_output" in result.fallback_reason
    assert not any(name == "internal_error" for name, _ in gateway.events)


@pytest.mark.parametrize(
    "usage",
    [
        "a string",
        ["list"],
        {"input_tokens": -5, "output_tokens": 1},
        {"input_tokens": 5, "output_tokens": -1},
        {"input_tokens": 5, "output_tokens": 1, "cost": -0.1},
        {"input_tokens": 5, "output_tokens": 1, "cost": float("nan")},
        {"input_tokens": 5, "output_tokens": 1, "cost": float("inf")},
        {"input_tokens": "many"},
    ],
)
def test_odd_jev_usage_is_a_schema_surprise(usage):
    answers = {"domain_relevance": {"type": "noul", "noul": 0.5}}
    with pytest.raises(SchemaSurprise):
        read_jev_response({"answers": answers, "usage": usage}, [qs.domain_relevance()])


# ---- config checks ----------------------------------------------------------------------


@pytest.mark.parametrize("text", ["rpm=0", "tpm=-5", "rpd=0", "tpd=-1"])
def test_limits_must_be_positive(text):
    with pytest.raises(ConfigError) as caught:
        GatewayConfig.from_env({"GATEWAY_LIMITS_GROQ": text})
    assert "GATEWAY_LIMITS_GROQ" in str(caught.value)


def test_direct_limits_are_checked_too():
    with pytest.raises(ConfigError):
        GatewayConfig(limits={"GROQ": Limits(rpm=0)})
    assert (
        GatewayConfig(limits={"GROQ": Limits(rpm=5)}).limits["GEMINI"] == DEFAULT_LIMITS["GEMINI"]
    )


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf", "soon"])
def test_the_timeout_must_be_positive_and_finite(value):
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"GATEWAY_TIMEOUT_SECONDS": value})


@pytest.mark.parametrize("value", [0, -3, math.nan, math.inf, True])
def test_a_direct_timeout_is_checked(value):
    with pytest.raises(ConfigError):
        GatewayConfig(timeout_seconds=value)


@pytest.mark.parametrize("text", ["a/model=0", "a/model=-5", "a/model=big"])
def test_context_overrides_must_be_positive_whole_numbers(text):
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"GATEWAY_CONTEXT_OVERRIDES": text})
    with pytest.raises(ConfigError):
        GatewayConfig(context_overrides={"a/model": 0})


def test_plans_are_exactly_free_or_paid():
    assert GatewayConfig.from_env({"GATEWAY_PLAN_GROQ": " Free "}).plans["GROQ"] == "free"
    for bad in ("Free", "PAID", "trial", ""):
        with pytest.raises(ConfigError) as caught:
            GatewayConfig(plans={"GROQ": bad})
        assert "GATEWAY_PLAN_GROQ" in str(caught.value)


def test_key_counts_apply_to_a_directly_built_config():
    too_many = [fake_key("x", i) for i in range(5)]
    with pytest.raises(ConfigError):
        GatewayConfig(groq_keys=too_many)
    with pytest.raises(ConfigError):
        GatewayConfig(embedding_keys=[fake_key("a"), fake_key("b"), fake_key("c")])
    with pytest.raises(ConfigError):
        GatewayConfig(groq_keys=[fake_key("a"), fake_key("a")])


def test_a_key_may_not_appear_in_two_pools():
    shared = fake_key("shared")
    cases = [
        {"jev_key": shared, "openrouter_keys": [shared]},
        {"eval_openrouter_keys": [shared], "openrouter_keys": [shared]},
        {"gemini_keys": [shared], "embedding_keys": [shared]},
        {"cohere_key": shared, "jina_key": shared},
        {"embedding_keys": [shared, shared]},
    ]
    for case in cases:
        with pytest.raises(ConfigError) as caught:
            GatewayConfig(**case)
        assert shared not in str(caught.value)  # variable names only
    with pytest.raises(ConfigError):
        GatewayConfig.from_env({"JEV_OPENROUTER_API_KEY": shared, "COHERE_API_KEY": shared})


def test_embedding_key_b_alone_is_an_error_not_a_silent_key_a():
    with pytest.raises(ConfigError) as caught:
        GatewayConfig.from_env({"GEMINI_EMBEDDING_KEY_B": fake_key("embedB")})
    assert "GEMINI_EMBEDDING_KEY_A" in str(caught.value)
    assert GatewayConfig.from_env({"GEMINI_EMBEDDING_KEY_A": fake_key("embedA")}).embedding_keys


def test_a_trailing_newline_does_not_get_through_the_jev_pin():
    with pytest.raises(ConfigError):
        validate_jev_model("typesafe/jev-1.13\n")
    with pytest.raises(ConfigError):
        GatewayConfig(models={"JEV_MODEL": "typesafe/jev-1.13\n"})


# ---- bounded events and the Retry-After cap --------------------------------------------


def test_events_keep_only_the_latest_thousand():
    gateway = make_gateway()
    for number in range(EVENT_BUFFER + 500):
        gateway._emit("probe", number=number)
    assert len(gateway.events) == EVENT_BUFFER
    assert gateway.events[0][1]["number"] == 500 and gateway.events[-1][1]["number"] == 1_499


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("99999", 3_600.0),
        ("inf", None),
        ("-5", None),
        ("nan", None),
        ("soon", None),
        (None, None),
    ],
)
def test_retry_after_is_finite_not_negative_and_capped_at_an_hour(value, expected):
    headers = {} if value is None else {"retry-after": value}
    assert retry_after_seconds(headers) == expected
    assert retry_after_seconds(None) is None


def test_a_huge_retry_after_cools_a_key_for_at_most_an_hour():
    error = litellm.RateLimitError(message="m", model="m", llm_provider="p")

    class Response:
        headers = {"retry-after": "9999999"}

    error.response = Response()
    backend = FakeBackend(lambda m, k, n: error if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    run(gateway.complete(MESSAGES))
    pool = gateway._pools["groq"]
    assert len(pool.cooling_fingerprints()) == 1
    gateway._clock.advance(3_601)
    assert pool.cooling_fingerprints() == set()


# ---- reasoning tokens: truncation, retry, allowance, effort ----------------------------


def reasoning_backend(first, second=None, other=None):
    """groq answers `first` then `second`; every other provider answers `other` (or fine)."""

    def handler(model, key, n):
        if provider_of(model) != "groq":
            return other or ok("a fine answer")
        return first if n == 1 else (second if second is not None else first)

    return FakeBackend(handler)


def empty_length(completion=400, reasoning=400, prompt=10):
    return ok(
        "", finish_reason="length", completion=completion, reasoning_tokens=reasoning, prompt=prompt
    )


def test_empty_text_with_reasoning_at_the_limit_is_retried_with_more_room():
    backend = reasoning_backend(empty_length(), ok("The waiting period is 30 days.", completion=50))
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "groq" and result.text.startswith("The waiting period")
    first, second = backend.requests[0], backend.requests[1]
    assert first["max_tokens"] == 400 + 512 and first["effort"] == "low"
    assert second["max_tokens"] == 2 * (400 + 512) and second["effort"] == "low"
    assert (result.input_tokens, result.output_tokens) == (20, 450)  # both calls are counted
    assert [e.tokens for e in groq_events(gateway)] == [470]
    assert result.fallback_reason is None


def test_the_retry_lowers_the_thinking_level_when_it_was_higher():
    backend = reasoning_backend(empty_length(), ok("an answer", completion=30))
    config = make_config(groq_keys=KEYS[:1], reasoning_effort_answer="high")
    run(make_gateway(config, backend=backend).complete(MESSAGES))
    assert [r["effort"] for r in backend.requests[:2]] == ["high", "low"]


def test_text_with_reasoning_is_returned_without_a_retry():
    backend = reasoning_backend(ok("The answer.", completion=90, reasoning_tokens=60))
    result = run(make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend).complete(MESSAGES))
    assert len([r for r in backend.requests if provider_of(r["model"]) == "groq"]) == 1
    assert result.text == "The answer." and result.reasoning_tokens == 60
    assert result.finish_reason == "stop" and not result.truncated


def test_text_present_needs_no_extra_room():
    backend = reasoning_backend(ok("Fine."))
    run(make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend).complete(MESSAGES))
    assert backend.requests[0]["max_tokens"] == 912 and len(backend.requests) == 1


def test_still_empty_after_the_retry_falls_to_the_next_provider_and_is_never_returned():
    backend = reasoning_backend(empty_length(), empty_length(prompt=10))
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "gemini" and result.text == "a fine answer"
    assert result.fallback_reason == "groq: truncated"
    assert [e.tokens for e in groq_events(gateway)] == [820]  # both attempts used tokens
    assert gateway._breaker("groq", "answer").state == CLOSED  # not a provider outage


def test_every_provider_truncating_is_an_error_not_an_empty_answer():
    backend = FakeBackend(lambda m, k, n: empty_length())
    with pytest.raises(AllProvidersFailed) as caught:
        run(make_gateway(backend=backend).complete(MESSAGES))
    assert [reason for _, reason in caught.value.skipped] == ["truncated"] * 3


def test_reasoning_equal_to_the_output_with_a_stop_reason_also_counts_as_truncated():
    all_thinking = ok("", finish_reason="stop", completion=60, reasoning_tokens=60)
    backend = reasoning_backend(all_thinking, ok("now with text", completion=20))
    result = run(make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend).complete(MESSAGES))
    assert result.decider == "groq" and result.text == "now with text"
    assert len(backend.requests) == 2


def test_an_empty_answer_with_no_sign_of_truncation_is_not_returned_either():
    silent = ok("   ", finish_reason="stop", completion=0)
    backend = reasoning_backend(silent)
    result = run(make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend).complete(MESSAGES))
    assert result.decider == "gemini" and result.fallback_reason == "groq: empty_output"
    assert len([r for r in backend.requests if provider_of(r["model"]) == "groq"]) == 1  # no retry


def test_the_allowance_is_configurable_and_the_limiter_reserves_the_same_number():
    seen = {}

    def handler(model, key, n):
        events = gateway._pools["groq"]._states[0].limiter._events
        seen["reserved"] = events[-1].tokens  # the open reservation while the call runs
        return ok("an answer", prompt=11, completion=5)

    config = make_config(groq_keys=KEYS[:1], reasoning_allowance=1_000)
    backend = FakeBackend(handler)
    gateway = make_gateway(config, backend=backend)
    run(gateway.complete(MESSAGES))
    prompt_tokens = gateway._counter.count_messages(MESSAGES)
    assert backend.requests[0]["max_tokens"] == 1_400
    assert seen["reserved"] == prompt_tokens + 1_400  # sent number == reserved number
    assert [e.tokens for e in groq_events(gateway)] == [16]  # corrected to the real usage

    zero = make_gateway(
        make_config(groq_keys=KEYS[:1], reasoning_allowance=0), backend=FakeBackend()
    )
    run(zero.complete(MESSAGES, max_tokens=30))
    assert zero._backend.requests[0]["max_tokens"] == 30


def test_json_calls_get_the_json_limit_plus_the_allowance():
    backend = FakeBackend(lambda m, k, n: ok('{"x": 1}'))
    run(make_gateway(backend=backend).complete(MESSAGES, json_mode=True))
    assert backend.requests[0]["max_tokens"] == 500 + 512


def test_effort_is_set_per_tier_and_can_be_switched_off():
    config = make_config(reasoning_effort_answer="medium", reasoning_effort_decide="high")
    backend = FakeBackend()
    gateway = make_gateway(config, backend=backend)
    run(gateway.complete(MESSAGES))
    run(gateway.complete(MESSAGES, tier="decide"))
    assert [r["effort"] for r in backend.requests] == ["medium", "high"]
    off = FakeBackend()
    run(make_gateway(make_config(reasoning_effort_answer=None), backend=off).complete(MESSAGES))
    assert off.requests[0]["effort"] is None


def test_decision_fallbacks_use_the_decide_effort_and_the_json_reservation():
    answer = {"domain_relevance": {"type": "noul", "noul": 0.3}}
    backend = FakeBackend(lambda m, k, n: ok(json.dumps(answer)))
    client = FakeDecisionClient(lambda call, n: lit("ServiceUnavailableError"))
    config = make_config(reasoning_effort_decide="medium", reasoning_allowance=200)
    gateway = make_gateway(config, decision_client=client, backend=backend)
    result = run(gateway.decide([qs.domain_relevance()], {"query": "hello there"}))
    assert result.decider == "groq"
    assert (
        backend.requests[0]["max_tokens"] == 500 + 200 and backend.requests[0]["effort"] == "medium"
    )


def test_reasoning_settings_from_the_environment():
    config = GatewayConfig.from_env({})
    assert (config.reasoning_allowance, config.reasoning_effort_answer) == (512, "low")
    assert config.reasoning_effort_decide == "low"
    config = GatewayConfig.from_env(
        {
            "GATEWAY_REASONING_ALLOWANCE_TOKENS": "256",
            "GATEWAY_REASONING_EFFORT_ANSWER": " High ",
            "GATEWAY_REASONING_EFFORT_DECIDE": "off",
        }
    )
    assert config.reasoning_allowance == 256 and config.reasoning_effort_answer == "high"
    assert config.reasoning_effort_decide is None
    for name, value in (
        ("GATEWAY_REASONING_EFFORT_ANSWER", "minimal"),
        ("GATEWAY_REASONING_EFFORT_DECIDE", "maximum"),
        ("GATEWAY_REASONING_ALLOWANCE_TOKENS", "-1"),
        ("GATEWAY_REASONING_ALLOWANCE_TOKENS", "lots"),
    ):
        with pytest.raises(ConfigError) as caught:
            GatewayConfig.from_env({name: value})
        assert name in str(caught.value)


def test_the_backend_passes_the_reasoning_effort_to_litellm_only_when_set(monkeypatch):
    seen = []
    original = litellm.acompletion

    async def spy(**kwargs):
        seen.append(dict(kwargs))
        return await original(mock_response="a mocked answer", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", spy)
    backend = backend_module.LiteLLMBackend()
    common = {
        "model": "groq/openai/gpt-oss-120b",
        "messages": MESSAGES,
        "api_key": fake_key("groq"),
        "max_tokens": 50,
        "timeout": 5,
    }
    run(backend.complete(**common, reasoning_effort="low"))
    run(backend.complete(**common))
    assert seen[0]["reasoning_effort"] == "low" and "reasoning_effort" not in seen[1]


# ---- the model-map setting and a real-HTTP leak check -----------------------------------


def test_the_bundled_model_map_is_forced_before_litellm_is_imported():
    import os

    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"].lower() == "true"
    assert os.environ["LITELLM_LOG"] == "ERROR"


def test_a_real_http_call_never_puts_the_key_in_any_log_or_output(caplog, capfd, monkeypatch):
    """LiteLLM talks real HTTP to a loopback server; every logger runs at DEBUG."""
    import logging
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    key = "sk-leakcheck-" + "z9y8x7w6v5u4"
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received["auth"] = self.headers.get("Authorization", "")
            body = json.dumps(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fake-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "a local answer"},
                        }
                    ],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = litellm.acompletion

    async def to_loopback(**kwargs):
        return await original(api_base=f"http://127.0.0.1:{server.server_port}", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", to_loopback)
    names = ("", "LiteLLM", "LiteLLM Router", "LiteLLM Proxy", "httpx", "httpcore", "openai")
    old_levels = {name: logging.getLogger(name).level for name in names}
    try:
        with caplog.at_level(logging.DEBUG):
            for name in names:
                logging.getLogger(name).setLevel(logging.DEBUG)
            raw = run(
                backend_module.LiteLLMBackend().complete(
                    model="openai/fake-model",
                    messages=MESSAGES,
                    api_key=key,
                    max_tokens=20,
                    timeout=10,
                )
            )
    finally:
        server.shutdown()
        server.server_close()
        for name, level in old_levels.items():
            logging.getLogger(name).setLevel(level)
    assert raw.text == "a local answer" and received["auth"] == f"Bearer {key}"  # it was sent
    seen = capfd.readouterr()
    everything = caplog.text + seen.out + seen.err
    # Even with every logger forced to DEBUG the key never appears. LiteLLM's own DEBUG curl
    # line shows a masked header ("Be****u4"), which is why "Authorization" itself is allowed
    # here; the gateway keeps these loggers at ERROR, where nothing is printed at all.
    for forbidden in (key, "z9y8x7w6v5u4", "sk-leak", "Bearer sk", f"Bearer {key[:6]}"):
        assert forbidden not in everything
