"""What each Langfuse observation shows as its input, output and metadata (Change round 1, step 5).

The trace of a request has one observation per stage. This module decides, for every stage, what is
put in its INPUT and OUTPUT, so that Langfuse shows what the stage was given and what it produced
instead of only timings. Everything here is a pure function of the finished request: it reads the
state, it never calls a service and it never changes anything.

WHICH TEXT MAY APPEAR (and only this):
- the QUESTION and the ANSWER as the chat store holds them: the pass-2 masked copies that
  `mask_for_storage` made (`ctx.stored_copy`). Never the raw question, never the pass-1 copy the
  models got (a `ModelCopy` raises if it is formatted, on purpose), never a rewritten question,
  never history or a summary;
- the text of the retrieved passages and the instructions of the answer prompt: public company
  documents and our own prompt file, no user data;
- fixed words, numbers, identifiers and hashes.
A masked copy that still looks like personal data (an address, a phone, a PAN, an Aadhaar or a
passport number) is withheld and says so. `LANGFUSE_SEND_TEXT=false` switches every text off.

Every builder is wrapped: a bug in one stage gives that stage no content, nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.guardrails.masking import looks_personal
from app.guardrails.prompts import assemble_answer, get_prompt, prompt_set_version

MAX_QUESTION_CHARS = 4_000
MAX_CHUNK_CHARS = 2_000  # one retrieved passage
MAX_BLOCK_CHARS = 14_000  # the whole document block of the prompt
MAX_CHUNKS = 30
WITHHELD = "[withheld: the masked text still looked like personal data]"
NO_HISTORY = "none: the answer model never sees the chat history"

# The Langfuse observation type of each stage (how Langfuse draws and groups it).
KINDS = {
    "authenticate": "span",
    "local_scan": "guardrail",
    "triage": "generation",
    "injection_decision": "guardrail",
    "rewrite": "generation",
    "domain_gate": "guardrail",
    "cache_lookup": "span",
    "embed": "embedding",
    "retrieve": "retriever",
    "rerank": "retriever",
    "context_guard": "guardrail",
    "confidence": "span",
    "generate": "generation",
    "output_guard": "guardrail",
    "cache_write": "span",
    "persist": "span",
    "respond": "span",
    "summary_fold": "generation",
}


@dataclass(frozen=True)
class Texts:
    """The only user-derived text of one request that may reach a trace."""

    question: str | None = None
    answer: str | None = None
    withheld: tuple[str, ...] = ()
    enabled: bool = False


@dataclass
class ChildContent:
    """A synthetic observation inside a stage (it has no timing of its own)."""

    name: str
    kind: str
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageContent:
    kind: str
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[ChildContent] = field(default_factory=list)


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " [cut]"


def make_texts(ctx: Any, enabled: bool) -> Texts:
    """The stored question and answer (masked, pass 2), or nothing when text is off or the request
    stored nothing (an early exit that has no plan, an unavailable answer)."""
    if not enabled:
        return Texts()
    copy = getattr(ctx, "stored_copy", None)
    if not (isinstance(copy, tuple) and len(copy) == 2 and all(isinstance(t, str) for t in copy)):
        return Texts(enabled=True)
    values: dict[str, str] = {}
    withheld: list[str] = []
    for name, text in zip(("question", "answer"), copy, strict=True):
        if looks_personal(text):
            withheld.append(name)
            values[name] = WITHHELD
        else:
            values[name] = cut(text, MAX_QUESTION_CHARS)
    return Texts(values["question"], values["answer"], tuple(withheld), True)


# ---- small readers (each tolerates a missing value) --------------------------------------------
def _num(value: Any, digits: int = 4) -> float | None:
    return round(float(value), digits) if isinstance(value, (int, float)) else None


def _chunk_row(
    rank: int, chunk: Any, *, text: bool, kept: set[str] | None = None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "rank": rank,
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "clause": chunk.clause_ref,
        "pages": (
            None
            if chunk.page_start is None
            else f"{chunk.page_start}-{chunk.page_end or chunk.page_start}"
        ),
        "effective_date": chunk.effective_date,
        "score": _num(chunk.score),
    }
    if kept is not None:
        row["kept_for_the_answer"] = chunk.chunk_id in kept
    if text:
        row["text"] = cut(chunk.text, MAX_CHUNK_CHARS)
    return row


def _stage_tokens(ctx: Any, stage: str) -> int | None:
    """The input tokens the stage's span recorded (None when it has none)."""
    for span in ctx.trace.spans:
        if span.stage == stage and span.input_tokens:
            return int(span.input_tokens)
    return None


def _history_turns(ctx: Any) -> int:
    state = getattr(ctx, "session_state", None)
    return len(getattr(state, "messages", None) or [])


def _floor(ctx: Any) -> float:
    cfg = ctx.deps.config
    return cfg.lexical_floor if ctx.reranker == "lexical" else cfg.relevance_floor


