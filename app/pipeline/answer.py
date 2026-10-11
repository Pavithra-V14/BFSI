"""Stages 11 to 14: context guard, confidence, generation, output guard (spec 4.1 steps 11-14,
9.3-9.5).

- Context guard: chunks below the relevance floor (0.30 for Cohere / Jina scores, the stricter
  lexical
  floor for the lexical fallback) and chunks that read like instructions are dropped; if nothing is
  left the answer is the fixed "no reliable information" text (and a review-queue row).
- Confidence: the best reranked score under 0.40 is LOW confidence: the answer is still generated,
  states that it may be incorrect, asks for a clearer question and goes to the review queue. A low
  confidence answer is never cached.
- Generation: static instructions first, then the labelled chunks, then the question. NO history, no
  facts, no summary: `assemble_answer` has no place for them. One retry on invalid JSON, only if
  time
  allows.
- Output guard: schema, numeric, citation and groundedness checks (`check_output`). A failed check
  returns the answer WITH a warning and the unverified figures marked, never withheld; an unreadable
  answer is "temporarily unavailable".
"""

from __future__ import annotations

from typing import Any

from app.guardrails import messages as guard_messages
from app.guardrails.context import build_context_block, context_guard
from app.guardrails.disclaimer import disclaimer_decision, with_disclaimer
from app.guardrails.output import check_output, parse_answer
from app.guardrails.prompts import assemble_answer, get_prompt, prompt_set_version
from app.pipeline import messages
from app.pipeline.state import PersistPlan, PipelineState, Result
from app.pipeline.trace import Span, StageError, clean_clause_ref


def floor_for(ctx: PipelineState) -> float:
    cfg = ctx.deps.config
    return cfg.lexical_floor if ctx.reranker == "lexical" else cfg.relevance_floor


def context_stage(ctx: PipelineState) -> None:
    """Stage 11. Sets `kept`, `labels` and `block`, or ends the request with the fixed refusal."""
    verdict = context_guard(ctx.candidates, floor=floor_for(ctx))
    ctx.trace.record(
        chunk_ids=[c.chunk_id for c in verdict.kept],
        content_hashes=[c.content_hash or "" for c in verdict.kept],
        clause_refs=[
            clean_clause_ref(c.clause_ref or (c.heading_path[-1] if c.heading_path else None)) or ""
            for c in verdict.kept
        ],
        context_kept=len(verdict.kept),
        context_dropped=len(verdict.dropped),
        context_drop_patterns=sorted({p for d in verdict.dropped for p in d.patterns}),
        reranker=ctx.reranker,
    )
    if verdict.refuse:
        ctx.exited = "context_guard"
        ctx.result = Result(
            "refusal",
            guard_messages.NO_RELIABLE_CONTEXT,
            reason="no_context",
            degraded=list(ctx.degraded),
        )
        ctx.plan = PersistPlan(
            "fixed_reply",
            guard_messages.NO_RELIABLE_CONTEXT,
            history_excluded=True,
            review_reason="no_context",
            review_kind="refused",
            unclear=False,
        )
        return
    ctx.kept = verdict.kept
    ctx.block, ctx.labels = build_context_block(verdict.kept)
    ctx.trace.record(
        retrieved_chunks=[c.chunk_id for c in verdict.kept],
        retrieved_hashes=[c.content_hash or "" for c in verdict.kept],
        retrieved_clauses=[c.clause_ref or "" for c in verdict.kept],
    )


def confidence_stage(ctx: PipelineState) -> None:
    """Stage 12. Low confidence is a flag, not an exit: the answer is generated and labelled."""
    ctx.best_score = max((c.score for c in ctx.kept), default=0.0)
    # a follow-up whose rewrite failed was answered from its bare text: always low confidence
    ctx.low_confidence = ctx.best_score < ctx.deps.config.low_confidence_score or ctx.rewrite_failed
    ctx.trace.record(best_score=round(ctx.best_score, 3), low_confidence=ctx.low_confidence)


def _schema_ok(generation: Any) -> bool:
    raw = getattr(generation, "parsed", None) or getattr(generation, "text", "")
    answer, _ = parse_answer(raw)
    return answer is not None


