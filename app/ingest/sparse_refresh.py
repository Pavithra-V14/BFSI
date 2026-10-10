"""Refresh out-of-date sparse vectors (spec 6.6, 10.3 item 6, Phase 9 [P]).

Every stored chunk records the tokenizer version its sparse vector was made with
(`sparse_version`, `app.vector.sparse.SPARSE_VERSION`). When the tokenizer changes the number is
raised, and the chunks with an older (or no) number are out of step with the questions. This
command makes their sparse vectors again from what is stored (text, summary, heading path). It needs
no PDF, no model and no embedding call: the dense vectors are not touched.

- Each document is locked while its chunks are rewritten.
- The pending-finish marker is set before the first write, and cleared only after the ONE corpus
  version bump at the end of the whole refresh succeeded (spec 10.3: the answers cached under the
  old index must not be served). A failed bump leaves the markers, and the same command run again
  finishes the job.
- One audit row (counts only, no text).
- Safe to repeat: with nothing out of date and no marker it does nothing and bumps nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qdrant_client import models

from app.accounts.audit import write_audit
from app.ingest import pending
from app.ingest.index import IngestContext, load_existing, refreshed_sparse_vector
from app.ingest.lock import document_lock
from app.ingest.model import check_doc_id
from app.store_errors import call_vector
from app.vector import sparse
from app.vector.documents import SPARSE_VECTOR, checked_update

BATCH = 64
REASON = "sparse_refresh"


@dataclass
class RefreshResult:
    documents: int = 0
    chunks_checked: int = 0
    chunks_refreshed: int = 0
    chunks_without_text: int = 0
    redone: list[str] = field(default_factory=list)  # documents whose unfinished marker was redone
    foreign: list[str] = field(default_factory=list)  # unfinished runs of another kind
    incomplete: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete


def _stale_doc_ids(ctx: IngestContext) -> list[str]:
    """Every document that has at least one chunk with a missing or different version."""
    current = models.FieldCondition(
        key="sparse_version", match=models.MatchValue(value=sparse.SPARSE_VERSION)
    )

    def work() -> list[str]:
        found: set[str] = set()
        offset = None
        while True:
            points, offset = ctx.qdrant.scroll(
                ctx.collection,
                scroll_filter=models.Filter(must_not=[current]),
                limit=256,
                offset=offset,
                with_payload=["doc_id"],
                with_vectors=False,
            )
            found.update(
                str(p.payload["doc_id"]) for p in points if p.payload and p.payload.get("doc_id")
            )
            if offset is None:
                return sorted(found)

    return call_vector(work)


def refresh_sparse_vectors(ctx: IngestContext, doc_id: str | None = None) -> RefreshResult:
    result = RefreshResult()
    now = ctx.now()
    if doc_id is not None:
        check_doc_id(doc_id)
        doc_ids = [doc_id]
    else:
        doc_ids = _stale_doc_ids(ctx)
    if doc_ids or pending.pending_docs(ctx.marker_dir):
        pending.require_cache(ctx.cache)  # before anything is written
    marked: list[str] = []
    for current_doc in doc_ids:
        with document_lock(ctx.lock_dir, current_doc):
            existing = load_existing(ctx.qdrant, ctx.collection, current_doc)
            result.chunks_checked += len(existing)
            stale = [
                e for e in existing if e.payload.get("sparse_version") != sparse.SPARSE_VERSION
            ]
            if not stale:
                continue
            vectors: list[models.PointVectors] = []
            done: list[str] = []
            for item in stale:
                vector = refreshed_sparse_vector(item.payload, {})
                if vector is None:
                    result.chunks_without_text += 1
                    continue
                vectors.append(models.PointVectors(id=item.id, vector={SPARSE_VECTOR: vector}))
                done.append(item.id)
            if not vectors:
                continue
            pending.mark(ctx.marker_dir, current_doc, now, REASON)  # before the first write
            if pending.marker_reason(ctx.marker_dir, current_doc) == REASON:
                marked.append(current_doc)
            else:  # an unfinished ingest, delete or review of this document is waiting:
                result.foreign.append(current_doc)  # its marker is not ours to clear

            def write(vectors=vectors, done=done) -> None:
                for start in range(0, len(vectors), BATCH):
                    ctx.qdrant.update_vectors(
                        ctx.collection, points=vectors[start : start + BATCH], wait=True
                    )
                ctx.qdrant.set_payload(
                    ctx.collection,
                    payload=checked_update({"sparse_version": sparse.SPARSE_VERSION}, now=now),
                    points=done,
                    wait=True,
                )

            call_vector(write)
            result.documents += 1
            result.chunks_refreshed += len(done)

    # A marker left by an earlier refresh that stopped before its bump is finished too.
    leftover = [
        name
        for name in pending.pending_docs(ctx.marker_dir)
        if name not in marked
        and (doc_id is None or name == doc_id)
        and pending.marker_reason(ctx.marker_dir, name) == REASON
    ]
    result.redone = leftover
    if marked or leftover or result.foreign:
        # ONE bump for the whole refresh, also for a document whose marker belongs to another
        # kind of run (its vectors changed all the same); only OUR markers are cleared below
        if ctx.cache.bump_corpus_version() is None:
            result.incomplete.append("the corpus version was not bumped")
        else:
            for name in marked + leftover:
                pending.clear(ctx.marker_dir, name)
    if result.chunks_refreshed:
        with ctx.db.transaction(write=True):
            write_audit(
                ctx.db,
                actor=ctx.actor,
                action="reindex",
                target=doc_id or "all documents",
                details={
                    "kind": REASON,
                    "documents": result.documents,
                    "chunks": result.chunks_refreshed,
                    "sparse_version": sparse.SPARSE_VERSION,
                    "incomplete": bool(result.incomplete),
                },
                now=now,
            )
    return result
