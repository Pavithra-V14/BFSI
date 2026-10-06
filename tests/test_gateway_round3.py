"""Third hardening round: 429 floor and attempt count, a full limiter is not a failure, waiting
for capacity, the hash-embedding refusal, the reasoning retry's own lease, Render rules, the
probe holder, multi-batch embeddings, and leak checks on real 401 and 429 responses."""

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import litellm
import pytest

from app.gateway import breaker as breaker_module
from app.gateway import questions as qs
from app.gateway.backend import LiteLLMBackend, RawEmbedding
from app.gateway.breaker import CLOSED, HALF_OPEN, CircuitBreaker
from app.gateway.config import DEFAULT_LIMITS, ConfigError, GatewayConfig, check_render_rules
from app.gateway.embeddings import DIMENSIONS, DevEmbedderForbidden, HashEmbedder
from app.gateway.errors import AllProvidersFailed, EmbeddingUnavailable
from app.gateway.keys import fingerprint
from app.gateway.limiter import Limits
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
from tests.test_gateway_decisions import jev_response
from tests.test_gateway_round2 import (
    KEYS,
    empty_length,
    groq_events,
    provider_of,
    reasoning_backend,
)

KEY_A, KEY_B = fake_key("embedA"), fake_key("embedB")


def rate_limited(retry_after=None):
    error = litellm.RateLimitError(message="m", model="m", llm_provider="p")
    if retry_after is not None:

        class Response:
            headers = {"retry-after": str(retry_after)}

        error.response = Response()  # type: ignore[assignment]
    return error


def groq_429(retry_after=None):
    error = rate_limited(retry_after)
    return FakeBackend(lambda m, k, n: error if provider_of(m) == "groq" else ok())


def groq_calls(backend):
    return len([c for c in backend.calls if provider_of(c[0]) == "groq"])


# ---- 1. 429: Retry-After with a floor, every 429 counts -------------------------------------


@pytest.mark.parametrize(
    ("header", "cools_for"), [("0", 5), ("1", 5), ("20", 20), ("99999", 3_600), (None, 60)]
)
def test_a_429_cools_the_key_for_retry_after_with_a_five_second_floor(header, cools_for):
    backend = groq_429(header)
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "gemini" and groq_calls(backend) == 1  # no instant re-call
    pool = gateway._pools["groq"]
    assert len(pool.cooling_fingerprints()) == 1
    gateway._clock.advance(cools_for - 1)
    assert len(pool.cooling_fingerprints()) == 1  # still cooling just before the time
    gateway._clock.advance(2)
    assert pool.cooling_fingerprints() == set()


def test_every_429_counts_as_an_attempt_up_to_the_size_of_the_pool():
    backend = groq_429("0")
    gateway = make_gateway(make_config(groq_keys=KEYS), backend=backend)  # four keys
    result = run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 4 and result.decider == "gemini"  # each key tried once, no more
    assert len({c[1] for c in backend.calls if provider_of(c[0]) == "groq"}) == 4
    assert result.fallback_reason == "groq: rate_limited"
    assert gateway._clock.slept == []  # the next key is tried at once; nobody hammers one key


def test_a_single_key_that_is_rate_limited_trips_the_breaker_not_a_loop():
    backend = groq_429("0")
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=backend)
    run(gateway.complete(MESSAGES))
    assert gateway.health()["groq"]["state"] == "circuit_open"
    run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 1  # the open breaker skips Groq altogether


# ---- 2. a full local limiter is not a failure; bounded waiting ------------------------------


def one_request_a_minute():
    return make_config(groq_keys=KEYS[:1], limits={**DEFAULT_LIMITS, "GROQ": Limits(rpm=1)})


def test_a_full_limiter_skips_the_step_without_tripping_the_breaker():
    gateway = make_gateway(one_request_a_minute(), backend=FakeBackend())
    assert run(gateway.complete(MESSAGES)).decider == "groq"
    second = run(gateway.complete(MESSAGES))
    assert second.decider == "gemini" and second.fallback_reason == "groq: limiter_full"
    events = [name for name, _ in gateway.events]
    assert "limiter_full" in events and "all_keys_exhausted" not in events
    assert gateway._breaker("groq", "answer").state == CLOSED
    gateway._clock.advance(61)  # the window frees: Groq is used again at once, no 30 s wait
    assert run(gateway.complete(MESSAGES)).decider == "groq"


