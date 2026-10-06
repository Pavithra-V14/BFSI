"""Shared fakes for the gateway tests. Nothing here touches a network or a real key."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx

from app.gateway.backend import RawCompletion, RawEmbedding
from app.gateway.config import GatewayConfig
from app.gateway.gateway import Gateway
from app.gateway.limiter import Limits


def fake_key(provider: str, index: int = 1) -> str:
    """A made-up key that is long enough to be noticed if it ever leaks."""
    return f"fake-{provider}-{index}-secretvalue"


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class WordCounter:
    """Counts words instead of tokens, so tests never download a tokenizer."""

    approximate = False

    def count_text(self, text: str) -> int:
        return len(text.split())

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        return sum(self.count_text(str(m.get("content", ""))) + 1 for m in messages)


def ok(text: str = "hello", prompt: int = 10, completion: int = 5, **kw: Any) -> RawCompletion:
    return RawCompletion(
        text=text,
        finish_reason=kw.get("finish_reason", "stop"),
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=kw.get("reasoning_tokens"),
        cost_usd=kw.get("cost_usd", 0.001),
        cost_source=kw.get("cost_source", "litellm"),
    )


class FakeBackend:
    """handler(model, key, call_number) returns a RawCompletion or raises an error."""

    def __init__(self, handler: Callable[[str, str, int], Any] | None = None) -> None:
        self.handler = handler or (lambda model, key, n: ok())
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list[float] = []
        self.requests: list[dict] = []  # what each complete() call asked for
        self.embed_handler: Callable[[str, str, list[str], int], Any] | None = None
        self.embed_calls: list[tuple[str, str, list[str]]] = []

    async def complete(
        self,
        *,
        model,
        messages,
        api_key,
        max_tokens,
        timeout,
        response_format=None,
        reasoning_effort=None,
    ):
        self.calls.append((model, api_key))
        self.timeouts.append(timeout)
        self.requests.append({"model": model, "max_tokens": max_tokens, "effort": reasoning_effort})
        result = self.handler(model, api_key, len(self.calls))
        if isinstance(result, BaseException):
            raise result
        return result

    async def embed(self, *, model, inputs, api_key, dimensions, timeout):
        self.embed_calls.append((model, api_key, list(inputs)))
        if self.embed_handler is None:
            vectors = [[1.0] + [0.0] * (dimensions - 1) for _ in inputs]
            return RawEmbedding(vectors, sum(len(i.split()) for i in inputs), 0.0001, "litellm")
        result = self.embed_handler(model, api_key, list(inputs), len(self.embed_calls))
        if isinstance(result, BaseException):
            raise result
        return result


class FakeDecisionClient:
    def __init__(self, handler: Callable[[dict[str, Any], int], Any]) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    async def decide(self, *, model, state, questions, api_key, timeout):
        self.calls.append({"model": model, "state": state, "questions": questions, "key": api_key})
        result = self.handler(self.calls[-1], len(self.calls))
        if isinstance(result, BaseException):
            raise result
        return result


def make_config(**overrides: Any) -> GatewayConfig:
    base: dict[str, Any] = {
        "groq_keys": [fake_key("groq", 1), fake_key("groq", 2)],
        "gemini_keys": [fake_key("gemini")],
        "openrouter_keys": [fake_key("openrouter")],
        "eval_openrouter_keys": [fake_key("eval-openrouter")],
        "jev_key": fake_key("jev"),
        "embedding_keys": [fake_key("embedA"), fake_key("embedB")],
        "cohere_key": fake_key("cohere"),
        "jina_key": fake_key("jina"),
    }
    base.update(overrides)
    return GatewayConfig(**base)


class NotProvided(BaseException):
    """Raised when a test reaches a real network client it did not replace with a fake.

    It is a BaseException on purpose: the gateway turns ordinary exceptions into fallbacks,
    which would hide the mistake. This one goes straight out and fails the test.
    """


class NoJevClient:
    async def decide(self, **kwargs: Any):
        raise NotProvided("this test used Jev but did not provide a Jev client")


def _no_network(request: Any):
    raise NotProvided("this test made an HTTP call but did not provide a fake transport")


def make_gateway(config: GatewayConfig | None = None, **kwargs: Any) -> Gateway:
    kwargs.setdefault("clock", FakeClock())
    kwargs.setdefault("counter", WordCounter())
    kwargs.setdefault("backend", FakeBackend())
    kwargs.setdefault("rng", lambda: 0.5)
    # A test must pass a fake Jev client or a fake rerank transport to use them; the real ones
    # are never reachable by accident.
    kwargs.setdefault("decision_client", NoJevClient())
    kwargs.setdefault("rerank_transport", httpx.MockTransport(_no_network))
    return Gateway(config or make_config(), **kwargs)


def run(coro):
    return asyncio.run(coro)


def lit(name: str, text: str = "boom") -> BaseException:
    """A LiteLLM exception of the given class name (rate limit, timeout, and so on)."""
    import litellm

    return getattr(litellm, name)(message=text, model="m", llm_provider="p")


UNLIMITED = Limits()
MESSAGES = [{"role": "user", "content": "what is the waiting period"}]
