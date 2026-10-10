"""Indexing and incremental reindexing (spec 6.3, 6.4, 6.5, 10.3 items 1-3 and 6).

One call, `ingest_document`, does the whole job for one document under the per-document lock:

1. A document with a failed page (or a failed caption) is NOT activated: nothing is written and
   the old version stays active until a later run has every page.
2. The new chunks are matched to the stored ones: by content hash first (an unchanged chunk keeps
   its point id, its vector and its version, even if it moved or was renumbered), then paired by
   identity key (a changed chunk keeps its point id and gets chunk_version + 1). What is left is
   new, or removed (removed chunks get `effective_to` at once).
3. Only new and changed chunks are enriched and embedded. All embedding happens before the first
   write, so a failed embedding leaves the index untouched.
4. Writes go to Qdrant, THEN the corpus version is bumped (so a request that began before the
   ingest cannot cache an answer under the new version), THEN caches are invalidated. A bump or
   an invalidation that fails makes the result `incomplete` (the command exits non-zero).
5. One audit row records the run (counts only, no document text).

Payload: the fields of 6.4, plus `text` (the chunk text, which retrieval must return), `summary`
(a table chunk's one-line summary, not hashed) and `quarantine` and `quarantine_reason`
(None, "pending" or "rejected": an injection-flagged chunk is stored inactive until the owner
approves it, 9.3). [P]
`doc_version` on a chunk is the document version at which that chunk's current content was
written, so adding a clause leaves every other chunk's version untouched.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from qdrant_client import QdrantClient, models

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.cache.store import CacheStore
from app.db.connection import Database
from app.gateway.embeddings import DIMENSIONS, vectors_ok
from app.ingest import pending
from app.ingest.chunker import TokenCount, chunk_document
from app.ingest.identity import Identified, assign_identities, image_source, point_id
from app.ingest.lock import document_lock
from app.ingest.model import (
    ACCESS_ROLES,
    DOC_TYPES,
    Chunk,
    DocRequest,
    FailedPage,
    ImageItem,
    IngestError,
    ParsedDocument,
    check_choice,
    check_doc_id,
    refuse_customer_document,
)
from app.store_errors import call_vector
from app.vector import sparse
from app.vector.documents import (
    COLLECTION,
    DENSE_VECTOR,
    SPARSE_VECTOR,
    checked_payload,
    checked_update,
    chunk_is_active,
)

UPSERT_BATCH = 64
SCROLL_BATCH = 256
CURRENT_QUARANTINE = ("pending", "rejected")

Embed = Callable[[list[str], str | None], list[list[float]]]


class Enricher(Protocol):
    """Fills in clause_type, table summaries, captions and quarantine flags on new and changed
    chunks. Returns the pages whose captions failed (the document is then not activated)."""

    def __call__(self, chunks: list[Chunk], request: DocRequest) -> list[FailedPage]: ...


@dataclass
class IngestContext:
    qdrant: QdrantClient
    cache: CacheStore
    db: Database
    embed: Embed
    count: TokenCount
    lock_dir: Path
    marker_dir: Path
    enrich: Enricher | None = None
    collection: str = COLLECTION
    actor: str = "cli"
    # Customer-specific documents are refused until a paid or zero-retention plan exists
    # (Phase 16); this is the one switch that will allow them.
    accept_customer_documents: bool = False
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class IngestResult:
    doc_id: str
    status: str  # ok | unchanged | finished | failed_pages
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    doc_version: int = 0
    failed_pages: list[FailedPage] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    images_skipped: int = 0
    sparse_refreshed: int = 0  # unchanged chunks whose sparse vector alone was rewritten

    @property
    def complete(self) -> bool:
        return self.status in ("ok", "unchanged", "finished") and not self.incomplete


@dataclass
class Existing:
    id: str
    payload: dict[str, Any]


def doc_filter(doc_id: str) -> models.Filter:
    return models.Filter(
        must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
    )


def load_existing(client: QdrantClient, collection: str, doc_id: str) -> list[Existing]:
    """Every stored point of the document, active or not (the history sets the next version)."""

    def work() -> list[Existing]:
        found: list[Existing] = []
        offset = None
        while True:
            points, offset = client.scroll(
                collection,
                scroll_filter=doc_filter(doc_id),
                limit=SCROLL_BATCH,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            found.extend(Existing(str(point.id), dict(point.payload or {})) for point in points)
            if offset is None:
                return found

    return call_vector(work)


def is_current(payload: dict[str, Any]) -> bool:
    """A chunk that is part of the document now: active, or held back for review (9.3)."""
    return chunk_is_active(payload) or payload.get("quarantine") in CURRENT_QUARANTINE


def image_chunk(request: DocRequest, item: ImageItem) -> Chunk:
    source = image_source(item.data)
    return Chunk(
        kind="image_caption",
        text="",
        source=source,
        clause_ref=item.clause_ref,
        heading_path=item.heading_path,
        page_start=item.page,
        page_end=item.page,
        image_ref=f"{request.doc_id}/{source[6:18]}",
        image_data=item.data,
    )


@dataclass
class Plan:
    unchanged: list[tuple[Identified, Existing]] = field(default_factory=list)
    changed: list[tuple[Identified, Existing]] = field(default_factory=list)
    new: list[Identified] = field(default_factory=list)
    removed: list[Existing] = field(default_factory=list)
    previous_version: dict[str, int] = field(default_factory=dict)  # id -> chunk_version


def plan_changes(doc_id: str, identified: list[Identified], existing: list[Existing]) -> Plan:
    plan = Plan()
    everything = {item.id: item for item in existing}
    current = [item for item in existing if is_current(item.payload)]
    by_hash: dict[str, list[Existing]] = {}
    for item in current:
        by_hash.setdefault(str(item.payload.get("content_hash")), []).append(item)
    claimed: set[str] = set()
    used: set[str] = set()
    leftover: list[Identified] = []

    # 1. By content hash: unchanged, even if moved or renumbered. It keeps its stored id.
    for ident in identified:
        candidates = [
            item
            for item in by_hash.get(ident.content_hash, [])
            if item.id not in claimed and item.payload.get("content_type") == ident.chunk.kind
        ]
        if candidates:
            match = next((c for c in candidates if c.id == ident.chunk_id), candidates[0])
            claimed.add(match.id)
            used.add(match.id)
            ident.chunk_id = match.id
            plan.unchanged.append((ident, match))
        else:
            leftover.append(ident)

    # 2. Paired by identity key (the same point id): the chunk changed.
    current_ids = {item.id: item for item in current}
    still: list[Identified] = []
    for ident in leftover:
        match = current_ids.get(ident.chunk_id)
        if match is not None and match.id not in claimed:
            claimed.add(match.id)
            used.add(match.id)
            plan.changed.append((ident, match))
        else:
            still.append(ident)

    # 3. The rest are new. A computed id that is already taken gets a salt. "Taken" includes the
    # id of a stored current chunk that matched nothing: it is about to be removed, and a new chunk
    # that reused its id would be deactivated by that same removal.
    unmatched = {item.id for item in current if item.id not in claimed}
    for ident in still:
        chunk_id, salt = ident.chunk_id, 0
        while chunk_id in used or chunk_id in unmatched:
            salt += 1
            chunk_id = point_id(doc_id, f"{ident.key}~s{salt}", ident.chunk.part_index)
        ident.chunk_id = chunk_id
        used.add(chunk_id)
        plan.new.append(ident)
        old = everything.get(chunk_id)  # an earlier, inactive version of this chunk
        if old is not None:
            plan.previous_version[chunk_id] = int(old.payload.get("chunk_version") or 0)

    # 4. Stored and current, but no longer in the document: removed.
    plan.removed = [item for item in current if item.id not in claimed]
    return plan


def embedding_text(chunk: Chunk) -> str:
    if chunk.kind == "table" and chunk.summary:
        return f"{chunk.summary}\n{chunk.text}"
    return chunk.text


def _sparse_text(chunk: Chunk) -> str:
    return sparse.source_text(chunk.heading_path, chunk.kind, chunk.text, chunk.summary)


def build_payload(
    request: DocRequest,
    ident: Identified,
    *,
    doc_version: int,
    chunk_version: int,
    ingested_at: str,
    parser: str,
    now_text: str,
) -> dict[str, Any]:
    chunk = ident.chunk
    return {
        "doc_id": request.doc_id,
        "source": request.source,
        "chunk_id": ident.chunk_id,
        "clause_ref": chunk.clause_ref,
        "part_index": chunk.part_index,
        "content_hash": ident.content_hash,
        "doc_version": doc_version,
        "chunk_version": chunk_version,
        "effective_date": request.effective_date,
        # A quarantined chunk is inactive from the start: it is never returned until approved.
        "effective_to": now_text if chunk.quarantine == "pending" else None,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "heading_path": list(chunk.heading_path),
        "clause_type": chunk.clause_type,
        "product_line": request.product_line,
        "content_type": chunk.kind,
        "image_ref": chunk.image_ref,
        "access_role": request.access_role,
        "ingested_at": ingested_at,
        "parser": parser,
        "text": chunk.text,
        "summary": chunk.summary,
        "quarantine": chunk.quarantine,
        "quarantine_reason": chunk.quarantine_reason,
        "sparse_version": sparse.SPARSE_VERSION,  # the tokenizer version of its sparse vector
    }


def refreshed_sparse_vector(payload: dict[str, Any], updates: dict[str, Any]):
    """The sparse vector of a stored chunk, made again from what is stored (its text, its summary
    and its heading path, the new one if it moved). None if the payload has no text to make it
    from. No embedding, no model call: this is the only thing a tokenizer change needs."""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    heading = updates.get("heading_path", payload.get("heading_path")) or []
    summary = payload.get("summary")
    return sparse.document_vector(
        sparse.source_text(
            [str(h) for h in heading],
            payload.get("content_type"),
            text,
            summary if isinstance(summary, str) else None,
        )
    )


def _metadata_updates(request: DocRequest, ident: Identified, payload: dict[str, Any]) -> dict:
    """Fields of an unchanged chunk that may still differ (it moved, or the run's metadata did)."""
    chunk = ident.chunk
    wanted = {
        "clause_ref": chunk.clause_ref,
        "part_index": chunk.part_index,
        "heading_path": list(chunk.heading_path),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source": request.source,
        "access_role": request.access_role,
        "product_line": request.product_line,
    }
    return {name: value for name, value in wanted.items() if payload.get(name) != value}


def _batches(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def ingest_document(
    ctx: IngestContext, request: DocRequest, parsed: ParsedDocument
) -> IngestResult:
    check_doc_id(request.doc_id)
    check_choice(request.doc_type, DOC_TYPES, "doc_type")
    check_choice(request.access_role, ACCESS_ROLES, "access_role")
    if not ctx.accept_customer_documents:
        refuse_customer_document(request.doc_type)
    with document_lock(ctx.lock_dir, request.doc_id):
        return _ingest_locked(ctx, request, parsed)


def _ingest_locked(ctx: IngestContext, request: DocRequest, parsed: ParsedDocument) -> IngestResult:
    if parsed.failed_pages:
        return IngestResult(request.doc_id, "failed_pages", failed_pages=list(parsed.failed_pages))

    chunked = chunk_document(parsed, ctx.count)
    chunks = list(chunked.chunks)
    images_skipped = 0
    if ctx.enrich is not None:
        chunks.extend(image_chunk(request, item) for item in chunked.images)
    else:
        images_skipped = len(chunked.images)
    if not chunks:
        raise IngestError(
            "no text was found in the document; nothing was changed (an empty parse must never "
            "deactivate a document that is already stored)"
        )
    identified = assign_identities(request.doc_id, chunks)
    existing = load_existing(ctx.qdrant, ctx.collection, request.doc_id)
    if ctx.enrich is None:
        # Without captioning, stored image captions are left alone, never treated as removed.
        existing = [e for e in existing if e.payload.get("content_type") != "image_caption"]
    plan = plan_changes(request.doc_id, identified, existing)
    checked = False
    if (
        plan.changed
        or plan.new
        or plan.removed
        or pending.is_pending(ctx.marker_dir, request.doc_id)
    ):  # before any model call is paid for
        pending.require_cache(ctx.cache)
        checked = True

    to_write = [i for i, _ in plan.changed] + plan.new
    if ctx.enrich is not None and to_write:
        failed = ctx.enrich([i.chunk for i in to_write], request)
        if failed:
            return IngestResult(request.doc_id, "failed_pages", failed_pages=list(failed))
    # An image whose caption is still empty has nothing to index.
    to_write = [i for i in to_write if i.chunk.text.strip()]
    written_ids = {i.chunk_id for i in to_write}
    plan.changed = [(i, e) for i, e in plan.changed if i.chunk_id in written_ids]
    plan.new = [i for i in plan.new if i.chunk_id in written_ids]

    vectors: list[list[float]] = []
    if to_write:
        texts = [embedding_text(i.chunk) for i in to_write]
        vectors = ctx.embed(texts, request.title)
        if not vectors_ok(vectors, len(texts), DIMENSIONS):
            raise IngestError(
                "the embedding service returned unusable vectors; nothing was written"
            )

    now = ctx.now()
    now_text = utc_text(now)
    any_change = bool(plan.changed or plan.new or plan.removed)
    top_version = max((int(e.payload.get("doc_version") or 0) for e in existing), default=0)
    doc_version = top_version + 1 if any_change else top_version

    points = []
    for ident, vector in zip(to_write, vectors, strict=True):
        previous = next((e for i, e in plan.changed if i is ident), None)
        if previous is not None:
            chunk_version = int(previous.payload.get("chunk_version") or 0) + 1
        else:
            chunk_version = plan.previous_version.get(ident.chunk_id, 0) + 1
        payload = build_payload(
            request,
            ident,
            doc_version=doc_version,
            chunk_version=chunk_version,
            ingested_at=now_text,
            parser=parsed.parser,
            now_text=now_text,
        )
        points.append(
            models.PointStruct(
                id=ident.chunk_id,
                vector={
                    DENSE_VECTOR: vector,
                    SPARSE_VECTOR: sparse.document_vector(_sparse_text(ident.chunk)),
                },
                payload=checked_payload(payload, now=now),
            )
        )

    refreshed: list[tuple[str, dict]] = []
    sparse_vectors: list[models.PointVectors] = []
    permission_changed = False
    for ident, stored in plan.unchanged:
        updates = _metadata_updates(request, ident, stored.payload)
        # An unchanged chunk keeps its dense vector, but its sparse vector is rewritten (no
        # embedding call) when it was made by an older tokenizer or its heading path moved: the
        # sparse text includes the heading path.
        if (
            stored.payload.get("sparse_version") != sparse.SPARSE_VERSION
            or "heading_path" in updates
        ):
            vector = refreshed_sparse_vector(stored.payload, updates)
            if vector is not None:
                sparse_vectors.append(
                    models.PointVectors(id=stored.id, vector={SPARSE_VECTOR: vector})
                )
                updates = {**updates, "sparse_version": sparse.SPARSE_VERSION}
        if updates:
            refreshed.append((stored.id, updates))
            permission_changed = permission_changed or any(
                name in updates for name in ("access_role", "product_line")
            )
    removed_ids = [e.id for e in plan.removed]
    # A removed chunk is inactive at once and also leaves the review state: a held or rejected
    # chunk whose clause is gone must not stay "current" or be approvable later.
    removed_payload = checked_update(
        {"effective_to": now_text, "quarantine": None, "quarantine_reason": None}, now=now
    )
    refreshed = [(point, checked_update(updates, now=now)) for point, updates in refreshed]

    needs_finish = any_change or permission_changed or bool(sparse_vectors)
    redo = not needs_finish and pending.is_pending(ctx.marker_dir, request.doc_id)
    if (needs_finish or redo) and not checked:  # a metadata or sparse-only change
        pending.require_cache(ctx.cache)
    if needs_finish:  # before the first write, so even a crash in between is found next time
        pending.mark(ctx.marker_dir, request.doc_id, now, "ingest")

    def write() -> None:
        for batch in _batches(points, UPSERT_BATCH):
            ctx.qdrant.upsert(ctx.collection, points=batch, wait=True)
        for batch in _batches(sparse_vectors, UPSERT_BATCH):
            ctx.qdrant.update_vectors(ctx.collection, points=batch, wait=True)
        for point, updates in refreshed:
            ctx.qdrant.set_payload(ctx.collection, payload=updates, points=[point], wait=True)
        for batch in _batches(removed_ids, UPSERT_BATCH):
            ctx.qdrant.set_payload(ctx.collection, payload=removed_payload, points=batch, wait=True)

    call_vector(write)

    incomplete: list[str] = []
    if needs_finish or redo:
        incomplete = pending.finish_cache_steps(
            ctx.cache,
            ctx.marker_dir,
            request.doc_id,
            chunk_ids=[e.id for _, e in plan.changed] + removed_ids if needs_finish else [],
            whole_document=bool(plan.new or permission_changed) or redo,
            now=now,
            reason="ingest",
        )

    result = IngestResult(
        request.doc_id,
        "ok" if needs_finish else ("finished" if redo else "unchanged"),
        new=len(plan.new),
        changed=len(plan.changed),
        unchanged=len(plan.unchanged),
        removed=len(plan.removed),
        doc_version=doc_version,
        incomplete=incomplete,
        images_skipped=images_skipped,
        sparse_refreshed=len(sparse_vectors),
    )
    if not (needs_finish or redo):
        return result  # a true no-op: nothing written, bumped, cleared or audited
    first_time = not any(is_current(e.payload) for e in existing)
    with ctx.db.transaction(write=True):
        write_audit(
            ctx.db,
            actor=ctx.actor,
            action="ingest" if first_time else "reindex",
            target=request.doc_id,
            details={
                "new": result.new,
                "changed": result.changed,
                "unchanged": result.unchanged,
                "removed": result.removed,
                "doc_version": doc_version,
                "sparse_refreshed": result.sparse_refreshed,
                "incomplete": bool(incomplete),
                "finished_earlier_run": redo,
            },
            now=now,
        )
    return result
