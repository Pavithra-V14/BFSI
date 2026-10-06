"""Fixes from the Phase 2 audit: cancellation, internal errors, the breaker rule, token use of
failed answers, bad embeddings, the Jev pin, quiet LiteLLM, and the test guards."""

import asyncio
import json
import logging
import math

import httpx
import litellm
import pytest

from app.gateway import backend as backend_module
from app.gateway import gateway as gateway_module
from app.gateway.backend import RawEmbedding
from app.gateway.breaker import CLOSED, HALF_OPEN
from app.gateway.config import DEFAULT_LIMITS, ConfigError, GatewayConfig, validate_jev_model
from app.gateway.embeddings import DIMENSIONS, vectors_ok
from app.gateway.errors import (
    CallTimeout,
    InternalError,
    ProviderUnavailable,
    classify,
    status_to_error,
)
from app.gateway.keys import fingerprint
from app.gateway.limiter import Limits
from tests.helpers import (
    MESSAGES,
    FakeBackend,
    FakeDecisionClient,
    NotProvided,
    fake_key,
    lit,
    make_config,
    make_gateway,
    ok,
    run,
)

GROQ_KEYS = [fake_key("groq", i) for i in range(1, 5)]


def provider_of(model: str) -> str:
    return model.split("/")[0]


def groq_events(gateway, index=0):
    return gateway._pools["groq"]._states[index].limiter._events


class HangingBackend(FakeBackend):
    """complete() starts, then waits forever, so a test can cancel it."""

    def __init__(self):
        super().__init__()
        self.started: asyncio.Event = asyncio.Event()
        self.hang = True

    async def complete(self, **kwargs):
        if not self.hang:
            return await super().complete(**kwargs)
        self.calls.append((kwargs["model"], kwargs["api_key"]))
        self.started.set()
        await asyncio.sleep(3600)


# ---- 1. cancellation ------------------------------------------------------------------


def test_cancelling_a_call_leaves_the_breaker_and_limiter_clean():
    backend = HangingBackend()
    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:1]), backend=backend)

    async def scenario():
        backend.started = asyncio.Event()
        task = asyncio.create_task(gateway.complete(MESSAGES))
        await backend.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        breaker = gateway._breakers[("groq", "answer")]
        assert breaker.state == CLOSED and breaker._probing is False
        assert [e.tokens for e in groq_events(gateway)] == [0]  # this call's tokens count as zero
        backend.handler = lambda model, key, n: ok()
        backend.hang = False  # use the normal complete() from now on
        result = await gateway.complete(MESSAGES)
        assert result.decider == "groq" and result.fallback_reason is None

    run(scenario())


def test_cancelling_a_half_open_probe_releases_the_probe():
    backend = HangingBackend()
    config = make_config(groq_keys=GROQ_KEYS[:2])
    gateway = make_gateway(config, backend=backend)
    breaker = gateway._breaker("groq", "answer")
    breaker.record_failure()  # as if both keys had just failed
    gateway._clock.advance(31)
    assert breaker.state == HALF_OPEN

    async def scenario():
        backend.started = asyncio.Event()
        task = asyncio.create_task(gateway.complete(MESSAGES))
        await backend.started.wait()
        assert breaker._probing is True  # the probe call is in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker.state == HALF_OPEN and breaker._probing is False
        backend.hang = False
        backend.handler = lambda model, key, n: ok()
        result = await gateway.complete(MESSAGES)  # a new probe is allowed and succeeds
        assert result.decider == "groq" and breaker.state == CLOSED

    run(scenario())


def test_a_second_caller_waits_while_a_probe_is_in_flight():
    backend = HangingBackend()
    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:2]), backend=backend)
    breaker = gateway._breaker("groq", "answer")
    breaker.record_failure()
    gateway._clock.advance(31)

    async def scenario():
        backend.started = asyncio.Event()
        first = asyncio.create_task(gateway.complete(MESSAGES))
        await backend.started.wait()
        backend.hang = False
        backend.handler = lambda model, key, n: ok()
        second = await gateway.complete(MESSAGES)  # groq is skipped while the probe runs
        assert second.decider == "gemini" and second.fallback_reason == "groq: circuit_open"
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    run(scenario())


# ---- 2. internal errors and transport errors ------------------------------------------


