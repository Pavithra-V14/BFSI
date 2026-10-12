"""Deleting a document (spec 6.7, 8.5, 10.3).

Removes every vector of the document at once, then bumps the corpus version and clears the
cache entries that cited it, then (in the main database) removes the document's allow-list
entries, flags the transcripts that cited it as "source removed", clears the summaries built from
it and the session-facts entities only it approved (Phase 7, `app.memory.documents`, with its own
counts-only audit row), records the id in `withdrawn_documents` (Phase 15 round 3 [P]: the id, the
time and a fixed reason code, so that a restore can never bring the document back; see
`app.ingest.withdrawn`) and writes ONE audit row
(document id, who, when, chunk count; no document text). Safe to repeat: a second run finds
nothing to delete, still bumps and invalidates (so a run that stopped half way is finished by
running it again), keeps the first withdrawn row and writes no second audit row.

The "source removed" rule: a citation in `messages.citations` (JSON text) is an object with a
`doc_id`; a message with any citation whose `doc_id` is this document is flagged. (The chat phases
must store citations in that shape.) The confirmation step is in the command line (cli.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from qdrant_client import models

from app.accounts.audit import write_audit
from app.ingest import pending
from app.ingest.index import IngestContext, doc_filter, load_existing
from app.ingest.lock import document_lock
from app.ingest.model import WITHDRAW_REASONS, check_choice, check_doc_id
from app.ingest.withdrawn import is_withdrawn
from app.ingest.withdrawn import record as record_withdrawn
from app.memory.documents import clear_memory_for_document
from app.store_errors import call_vector


@dataclass
class DeleteResult:
    doc_id: str
    chunks: int = 0
    allowlist_entries: int = 0
    transcripts_flagged: int = 0
    summaries_cleared: int = 0
    entities_cleared: int = 0
    recorded: bool = False  # this run added the id to `withdrawn_documents`
    already_recorded: bool = False  # the id was on the list before this run
    incomplete: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete


def _cites(citations_text: str, doc_id: str) -> bool:
    try:
        citations = json.loads(citations_text)
    except (TypeError, ValueError):
        return False
    return isinstance(citations, list) and any(
        isinstance(item, dict) and item.get("doc_id") == doc_id for item in citations
    )


def flag_source_removed(db, doc_id: str) -> int:
    """Flag every message that cites the document. The caller commits."""
    rows = db.query_all(
        "SELECT id, citations FROM messages WHERE source_removed = ? AND citations LIKE ?",
        (False, f'%"{doc_id}"%'),
    )
    ids = [row[0] for row in rows if _cites(row[1], doc_id)]
    for message_id in ids:
        db.execute("UPDATE messages SET source_removed = ? WHERE id = ?", (True, message_id))
    return len(ids)


def delete_document(
    ctx: IngestContext, doc_id: str, reason: str = "other", *, even_if_not_stored: bool = False
) -> DeleteResult:
    check_doc_id(doc_id)
    check_choice(reason, WITHDRAW_REASONS, "reason")
    result = DeleteResult(doc_id)
    with document_lock(ctx.lock_dir, doc_id):
        existing = load_existing(ctx.qdrant, ctx.collection, doc_id)
        result.chunks = len(existing)
        now = ctx.now()
        # A repeat finishes what an earlier run left (the marker), and does nothing when there is
        # nothing stored and no unfinished run.
        redo = pending.is_pending(ctx.marker_dir, doc_id)
        if existing or redo:  # nothing is changed unless the cache can be told
            pending.require_cache(ctx.cache)
        if existing:
            pending.mark(ctx.marker_dir, doc_id, now, "delete")  # before the first change
            call_vector(
                lambda: ctx.qdrant.delete(
                    ctx.collection,
                    points_selector=models.FilterSelector(filter=doc_filter(doc_id)),
                    wait=True,
                )
            )
        if existing or redo:
            result.incomplete += pending.finish_cache_steps(
                ctx.cache,
                ctx.marker_dir,
                doc_id,
                chunk_ids=[e.id for e in existing],
                whole_document=True,
                now=now,
                reason="delete",
            )

        with ctx.db.transaction(write=True):
            cursor = ctx.db.execute("DELETE FROM allowlist_entities WHERE doc_id = ?", (doc_id,))
            result.allowlist_entries = max(cursor.rowcount or 0, 0)
            result.transcripts_flagged = flag_source_removed(ctx.db, doc_id)
            # spec 8.4: a summary built from the deleted document is cleared, and so are the
            # session-facts entities only it approved (one counts-only audit row of its own)
            cleared = clear_memory_for_document(ctx.db, doc_id, actor=ctx.actor, now=now)
            result.summaries_cleared = cleared.summaries
            result.entities_cleared = cleared.entities
            # The record is written in the same transaction as the rest: either the document is
            # withdrawn and recorded, or neither. A repeat keeps the first time and reason. An id
            # that held nothing (no vectors, no allow-list rows, no chats, no unfinished run) is
            # NOT recorded unless the owner says so: a typo must not burn an id.
            result.already_recorded = is_withdrawn(ctx.db, doc_id)
            held_something = bool(
                result.chunks or redo or result.allowlist_entries or result.transcripts_flagged
            )
            if held_something or even_if_not_stored:
                result.recorded = record_withdrawn(ctx.db, doc_id, reason, now)
            if (
                result.chunks
                or result.allowlist_entries
                or result.transcripts_flagged
                or result.recorded
            ):
                write_audit(
                    ctx.db,
                    actor=ctx.actor,
                    action="delete",
                    target=doc_id,
                    details={
                        "chunks": result.chunks,
                        "incomplete": bool(result.incomplete),
                        "reason": reason,
                    },
                    now=now,
                )
    return result
