"""Stages 7 and 15: the exact answer cache (spec 4.1 steps 7 and 15, 10.1-10.3).

Lookup (7): the key is a hash of the normalised FINAL question (pass-1: it is itself masked), the
role,
the corpus version and the prompt-set version. Only the hash is ever stored: no question text and no
PII in the key or in the entry. A hit is VERIFIED before it is returned: every chunk the entry cited
is read back from Qdrant and must still exist, still be active, still be visible to the role and
have
the same content hash; any mismatch discards the entry (the request goes on as a miss).

Write (15): only a grounded, verified answer with no warning, no low-confidence flag, not degraded
and
not a mock, and only if the corpus version is still the one the request started with (an ingest
during
the request must not leave a stale entry).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.cache.store import HIT, MISS, ChunkRef, exact_key
from app.guardrails.prompts import prompt_set_version
from app.pipeline.state import PipelineState
from app.pipeline.trace import Span
from app.store_errors import call_vector
from app.vector.documents import chunk_is_active


def verify_entry(ctx: PipelineState, entry: dict[str, Any]) -> bool:
    """Read the cited chunks back and compare (spec 10.3 item 4). False on ANY mismatch."""
    chunks = entry.get("chunks")
    payload = entry.get("payload")
    if not isinstance(chunks, list) or not chunks or not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("answer"), str) or not payload["answer"].strip():
        return False
    ids = [str(c.get("chunk_id", "")) for c in chunks if isinstance(c, dict)]
    if not ids or len(ids) != len(chunks):
        return False
    try:
        points = call_vector(
            lambda: ctx.deps.qdrant.retrieve(
                ctx.deps.collection,
                ids=ids,
                with_payload=["content_hash", "effective_to", "access_role", "doc_id"],
                with_vectors=False,
            )
        )
    except Exception:
        return False
    found = {str(p.id): (p.payload or {}) for p in points}
    for cited in chunks:
        current = found.get(str(cited.get("chunk_id")))
        if current is None or not chunk_is_active(current):
            return False
        if current.get("content_hash") != cited.get("content_hash"):
            return False
        if current.get("access_role") not in ctx.visible_roles:
            return False
        if current.get("doc_id") != cited.get("doc_id"):
            return False
    return True


async def lookup(ctx: PipelineState, span: Span) -> dict[str, Any] | None:
    """The verified cached entry, or None. Sets `cache_status`, `corpus_start` and `cache_key`."""
    cache = ctx.deps.cache
    if cache is None or ctx.question is None:
        ctx.cache_status = "none"
        return None
    version = await asyncio.to_thread(cache.corpus_version)
    if version is None:  # Upstash is unavailable: skip the cache and answer normally
        ctx.cache_status = "unavailable"
        return None
    ctx.corpus_start = version
    ctx.cache_key = exact_key(ctx.question.text, ctx.request.role, version, prompt_set_version())
    found = await asyncio.to_thread(cache.get_entry, ctx.cache_key)
    if found.status == MISS or found.entry is None:
        ctx.cache_status = "unavailable" if found.status not in (HIT, MISS) else "miss"
        return None
    if await asyncio.to_thread(verify_entry, ctx, found.entry):
        ctx.cache_status = "hit"
        return found.entry
    ctx.cache_status = "discarded"
    ctx.emit("cache_entry_discarded")
    return None


async def write(ctx: PipelineState, span: Span) -> None:
    """Stage 15: store the answer if every rule of spec 10.2 holds."""
    cache = ctx.deps.cache
    if cache is None or not ctx.cacheable or ctx.cache_key is None or ctx.corpus_start is None:
        span.status = "skipped"
        return
    now_version = await asyncio.to_thread(cache.corpus_version)
    if now_version != ctx.corpus_start:  # an ingest happened during the request (spec 10.3 item 5)
        ctx.emit("cache_write_skipped", reason="corpus_changed")
        span.status = "skipped"
        return
    cited = set(ctx.verdict.cited_chunk_ids)
    by_id = {c.chunk_id: c for c in ctx.kept}
    refs = [
        ChunkRef(c.chunk_id, c.content_hash or "", c.doc_id)
        for cid in sorted(cited)
        if (c := by_id.get(cid)) is not None and c.content_hash
    ]
    if not refs:
        span.status = "skipped"
        return
    payload = {"answer": ctx.verdict.text, "citations": ctx.result.citations if ctx.result else []}
    stored = await asyncio.to_thread(
        cache.put_entry, ctx.cache_key, payload, refs, corpus_version=ctx.corpus_start
    )
    span.add(stored=bool(stored))