def test_an_unexpected_exception_is_internal_not_retried_and_logged_by_class_name(caplog):
    caplog.set_level(logging.DEBUG, logger="app.gateway")
    secret_detail = "detail-" + GROQ_KEYS[0]

    def handler(model, key, n):
        return TypeError(secret_detail) if provider_of(model) == "groq" else ok()

    backend = FakeBackend(handler)
    gateway = make_gateway(backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert len([c for c in backend.calls if provider_of(c[0]) == "groq"]) == 1  # no retry
    assert result.decider == "gemini" and result.fallback_reason == "groq: internal_error"
    assert gateway.health()["groq"]["state"] != "circuit_open"  # the breaker stays neutral
    assert "TypeError" in caplog.text
    assert secret_detail not in caplog.text and GROQ_KEYS[0] not in caplog.text
    assert ("internal_error", {"provider": "groq", "tier": "answer", "exception": "TypeError"}) in (
        gateway.events
    )
    assert gateway._clock.slept == []  # no backoff either


def test_a_bug_in_a_validator_is_internal_and_its_tokens_still_count(monkeypatch):
    real = gateway_module.extract_json
    calls = {"n": 0}

    def broken(text):
        calls["n"] += 1
        if calls["n"] == 1:  # only the first provider's answer hits the bug
            raise KeyError("some value " + text)
        return real(text)

    monkeypatch.setattr(gateway_module, "extract_json", broken)
    gateway = make_gateway(
        make_config(groq_keys=GROQ_KEYS[:1]),
        backend=FakeBackend(lambda m, k, n: ok("{}", prompt=70, completion=30)),
    )
    result = run(gateway.complete(MESSAGES, json_mode=True))
    assert result.decider == "gemini" and result.fallback_reason == "groq: internal_error"
    assert [e.tokens for e in groq_events(gateway)] == [100]


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route"),
        httpx.ReadError("reset"),
        ConnectionResetError("reset"),
        OSError("network is unreachable"),
        lit("APIConnectionError"),
        lit("ServiceUnavailableError"),
        lit("InternalServerError"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_known_transport_errors_are_provider_unavailable_and_retried(error):
    assert isinstance(classify(error), ProviderUnavailable)
    backend = FakeBackend(lambda m, k, n: error if provider_of(m) == "groq" else ok())
    result = run(make_gateway(backend=backend).complete(MESSAGES))
    assert len([c for c in backend.calls if provider_of(c[0]) == "groq"]) == 3
    assert result.decider == "gemini" and result.fallback_reason == "groq: provider_unavailable"


def test_classify_and_status_mapping():
    error = classify(KeyError("a value that must not be copied"))
    assert isinstance(error, InternalError) and error.exception_name == "KeyError"
    assert str(error) == "internal_error" and "must not" not in repr(error)
    assert isinstance(classify(ValueError("x")), InternalError)
    assert isinstance(classify(asyncio.TimeoutError()), CallTimeout)
    assert isinstance(status_to_error(408), CallTimeout)
    assert isinstance(status_to_error(500), ProviderUnavailable)
    assert isinstance(status_to_error(524), ProviderUnavailable)
    assert status_to_error(418).reason == "bad_request"


# ---- 3. the Jev pin -------------------------------------------------------------------

FORBIDDEN_JEV = [
    "typesafe/jev-router",
    "~typesafe/jev-latest",
    "typesafe/jev-latest",
    "~typesafe/jev-1.13",
    "typesafe/jev-1.13-router",
    "typesafe/Jev-Router",
    "typesafe/JEV-LATEST",
    "someone-else/jev-1.13",
    "typesafe/jev-",
    "typesafe/jev-x1",
    "typesafe/other-1.13",
]


@pytest.mark.parametrize("value", FORBIDDEN_JEV)
def test_a_router_latest_alias_or_unpinned_jev_model_is_refused(value):
    with pytest.raises(ConfigError) as from_env:
        GatewayConfig.from_env({"JEV_MODEL": value})
    # The message holds one fixed example (typesafe/jev-1.13); take it out before looking for
    # the rejected value, which could be a prefix of that example.
    shown = str(from_env.value).replace("typesafe/jev-1.13", "<example>")
    assert "JEV_MODEL" in shown and value not in shown
    with pytest.raises(ConfigError) as direct:
        GatewayConfig(models={"JEV_MODEL": value})
    assert value not in str(direct.value).replace("typesafe/jev-1.13", "<example>")
    with pytest.raises(ConfigError):
        validate_jev_model(value)


@pytest.mark.parametrize(
    "value", ["typesafe/jev-1.13", "typesafe/jev-1.14", "typesafe/jev-1.13-20260917"]
)
def test_an_exact_jev_version_is_accepted(value):
    assert GatewayConfig.from_env({"JEV_MODEL": value}).models["JEV_MODEL"] == value
    assert GatewayConfig.from_env({}).models["JEV_MODEL"] == "typesafe/jev-1.13"


# ---- 4. the breaker trips only when every key failed -----------------------------------


def test_one_bad_key_of_four_never_trips_the_breaker():
    def handler(model, key, n):
        return lit("Timeout") if key == GROQ_KEYS[0] else ok()

    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS), backend=FakeBackend(handler))
    for _ in range(8):
        assert run(gateway.complete(MESSAGES)).decider == "groq"
    assert gateway.health()["groq"]["state"] != "circuit_open"