def test_a_cooling_pool_still_trips_the_breaker():
    gateway = make_gateway(make_config(groq_keys=KEYS[:1]), backend=groq_429("30"))
    run(gateway.complete(MESSAGES))
    assert gateway._breaker("groq", "answer").state != CLOSED


def test_interactive_calls_do_not_wait_by_default():
    gateway = make_gateway(one_request_a_minute(), backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    result = run(gateway.complete(MESSAGES))
    assert result.decider == "gemini" and gateway._clock.slept == []


def test_a_bulk_caller_waits_for_capacity_instead_of_failing():
    gateway = make_gateway(one_request_a_minute(), backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    result = run(gateway.complete(MESSAGES, max_wait_s=120))
    assert result.decider == "groq" and result.fallback_reason is None
    assert gateway._clock.slept == [pytest.approx(60.0)]


def test_the_wait_is_bounded_by_the_callers_maximum():
    gateway = make_gateway(one_request_a_minute(), backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    result = run(gateway.complete(MESSAGES, max_wait_s=10))  # needs 60 s: too long
    assert result.decider == "gemini" and gateway._clock.slept == []
    assert result.fallback_reason == "groq: limiter_full"


def test_the_wait_never_overruns_the_callers_deadline():
    gateway = make_gateway(one_request_a_minute(), backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    result = run(gateway.complete(MESSAGES, max_wait_s=120, timeout_s=30))
    assert result.decider == "gemini" and gateway._clock.slept == []


def test_cancelling_a_wait_for_capacity_cleans_up():
    class Cancelling(FakeClock):
        async def sleep(self, seconds):
            raise asyncio.CancelledError

    config = one_request_a_minute()
    gateway = make_gateway(config, backend=FakeBackend(), clock=Cancelling())
    run(gateway.complete(MESSAGES))
    breaker = gateway._breaker("groq", "answer")
    with pytest.raises(asyncio.CancelledError):
        run(gateway.complete(MESSAGES, max_wait_s=120))
    assert breaker.state == CLOSED and breaker._probing is False
    assert len(groq_events(gateway)) == 1  # nothing was reserved while waiting


def test_embedding_batches_wait_for_capacity_when_the_caller_allows():
    config = make_config(
        embedding_keys=[KEY_A], limits={**DEFAULT_LIMITS, "EMBEDDING": Limits(rpm=1)}
    )
    gateway = make_gateway(config, backend=FakeBackend())
    run(gateway.embed(["first"]))
    with pytest.raises(EmbeddingUnavailable) as caught:
        run(gateway.embed(["second"]))
    assert caught.value.skipped == [("gemini_embedding", "limiter_full")]
    assert gateway.health()["gemini_embedding"]["tiers"]["embed"] == "closed"
    result = run(gateway.embed(["third"], max_wait_s=70))
    assert len(result.vectors) == 1 and gateway._clock.slept == [pytest.approx(60.0)]


def test_a_full_jev_limiter_falls_to_the_next_decider_without_tripping():
    answers = {"domain_relevance": {"type": "noul", "noul": 0.2}}
    client = FakeDecisionClient(lambda call, n: jev_response())
    config = make_config(limits={**DEFAULT_LIMITS, "JEV": Limits(rpm=1)})
    gateway = make_gateway(
        config, decision_client=client, backend=FakeBackend(lambda m, k, n: ok(json.dumps(answers)))
    )
    questions = [qs.domain_relevance()]
    assert run(gateway.decide(questions, {"message": "one"})).decider == "jev"
    second = run(gateway.decide(questions, {"message": "two"}))
    assert second.decider == "groq" and "jev: limiter_full" in second.fallback_reason
    assert gateway._breaker("jev", "decide").state == CLOSED


# ---- 3. the hash embedding is refused next to any remote store ------------------------------

REMOTE = {
    "QDRANT_URL": "https://example-cluster.qdrant.test:6333",
    "DATABASE_URL": "postgresql://someone:not-a-real-password@db.example.test/app?sslmode=require",
    "EVAL_DATABASE_URL": "postgresql://someone:not-a-real-password@db.example.test/eval",
    "PGHOST": "db.example.test",
}


def failing_embeddings():
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: lit("ServiceUnavailableError")
    return backend


@pytest.mark.parametrize("name", sorted(REMOTE))
def test_any_remote_store_url_refuses_the_hash_fallback(name):
    env = {name: REMOTE[name]}
    gateway = make_gateway(backend=failing_embeddings(), allow_dev_embedding=True, env=env)
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["x"]))
    refused = [fields for event, fields in gateway.events if event == "dev_embedding_refused"]
    assert refused == [{"variables": [name]}]  # the name, never the value
    assert REMOTE[name] not in repr(gateway.events)
    with pytest.raises(DevEmbedderForbidden) as caught:
        HashEmbedder(env)
    assert name in str(caught.value) and REMOTE[name] not in str(caught.value)


def test_local_urls_and_an_empty_environment_still_allow_the_hash_fallback():
    local = {"QDRANT_URL": ":memory:", "DATABASE_URL": "sqlite:///dev.db"}
    for env in ({}, local, {"QDRANT_URL": "http://localhost:6333"}):
        gateway = make_gateway(backend=failing_embeddings(), allow_dev_embedding=True, env=env)
        result = run(gateway.embed(["x"]))
        assert result.is_dev_fallback and len(result.vectors[0]) == DIMENSIONS
        assert not any(event == "dev_embedding_refused" for event, _ in gateway.events)


def test_the_flag_is_still_needed_even_with_no_remote_store():
    gateway = make_gateway(backend=failing_embeddings(), env={})
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["x"]))


