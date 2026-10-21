"""Gateway settings, read from environment variables by name (never from files).

Every variable read here is listed in ENV_NAMES, and tests check that each one is
documented in .env.example. Rate limits for Gemini, the Decisions API, embeddings
and Cohere are unpublished [T]; the defaults below are conservative and every one
can be changed with a GATEWAY_LIMITS_<PROVIDER> variable.

Settings are checked when the config is built, whether it comes from the environment
or is constructed directly: limits, timeouts and windows must be positive and finite,
plans are exactly "free" or "paid", the Jev model is pinned, and a key may appear in
only one pool. Error messages name the variable, never a value.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.gateway.embeddings import running_on_render
from app.gateway.keys import MAX_KEYS_PER_PROVIDER, ConfigError, parse_keys
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
    "EVAL_JUDGE_GROQ_API_KEYS",
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
    "EVAL_JUDGE_MODEL",
    "EVAL_JUDGE_GROQ_MODEL",
    "GATEWAY_TIMEOUT_SECONDS",
    "GATEWAY_CONTEXT_OVERRIDES",
    "GATEWAY_ENABLE_MOCK",
    "GATEWAY_REASONING_ALLOWANCE_TOKENS",
    "GATEWAY_REASONING_EFFORT_ANSWER",
    "GATEWAY_REASONING_EFFORT_DECIDE",
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
# Reasoning models count their hidden thinking tokens against the output limit, so an answer
# limit alone can leave no room for any text. The allowance is added to the answer limit, both
# for the limit sent to the provider and for the limiter reservation. [P]
DEFAULT_REASONING_ALLOWANCE = 512
# "low" is the lowest level that Gemini 3.x and Groq's gpt-oss both accept ("minimal" is
# rejected by Gemini). "off" sends no reasoning setting at all. [P]
REASONING_EFFORTS = ("low", "medium", "high")
DEFAULT_REASONING_EFFORT = "low"
EMBEDDING_DIMENSIONS = 768
JEV_CONTEXT_TOKENS = 32_000
DEFAULT_TIMEOUT_SECONDS = 10.0
KEY_COOLDOWN_SECONDS = 60.0
RATE_LIMIT_FLOOR_SECONDS = 5.0  # a Retry-After shorter than this is raised to it
KEY_FAILURE_COOLDOWN_SECONDS = 30.0  # a key that timed out or returned 5xx until the call ended
BREAKER_OPEN_SECONDS = 30.0
EMBEDDING_FAILOVER_COOLDOWN_SECONDS = 30.0


def _positive_int(value: int | None, name: str) -> int | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
        raise ConfigError(f"{name} must hold positive whole numbers")
    return value


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
    return validate_limits(Limits(**values), name)


def validate_limits(limits: Limits, name: str) -> Limits:
    for value in (limits.rpm, limits.tpm, limits.rpd, limits.tpd):
        _positive_int(value, name)
    return limits


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


def _parse_effort(raw: str | None, name: str) -> str | None:
    value = (raw or "").strip().lower()
    if not value:
        return DEFAULT_REASONING_EFFORT
    if value in ("off", "none"):
        return None
    if value not in REASONING_EFFORTS:
        raise ConfigError(f"{name} must be low, medium, high or off")
    return value


_JEV_MODEL_PATTERN = re.compile(r"typesafe/jev-[0-9][A-Za-z0-9._-]*")


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
        or not _JEV_MODEL_PATTERN.fullmatch(value)
    ):
        raise ConfigError(
            "JEV_MODEL must be an exact pinned version such as typesafe/jev-1.13, "
            "never a router or a latest alias"
        )


# The judge of the evaluation must come from a different model family than every model that can
# write an answer (the generator and its fallbacks): a model grading its own family's answers is
# not a check. The family is the vendor (a strict reading: Gemini and Gemma are both Google).
_FAMILY_WORDS = (
    (("gpt", "openai", "o1", "o3", "o4", "chatgpt"), "openai"),
    (("gemini", "gemma", "palm", "google"), "google"),
    (("llama", "meta-llama", "meta"), "meta"),
    (("qwen", "alibaba", "qwq"), "alibaba"),
    (("mistral", "mixtral", "ministral", "codestral", "magistral"), "mistral"),
    (("deepseek",), "deepseek"),
    (("claude", "anthropic"), "anthropic"),
    (("command", "cohere", "aya"), "cohere"),
    (("grok", "x-ai", "xai"), "xai"),
    (("phi", "microsoft"), "microsoft"),
    (("nemotron", "nvidia"), "nvidia"),
    (("glm", "zhipu", "z-ai"), "zhipu"),
    (("kimi", "moonshot", "moonshotai"), "moonshot"),
)


def model_family(model: str) -> str:
    """The vendor family of a model name such as 'openai/gpt-oss-120b' or 'gemini-3.8-flash'."""
    text = (model or "").strip().lower().removesuffix(":free")
    tokens = [t for t in re.split(r"[/\-_.:\s]+", text) if t]
    for words, family in _FAMILY_WORDS:
        if any(word in tokens for word in words):
            return family
    # unknown: the vendor prefix before the slash, else the first word
    prefix = text.split("/", 1)[0] if "/" in text else (tokens[0] if tokens else "")
    return prefix or "unknown"


def judge_model_problem(judge: str | None, answer_models: Mapping[str, str]) -> str | None:
    """None when the judge is usable, else the name of the answer model setting it clashes with."""
    if not judge:
        return None
    family = model_family(judge)
    for name in (
        "GROQ_MODEL",
        "GEMINI_MODEL",
        "OPENROUTER_MODEL",
        "GROQ_DECIDE_MODEL",
        "GEMINI_DECIDE_MODEL",
        "OPENROUTER_DECIDE_MODEL",
    ):
        model = answer_models.get(name)
        if model and model_family(model) == family:
            return name
    return None


def effective_judge_model(config: Any) -> str | None:
    """The judge model that answers first: the Groq one when it has a key, else the OpenRouter one.
    Works on any object that has the fields (a test double may have only `eval_judge_model`)."""
    if getattr(config, "eval_judge_groq_keys", None) and getattr(
        config, "eval_judge_groq_model", None
    ):
        return config.eval_judge_groq_model
    return getattr(config, "eval_judge_model", None)


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


# Evaluation keys and the offline mock answer are for the admin machine only (spec section 2,
# .env.example). Code enforces it: on Render the gateway refuses to start with any of them.
ADMIN_ONLY_KEY_VARIABLES = (
    "EVAL_GROQ_API_KEYS",
    "EVAL_GEMINI_API_KEYS",
    "EVAL_OPENROUTER_API_KEYS",
    "EVAL_JUDGE_GROQ_API_KEYS",
)


def check_render_rules(config: GatewayConfig, env: Mapping[str, str] | None = None) -> None:
    """Refuse eval keys and the mock switch when running on Render. Names only, never values."""
    env = os.environ if env is None else env
    if not running_on_render(env):
        return
    names = [name for name in ADMIN_ONLY_KEY_VARIABLES if (env.get(name) or "").strip()]
    if config.eval_openrouter_keys and "EVAL_OPENROUTER_API_KEYS" not in names:
        names.append("EVAL_OPENROUTER_API_KEYS")
    if config.eval_judge_groq_keys and "EVAL_JUDGE_GROQ_API_KEYS" not in names:
        names.append("EVAL_JUDGE_GROQ_API_KEYS")
    if config.enable_mock:
        names.append("GATEWAY_ENABLE_MOCK")
    if names:
        raise ConfigError(
            "refusing to start on Render: " + ", ".join(names) + " must only be set on the "
            "admin machine"
        )


@dataclass(frozen=True)
class GatewayConfig:
    # Key fields use repr=False so printing or logging the config never shows a key.
    groq_keys: list[str] = field(default_factory=list, repr=False)
    gemini_keys: list[str] = field(default_factory=list, repr=False)
    openrouter_keys: list[str] = field(default_factory=list, repr=False)
    eval_openrouter_keys: list[str] = field(default_factory=list, repr=False)
    eval_judge_groq_keys: list[str] = field(
        default_factory=list, repr=False
    )  # the judge's own Groq key
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
    eval_judge_model: str | None = None  # the OpenRouter judge (the fallback when Groq judges)
    eval_judge_groq_model: str | None = None  # [P] the judge on Groq, tried first when it has a key
    reasoning_allowance: int = DEFAULT_REASONING_ALLOWANCE
    reasoning_effort_answer: str | None = DEFAULT_REASONING_EFFORT
    reasoning_effort_decide: str | None = DEFAULT_REASONING_EFFORT

    def __post_init__(self) -> None:
        # A directly built config is as complete and as checked as one read from the environment.
        object.__setattr__(self, "models", {**DEFAULT_MODELS, **self.models})
        object.__setattr__(self, "limits", {**DEFAULT_LIMITS, **self.limits})
        object.__setattr__(self, "plans", {**DEFAULT_PLANS, **self.plans})
        validate_jev_model(self.models["JEV_MODEL"])
        for variable, judge in (
            ("EVAL_JUDGE_MODEL", self.eval_judge_model),
            ("EVAL_JUDGE_GROQ_MODEL", self.eval_judge_groq_model),
        ):
            clash = judge_model_problem(judge, self.models)
            if clash is not None:
                raise ConfigError(
                    f"{variable} must be a different model family than {clash} "
                    "and the other answer models"
                )
        if self.eval_judge_groq_keys and not self.eval_judge_groq_model:
            raise ConfigError("EVAL_JUDGE_GROQ_API_KEYS is set but EVAL_JUDGE_GROQ_MODEL is not")
        for provider, limits in self.limits.items():
            validate_limits(limits, f"GATEWAY_LIMITS_{provider}")
        for provider, plan in self.plans.items():
            if plan not in ("free", "paid"):
                raise ConfigError(f"GATEWAY_PLAN_{provider} must be free or paid")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ConfigError("GATEWAY_TIMEOUT_SECONDS must be a positive, finite number")
        for tokens in self.context_overrides.values():
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
                raise ConfigError("GATEWAY_CONTEXT_OVERRIDES must hold positive whole numbers")
        if (
            isinstance(self.reasoning_allowance, bool)
            or not isinstance(self.reasoning_allowance, int)
            or self.reasoning_allowance < 0
        ):
            raise ConfigError("GATEWAY_REASONING_ALLOWANCE_TOKENS must be zero or more")
        for name, effort in (
            ("GATEWAY_REASONING_EFFORT_ANSWER", self.reasoning_effort_answer),
            ("GATEWAY_REASONING_EFFORT_DECIDE", self.reasoning_effort_decide),
        ):
            if effort is not None and effort not in REASONING_EFFORTS:
                raise ConfigError(f"{name} must be low, medium, high or off")
        self._validate_keys()

    def _validate_keys(self) -> None:
        pools = {
            "GROQ_API_KEYS": (self.groq_keys, MAX_KEYS_PER_PROVIDER),
            "GEMINI_API_KEYS": (self.gemini_keys, MAX_KEYS_PER_PROVIDER),
            "OPENROUTER_API_KEYS": (self.openrouter_keys, MAX_KEYS_PER_PROVIDER),
            "EVAL_OPENROUTER_API_KEYS": (self.eval_openrouter_keys, MAX_KEYS_PER_PROVIDER),
            "EVAL_JUDGE_GROQ_API_KEYS": (self.eval_judge_groq_keys, MAX_KEYS_PER_PROVIDER),
            "GEMINI_EMBEDDING_KEY_A and GEMINI_EMBEDDING_KEY_B": (self.embedding_keys, 2),
            "JEV_OPENROUTER_API_KEY": ([self.jev_key] if self.jev_key else [], 1),
            "COHERE_API_KEY": ([self.cohere_key] if self.cohere_key else [], 1),
            "JINA_API_KEY": ([self.jina_key] if self.jina_key else [], 1),
        }
        seen: dict[str, str] = {}
        for name, (keys, limit) in pools.items():
            if len(keys) > limit:
                raise ConfigError(f"{name} holds more than {limit} key(s)")
            if len(set(keys)) != len(keys):
                raise ConfigError(f"{name} repeats a key")
            for key in keys:
                if key in seen and seen[key] != name:
                    raise ConfigError(f"the same key is used in {seen[key]} and {name}")
                seen[key] = name

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
        key_a = (env.get("GEMINI_EMBEDDING_KEY_A") or "").strip()
        key_b = (env.get("GEMINI_EMBEDDING_KEY_B") or "").strip()
        if key_b and not key_a:
            raise ConfigError("GEMINI_EMBEDDING_KEY_B is set but GEMINI_EMBEDDING_KEY_A is not")
        timeout_raw = (env.get("GATEWAY_TIMEOUT_SECONDS") or "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
        except ValueError:
            raise ConfigError("GATEWAY_TIMEOUT_SECONDS must be a number") from None
        allowance_raw = (env.get("GATEWAY_REASONING_ALLOWANCE_TOKENS") or "").strip()
        try:
            allowance = int(allowance_raw) if allowance_raw else DEFAULT_REASONING_ALLOWANCE
        except ValueError:
            raise ConfigError("GATEWAY_REASONING_ALLOWANCE_TOKENS must be a whole number") from None
        config = cls(
            groq_keys=parse_keys(env.get("GROQ_API_KEYS"), name="GROQ_API_KEYS"),
            gemini_keys=parse_keys(env.get("GEMINI_API_KEYS"), name="GEMINI_API_KEYS"),
            openrouter_keys=parse_keys(env.get("OPENROUTER_API_KEYS"), name="OPENROUTER_API_KEYS"),
            eval_openrouter_keys=parse_keys(
                env.get("EVAL_OPENROUTER_API_KEYS"), name="EVAL_OPENROUTER_API_KEYS"
            ),
            eval_judge_groq_keys=parse_keys(
                env.get("EVAL_JUDGE_GROQ_API_KEYS"), name="EVAL_JUDGE_GROQ_API_KEYS"
            ),
            jev_key=(env.get("JEV_OPENROUTER_API_KEY") or "").strip() or None,
            embedding_keys=[k for k in (key_a, key_b) if k],
            cohere_key=(env.get("COHERE_API_KEY") or "").strip() or None,
            jina_key=(env.get("JINA_API_KEY") or "").strip() or None,
            models=models,
            eval_judge_model=(env.get("EVAL_JUDGE_MODEL") or "").strip() or None,
            eval_judge_groq_model=(env.get("EVAL_JUDGE_GROQ_MODEL") or "").strip() or None,
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
            reasoning_allowance=allowance,
            reasoning_effort_answer=_parse_effort(
                env.get("GATEWAY_REASONING_EFFORT_ANSWER"), "GATEWAY_REASONING_EFFORT_ANSWER"
            ),
            reasoning_effort_decide=_parse_effort(
                env.get("GATEWAY_REASONING_EFFORT_DECIDE"), "GATEWAY_REASONING_EFFORT_DECIDE"
            ),
        )
        check_render_rules(config, env)
        return config

    @classmethod
    def for_evaluation(cls, env: Mapping[str, str] | None = None) -> GatewayConfig:
        """The gateway settings of an evaluation run (admin machine only, never Render).

        The system under test answers with the EVALUATION keys (`EVAL_GROQ_API_KEYS`,
        `EVAL_GEMINI_API_KEYS`), so a run never spends the production keys' limits or budgets.
        The judge uses its own Groq key (`EVAL_JUDGE_GROQ_API_KEYS`, model
        `EVAL_JUDGE_GROQ_MODEL`) first when both are set, and falls back to
        `EVAL_OPENROUTER_API_KEYS` with `EVAL_JUDGE_MODEL`. There is no OpenRouter answer fallback
        in a run and the offline mock answer is switched off (a mock text must never be scored)."""
        values = dict(os.environ if env is None else env)
        if running_on_render(values):
            raise ConfigError("refusing to start on Render: evaluation is for the admin machine")
        names = (
            "EVAL_GROQ_API_KEYS",
            "EVAL_GEMINI_API_KEYS",
            "EVAL_OPENROUTER_API_KEYS",
            "EVAL_JUDGE_GROQ_API_KEYS",
        )
        if not any((values.get(name) or "").strip() for name in names):
            raise ConfigError("no evaluation key is set (EVAL_GROQ_API_KEYS, EVAL_GEMINI_API_KEYS)")
        values["GROQ_API_KEYS"] = values.get("EVAL_GROQ_API_KEYS") or ""
        values["GEMINI_API_KEYS"] = values.get("EVAL_GEMINI_API_KEYS") or ""
        values["OPENROUTER_API_KEYS"] = ""
        values["GATEWAY_ENABLE_MOCK"] = ""
        return cls.from_env(values)
