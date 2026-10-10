"""The LangGraph graph: the 17 stages of the workflow diagram (spec 4, spec/query_workflow.png).

One node per stage, in the diagram's order. After a node the router looks at the state: no result
yet
means "next"; a result with a persist plan means "persist" (an early exit that is stored); a result
without a plan (rate limited, unavailable ...) means "respond". The normal path runs on through the
output guard to the cache write.

Every node is wrapped (`make_node`):
- it gets the SMALLER of its own timeout and the time left of the 30 s budget, and it is not started
  at all unless at least its minimum is left (a call that cannot finish is never begun);
- a failure is a structured `StageError` recorded in a span; a node that can degrade (triage,
  rewrite,
  cache, rerank, cache write, persist) carries on with its fallback, any other becomes the fixed
  "temporarily unavailable" answer;
- a span records its time, the decider / model / key fingerprint and token counts, never text.

The graph is compiled with NO checkpointer and tracing is switched off (`safety.py`): the state
holds
the raw question.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.accounts.tokens import utc_text
from app.guardrails import messages as guard_messages
from app.guardrails.allowlist import AllowList, load_allowlist
from app.guardrails.disclaimer import disclaimer_decision, with_disclaimer
from app.guardrails.injection import decide_injection
from app.guardrails.scan import local_scan
from app.memory.store import SessionNotFound, load_session
from app.pipeline import answer, cache_stage, messages, persist, retrieval, rewrite, triage
from app.pipeline.roles import UnknownRole, visible_roles
from app.pipeline.safety import disable_tracing, require_tracing_off
from app.pipeline.state import PersistPlan, PipelineState, Result, Triage
from app.pipeline.trace import Span, StageError

ORDER = (
    "authenticate",
    "local_scan",
    "triage",
    "injection_decision",
    "rewrite",
    "domain_gate",
    "cache_lookup",
    "embed",
    "retrieve",
    "rerank",
    "context_guard",
    "confidence",
    "generate",
    "output_guard",
    "cache_write",
    "persist",
    "respond",
)
ALWAYS_RUN = {"cache_write", "persist", "respond"}  # they run after a result exists
SKIPPABLE = {"triage", "rewrite", "cache_lookup", "rerank", "cache_write", "persist"}


class GraphState(TypedDict):
    ctx: PipelineState


Stage = Callable[[PipelineState, Span, float], Awaitable[None] | None]


def unavailable(ctx: PipelineState, code: str) -> None:
    ctx.exited = ctx.exited or "error"
    ctx.plan = None
    ctx.result = Result("unavailable", messages.UNAVAILABLE, reason=code)


def make_node(name: str, fn: Stage, fallback: Callable[[PipelineState], None] | None = None):
    async def run(state: GraphState) -> dict[str, Any]:
        ctx = state["ctx"]
        if ctx.result is not None and name not in ALWAYS_RUN:
            return {}
        timeout, minimum = ctx.deps.config.limits_for(name)
        left = ctx.time_left()
        began = ctx.deps.clock()
        span = Span(name, started_ms=round((began - ctx.started) * 1000, 1))
        failure: str | None = None
        try:
            if left < minimum:  # never start what cannot finish
                raise StageError(name, "no_time")
            limit = max(min(timeout, left), 0.05)
            outcome = fn(ctx, span, limit)
            if asyncio.iscoroutine(outcome):
                await asyncio.wait_for(outcome, timeout=limit)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            span.status, failure = "timeout", "timeout"
        except StageError as error:
            span.status, failure = "error", error.code
        except Exception as error:  # a bug or a dependency: a code, never text
            span.status, failure = "error", type(error).__name__
        finally:
            span.duration_ms = round((ctx.deps.clock() - began) * 1000, 1)
        if failure is not None:
            span.error_code = failure
            ctx.emit("stage_failed", stage=name, code=failure)
            if name in SKIPPABLE:
                if fallback is not None:
                    fallback(ctx)
            else:
                unavailable(ctx, failure)
        ctx.trace.finish(span)
        return {}

    run.__name__ = f"node_{name}"
    return run


# ---- the stages that are not in their own module
# ---------------------------------------------------------
async def authenticate(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 1: the role's visible `access_role` values, the per-user rate limit and the daily
    budget.
    (The route has already checked the token; Phase 10.)"""
    try:
        ctx.visible_roles = visible_roles(ctx.request.role)
    except UnknownRole:
        ctx.exited = "authenticate"
        ctx.result = Result("refusal", guard_messages.BLOCKED, reason="unknown_role")
        return
    usage = ctx.deps.usage
    if usage is None:
        span.status = "skipped"
        return
    rate = await asyncio.to_thread(usage.check_rate, ctx.request.user_id)
    if not rate.allowed:
        ctx.exited = "authenticate"
        ctx.result = Result(
            "rate_limited",
            messages.RATE_LIMITED,
            reason="rate_limited",
            retry_after=rate.retry_after,
        )
        return
    budget = await asyncio.to_thread(
        usage.check_budget,
        ctx.request.user_id,
        ctx.request.role,
        ctx.deps.config.estimated_request_tokens,
    )
    if not budget.allowed:
        ctx.exited = "authenticate"
        ctx.result = Result("over_budget", messages.OVER_BUDGET, reason="over_budget")