# ---- one builder per stage -----------------------------------------------------------------------
def _authenticate(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "span",
        input={"role": ctx.request.role},
        output={"visible_roles": list(ctx.visible_roles)},
    )


def _local_scan(ctx: Any, result: Any, texts: Texts) -> StageContent:
    scan = ctx.scan
    output: dict[str, Any] = {}
    if scan is not None:
        output = {
            "action": scan.action,
            "reason": scan.reason,
            "tokens": scan.tokens,
            "masked_entities": dict(scan.mask_counts),  # counts per kind, never the values
            "injection_patterns": {
                "strong": list(scan.injection.strong),
                "weak": list(scan.injection.weak),
            },
        }
    return StageContent("guardrail", input={"question": texts.question}, output=output)


def _triage(ctx: Any, result: Any, texts: Texts) -> StageContent:
    t = ctx.triage
    return StageContent(
        "generation",
        input={"question": texts.question},
        output={
            "decider": t.decider,
            "in_domain_probability": _num(t.in_domain_probability),
            "injection_score": _num(t.injection_score),
            "needs_history": _num(t.needs_history),
            "vague": _num(t.vague),
            "product_line": t.product_line,
            "product_line_probability": _num(t.product_line_probability),
            "clause_type": t.clause_type,
            "clause_type_probability": _num(t.clause_type_probability),
            "failed": t.failed,
        },
    )


def _injection(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "guardrail",
        input={"injection_score": _num(ctx.triage.injection_score)},
        output={"exited": ctx.exited if ctx.exited == "injection_decision" else None},
    )


def _rewrite(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "generation",
        input={"question": texts.question, "history_turns_used": _history_turns(ctx)},
        output={
            "rewritten": bool(ctx.rewritten),
            "rewrite_failed": bool(ctx.rewrite_failed),
            "note": "the rewritten question is a model-bound copy and is not shown",
        },
    )


def _domain_gate(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "guardrail",
        input={"in_domain_probability": _num(ctx.triage.in_domain_probability)},
        output={"checked": bool(ctx.domain_checked), "history_used": bool(ctx.history_used)},
    )


def _cache_lookup(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "span",
        input={"cache_key_prefix": (ctx.cache_key or "")[:16] or None},
        output={"cache_status": ctx.cache_status, "corpus_version_at_start": ctx.corpus_start},
    )


def _embed(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "embedding",
        input={"task": "query", "text": "the model-bound copy of the question (not shown)"},
        output={"vector_dimensions": len(ctx.query_vector or [])},
    )


def _retrieve(ctx: Any, result: Any, texts: Texts) -> StageContent:
    cfg = ctx.deps.config
    return StageContent(
        "retriever",
        input={
            "question": texts.question,
            "search": {
                "collection": ctx.deps.collection,
                "method": "dense + sparse, fused by reciprocal rank fusion",
                "rrf_k": cfg.rrf_k,
                "limit_per_branch": cfg.candidates,
                "hard_filters": ["access_role in visible_roles", "active (no effective_to)"],
                "visible_roles": list(ctx.visible_roles),
                "soft_filters": dict(ctx.soft_applied),
                "soft_filters_dropped_and_repeated": bool(ctx.soft_retried),
            },
        },
        output={
            "found": len(ctx.retrieved),
            "chunks": [
                {
                    "rank": i,
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "clause": clause,
                    "fused_score": score,
                }
                for i, (chunk_id, doc_id, clause, score) in enumerate(
                    ctx.retrieved[:MAX_CHUNKS], start=1
                )
            ],
        },
    )


def _rerank(ctx: Any, result: Any, texts: Texts) -> StageContent:
    kept = {c.chunk_id for c in ctx.kept}
    return StageContent(
        "retriever",
        input={"candidates": len(ctx.retrieved), "top_k": ctx.deps.config.top_k},
        output={
            "reranker": ctx.reranker,
            "relevance_floor": _floor(ctx),
            "chunks": [
                _chunk_row(i, c, text=texts.enabled, kept=kept)
                for i, c in enumerate(ctx.candidates[:MAX_CHUNKS], start=1)
            ],
        },
    )


def _context_guard(ctx: Any, result: Any, texts: Texts) -> StageContent:
    audit = ctx.trace.audit
    return StageContent(
        "guardrail",
        input={"candidates": len(ctx.candidates), "relevance_floor": _floor(ctx)},
        output={
            "kept": len(ctx.kept),
            "dropped": audit.get("context_dropped"),
            "kept_chunk_ids": [c.chunk_id for c in ctx.kept],
            "instruction_like_patterns": audit.get("context_drop_patterns"),
        },
    )


def _confidence(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "span",
        input={"best_score": _num(ctx.best_score), "floor": ctx.deps.config.low_confidence_score},
        output={
            "low_confidence": bool(ctx.low_confidence),
            "rewrite_failed": bool(ctx.rewrite_failed),
        },
    )