def test_render_alone_still_refuses_the_hash_embedder():
    config = make_config(eval_openrouter_keys=[])
    gateway = make_gateway(
        config, backend=failing_embeddings(), allow_dev_embedding=True, env={"RENDER": "1"}
    )
    with pytest.raises(EmbeddingUnavailable):
        run(gateway.embed(["x"]))
    with pytest.raises(DevEmbedderForbidden):
        HashEmbedder({"RENDER": "true"})


# ---- 4. the reasoning retry has its own limiter lease ---------------------------------------


def tpm_limit(tokens):
    return make_config(groq_keys=KEYS[:1], limits={**DEFAULT_LIMITS, "GROQ": Limits(tpm=tokens)})


RETRY_OK = ok("The waiting period is 30 days.", completion=50)


def test_the_retry_is_skipped_when_its_lease_does_not_fit_and_the_step_falls_through():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    gateway = make_gateway(tpm_limit(2_000), backend=backend)  # 410 + 1,830 does not fit
    result = run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 1 and result.decider == "gemini"
    assert result.fallback_reason == "groq: truncated"
    assert [e.tokens for e in groq_events(gateway)] == [410]  # the first call still counts
    assert any(name == "retry_skipped_no_capacity" for name, _ in gateway.events)
    assert gateway._breaker("groq", "answer").state == CLOSED


def test_the_retry_goes_ahead_when_its_lease_fits():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    gateway = make_gateway(tpm_limit(2_500), backend=backend)
    result = run(gateway.complete(MESSAGES))
    assert groq_calls(backend) == 2 and result.decider == "groq"
    assert [e.tokens for e in groq_events(gateway)] == [410, 60]


def test_the_retry_counts_as_a_request_in_the_rpm_window():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    config = make_config(groq_keys=KEYS[:1], limits={**DEFAULT_LIMITS, "GROQ": Limits(rpm=1)})
    result = run(make_gateway(config, backend=backend).complete(MESSAGES))
    assert groq_calls(backend) == 1 and result.fallback_reason == "groq: truncated"