def _flush_queue(ctx: PipelineState) -> None:
    """Try the turns that could not be written earlier (best effort, before anything else)."""
    try:
        if len(ctx.deps.queue):
            ctx.deps.queue.flush(ctx.db, ctx.deps.now())
    except Exception:  # Postgres is still down: they stay queued
        pass


def _load_context(ctx: PipelineState) -> None:
    """The allow-list of the visible roles and, for an existing session, its state."""
    try:
        ctx.allow = load_allowlist(ctx.db, ctx.visible_roles)
    except Exception as error:  # Postgres down: mask with no approved terms (stricter, not looser)
        ctx.allow = AllowList()
        ctx.emit("allowlist_unavailable", reason=type(error).__name__)
    ctx.session_state = None
    if ctx.request.session_id is None:
        ctx.first_message = True
        return
    state = load_session(ctx.db, ctx.request.user_id, ctx.request.session_id)  # may raise
    ctx.session_state = state
    ctx.first_message = not state.messages and not state.summary_text


async def local_scan_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 2: limits, greeting, injection regex, masking (no model, no network)."""
    await asyncio.to_thread(_flush_queue, ctx)
    try:
        await asyncio.to_thread(_load_context, ctx)
    except SessionNotFound:
        ctx.exited = "local_scan"
        ctx.result = Result("session_not_found", messages.SESSION_NOT_FOUND, reason="not_found")
        return
    except Exception as error:  # Postgres down for a resume: go on without the history
        ctx.session_state = None
        ctx.first_message = False
        ctx.emit("session_unavailable", reason=type(error).__name__)
    try:
        scan = local_scan(
            ctx.raw_query or "",
            first_message=ctx.first_message,
            masker=ctx.deps.masker.masker,
            counter=ctx.deps.counter,
            allow_terms=ctx.allow.terms(),
        )
    except Exception as error:  # fail closed: a masker error refuses the question
        raise StageError("local_scan", "masker_failed") from error
    ctx.scan = scan
    span.add(tokens=scan.tokens, **{f"t_{k}": v for k, v in scan.timings_ms.items()})
    if scan.action == "refuse":
        ctx.exited = "local_scan"
        ctx.result = Result("refusal", scan.message or "", reason=scan.reason)
        return
    if scan.action == "reply":
        ctx.exited = "local_scan"
        text = scan.message or ""
        ctx.result = Result("reply", text, reason=scan.reason)
        ctx.plan = PersistPlan("fixed_reply", text)
        return
    ctx.pass1 = scan.masked
    ctx.question = scan.masked


async def triage_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 3: one Jev call; the choice questions use the distinct values stored in Qdrant."""
    values: dict[str, list[str]] = {name: [] for name in retrieval.SOFT_FIELDS}
    cache = ctx.deps.cache
    try:
        version = await asyncio.to_thread(cache.corpus_version) if cache is not None else None
        values = await asyncio.to_thread(
            ctx.values.get, ctx.deps.qdrant, ctx.deps.collection, version, ctx.deps.clock()
        )
    except Exception as error:
        ctx.emit("filter_values_unavailable", reason=type(error).__name__)
    ctx.triage = await triage.run_triage(ctx, span, limit, values)
    span.add(decider=ctx.triage.decider, triage_failed=ctx.triage.failed)
    ctx.trace.record(
        decider=ctx.triage.decider,
        in_domain=ctx.triage.in_domain_probability,
        injection_score=ctx.triage.injection_score,
    )


