"""Stages 8 to 10: embed, hybrid retrieval, rerank (spec 4.1 steps 8-10, spec 7).

- Embedding goes through the gateway (key A, then key B); if both fail the request is "temporarily
  unavailable".
- ONE Qdrant Query API call runs the dense and the sparse search as prefetch branches and fuses them
  with Reciprocal Rank Fusion (k = 60). The hard filters (`access_role` of the user's visible roles,
  and "active": `effective_to` empty) are repeated on BOTH branches and on the fusion itself. The
  soft
  filters (`product_line`, `clause_type`) are added only at a high triage probability; if the
  filtered
  search returns nothing it is repeated once without them.
- Rerank goes through the gateway (Cohere, Jina, lexical). A failed rerank never fails the request:
  a
  local lexical order is used and the answer is marked degraded. The relevance floor applies to the
  Cohere and Jina scores directly; the lexical fallback has its own, stricter floor and its
  relevance
  is the share of the question's words found in the chunk.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from qdrant_client import models

from app.gateway.rerank import lexical_scores, top_items
from app.guardrails.context import ContextChunk
from app.guardrails.output import content_stems
from app.pipeline.state import PipelineState
from app.pipeline.trace import Span, StageError
from app.store_errors import StoreRejected, StoreUnavailable, call_vector
from app.vector.documents import DENSE_VECTOR, SPARSE_VECTOR
from app.vector.sparse import query_vector

SOFT_FIELDS = ("product_line", "clause_type")
VALUES_TTL_SECONDS = (
    300.0  # how long the allowed values are kept when the corpus version is unknown
)
MAX_VALUES = 100


# ---- the allowed soft-filter values: the distinct values stored in Qdrant
# ----------------------------
class ValuesCache:
    """The distinct `product_line` and `clause_type` values stored in Qdrant, refreshed when the
    corpus version changes (spec 7). With no corpus version they are refreshed every few minutes."""

    def __init__(self, ttl: float = VALUES_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._version: int | None = None
        self._loaded_at = -1e9
        self._values: dict[str, list[str]] = {name: [] for name in SOFT_FIELDS}

    def get(
        self, client: Any, collection: str, version: int | None, now: float
    ) -> dict[str, list[str]]:
        stale = (version is None and now - self._loaded_at > self._ttl) or version != self._version
        if stale or self._loaded_at < 0:
            self._values = {name: distinct_values(client, collection, name) for name in SOFT_FIELDS}
            self._version, self._loaded_at = version, now
        return {name: list(values) for name, values in self._values.items()}


def distinct_values(client: Any, collection: str, key: str) -> list[str]:
    """The distinct active values of one payload field. Qdrant's facet call when it has one (it
    needs the payload index that the collection has); otherwise a bounded scroll (local mode)."""
    active = models.Filter(
        must=[models.IsEmptyCondition(is_empty=models.PayloadField(key="effective_to"))]
    )
    try:
        response = client.facet(collection, key=key, facet_filter=active, limit=MAX_VALUES)
        found = [str(hit.value) for hit in response.hits if hit.value not in (None, "")]
        return sorted(found)[:MAX_VALUES]
    except Exception:
        pass
    seen: set[str] = set()
    offset = None
    for _ in range(50):  # bounded: at most 50 pages
        points, offset = client.scroll(
            collection,
            scroll_filter=active,
            limit=256,
            offset=offset,
            with_payload=[key],
            with_vectors=False,
        )
        for point in points:
            value = (point.payload or {}).get(key)
            if isinstance(value, str) and value:
                seen.add(value)
        if offset is None or len(seen) >= MAX_VALUES:
            break
    return sorted(seen)[:MAX_VALUES]


# ---- filters
# ----------------------------------------------------------------------------------------
def build_filter(roles: Sequence[str], soft: Mapping[str, str] | None = None) -> models.Filter:
    """The hard filters (always) plus the soft ones given. Used on every branch."""
    must: list[Any] = [
        models.FieldCondition(key="access_role", match=models.MatchAny(any=list(roles))),
        models.IsEmptyCondition(is_empty=models.PayloadField(key="effective_to")),
    ]
    for key, value in (soft or {}).items():
        must.append(models.FieldCondition(key=key, match=models.MatchValue(value=value)))
    return models.Filter(must=must)


def soft_filters(triage: Any, threshold: float) -> dict[str, str]:
    chosen: dict[str, str] = {}
    if triage.product_line and (triage.product_line_probability or 0.0) >= threshold:
        chosen["product_line"] = triage.product_line
    if triage.clause_type and (triage.clause_type_probability or 0.0) >= threshold:
        chosen["clause_type"] = triage.clause_type
    return chosen


def hybrid_search(
    client: Any,
    collection: str,
    dense: list[float],
    sparse: Any,
    flt: models.Filter,
    *,
    limit: int,
    rrf_k: int,
) -> list[Any]:
    """The one Query API call: dense and sparse prefetch branches, each with the filter, fused by
    Reciprocal Rank Fusion, the filter repeated on the fusion."""
    response = client.query_points(
        collection,
        prefetch=[
            models.Prefetch(query=dense, using=DENSE_VECTOR, filter=flt, limit=limit),
            models.Prefetch(query=sparse, using=SPARSE_VECTOR, filter=flt, limit=limit),
        ],
        query=models.RrfQuery(rrf=models.Rrf(k=rrf_k)),
        query_filter=flt,
        limit=limit,
        with_payload=True,
    )
    return list(response.points)


def chunk_from_point(point: Any) -> ContextChunk | None:
    payload = point.payload or {}
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    heading = payload.get("heading_path") or ()
    return ContextChunk(
        chunk_id=str(point.id),
        text=text,
        score=float(point.score or 0.0),
        doc_id=str(payload.get("doc_id", "")),
        source=str(payload.get("source", "")),
        effective_date=str(payload.get("effective_date", "")),
        clause_ref=payload.get("clause_ref") or None,
        heading_path=tuple(str(h) for h in heading) if isinstance(heading, (list, tuple)) else (),
        page_start=payload.get("page_start"),
        page_end=payload.get("page_end"),
        content_hash=payload.get("content_hash"),
    )


# ---- the nodes' work
# ---------------------------------------------------------------------------------
async def embed_question(ctx: PipelineState, span: Span, timeout_s: float) -> None:
    text = ctx.question.text if ctx.question else ""
    try:
        result = await ctx.deps.gateway.embed([text], task="query", timeout_s=timeout_s)
    except Exception as error:
        span.error_code = type(error).__name__
        raise StageError("embed", "embedding_unavailable") from None
    if not result.vectors:
        raise StageError("embed", "embedding_unavailable")
    ctx.query_vector = list(result.vectors[0])
    span.model = result.model
    span.key_fingerprint = result.key_fingerprint
    span.input_tokens = result.input_tokens
    span.cost_billed = result.cost_billed
    span.shadow_cost = result.shadow_cost
    if result.is_dev_fallback:
        ctx.degraded.append("dev_embedding")


async def retrieve_candidates(ctx: PipelineState, span: Span) -> None:
    cfg = ctx.deps.config
    text = ctx.question.text if ctx.question else ""
    sparse = query_vector(text)
    soft = (
        {}
        if ctx.triage.failed or ctx.triage.decider == "rules"
        else soft_filters(ctx.triage, cfg.soft_filter_probability)
    )

    def search(filters: Mapping[str, str]) -> list[Any]:
        flt = build_filter(ctx.visible_roles, filters)
        return call_vector(
            lambda: hybrid_search(
                ctx.deps.qdrant,
                ctx.deps.collection,
                ctx.query_vector or [],
                sparse,
                flt,
                limit=cfg.candidates,
                rrf_k=cfg.rrf_k,
            )
        )

    try:
        points = await asyncio.to_thread(search, soft)
        ctx.filters_applied = ["access_role", "active", *sorted(soft)]
        if not points and soft:  # spec 7: retry once without the soft filters
            points = await asyncio.to_thread(search, {})
            ctx.filters_applied = ["access_role", "active", "soft_filters_dropped"]
    except StoreRejected:
        ctx.emit("vector_store_rejected")
        raise StageError("retrieve", "vector_store_rejected") from None
    except StoreUnavailable:
        raise StageError("retrieve", "vector_store_unavailable") from None
    chunks = [c for c in (chunk_from_point(p) for p in points) if c is not None]
    ctx.candidates = chunks
    ctx.payloads = {str(p.id): dict(p.payload or {}) for p in points}
    span.add(candidates=len(chunks), filters=ctx.filters_applied)
    ctx.trace.record(filters_applied=ctx.filters_applied)


def lexical_relevance(question: str, text: str) -> float:
    """The share of the question's content words found in the chunk: the 0-1 relevance the
    lexical fallback is judged by (its raw score has no fixed scale)."""
    wanted = content_stems(question)
    return len(wanted & content_stems(text)) / len(wanted) if wanted else 0.0


async def rerank_candidates(ctx: PipelineState, span: Span, timeout_s: float) -> None:
    cfg = ctx.deps.config
    question = ctx.question.text if ctx.question else ""
    chunks = ctx.candidates
    started = time.perf_counter()
    items = None
    try:
        result = await ctx.deps.gateway.rerank(
            question, [c.text for c in chunks], top_n=cfg.top_k, timeout_s=timeout_s
        )
        items, ctx.reranker = result.items, result.reranker
        span.model = result.model
        span.key_fingerprint = result.key_fingerprint
        span.input_tokens = result.input_tokens
        if result.degraded:
            ctx.degraded.append("lexical_rerank")
    except Exception as error:  # rerank never fails the request: a local lexical order instead
        span.error_code = type(error).__name__
        ctx.emit("rerank_failed", reason=type(error).__name__)
        items = top_items(lexical_scores(question, [c.text for c in chunks]), cfg.top_k)
        ctx.reranker = "lexical"
        ctx.degraded.append("lexical_rerank")
    ranked: list[ContextChunk] = []
    for item in items:
        chunk = chunks[item.index]
        score = lexical_relevance(question, chunk.text) if ctx.reranker == "lexical" else item.score
        ranked.append(replace(chunk, score=float(score)))
    ctx.candidates = ranked
    span.add(
        reranker=ctx.reranker,
        kept=len(ranked),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
    )