async def generate_stage(ctx: PipelineState, span: Span, timeout_s: float) -> None:
    """Stage 13. One call; one retry when the JSON is invalid and there is time for it."""
    prompt = assemble_answer(
        get_prompt("answer"), ctx.block, ctx.question.text if ctx.question else ""
    )
    cfg = ctx.deps.config
    ctx.trace.record(prompt_answer=get_prompt("answer").short_hash, prompt_set=prompt_set_version())
    attempts = 1 + cfg.generator_retries
    min_start = cfg.limits_for("generate")[1]
    node_deadline = ctx.deps.clock() + timeout_s  # the node's own limit, not only the 30 s budget
    for attempt in range(attempts):
        left = min(node_deadline - ctx.deps.clock(), ctx.time_left())
        if attempt and left < min_start:
            break  # a retry that cannot finish is not started
        try:
            generation = await ctx.deps.gateway.complete(
                [{"role": "user", "content": prompt}],
                tier="answer",
                json_mode=True,
                timeout_s=max(left, 0.1),
            )
        except Exception as error:
            span.error_code = type(error).__name__
            raise StageError("generate", "providers_unavailable") from None
        span.decider = generation.decider
        span.model = generation.model
        span.provider = generation.provider
        span.key_fingerprint = generation.key_fingerprint
        span.input_tokens += generation.input_tokens
        span.output_tokens += generation.output_tokens
        span.cost_billed = (span.cost_billed or 0.0) + (generation.cost_billed or 0.0)
        span.shadow_cost += generation.shadow_cost
        ctx.generation = generation
        if _schema_ok(generation):
            break
        span.add(schema_retry=attempt + 1)
    if getattr(ctx.generation, "is_mock", False):
        ctx.degraded.append("mock_answer")


def _citation_meta(ctx: PipelineState, chunk_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    by_id = {c.chunk_id: c for c in ctx.kept}
    out: list[dict[str, Any]] = []
    for chunk_id in chunk_ids:
        chunk = by_id.get(chunk_id)
        if chunk is None:
            continue
        out.append(
            {
                "doc_id": chunk.doc_id,
                "source": chunk.source,
                "clause": chunk.clause_ref,
                "page": chunk.page_start,
                "page_end": chunk.page_end,
                "effective_date": chunk.effective_date,
            }
        )
    return out


def guard_stage(ctx: PipelineState) -> None:
    """Stage 14. Builds the answer `Result` and the persist plan."""
    generation = ctx.generation
    raw = getattr(generation, "parsed", None) or getattr(generation, "text", "")
    verdict = check_output(
        raw, chunks=ctx.kept, labels=ctx.labels, curated_domains=ctx.deps.curated_domains
    )
    if not verdict.schema_ok:
        raise StageError("output_guard", "unreadable_answer")
    ctx.verdict = verdict
    question = ctx.question.text if ctx.question else ""
    base = verdict.marked_text if verdict.unverified else verdict.text
    decision = disclaimer_decision(question, verdict.text)
    text = with_disclaimer(base, decision)
    warnings = list(verdict.warnings)
    if ctx.low_confidence:
        text = f"{text}\n\n{messages.LOW_CONFIDENCE}"
    degraded = list(ctx.degraded)
    mock = "mock_answer" in degraded
    ctx.cacheable = bool(
        verdict.grounded
        and not verdict.has_warning
        and not ctx.low_confidence
        and not degraded
        and not mock
    )
    cited = _citation_meta(ctx, verdict.cited_chunk_ids)
    ctx.answer_text = verdict.text
    ctx.disclaimer_shown = decision.show
    ctx.result = Result(
        "answer",
        text,
        citations=cited,
        warnings=warnings,
        confidence="low" if ctx.low_confidence else None,
        degraded=degraded,
        disclaimer=decision.show,
    )
    ctx.trace.record(
        grounded=bool(verdict.grounded),
        numeric_ok=bool(verdict.numeric_ok),
        citation_ok=bool(verdict.citation_ok),
        coverage=round(float(verdict.coverage), 3),
        warnings=len(warnings),
        cacheable=ctx.cacheable,
        cited_chunks=list(verdict.cited_chunk_ids),
    )
    ctx.plan = PersistPlan(
        "answer",
        verdict.text,
        grounded=bool(verdict.grounded and not verdict.has_warning and not ctx.low_confidence),
        citations=[
            {"doc_id": c["doc_id"], "clause": c["clause"], "page": c["page"]} for c in cited
        ],
        unclear=False,
        review_reason="low_confidence" if ctx.low_confidence else None,
        review_kind="low_confidence",
        surviving_doc_ids=frozenset(c.doc_id for c in ctx.kept),
        update_facts=True,
    )