def test_the_first_calls_tokens_count_even_when_the_retry_fails():
    def handler(model, key, n):
        if provider_of(model) != "groq":
            return ok("a fine answer")
        return empty_length() if n == 1 else lit("Timeout")

    gateway = make_gateway(tpm_limit(8_000), backend=FakeBackend(handler))
    run(gateway.complete(MESSAGES))
    assert sum(e.tokens for e in groq_events(gateway)) == 410


def test_the_first_calls_tokens_count_when_the_retry_is_cancelled():
    def handler(model, key, n):
        return empty_length() if n == 1 else asyncio.CancelledError()

    gateway = make_gateway(tpm_limit(8_000), backend=FakeBackend(handler))
    with pytest.raises(asyncio.CancelledError):
        run(gateway.complete(MESSAGES))
    assert sum(e.tokens for e in groq_events(gateway)) == 410
    assert gateway._breaker("groq", "answer").state == CLOSED


def test_a_retry_that_succeeds_is_not_double_counted():
    backend = reasoning_backend(empty_length(), RETRY_OK)
    gateway = make_gateway(tpm_limit(8_000), backend=backend)
    run(gateway.complete(MESSAGES))
    assert sum(e.tokens for e in groq_events(gateway)) == 410 + 60


# ---- 5. Render refuses eval keys and the mock switch ----------------------------------------

EVAL_SECRET = "eval-key-should-never-be-printed"


@pytest.mark.parametrize(
    "name",
    [
        "EVAL_OPENROUTER_API_KEYS",
        "EVAL_GROQ_API_KEYS",
        "EVAL_GEMINI_API_KEYS",
        "GATEWAY_ENABLE_MOCK",
    ],
)
def test_the_config_refuses_admin_only_settings_on_render(name):
    value = "true" if name == "GATEWAY_ENABLE_MOCK" else EVAL_SECRET
    with pytest.raises(ConfigError) as caught:
        GatewayConfig.from_env({"RENDER": "true", name: value})
    assert name in str(caught.value) and EVAL_SECRET not in str(caught.value)
    GatewayConfig.from_env({name: value})  # the same settings are fine on the admin machine


def test_render_without_admin_only_settings_starts_normally():
    config = GatewayConfig.from_env({"RENDER": "true", "GROQ_API_KEYS": fake_key("groq")})
    assert config.groq_keys == [fake_key("groq")] and not config.enable_mock


def test_a_directly_built_gateway_applies_the_same_rule():
    with pytest.raises(ConfigError):
        make_gateway(make_config(), env={"RENDER": "yes"})  # make_config has eval keys
    with pytest.raises(ConfigError):
        make_gateway(make_config(eval_openrouter_keys=[], enable_mock=True), env={"RENDER": "1"})
    make_gateway(make_config(eval_openrouter_keys=[]), env={"RENDER": "1"})
    check_render_rules(make_config(), {})  # not on Render: nothing is refused


# ---- 7. the probe is released only by its holder --------------------------------------------


def test_only_the_probe_holder_releases_the_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(clock, open_seconds=30)
    ordinary = breaker.acquire()  # a call that started while the breaker was closed
    assert ordinary is not None and not ordinary.is_probe
    breaker.record_failure()
    clock.advance(31)
    assert breaker.state == HALF_OPEN
    probe = breaker.acquire()
    assert probe is not None and probe.is_probe and breaker.acquire() is None
    breaker.record_neutral(ordinary)  # a stranger finishing neutrally changes nothing
    breaker.record_neutral()
    assert breaker._probing is True and breaker.acquire() is None
    breaker.record_neutral(probe)  # the holder releases it
    assert breaker._probing is False and breaker.acquire() is not None


def test_a_stale_probe_permit_cannot_release_a_newer_probe():
    clock = FakeClock()
    breaker = CircuitBreaker(clock, open_seconds=30)
    breaker.record_failure()
    clock.advance(31)
    old_probe = breaker.acquire()
    breaker.record_failure()  # something else reopened the breaker
    clock.advance(31)
    new_probe = breaker.acquire()
    assert new_probe is not None and breaker._probing is True
    breaker.record_neutral(old_probe)
    assert breaker._probing is True  # still held by the new probe
    breaker.record_neutral(new_probe)
    assert breaker._probing is False


