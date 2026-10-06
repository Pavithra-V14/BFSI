"""No key value may appear in logs, events, results, errors or the printed config (rule 2)."""

import json
import logging

import httpx
import pytest

from app.gateway import questions as qs
from app.gateway.decisions import OpenRouterDecisionsClient
from app.gateway.errors import (
    AllProvidersFailed,
    ContextWindowExceeded,
    EmbeddingUnavailable,
    classify,
)
from app.gateway.keys import fingerprint
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

ALL_KEYS = [
    fake_key("groq", 1),
    fake_key("groq", 2),
    fake_key("gemini"),
    fake_key("openrouter"),
    fake_key("eval-openrouter"),
    fake_key("jev"),
    fake_key("embedA"),
    fake_key("embedB"),
    fake_key("cohere"),
    fake_key("jina"),
]


def assert_no_key(text: str) -> None:
    for key in ALL_KEYS:
        assert key not in text, "a key value leaked"
    assert "secretvalue" not in text


def test_the_printed_config_shows_no_key():
    config = make_config()
    assert_no_key(repr(config))
    assert_no_key(str(config))


def test_provider_error_text_that_echoes_a_key_is_not_copied():
    for name in (
        "RateLimitError",
        "AuthenticationError",
        "BadRequestError",
        "Timeout",
        "ServiceUnavailableError",
        "ContextWindowExceededError",
    ):
        error = classify(lit(name, f"Incorrect API key provided: {fake_key('groq', 1)}"))
        assert_no_key(str(error) + repr(error))


def test_logs_events_results_and_errors_never_contain_a_key(caplog):
    caplog.set_level(logging.DEBUG, logger="app.gateway")

    def handler(model, key, n):
        return lit("RateLimitError", f"limit reached for key {key}")

    backend = FakeBackend(handler)
    gateway = make_gateway(backend=backend)
    with pytest.raises(AllProvidersFailed) as caught:
        run(gateway.complete(MESSAGES))
    assert_no_key(str(caught.value) + repr(caught.value) + repr(caught.value.skipped))

    backend.handler = lambda model, key, n: ok(f"the model says hello {n}")
    gateway._breakers.clear()
    gateway._clock.advance(120)
    result = run(gateway.complete(MESSAGES))
    assert_no_key(repr(result))
    assert result.key_fingerprint in {fingerprint(k) for k in ALL_KEYS}

    backend.embed_handler = lambda model, key, inputs, n: lit("ServiceUnavailableError", key)
    with pytest.raises(EmbeddingUnavailable) as embed_error:
        run(gateway.embed(["x"]))
    assert_no_key(str(embed_error.value) + repr(embed_error.value))
    with pytest.raises(ContextWindowExceeded) as window_error:
        run(gateway.embed(["word " * 9_000]))
    assert_no_key(str(window_error.value))

    assert_no_key(caplog.text)
    assert_no_key(repr(gateway.events) + repr(gateway.health()))


def test_decision_failures_log_and_report_reasons_only(caplog):
    caplog.set_level(logging.DEBUG, logger="app.gateway")
    client = FakeDecisionClient(lambda call, n: lit("AuthenticationError", call["key"]))
    backend = FakeBackend(lambda model, key, n: lit("ServiceUnavailableError", key))
    gateway = make_gateway(decision_client=client, backend=backend)
    result = run(gateway.decide([qs.domain_relevance()], {"query": "hello there"}))
    assert result.needs_rules
    assert_no_key(repr(result) + caplog.text + repr(gateway.events))


def test_the_decisions_client_sends_the_key_only_in_the_authorization_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(500)

    client = OpenRouterDecisionsClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):  # noqa: B017 - any typed error is fine here
        run(
            client.decide(
                model="typesafe/jev-1.13",
                state={"q": "x"},
                questions={},
                api_key=fake_key("jev"),
                timeout=2,
            )
        )
    assert fake_key("jev") not in seen["url"] and fake_key("jev") not in seen["body"]
    assert seen["auth"] == f"Bearer {fake_key('jev')}"
    json.loads(seen["body"])


def test_only_a_fingerprint_identifies_a_key():
    for key in ALL_KEYS:
        assert len(fingerprint(key)) == 8 and key not in fingerprint(key)
