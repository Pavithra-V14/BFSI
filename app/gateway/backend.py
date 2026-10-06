"""The only code that calls LiteLLM. Everything else talks to a backend object.

Keeping LiteLLM behind this small class lets tests replace it with a fake, so no
test needs a network or a key. LiteLLM caching is not used (spec 5.4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import litellm

LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def silence_litellm() -> None:
    """Turn LiteLLM's debug and verbose output off, at startup.

    LiteLLM can log request details (headers, keys, prompts) when verbose or debug output is
    on. Nothing in this project needs it, so it stays off.
    """
    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    litellm.turn_off_message_logging = True  # keep message content out of LiteLLM's callbacks
    for name in LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


silence_litellm()


@dataclass
class RawCompletion:
    text: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    cost_source: str


@dataclass
class RawEmbedding:
    vectors: list[list[float]]
    prompt_tokens: int | None
    cost_usd: float | None
    cost_source: str


class Backend(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        api_key: str,
        max_tokens: int,
        timeout: float,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> RawCompletion: ...

    async def embed(
        self, *, model: str, inputs: list[str], api_key: str, dimensions: int, timeout: float
    ) -> RawEmbedding: ...


def _cost(response: Any, model: str) -> tuple[float | None, str]:
    if model.endswith(":free"):
        return 0.0, "free_model"
    try:
        return float(litellm.completion_cost(completion_response=response)), "litellm"
    except Exception:  # LiteLLM does not know this model's price
        return None, "unknown"


class LiteLLMBackend:
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        api_key: str,
        max_tokens: int,
        timeout: float,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> RawCompletion:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "api_key": api_key,
            "max_tokens": max_tokens,
            "timeout": timeout,
            "num_retries": 0,  # the gateway does its own retries
            "drop_params": True,  # skip parameters a model does not support
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if reasoning_effort is not None:
            # LiteLLM maps this to Gemini's thinking level and passes it to Groq's gpt-oss;
            # a model that does not support it simply drops it (drop_params).
            kwargs["reasoning_effort"] = reasoning_effort
        response = await litellm.acompletion(**kwargs)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        cost, source = _cost(response, model)
        return RawCompletion(
            text=choice.message.content or "",
            finish_reason=getattr(choice, "finish_reason", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            reasoning_tokens=getattr(details, "reasoning_tokens", None),
            cost_usd=cost,
            cost_source=source,
        )

    async def embed(
        self, *, model: str, inputs: list[str], api_key: str, dimensions: int, timeout: float
    ) -> RawEmbedding:
        response = await litellm.aembedding(
            model=model,
            input=inputs,
            api_key=api_key,
            dimensions=dimensions,
            timeout=timeout,
        )
        vectors = [list(item["embedding"]) for item in response.data]
        usage = getattr(response, "usage", None)
        cost, source = _cost(response, model)
        return RawEmbedding(vectors, getattr(usage, "prompt_tokens", None), cost, source)