def _generate(ctx: Any, result: Any, texts: Texts) -> StageContent:
    prompt = get_prompt("answer")
    metadata = {
        "prompt": f"{prompt.name}.v{prompt.version}",
        "prompt_hash": prompt.short_hash,
        "prompt_set": prompt_set_version(),
        "streaming": False,
        "history_turns_used": 0,
    }
    content = StageContent("generation", metadata=metadata)
    synthesis = ChildContent(
        "prompt_synthesis",
        "chain",
        metadata={
            "timing": "built inside the generate stage; it has no timing of its own",
            "history": NO_HISTORY,
        },
    )
    synthesis.input = {
        "prompt": f"{prompt.name}.v{prompt.version} ({prompt.short_hash})",
        "documents": [
            {
                "label": label,
                "chunk_id": chunk_id,
                "clause": next((c.clause_ref for c in ctx.kept if c.chunk_id == chunk_id), None),
            }
            for label, chunk_id in ctx.labels.items()
        ],
        "history": NO_HISTORY,
    }
    assembled = ""
    if texts.enabled and ctx.block and texts.question is not None:
        assembled = assemble_answer(prompt, cut(ctx.block, MAX_BLOCK_CHARS), texts.question)
    synthesis.output = {
        "prompt_characters": len(assembled) or None,
        "prompt_tokens": _stage_tokens(ctx, "generate"),
        "documents_in_prompt": len(ctx.kept),
    }
    content.children.append(synthesis)
    if assembled:
        content.input = {
            "messages": [{"role": "user", "content": assembled}],
            "note": (
                "instructions, documents and question are sent as ONE user message; the question "
                "shown is the stored (masked) copy"
            ),
        }
        content.output = {"answer": texts.answer}
    return content


def _output_guard(ctx: Any, result: Any, texts: Texts) -> StageContent:
    audit = ctx.trace.audit
    return StageContent(
        "guardrail",
        input={"documents": len(ctx.kept)},
        output={
            "grounded": audit.get("grounded"),
            "numeric_ok": audit.get("numeric_ok"),
            "citation_ok": audit.get("citation_ok"),
            "coverage": audit.get("coverage"),
            "warnings": audit.get("warnings"),
            "cited_chunks": audit.get("cited_chunks"),
        },
    )


def _cache_write(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "span",
        input={"cacheable": bool(ctx.cacheable)},
        output={"cache_status": ctx.cache_status},
    )


def _persist(ctx: Any, result: Any, texts: Texts) -> StageContent:
    plan = ctx.plan
    return StageContent(
        "span",
        input={"plan": plan.kind if plan is not None else None},
        output={
            "stored_masked_copy": ctx.stored_copy is not None,
            "grounded": bool(plan.grounded) if plan is not None else None,
            "review_reason": plan.review_reason if plan is not None else None,
        },
    )


def _respond(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "span",
        input={"kind": result.kind},
        output={
            "kind": result.kind,
            "reason": result.reason,
            "citations": len(result.citations),
            "warnings": len(result.warnings),
            "disclaimer": bool(result.disclaimer),
        },
    )


def _summary_fold(ctx: Any, result: Any, texts: Texts) -> StageContent:
    return StageContent(
        "generation",
        input={"history_turns": _history_turns(ctx)},
        output={"note": "the summary is a model-bound copy of the chat and is not shown"},
    )


BUILDERS = {
    "authenticate": _authenticate,
    "local_scan": _local_scan,
    "triage": _triage,
    "injection_decision": _injection,
    "rewrite": _rewrite,
    "domain_gate": _domain_gate,
    "cache_lookup": _cache_lookup,
    "embed": _embed,
    "retrieve": _retrieve,
    "rerank": _rerank,
    "context_guard": _context_guard,
    "confidence": _confidence,
    "generate": _generate,
    "output_guard": _output_guard,
    "cache_write": _cache_write,
    "persist": _persist,
    "respond": _respond,
    "summary_fold": _summary_fold,
}


def stage_content(name: str, ctx: Any, result: Any, texts: Texts) -> StageContent:
    """The content of one stage's observation. A stage with no builder, or a builder that fails,
    gives an empty observation of the right type (the timings and fields are still sent)."""
    kind = KINDS.get(name, "span")
    builder = BUILDERS.get(name)
    if builder is None:
        return StageContent(kind)
    try:
        content = builder(ctx, result, texts)
    except Exception:  # a bug here must cost this stage its content, nothing more
        return StageContent(kind)
    content.kind = kind
    return content


def trace_content(ctx: Any, result: Any, texts: Texts) -> tuple[Any, Any]:
    """The input and output of the whole request (the root observation)."""
    question = {"question": texts.question, "role": ctx.request.role}
    output: dict[str, Any] = {
        "kind": result.kind,
        "reason": result.reason,
        "citations": [
            {"doc_id": c.get("doc_id"), "clause": c.get("clause"), "page": c.get("page")}
            for c in result.citations
            if isinstance(c, dict)
        ],
    }
    if texts.answer is not None:
        output["answer"] = texts.answer
    if texts.withheld:
        output["withheld"] = list(texts.withheld)
    return question, output