def triage_fallback(ctx: PipelineState) -> None:
    ctx.triage = Triage(decider="none", failed=True)


def injection_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 4: regex plus the Jev score. Strong blocks; mixed signals ask to reword; no score means
    the regexes alone decide."""
    assert ctx.scan is not None
    decision = decide_injection(ctx.scan.injection, ctx.triage.injection_score)
    span.add(action=decision.action, detector=(decision.detector or "none").replace(":", "_"))
    ctx.trace.record(injection_action=decision.action, injection_detector=decision.detector)
    if decision.action == "block":
        ctx.exited = "injection"
        ctx.result = Result("refusal", guard_messages.BLOCKED, reason="blocked")
        ctx.plan = PersistPlan("fixed_reply", guard_messages.BLOCKED, history_excluded=True)
    elif decision.action == "reword":
        ctx.exited = "injection"
        ctx.result = Result("reword", guard_messages.REWORD, reason="reword")
        ctx.plan = PersistPlan("fixed_reply", guard_messages.REWORD, history_excluded=True)


def domain_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 6: out-of-domain is refused (and queued) only on a real decision of a decider; a failed
    call, the keyword rules, or a follow-up that was rewritten from history never refuse."""
    cfg = ctx.deps.config
    triage_ = ctx.triage
    probability = triage_.in_domain_probability
    decided = triage_.decider in ("jev", "groq", "gemini", "openrouter") and probability is not None
    if not decided or ctx.rewritten or ctx.history_used:
        span.status = "skipped"
        return
    out_of_scope = 1.0 - probability
    span.add(out_of_scope=round(out_of_scope, 3))
    if out_of_scope >= cfg.out_of_domain_probability:
        ctx.exited = "domain_gate"
        ctx.result = Result("refusal", messages.OUT_OF_DOMAIN, reason="out_of_domain")
        ctx.plan = PersistPlan(
            "fixed_reply",
            messages.OUT_OF_DOMAIN,
            history_excluded=True,
            review_reason="out_of_domain",
            review_kind="refused",
        )


async def cache_lookup_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    entry = await cache_stage.lookup(ctx, span)
    span.add(cache=ctx.cache_status)
    ctx.trace.record(cache_status=ctx.cache_status, corpus_version=ctx.corpus_start)
    if entry is None:
        return
    payload = entry["payload"]
    text = payload["answer"]
    decision = disclaimer_decision(ctx.question.text if ctx.question else "", text)
    cited = payload.get("citations") or []
    ctx.exited = "cache"
    ctx.result = Result(
        "answer",
        with_disclaimer(text, decision),
        citations=list(cited),
        disclaimer=decision.show,
        cached=True,
    )
    docs = frozenset(
        str(c.get("doc_id", "")) for c in entry.get("chunks", []) if isinstance(c, dict)
    )
    ctx.plan = PersistPlan(
        "answer",
        text,
        grounded=True,
        citations=[
            {"doc_id": c.get("doc_id"), "clause": c.get("clause"), "page": c.get("page")}
            for c in cited
            if isinstance(c, dict) and c.get("doc_id")
        ],
        unclear=False,
        surviving_doc_ids=docs,
        update_facts=True,
    )


def cache_lookup_fallback(ctx: PipelineState) -> None:
    ctx.cache_status = "unavailable"


async def embed_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await retrieval.embed_question(ctx, span, limit)


async def retrieve_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await retrieval.retrieve_candidates(ctx, span)


async def rerank_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await retrieval.rerank_candidates(ctx, span, limit)


def rerank_fallback(ctx: PipelineState) -> None:
    """The rerank call timed out: order the candidates with the local lexical scores."""
    question = ctx.question.text if ctx.question else ""
    texts = [c.text for c in ctx.candidates]
    from dataclasses import replace

    from app.gateway.rerank import lexical_scores, top_items

    items = top_items(lexical_scores(question, texts), ctx.deps.config.top_k)
    ctx.reranker = "lexical"
    ctx.degraded.append("lexical_rerank")
    ctx.candidates = [
        replace(
            ctx.candidates[i.index], score=retrieval.lexical_relevance(question, texts[i.index])
        )
        for i in items
    ]