def test_a_success_closes_the_breaker_and_old_permits_go_stale():
    clock = FakeClock()
    breaker = CircuitBreaker(clock, open_seconds=30)
    breaker.record_failure()
    clock.advance(31)
    probe = breaker.acquire()
    breaker.record_success()
    assert breaker.state == breaker_module.CLOSED
    breaker.record_failure()
    clock.advance(31)
    assert breaker.acquire() is not None  # the stale flag from the old probe blocks nothing
    breaker.record_neutral(probe)


# ---- 7. multi-batch embeddings report every key and the failover ----------------------------


def vectors_for(inputs):
    return RawEmbedding([[1.0] + [0.0] * (DIMENSIONS - 1) for _ in inputs], len(inputs), 0.0, "x")


def test_a_failover_in_a_later_batch_is_reported():
    def handler(model, key, inputs, n):
        if n == 2 and key == KEY_A:  # the second batch: key A fails, key B answers
            return lit("ServiceUnavailableError")
        return vectors_for(inputs)

    backend = FakeBackend()
    backend.embed_handler = handler
    gateway = make_gateway(backend=backend)
    result = run(gateway.embed([f"text {i}" for i in range(20)]))  # two batches of 16 and 4
    assert len(result.vectors) == 20
    assert result.key_fingerprints == [fingerprint(KEY_A), fingerprint(KEY_B)]
    assert result.key_fingerprint == f"{fingerprint(KEY_A)},{fingerprint(KEY_B)}"
    assert result.fallback_reason == "primary key failed"


def test_all_batches_on_key_a_report_no_fallback():
    backend = FakeBackend()
    backend.embed_handler = lambda model, key, inputs, n: vectors_for(inputs)
    result = run(make_gateway(backend=backend).embed([f"text {i}" for i in range(20)]))
    assert result.key_fingerprints == [fingerprint(KEY_A)] and result.fallback_reason is None
    assert result.key_fingerprint == fingerprint(KEY_A)


# ---- 7. leak checks on real 401 and 429 responses -------------------------------------------


def serve(status, headers, key):
    """A loopback server that answers every POST with an error that echoes the key back,
    the way some providers do. Returns (server, requests-seen counter)."""
    seen = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen["count"] += 1
            body = json.dumps(
                {"error": {"message": f"Incorrect API key provided: {key}", "type": "error"}}
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.mark.parametrize(("status", "headers"), [(401, {}), (429, {"Retry-After": "0"})])
def test_a_real_failure_response_never_leaks_the_key_it_echoes(
    status, headers, caplog, capfd, monkeypatch
):
    key = "sk-failure-leak-" + "q1w2e3r4t5y6"
    server, seen = serve(status, headers, key)
    original = litellm.acompletion

    async def to_loopback(**kwargs):
        return await original(api_base=f"http://127.0.0.1:{server.server_port}", **kwargs)

    monkeypatch.setattr(litellm, "acompletion", to_loopback)
    config = make_config(groq_keys=[key], gemini_keys=[], openrouter_keys=[])
    gateway = make_gateway(config, backend=LiteLLMBackend())
    try:
        with caplog.at_level(logging.DEBUG), pytest.raises(AllProvidersFailed) as caught:
            run(gateway.complete(MESSAGES))
    finally:
        server.shutdown()
        server.server_close()
    expected_reason = "auth_failed" if status == 401 else "rate_limited"
    assert caught.value.skipped == [
        ("groq", expected_reason),
        ("gemini", "no_keys"),
        ("openrouter", "no_keys"),
    ]
    if status == 429:
        assert seen["count"] == 1  # Retry-After 0 did not cause an instant re-call
    seen_output = capfd.readouterr()
    everything = " ".join(
        [
            str(caught.value),
            repr(caught.value),
            repr(caught.value.skipped),
            repr(list(gateway.events)),
            repr(gateway.health()),
            repr(config),
            caplog.text,
            seen_output.out,
            seen_output.err,
        ]
    )
    for forbidden in (key, "q1w2e3r4t5y6", "sk-failure"):
        assert forbidden not in everything
