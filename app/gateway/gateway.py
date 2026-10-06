"""The gateway: the single module that makes every model call (spec section 5).

LLM, embedding and rerank calls all go through the same key pools, limiter and
breaker. Tiers: answer (Groq, Gemini, OpenRouter), decide (Jev through the
Decisions API, then chat models answering the same typed questions, then rules),
and evaluation verdicts (Jev, then the judge model, then "not scored").

Every result records who answered: the decider, model, key fingerprint and the
reason any earlier step was skipped. Keys never appear in results, logs or errors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.gateway import config as cfg_module
from app.gateway.backend import Backend, LiteLLMBackend, RawCompletion
from app.gateway.backoff import MAX_ATTEMPTS, backoff_delay
from app.gateway.breaker import HALF_OPEN, OPEN, CircuitBreaker
from app.gateway.clock import Clock, SystemClock
from app.gateway.config import GatewayConfig
from app.gateway.context import ContextWindows
from app.gateway.cost import Cost, build_cost, load_pricing
from app.gateway.decisions import (
    DecisionClient,
    DecisionResult,
    OpenRouterDecisionsClient,
    Question,
    build_fallback_messages,
    extract_json,
    parse_fallback_answers,
    questions_payload,
    read_jev_response,
    validate_questions,
)
from app.gateway.embeddings import (
    BATCH_SIZE,
    DIMENSIONS,
    MAX_INPUT_TOKENS,
    HashEmbedder,
    format_document,
    format_query,
    normalise,
    running_on_render,
    vectors_ok,
)
from app.gateway.errors import (
    AllProvidersFailed,
    AuthFailed,
    BadEmbedding,
    BadRequest,
    ContextWindowExceeded,
    CreditExhausted,
    EmbeddingUnavailable,
    GatewayError,
    InternalError,
    InvalidOutput,
    RateLimited,
    SchemaSurprise,
    classify,
)
from app.gateway.keys import KeyPool
from app.gateway.rerank import (
    RerankResult,
    http_rerank,
    lexical_scores,
    top_items,
)
from app.gateway.stats import DeciderStats
from app.gateway.tokens import TokenCounter

log = logging.getLogger("app.gateway")

AUTH_COOLDOWN_SECONDS = 3_600.0
LITELLM_PREFIX = {"groq": "groq", "gemini": "gemini", "openrouter": "openrouter"}
MOCK_TEXT = "[offline mock answer: no model could be reached]"


@dataclass(frozen=True)
class Step:
    provider: str  # groq, gemini, openrouter: also the 'decider' name
    model: str
    pool: str | None = None  # key pool name; defaults to the provider

    @property
    def pool_name(self) -> str:
        return self.pool or self.provider

    @property
    def litellm_model(self) -> str:
        return f"{LITELLM_PREFIX[self.provider]}/{self.model}"


@dataclass
class StepOutcome:
    ok: bool
    value: Any = None
    fingerprint: str | None = None
    reason: str = ""


@dataclass
class CompletionResult:
    text: str
    decider: str
    model: str
    key_fingerprint: str | None
    parsed: Any = None
    provider: str | None = None
    fallback_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int | None = None
    truncated: bool = False  # ran out of output tokens (reasoning can use the whole budget)
    cost_billed: float | None = None
    cost_source: str = "none"
    shadow_cost: float = 0.0
    shadow_source: str = "none"
    latency_s: float = 0.0
    is_mock: bool = False
    skipped: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    key_fingerprint: str | None = None
    fallback_reason: str | None = None
    input_tokens: int = 0
    cost_billed: float | None = None
    cost_source: str = "none"
    shadow_cost: float = 0.0
    is_dev_fallback: bool = False  # hash vectors: never allowed in production


class Gateway:
    def __init__(
        self,
        config: GatewayConfig,
        *,
        backend: Backend | None = None,
        decision_client: DecisionClient | None = None,
        rerank_transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        rng: Callable[[], float] = random.random,
        counter: TokenCounter | None = None,
        pricing: dict[str, Any] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        allow_dev_embedding: bool = False,
        env: dict[str, str] | None = None,
    ) -> None:
        self.config = config
        self._backend = backend or LiteLLMBackend()
        self._decisions = decision_client or OpenRouterDecisionsClient()
        self._rerank_transport = rerank_transport
        self._clock = clock or SystemClock()
        self._rng = rng
        self._counter = counter or TokenCounter()
        self._pricing = pricing if pricing is not None else load_pricing()
        self._on_event = on_event
        self._allow_dev_embedding = allow_dev_embedding
        self._env = env
        self._windows = ContextWindows(config.context_overrides)
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.stats = DeciderStats(self._clock)
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}
        self._pools = self._build_pools()

    # ---- setup ----------------------------------------------------------------------

    def _pool(self, name: str, keys: list[str], limits_name: str, strategy: str = "round_robin"):
        return KeyPool(
            name,
            keys,
            self.config.limits[limits_name],
            self._clock,
            strategy=strategy,
            default_cooldown=cfg_module.KEY_COOLDOWN_SECONDS,
        )

    def _build_pools(self) -> dict[str, KeyPool]:
        c = self.config
        pools = {
            "groq": self._pool("groq", c.groq_keys, "GROQ"),
            "gemini": self._pool("gemini", c.gemini_keys, "GEMINI"),
            "openrouter": self._pool("openrouter", c.openrouter_keys, "OPENROUTER"),
            "eval_openrouter": self._pool("eval_openrouter", c.eval_openrouter_keys, "OPENROUTER"),
            "gemini_embedding": self._pool(
                "gemini_embedding", c.embedding_keys, "EMBEDDING", "ordered"
            ),
        }
        if c.jev_key:
            pools["jev"] = self._pool("jev", [c.jev_key], "JEV")
        if c.cohere_key:
            pools["cohere"] = self._pool("cohere", [c.cohere_key], "COHERE")
        if c.jina_key:
            pools["jina"] = self._pool("jina", [c.jina_key], "JINA")
        return pools

    def _breaker(self, pool: str, tier: str) -> CircuitBreaker:
        key = (pool, tier)
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(
                self._clock, open_seconds=cfg_module.BREAKER_OPEN_SECONDS
            )
        return self._breakers[key]

    def _emit(self, name: str, **fields: Any) -> None:
        self.events.append((name, fields))
        log.info("gateway event %s %s", name, fields)
        if self._on_event:
            self._on_event(name, fields)

    def _model(self, name: str) -> str:
        return self.config.models[name]

    # ---- chains ---------------------------------------------------------------------

    def answer_steps(self) -> list[Step]:
        return [
            Step("groq", self._model("GROQ_MODEL")),
            Step("gemini", self._model("GEMINI_MODEL")),
            Step("openrouter", self._model("OPENROUTER_MODEL")),
        ]

    def decide_steps(self) -> list[Step]:
        """Chat-model deciders after Jev: Groq 20b, Gemini, OpenRouter, then the answer models."""
        steps = [
            Step("groq", self._model("GROQ_DECIDE_MODEL")),
            Step("gemini", self._model("GEMINI_DECIDE_MODEL")),
            Step("openrouter", self._model("OPENROUTER_DECIDE_MODEL")),
            *self.answer_steps(),
        ]
        seen: set[tuple[str, str]] = set()
        unique = []
        for step in steps:
            if (step.provider, step.model) not in seen:
                seen.add((step.provider, step.model))
                unique.append(step)
        return unique

    def judge_steps(self) -> list[Step]:
        judge = self.config.eval_judge_model
        return [Step("openrouter", judge, pool="eval_openrouter")] if judge else []

    # ---- timing ---------------------------------------------------------------------

    def _timing(self, timeout_s: float | None) -> tuple[float, float | None]:
        """Return (per-call timeout, deadline). The caller's budget can only shorten it."""
        per_call = self.config.timeout_seconds
        if timeout_s is None:
            return per_call, None
        return min(per_call, timeout_s), self._clock.now() + timeout_s

    def _remaining(self, per_call: float, deadline: float | None) -> float:
        if deadline is None:
            return per_call
        return max(0.1, min(per_call, deadline - self._clock.now()))

    # ---- the shared step runner -----------------------------------------------------

    async def _run_step(
        self,
        pool_name: str,
        tier: str,
        estimated_tokens: int,
        call: Callable[[str], Awaitable[tuple[Any, int]]],
        *,
        max_attempts: int = MAX_ATTEMPTS,
        failure_cooldown: float | None = None,
        deadline: float | None = None,
    ) -> StepOutcome:
        """Run one provider step: rotate keys, limit, retry, and trip the breaker.

        `call(key)` returns (value, actual_tokens). A 429 cools down only that key and the
        call moves to the next key. Timeouts and 5xx errors are retried with backoff, up to
        `max_attempts`. The breaker trips only when the whole pool is limited or failing.
        """
        pool = self._pools.get(pool_name)
        if pool is None or pool.size == 0:
            return StepOutcome(False, reason="no_keys")
        if not pool.can_ever_fit(estimated_tokens):
            return StepOutcome(False, reason="too_large_for_key")
        breaker = self._breaker(pool_name, tier)
        if breaker.state == HALF_OPEN and pool.status(estimated_tokens) != "healthy":
            # The open period is over, but no key could take the probe yet: keep waiting.
            return StepOutcome(False, reason="circuit_open")
        if not breaker.allow():
            return StepOutcome(False, reason="circuit_open")
        attempts = 0
        reason = "all_keys_limited"
        failed: set[str] = set()  # fingerprints of keys that failed during this call
        while True:
            if deadline is not None and self._clock.now() >= deadline:
                breaker.record_neutral()
                return StepOutcome(False, reason="deadline")
            lease = pool.acquire(estimated_tokens)
            if lease is None:
                breaker.record_failure()
                self._emit("all_keys_exhausted", provider=pool_name, tier=tier)
                return StepOutcome(False, reason=reason)
            try:
                value, actual = await call(lease.key)
            except asyncio.CancelledError:
                # The caller gave up (for example its time budget ran out). Always leave a clean
                # state: this call's tokens count as zero and a half-open probe is released.
                pool.correct(lease, 0)
                breaker.record_neutral()
                raise
            except Exception as exc:
                error = classify(exc)
                pool.correct(lease, error.used_tokens)  # a failed answer still used tokens
                if isinstance(error, ContextWindowExceeded):
                    breaker.record_neutral()
                    raise error from None
                if isinstance(error, InternalError):
                    # A bug in our own code, not an outage: no retry, the breaker stays neutral,
                    # and only the exception class name is logged (never its message).
                    breaker.record_neutral()
                    log.error(
                        "gateway internal error: pool=%s exception=%s",
                        pool_name,
                        error.exception_name,
                    )
                    self._emit(
                        "internal_error",
                        provider=pool_name,
                        tier=tier,
                        exception=error.exception_name,
                    )
                    return StepOutcome(False, reason=error.reason, fingerprint=lease.fingerprint)
                if isinstance(error, RateLimited):
                    pool.cool_down(lease.fingerprint, error.retry_after)
                    failed.add(lease.fingerprint)
                    reason = "rate_limited"
                    continue
                if isinstance(error, (AuthFailed, CreditExhausted)):
                    pool.cool_down(lease.fingerprint, AUTH_COOLDOWN_SECONDS)
                    failed.add(lease.fingerprint)
                    reason = error.reason
                    continue
                if isinstance(error, SchemaSurprise):
                    breaker.record_failure()
                    return StepOutcome(False, reason=error.reason, fingerprint=lease.fingerprint)
                if isinstance(error, (BadRequest, InvalidOutput)):
                    breaker.record_neutral()
                    return StepOutcome(False, reason=error.reason, fingerprint=lease.fingerprint)
                # A timeout or a 5xx: retry with backoff, possibly on the next key.
                failed.add(lease.fingerprint)
                reason = error.reason
                if failure_cooldown is not None:
                    pool.cool_down(lease.fingerprint, failure_cooldown)
                attempts += 1
                if attempts >= max_attempts:
                    # Give up on this provider for this call. The breaker trips only if every
                    # key of the pool is failing or cooling down; otherwise the provider is
                    # merely skipped this time.
                    if len(failed | pool.cooling_fingerprints()) >= pool.size:
                        breaker.record_failure()
                    else:
                        breaker.record_neutral()
                    return StepOutcome(False, reason=reason, fingerprint=lease.fingerprint)
                await self._clock.sleep(backoff_delay(attempts - 1, rng=self._rng))
                continue
            pool.correct(lease, actual)
            breaker.record_success()
            return StepOutcome(True, value, lease.fingerprint)

    # ---- chat completions -----------------------------------------------------------

    def _supports_response_format(self, litellm_model: str) -> bool:
        try:
            import litellm

            provider = litellm_model.split("/", 1)[0]
            params = litellm.get_supported_openai_params(
                model=litellm_model.split("/", 1)[1], custom_llm_provider=provider
            )
            return bool(params) and "response_format" in params
        except Exception:
            return False

    async def _llm_step(
        self,
        step: Step,
        tier: str,
        messages: list[dict[str, Any]],
        *,
        prompt_tokens: int,
        max_tokens: int,
        json_mode: bool,
        validator: Callable[[str], Any] | None,
        per_call: float,
        deadline: float | None,
    ) -> StepOutcome:
        model = step.litellm_model
        response_format = (
            {"type": "json_object"} if json_mode and self._supports_response_format(model) else None
        )

        async def call(key: str) -> tuple[tuple[RawCompletion, Any], int]:
            raw = await self._backend.complete(
                model=model,
                messages=messages,
                api_key=key,
                max_tokens=max_tokens,
                timeout=self._remaining(per_call, deadline),
                response_format=response_format,
            )
            used_in = raw.prompt_tokens if raw.prompt_tokens is not None else prompt_tokens
            used_out = (
                raw.completion_tokens
                if raw.completion_tokens is not None
                else self._counter.count_text(raw.text)
            )
            try:
                parsed = validator(raw.text) if validator else None
            except GatewayError as error:
                error.used_tokens = used_in + used_out  # the provider counted these tokens
                raise
            except Exception as exc:  # a bug in a validator: internal, never retried
                internal = InternalError(type(exc).__name__)
                internal.used_tokens = used_in + used_out
                raise internal from None
            return (raw, parsed), used_in + used_out

        return await self._run_step(
            step.pool_name, tier, prompt_tokens + max_tokens, call, deadline=deadline
        )

    def _with_plan(self, plan_key: str, cost: Cost) -> Cost:
        """On a free plan the billed cost is $0; the shadow cost keeps the list price."""
        if self.config.plans.get(plan_key, "free") != "free":
            return cost
        source = "free_model" if cost.billed_source == "free_model" else "free_plan"
        return Cost(0.0, source, cost.shadow, cost.shadow_source)

    def _completion_result(
        self,
        step: Step,
        outcome: StepOutcome,
        skipped: list[tuple[str, str]],
        started: float,
        prompt_tokens: int,
    ) -> CompletionResult:
        raw, parsed = outcome.value
        tokens_in = raw.prompt_tokens if raw.prompt_tokens is not None else prompt_tokens
        tokens_out = (
            raw.completion_tokens
            if raw.completion_tokens is not None
            else self._counter.count_text(raw.text)
        )
        cost = self._with_plan(
            step.provider.upper(),
            build_cost(
                step.litellm_model,
                tokens_in,
                tokens_out,
                self._pricing,
                billed=raw.cost_usd,
                billed_source=raw.cost_source,
            ),
        )
        return CompletionResult(
            text=raw.text,
            parsed=parsed,
            decider=step.provider,
            provider=step.provider,
            model=step.model,
            key_fingerprint=outcome.fingerprint,
            fallback_reason=_reason(skipped),
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            reasoning_tokens=raw.reasoning_tokens,
            truncated=raw.finish_reason == "length",
            cost_billed=cost.billed,
            cost_source=cost.billed_source,
            shadow_cost=cost.shadow,
            shadow_source=cost.shadow_source,
            latency_s=self._clock.now() - started,
            skipped=list(skipped),
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tier: str = "answer",
        max_tokens: int | None = None,
        json_mode: bool = False,
        timeout_s: float | None = None,
        only_provider: str | None = None,
    ) -> CompletionResult:
        """A chat completion on the answer or decide tier (decide skips Jev: no prose from Jev).

        The prompt is built once by the caller and reused on every fallback. A prompt that
        does not fit the smallest planned window raises ContextWindowExceeded and is never
        retried. If every step fails, AllProvidersFailed is raised ("temporarily unavailable").
        """
        if tier not in ("answer", "decide"):
            raise ValueError("tier must be 'answer' or 'decide'")
        started = self._clock.now()
        max_tokens = max_tokens or (
            cfg_module.MAX_OUTPUT_TOKENS_JSON if json_mode else cfg_module.MAX_OUTPUT_TOKENS_ANSWER
        )
        steps = self.answer_steps() if tier == "answer" else self.decide_steps()
        if only_provider is not None:  # a diagnostic switch, used by the smoke test
            steps = [s for s in steps if s.provider == only_provider]
        prompt_tokens = self._counter.count_messages(messages)
        self._windows.check(prompt_tokens, max_tokens, [s.litellm_model for s in steps])
        per_call, deadline = self._timing(timeout_s)
        validator = extract_json if json_mode else None
        skipped: list[tuple[str, str]] = []
        for step in steps:
            outcome = await self._llm_step(
                step,
                tier,
                messages,
                prompt_tokens=prompt_tokens,
                max_tokens=max_tokens,
                json_mode=json_mode,
                validator=validator,
                per_call=per_call,
                deadline=deadline,
            )
            if outcome.ok:
                result = self._completion_result(step, outcome, skipped, started, prompt_tokens)
                self.stats.record(tier, result.decider)
                return result
            skipped.append((step.provider, outcome.reason))
            self._emit("step_skipped", tier=tier, provider=step.provider, reason=outcome.reason)
        if tier == "answer" and self.config.enable_mock:
            self.stats.record(tier, "mock")
            return CompletionResult(
                text=MOCK_TEXT,
                decider="mock",
                model="mock",
                key_fingerprint=None,
                provider="mock",
                fallback_reason=_reason(skipped),
                is_mock=True,
                skipped=skipped,
            )
        raise AllProvidersFailed(skipped)

    # ---- typed decisions ------------------------------------------------------------

    async def decide(
        self,
        questions: list[Question],
        state: Any,
        *,
        tier: str = "decide",
        timeout_s: float | None = None,
    ) -> DecisionResult:
        """Answer typed questions: Jev first, then chat models, then rules ('not scored' for eval).

        `state` must already be masked text. When nothing answers, the result has
        answers=None and decider 'rules' (or 'not_scored'): the caller applies its own rules.
        """
        if tier not in ("decide", "eval"):
            raise ValueError("tier must be 'decide' or 'eval'")
        validate_questions(questions)
        started = self._clock.now()
        per_call, deadline = self._timing(timeout_s)
        skipped: list[tuple[str, str]] = []

        result = await self._try_jev(questions, state, tier, per_call, deadline, started)
        if isinstance(result, DecisionResult):
            return result
        skipped.append(("jev", result))
        self._emit("jev_unavailable", tier=tier, reason=result)

        steps = self.decide_steps() if tier == "decide" else self.judge_steps()
        messages = build_fallback_messages(state, questions)
        prompt_tokens = self._counter.count_messages(messages)
        max_tokens = cfg_module.MAX_OUTPUT_TOKENS_JSON
        try:
            self._windows.check(prompt_tokens, max_tokens, [s.litellm_model for s in steps])
        except ContextWindowExceeded as error:
            skipped.append(("all", error.reason))
            steps = []
        for step in steps:
            outcome = await self._llm_step(
                step,
                tier,
                messages,
                prompt_tokens=prompt_tokens,
                max_tokens=max_tokens,
                json_mode=True,
                validator=lambda text: parse_fallback_answers(text, questions),
                per_call=per_call,
                deadline=deadline,
            )
            if outcome.ok:
                completion = self._completion_result(step, outcome, skipped, started, prompt_tokens)
                self.stats.record(tier, step.provider)
                return DecisionResult(
                    answers=outcome.value[1],
                    decider=step.provider,
                    model=step.model,
                    provider=step.provider,
                    key_fingerprint=outcome.fingerprint,
                    fallback_reason=completion.fallback_reason,
                    calibrated=False,
                    cost_billed=completion.cost_billed,
                    cost_source=completion.cost_source,
                    shadow_cost=completion.shadow_cost,
                    input_tokens=completion.input_tokens,
                    output_tokens=completion.output_tokens,
                    latency_s=completion.latency_s,
                    skipped=list(skipped),
                )
            skipped.append((step.provider, outcome.reason))
        decider = "rules" if tier == "decide" else "not_scored"
        self.stats.record(tier, decider)
        self._emit("decision_fell_to_rules", tier=tier)
        return DecisionResult(
            answers=None,
            decider=decider,
            fallback_reason=_reason(skipped),
            latency_s=self._clock.now() - started,
            skipped=skipped,
        )

    async def _try_jev(
        self,
        questions: list[Question],
        state: Any,
        tier: str,
        per_call: float,
        deadline: float | None,
        started: float,
    ) -> DecisionResult | str:
        """Return a DecisionResult, or the reason Jev was not used."""
        payload = questions_payload(questions)
        tokens = self._counter.count_text(
            json.dumps(state, ensure_ascii=False)
        ) + self._counter.count_text(json.dumps(payload, ensure_ascii=False))
        if tokens > cfg_module.JEV_CONTEXT_TOKENS:
            return "too_large"
        model = self._model("JEV_MODEL")

        async def call(key: str) -> tuple[dict[str, Any], int]:
            data = await self._decisions.decide(
                model=model,
                state=state,
                questions=payload,
                api_key=key,
                timeout=self._remaining(per_call, deadline),
            )
            try:
                parsed = read_jev_response(data, questions)
            except GatewayError as error:
                error.used_tokens = _reported_input_tokens(data)
                raise
            return parsed, parsed["input_tokens"] or tokens

        outcome = await self._run_step("jev", tier, tokens, call, max_attempts=1, deadline=deadline)
        if not outcome.ok:
            return outcome.reason
        parsed = outcome.value
        cost = self._with_plan(
            "JEV",
            build_cost(
                model,
                parsed["input_tokens"],
                parsed["output_tokens"],
                self._pricing,
                billed=parsed["cost"],
                billed_source="provider_reported" if parsed["cost"] is not None else "unknown",
            ),
        )
        self.stats.record(tier, "jev")
        return DecisionResult(
            answers=parsed["answers"],
            decider="jev",
            model=model,
            model_snapshot=parsed["snapshot"] or None,
            provider=parsed["provider"] or None,
            key_fingerprint=outcome.fingerprint,
            calibrated=True,
            cost_billed=cost.billed,
            cost_source=cost.billed_source,
            shadow_cost=cost.shadow,
            input_tokens=parsed["input_tokens"],
            output_tokens=parsed["output_tokens"],
            latency_s=self._clock.now() - started,
        )

    # ---- embeddings -----------------------------------------------------------------

    async def embed(
        self,
        texts: Sequence[str],
        *,
        task: str = "document",
        titles: Sequence[str | None] | None = None,
        timeout_s: float | None = None,
    ) -> EmbeddingResult:
        """Embed texts with gemini-embedding-2 at 768 dimensions: key A, then key B.

        If both keys fail, EmbeddingUnavailable is raised ("temporarily unavailable"),
        unless the development-only hash embedder was explicitly allowed.
        """
        if task not in ("query", "document"):
            raise ValueError("task must be 'query' or 'document'")
        model = f"gemini/{self._model('GEMINI_EMBEDDING_MODEL')}"
        if not texts:
            return EmbeddingResult([], model)
        for text in texts:
            if self._counter.count_text(text) > MAX_INPUT_TOKENS:
                raise ContextWindowExceeded()
        if task == "query":
            inputs = [format_query(t) for t in texts]
        else:
            tlist = list(titles) if titles is not None else [None] * len(texts)
            inputs = [format_document(t, ti) for t, ti in zip(texts, tlist, strict=True)]
        per_call, deadline = self._timing(timeout_s)
        vectors: list[list[float]] = []
        tokens_in = 0
        billed: float | None = 0.0
        source = "none"
        fingerprint: str | None = None
        first_fp = self._first_embedding_fingerprint()
        for start in range(0, len(inputs), BATCH_SIZE):
            batch = inputs[start : start + BATCH_SIZE]
            estimate = sum(self._counter.count_text(x) for x in batch)

            async def call(key: str, batch: list[str] = batch, estimate: int = estimate):
                raw = await self._backend.embed(
                    model=model,
                    inputs=batch,
                    api_key=key,
                    dimensions=DIMENSIONS,
                    timeout=self._remaining(per_call, deadline),
                )
                if not vectors_ok(raw.vectors, len(batch)):
                    bad = BadEmbedding()  # treated like a failing key: the next key is tried
                    bad.used_tokens = raw.prompt_tokens or estimate
                    raise bad
                return raw, raw.prompt_tokens or estimate

            outcome = await self._run_step(
                "gemini_embedding",
                "embed",
                estimate,
                call,
                max_attempts=2,
                failure_cooldown=cfg_module.EMBEDDING_FAILOVER_COOLDOWN_SECONDS,
                deadline=deadline,
            )
            if not outcome.ok:
                self._emit("embedding_unavailable", reason=outcome.reason)
                return self._dev_or_raise(texts, outcome.reason, model)
            raw = outcome.value
            vectors.extend(normalise(v) for v in raw.vectors)
            tokens_in += raw.prompt_tokens or estimate
            fingerprint = outcome.fingerprint
            if raw.cost_usd is None or billed is None:
                billed = None
            else:
                billed += raw.cost_usd
            source = raw.cost_source
        cost = self._with_plan(
            "EMBEDDING",
            build_cost(model, tokens_in, 0, self._pricing, billed=billed, billed_source=source),
        )
        self.stats.record("embed", "gemini_embedding")
        return EmbeddingResult(
            vectors=vectors,
            model=model,
            key_fingerprint=fingerprint,
            fallback_reason="primary key failed" if fingerprint != first_fp else None,
            input_tokens=tokens_in,
            cost_billed=cost.billed,
            cost_source=cost.billed_source,
            shadow_cost=cost.shadow,
        )

    def _first_embedding_fingerprint(self) -> str | None:
        from app.gateway.keys import fingerprint

        keys = self.config.embedding_keys
        return fingerprint(keys[0]) if keys else None

    def _dev_or_raise(self, texts: Sequence[str], reason: str, model: str) -> EmbeddingResult:
        if self._allow_dev_embedding and not running_on_render(self._env):
            embedder = HashEmbedder(self._env)
            self.stats.record("embed", "hash_dev")
            return EmbeddingResult(
                vectors=[embedder.embed(t) for t in texts],
                model=embedder.model,
                fallback_reason=reason,
                is_dev_fallback=True,
            )
        raise EmbeddingUnavailable([("gemini_embedding", reason)])

    # ---- rerank ---------------------------------------------------------------------

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
        timeout_s: float | None = None,
    ) -> RerankResult:
        """Cohere, then Jina, then lexical. `degraded` is True when lexical was used."""
        if not documents:
            return RerankResult([], "none")
        per_call, deadline = self._timing(timeout_s)
        estimate = self._counter.count_text(query) + sum(
            self._counter.count_text(d) for d in documents
        )
        skipped: list[tuple[str, str]] = []
        for provider, model_var in (
            ("cohere", "COHERE_RERANK_MODEL"),
            ("jina", "JINA_RERANK_MODEL"),
        ):
            model = self._model(model_var)

            async def call(key: str, provider: str = provider, model: str = model):
                items = await http_rerank(
                    provider,
                    model=model,
                    query=query,
                    documents=documents,
                    top_n=top_n,
                    api_key=key,
                    timeout=self._remaining(per_call, deadline),
                    transport=self._rerank_transport,
                )
                return items, estimate

            outcome = await self._run_step(provider, "rerank", estimate, call, deadline=deadline)
            if outcome.ok:
                self.stats.record("rerank", provider)
                return RerankResult(
                    items=outcome.value,
                    reranker=provider,
                    model=model,
                    key_fingerprint=outcome.fingerprint,
                    fallback_reason=_reason(skipped),
                    input_tokens=estimate,
                    skipped=skipped,
                )
            skipped.append((provider, outcome.reason))
            self._emit("step_skipped", tier="rerank", provider=provider, reason=outcome.reason)
        self.stats.record("rerank", "lexical")
        return RerankResult(
            items=top_items(lexical_scores(query, documents), top_n),
            reranker="lexical",
            fallback_reason=_reason(skipped),
            degraded=True,
            skipped=skipped,
        )

    # ---- health ---------------------------------------------------------------------

    def health(self) -> dict[str, dict[str, Any]]:
        """Per provider: healthy, cooling_down or circuit_open (spec 5.3)."""
        report: dict[str, dict[str, Any]] = {}
        for name, pool in self._pools.items():
            if pool.size == 0:
                continue
            tiers: dict[str, str] = {}
            for (pool_name, tier), breaker in self._breakers.items():
                if pool_name == name:
                    tiers[tier] = "circuit_open" if breaker.state == OPEN else breaker.state
            if any(state == "circuit_open" for state in tiers.values()):
                state = "circuit_open"
            else:
                state = pool.status()
            report[name] = {"keys": pool.size, "state": state, "tiers": tiers}
        return report


def _reported_input_tokens(data: Any) -> int:
    """Input tokens a Decisions response reports, even if the answers were unusable."""
    try:
        return max(0, int(data["usage"]["input_tokens"]))
    except (KeyError, TypeError, ValueError):
        return 0


def _reason(skipped: list[tuple[str, str]]) -> str | None:
    return "; ".join(f"{provider}: {reason}" for provider, reason in skipped) or None


__all__ = ["CompletionResult", "EmbeddingResult", "Gateway", "GatewayError", "Step"]