def context_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    answer.context_stage(ctx)


def confidence_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    answer.confidence_stage(ctx)


async def generate_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await answer.generate_stage(ctx, span, limit)


def guard_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    answer.guard_stage(ctx)


async def cache_write_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await cache_stage.write(ctx, span)


async def persist_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    await persist.persist_stage(ctx, span, limit)


def persist_fallback(ctx: PipelineState) -> None:
    """The persist stage could not run (no time left): queue the masked turn, do not lose it."""
    try:
        job = persist.build_job(ctx)
        ctx.drop_raw()
        ctx.deps.queue.add(job)
        ctx.emit("persist_queued", reason="no_time", queued=len(ctx.deps.queue))
    except Exception as error:
        ctx.drop_raw()
        ctx.emit("persist_masking_failed", reason=type(error).__name__)


def respond_stage(ctx: PipelineState, span: Span, limit: float) -> None:
    """Stage 17: the final result. Notices for a degraded mode are added here."""
    result = ctx.result
    if result is None:
        unavailable(ctx, "no_result")
        result = ctx.result
    assert result is not None
    if "lexical_rerank" in result.degraded or "lexical_rerank" in ctx.degraded:
        if messages.DEGRADED_RERANK not in result.warnings:
            result.warnings.append(messages.DEGRADED_RERANK)
    if "mock_answer" in ctx.degraded and messages.DEGRADED_MOCK not in result.warnings:
        result.warnings.append(messages.DEGRADED_MOCK)
    result.degraded = sorted(set(result.degraded) | set(ctx.degraded))
    result.correlation_id = ctx.request.correlation_id
    result.timings_ms = ctx.trace.timings_ms()
    result.tokens = ctx.trace.total_tokens()
    ctx.tokens = result.tokens
    ctx.trace.record(
        kind=result.kind,
        reason=result.reason,
        degraded=result.degraded,
        total_tokens=result.tokens,
        finished=utc_text(ctx.deps.now()),
    )
    result.audit = dict(ctx.trace.audit)


def route(name: str) -> Callable[[GraphState], str]:
    def decide(state: GraphState) -> str:
        ctx = state["ctx"]
        if name == "output_guard" and ctx.result is not None and ctx.plan is not None:
            return "next"  # a finished answer goes on to the cache write
        if ctx.result is None:
            return "next"
        return "persist" if ctx.plan is not None else "respond"

    return decide


def build_graph(pipeline: Any):
    """Compile the graph. No checkpointer; tracing off; one node per stage."""
    disable_tracing()
    require_tracing_off()
    stages: dict[str, tuple[Stage, Callable[[PipelineState], None] | None]] = {
        "authenticate": (authenticate, None),
        "local_scan": (local_scan_stage, None),
        "triage": (triage_stage, triage_fallback),
        "injection_decision": (injection_stage, None),
        "rewrite": (rewrite.rewrite_stage, None),
        "domain_gate": (domain_stage, None),
        "cache_lookup": (cache_lookup_stage, cache_lookup_fallback),
        "embed": (embed_stage, None),
        "retrieve": (retrieve_stage, None),
        "rerank": (rerank_stage, rerank_fallback),
        "context_guard": (context_stage, None),
        "confidence": (confidence_stage, None),
        "generate": (generate_stage, None),
        "output_guard": (guard_stage, None),
        "cache_write": (cache_write_stage, None),
        "persist": (persist_stage, persist_fallback),
        "respond": (respond_stage, None),
    }
    graph = StateGraph(GraphState)
    for name in ORDER:
        fn, fallback = stages[name]
        graph.add_node(name, make_node(name, fn, fallback))
    graph.add_edge(START, ORDER[0])
    for index, name in enumerate(ORDER[:-1]):
        following = ORDER[index + 1]
        if name in ("cache_write", "persist"):
            graph.add_edge(name, following)
        else:
            graph.add_conditional_edges(
                name, route(name), {"next": following, "persist": "persist", "respond": "respond"}
            )
    graph.add_edge("respond", END)
    return graph.compile()  # no checkpointer: the state holds the raw question