def test_three_bad_keys_of_four_skip_the_provider_for_that_call_without_tripping():
    def handler(model, key, n):
        if provider_of(model) != "groq":
            return ok()
        return ok() if key == GROQ_KEYS[3] else lit("Timeout")

    backend = FakeBackend(handler)
    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS), backend=backend)
    first = run(gateway.complete(MESSAGES))
    assert first.decider == "gemini" and first.fallback_reason == "groq: timeout"
    assert gateway._breaker("groq", "answer").state == CLOSED  # not tripped: key 4 never failed
    second = run(gateway.complete(MESSAGES))  # the rotation now reaches the good key
    assert second.decider == "groq" and second.key_fingerprint == fingerprint(GROQ_KEYS[3])


def test_the_breaker_trips_when_every_key_failed_during_the_call():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:2]), backend=backend)
    run(gateway.complete(MESSAGES))
    assert gateway.health()["groq"]["state"] == "circuit_open"
    third = run(gateway.complete(MESSAGES))
    assert third.fallback_reason == "groq: circuit_open"


def test_a_cooling_key_counts_as_failing_for_the_breaker():
    def handler(model, key, n):
        if provider_of(model) != "groq":
            return ok()
        return lit("RateLimitError") if key == GROQ_KEYS[0] else lit("Timeout")

    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:2]), backend=FakeBackend(handler))
    run(gateway.complete(MESSAGES))
    assert gateway.health()["groq"]["state"] == "circuit_open"


# ---- 5. tokens of failed or invalid answers count toward the limiter -------------------


def test_an_invalid_json_answer_still_counts_its_tokens():
    def handler(model, key, n):
        if provider_of(model) == "groq":
            return ok("not json", prompt=100, completion=50)  # tokens were spent, answer unusable
        return ok('{"x": 1}')

    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:1]), backend=FakeBackend(handler))
    result = run(gateway.complete(MESSAGES, json_mode=True))
    assert result.decider == "gemini" and result.fallback_reason == "groq: invalid_output"
    assert [e.tokens for e in groq_events(gateway)] == [150]


def test_a_failed_call_without_usage_counts_zero_tokens():
    backend = FakeBackend(lambda m, k, n: lit("Timeout") if provider_of(m) == "groq" else ok())
    gateway = make_gateway(make_config(groq_keys=GROQ_KEYS[:1]), backend=backend)
    run(gateway.complete(MESSAGES))
    assert [e.tokens for e in groq_events(gateway)] == [0, 0, 0]


def test_a_jev_answer_with_a_bad_shape_still_counts_its_reported_input_tokens():
    from app.gateway import questions as qs

    response = {"answers": {}, "usage": {"input_tokens": 476, "output_tokens": 70, "cost": 0.00002}}
    client = FakeDecisionClient(lambda call, n: response)
    gateway = make_gateway(decision_client=client, backend=FakeBackend(lambda m, k, n: ok("{}")))
    run(gateway.decide([qs.domain_relevance()], {"query": "hello"}))
    jev_events = gateway._pools["jev"]._states[0].limiter._events
    assert [e.tokens for e in jev_events] == [476]


def test_the_tokens_of_unusable_embeddings_count():
    backend = FakeBackend()
    backend.embed_handler = lambda m, k, inputs, n: RawEmbedding(
        [[1.0, 0.0] for _ in inputs], 33, 0.0, "litellm"
    )
    gateway = make_gateway(backend=backend)
    with pytest.raises(Exception):  # noqa: B017 - EmbeddingUnavailable, checked below
        run(gateway.embed(["x"]))
    events = gateway._pools["gemini_embedding"]._states[0].limiter._events
    assert [e.tokens for e in events] == [33]


# ---- 6. bad embeddings never get through ------------------------------------------------

KEY_A, KEY_B = fake_key("embedA"), fake_key("embedB")


def good_vectors(inputs):
    return [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in inputs]


