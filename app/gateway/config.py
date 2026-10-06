"""Gateway settings, read from environment variables by name (never from files).

Every variable read here is listed in ENV_NAMES, and tests check that each one is
documented in .env.example. Rate limits for Gemini, the Decisions API, embeddings
and Cohere are unpublished [T]; the defaults below are conservative and every one
can be changed with a GATEWAY_LIMITS_<PROVIDER> variable.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.gateway.keys import ConfigError, parse_keys
from app.gateway.limiter import Limits

# Variable names that hold keys (values are never logged).
KEY_VARIABLES = (
    "GROQ_API_KEYS",
    "GEMINI_API_KEYS",
    "OPENROUTER_API_KEYS",
    "JEV_OPENROUTER_API_KEY",
    "GEMINI_EMBEDDING_KEY_A",
    "GEMINI_EMBEDDING_KEY_B",
    "COHERE_API_KEY",
    "JINA_API_KEY",
    "EVAL_OPENROUTER_API_KEYS",
)
MODEL_VARIABLES = (
    "GROQ_MODEL",
    "GROQ_DECIDE_MODEL",
    "GEMINI_MODEL",
    "GEMINI_DECIDE_MODEL",
    "OPENROUTER_MODEL",
    "OPENROUTER_DECIDE_MODEL",
    "JEV_MODEL",
    "GEMINI_EMBEDDING_MODEL",
    "COHERE_RERANK_MODEL",
    "JINA_RERANK_MODEL",
)
LIMIT_PROVIDERS = ("GROQ", "GEMINI", "OPENROUTER", "JEV", "EMBEDDING", "COHERE", "JINA")
LIMIT_VARIABLES = tuple(f"GATEWAY_LIMITS_{p}" for p in LIMIT_PROVIDERS)
OTHER_VARIABLES = (
    "GATEWAY_TIMEOUT_SECONDS",
    "GATEWAY_CONTEXT_OVERRIDES",
    "GATEWAY_ENABLE_MOCK",
)
PLAN_PROVIDERS = ("GROQ", "GEMINI", "OPENROUTER", "JEV", "EMBEDDING")
PLAN_VARIABLES = tuple(f"GATEWAY_PLAN_{p}" for p in PLAN_PROVIDERS)
ENV_NAMES = KEY_VARIABLES + MODEL_VARIABLES + LIMIT_VARIABLES + PLAN_VARIABLES + OTHER_VARIABLES

# Billing plan per provider. On a "free" plan the billed cost is $0 and only the shadow cost
# (list price) is shown. The pilot uses free tiers (spec section 18); Jev is paid per input token.
DEFAULT_PLANS = {
    "GROQ": "free",
    "GEMINI": "free",
    "OPENROUTER": "free",
    "JEV": "paid",
    "EMBEDDING": "free",
}

DEFAULT_MODELS = {
    "GROQ_MODEL": "openai/gpt-oss-120b",
    "GROQ_DECIDE_MODEL": "openai/gpt-oss-20b",
    "GEMINI_MODEL": "gemini-3.8-flash",
    "GEMINI_DECIDE_MODEL": "gemini-3.8-flash",
    "OPENROUTER_MODEL": "google/gemma-4-31b-it:free",
    "OPENROUTER_DECIDE_MODEL": "google/gemma-4-31b-it:free",
    "JEV_MODEL": "typesafe/jev-1.13",
    "GEMINI_EMBEDDING_MODEL": "gemini-embedding-2",
    "COHERE_RERANK_MODEL": "rerank-v3.5",
    "JINA_RERANK_MODEL": "jina-reranker-v3",
}

# Verified against provider documentation: Groq free plan (30 RPM, 8K TPM, 1K requests and
# 200K tokens a day, per model), OpenRouter free models (20 RPM, 50 requests a day under
# $10 of credits), Jina free tier (100 RPM, 100K TPM). Unpublished [T]: Gemini (the spec's
# 10 RPM and 250K TPM), the Decisions API, Gemini embeddings and Cohere (conservative guesses).
DEFAULT_LIMITS = {
    "GROQ": Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000),
    "GEMINI": Limits(rpm=10, tpm=250_000),
    "OPENROUTER": Limits(rpm=20, rpd=50),
    "JEV": Limits(rpm=20),
    "EMBEDDING": Limits(rpm=10),
    "COHERE": Limits(rpm=10),
    "JINA": Limits(rpm=100, tpm=100_000),
}

MAX_OUTPUT_TOKENS_ANSWER = 400  # [P]
MAX_OUTPUT_TOKENS_JSON = 500  # [P]
EMBEDDING_DIMENSIONS = 768
JEV_CONTEXT_TOKENS = 32_000
DEFAULT_TIMEOUT_SECONDS = 10.0
KEY_COOLDOWN_SECONDS = 60.0
BREAKER_OPEN_SECONDS = 30.0
EMBEDDING_FAILOVER_COOLDOWN_SECONDS = 30.0


def parse_limits(raw: str | None, base: Limits, *, name: str) -> Limits:
    """Read 'rpm=30,tpm=8000,rpd=none' on top of the defaults."""
    values = {"rpm": base.rpm, "tpm": base.tpm, "rpd": base.rpd, "tpd": base.tpd}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        field_name, _, value = part.partition("=")
        field_name, value = field_name.strip().lower(), value.strip().lower()
        if field_name not in values:
            raise ConfigError(f"{name} has an unknown field")
        if value in ("none", "off", "unlimited"):
            values[field_name] = None
            continue
        try:
            values[field_name] = int(value)
        except ValueError:
            raise ConfigError(f"{name} has a field that is not a whole number") from None
    return Limits(**values)


def _parse_plan(raw: str | None, default: str, *, name: str) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value not in ("free", "paid"):
        raise ConfigError(f"{name} must be free or paid")
    return value


def _parse_overrides(raw: str | None) -> dict[str, int]:
    overrides: dict[str, int] = {}
    for part in (raw or "").split(","):
        if not part.strip():
            continue
        model, _, value = part.rpartition("=")
        try:
            overrides[model.strip()] = int(value)
        except ValueError:
            raise ConfigError("GATEWAY_CONTEXT_OVERRIDES must look like model=tokens") from None
    return overrides


_JEV_MODEL_PATTERN = re.compile(r"^typesafe/jev-[0-9][A-Za-z0-9._-]*$")


def validate_jev_model(value: str) -> None:
    """Jev must be pinned to an exact version, so a model change shows up as drift.

    The router, any 'latest' alias and any '~' alias are rejected. The message never
    contains the value that was given.
    """
    lowered = value.lower()
    if (
        "router" in lowered
        or "latest" in lowered
        or value.startswith("~")
        or not _JEV_MODEL_PATTERN.match(value)
    ):
        raise ConfigError(
            "JEV_MODEL must be an exact pinned version such as typesafe/jev-1.13, "
            "never a router or a latest alias"
        )


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class GatewayConfig:
    # Key fields use repr=False so printing or logging the config never shows a key.
    groq_keys: list[str] = field(default_factory=list, repr=False)
    gemini_keys: list[str] = field(default_factory=list, repr=False)
    openrouter_keys: list[str] = field(default_factory=list, repr=False)
    eval_openrouter_keys: list[str] = field(default_factory=list, repr=False)
    jev_key: str | None = field(default=None, repr=False)
    embedding_keys: list[str] = field(default_factory=list, repr=False)  # key A, then key B
    cohere_key: str | None = field(default=None, repr=False)
    jina_key: str | None = field(default=None, repr=False)
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    limits: dict[str, Limits] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    plans: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PLANS))
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    context_overrides: dict[str, int] = field(default_factory=dict)
    enable_mock: bool = False
    eval_judge_model: str | None = None  # set in the evaluation phase

    def __post_init__(self) -> None:
        validate_jev_model(self.models.get("JEV_MODEL", ""))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        env = os.environ if env is None else env
        models = dict(DEFAULT_MODELS)
        for name in MODEL_VARIABLES:
            value = (env.get(name) or "").strip()
            if value:
                models[name] = value
        if not (env.get("GEMINI_DECIDE_MODEL") or "").strip():
            models["GEMINI_DECIDE_MODEL"] = models["GEMINI_MODEL"]  # defaults to the same model
        limits = {
            p: parse_limits(
                env.get(f"GATEWAY_LIMITS_{p}"), DEFAULT_LIMITS[p], name=f"GATEWAY_LIMITS_{p}"
            )
            for p in LIMIT_PROVIDERS
        }
        embedding_keys = [
            (env.get(name) or "").strip()
            for name in ("GEMINI_EMBEDDING_KEY_A", "GEMINI_EMBEDDING_KEY_B")
        ]
        timeout_raw = (env.get("GATEWAY_TIMEOUT_SECONDS") or "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
        except ValueError:
            raise ConfigError("GATEWAY_TIMEOUT_SECONDS must be a number") from None
        return cls(
            groq_keys=parse_keys(env.get("GROQ_API_KEYS"), name="GROQ_API_KEYS"),
            gemini_keys=parse_keys(env.get("GEMINI_API_KEYS"), name="GEMINI_API_KEYS"),
            openrouter_keys=parse_keys(env.get("OPENROUTER_API_KEYS"), name="OPENROUTER_API_KEYS"),
            eval_openrouter_keys=parse_keys(
                env.get("EVAL_OPENROUTER_API_KEYS"), name="EVAL_OPENROUTER_API_KEYS"
            ),
            jev_key=(env.get("JEV_OPENROUTER_API_KEY") or "").strip() or None,
            embedding_keys=[k for k in embedding_keys if k],
            cohere_key=(env.get("COHERE_API_KEY") or "").strip() or None,
            jina_key=(env.get("JINA_API_KEY") or "").strip() or None,
            models=models,
            limits=limits,
            plans={
                p: _parse_plan(
                    env.get(f"GATEWAY_PLAN_{p}"), DEFAULT_PLANS[p], name=f"GATEWAY_PLAN_{p}"
                )
                for p in PLAN_PROVIDERS
            },
            timeout_seconds=timeout,
            context_overrides=_parse_overrides(env.get("GATEWAY_CONTEXT_OVERRIDES")),
            enable_mock=_truthy(env.get("GATEWAY_ENABLE_MOCK")),
        )