@pytest.mark.parametrize(
    "make_bad",
    [
        lambda inputs: [[1.0, 0.0] for _ in inputs],  # wrong size
        lambda inputs: good_vectors(inputs)[:-1] if len(inputs) > 1 else [],  # wrong count
        lambda inputs: [[0.0] * DIMENSIONS for _ in inputs],  # all zeros
        lambda inputs: [[math.nan] + [0.0] * (DIMENSIONS - 1) for _ in inputs],
        lambda inputs: [[math.inf] + [0.0] * (DIMENSIONS - 1) for _ in inputs],
    ],
    ids=["wrong-size", "wrong-count", "zero", "nan", "inf"],
)
def test_bad_vectors_from_key_a_make_the_gateway_try_key_b(make_bad):
    def handler(model, key, inputs, n):
        vectors = make_bad(inputs) if key == KEY_A else good_vectors(inputs)
        return RawEmbedding(vectors, 5, 0.0, "litellm")

    backend = FakeBackend()
    backend.embed_handler = handler
    result = run(make_gateway(backend=backend).embed(["a", "b"]))
    assert [k for _, k, _ in backend.embed_calls] == [KEY_A, KEY_B]
    assert result.key_fingerprint == fingerprint(KEY_B)
    assert result.fallback_reason == "primary key failed" and not result.is_dev_fallback
    assert len(result.vectors) == 2 and all(len(v) == DIMENSIONS for v in result.vectors)


def test_bad_vectors_from_both_keys_mean_temporarily_unavailable():
    from app.gateway.errors import EmbeddingUnavailable

    backend = FakeBackend()
    backend.embed_handler = lambda m, k, inputs, n: RawEmbedding(
        [[1.0, 0.0] for _ in inputs], 5, 0.0, "litellm"
    )
    gateway = make_gateway(backend=backend)
    with pytest.raises(EmbeddingUnavailable) as caught:
        run(gateway.embed(["x"]))
    assert caught.value.skipped == [("gemini_embedding", "bad_embedding")]
    assert gateway.health()["gemini_embedding"]["state"] == "circuit_open"


def test_vectors_ok_rules():
    unit = [1.0] + [0.0] * (DIMENSIONS - 1)
    assert vectors_ok([unit], 1)
    assert not vectors_ok([unit], 2) and not vectors_ok([], 1)
    assert not vectors_ok([[1.0] * 5], 1) and not vectors_ok([[0.0] * DIMENSIONS], 1)
    assert not vectors_ok([[math.nan] * DIMENSIONS], 1)


# ---- 7. LiteLLM stays quiet ------------------------------------------------------------


def test_litellm_debug_and_verbose_output_are_off():
    assert litellm.suppress_debug_info is True and litellm.set_verbose is False
    assert litellm.turn_off_message_logging is True
    for name in backend_module.LITELLM_LOGGERS:
        assert logging.getLogger(name).level >= logging.ERROR


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_a_key_or_authorization_header_never_reaches_log_output(monkeypatch, capsys):
    collector = Collect()
    names = ["", "app.gateway", "httpx", "httpcore", "openai", *backend_module.LITELLM_LOGGERS]
    saved = []
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.level))
        logger.setLevel(logging.DEBUG)  # even if debug logging were switched on
        logger.addHandler(collector)
    original = litellm.acompletion

    async def with_mock(**kwargs):
        return await original(mock_response="a mocked answer", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", with_mock)
    try:
        key = fake_key("groq", 1)
        run(
            backend_module.LiteLLMBackend().complete(
                model="groq/openai/gpt-oss-120b",
                messages=MESSAGES,
                api_key=key,
                max_tokens=10,
                timeout=5,
            )
        )
        rerank_key = fake_key("cohere")
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
        gateway = make_gateway(rerank_transport=transport)
        run(gateway.rerank("claims", ["a clause about claims"]))
        jev_key = fake_key("jev")
        from app.gateway.decisions import OpenRouterDecisionsClient

        client = OpenRouterDecisionsClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(500))
        )
        with pytest.raises(ProviderUnavailable):
            run(
                client.decide(
                    model="typesafe/jev-1.13", state={}, questions={}, api_key=jev_key, timeout=2
                )
            )
    finally:
        for logger, level in saved:
            logger.setLevel(level)
            logger.removeHandler(collector)
    joined = "\n".join(collector.lines) + capsys.readouterr().out + capsys.readouterr().err
    for secret in (key, rerank_key, jev_key):
        assert secret not in joined
    assert "authorization" not in joined.lower() and "bearer" not in joined.lower()


# ---- 8. test guards ---------------------------------------------------------------------


def test_a_gateway_made_for_tests_cannot_reach_the_real_jev_or_rerank_services():
    from app.gateway import questions as qs

    gateway = make_gateway()
    with pytest.raises(NotProvided):
        run(gateway.decide([qs.domain_relevance()], {"query": "hello"}))
    with pytest.raises(NotProvided):
        run(gateway.rerank("claims", ["a clause"]))


def test_the_default_limits_still_hold_after_the_changes():
    assert DEFAULT_LIMITS["GROQ"] == Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000)
    assert json.dumps(sorted(GROQ_KEYS)) and len({fingerprint(k) for k in GROQ_KEYS}) == 4
