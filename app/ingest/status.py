"""`ingest status --doc-id X`: a read-only picture of one document, counts only.

It reads Qdrant, the main database and the marker folder, and writes nothing. It never returns or
prints chunk text, a name, a caption or any other content of the document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ingest import pending
from app.ingest.index import load_existing
from app.ingest.model import check_doc_id
from app.ingest.queue import KIND as FAILED_PAGE_KIND
from app.vector.documents import chunk_is_active
from app.vector.sparse import SPARSE_VERSION


@dataclass
class DocumentStatus:
    doc_id: str
    active: int = 0
    quarantined_pending: int = 0
    quarantined_rejected: int = 0
    inactive: int = 0
    doc_version: int = 0
    pending_finish: bool = False
    allowlist: dict[str, int] = field(default_factory=dict)  # status -> count
    open_failed_pages: int = 0
    sparse_out_of_date: int = 0  # sparse vectors made by another tokenizer version

    @property
    def stored(self) -> int:
        return self.active + self.quarantined_pending + self.quarantined_rejected + self.inactive


def document_status(ctx: Any, doc_id: str) -> DocumentStatus:
    """`ctx` needs `.qdrant`, `.collection`, `.db` and `.marker_dir` (an IngestContext has all)."""
    check_doc_id(doc_id)
    status = DocumentStatus(doc_id)
    for stored in load_existing(ctx.qdrant, ctx.collection, doc_id):
        payload = stored.payload
        status.doc_version = max(status.doc_version, int(payload.get("doc_version") or 0))
        quarantine = payload.get("quarantine")
        if quarantine == "pending":
            status.quarantined_pending += 1
        elif quarantine == "rejected":
            status.quarantined_rejected += 1
        elif chunk_is_active(payload):
            status.active += 1
        else:
            status.inactive += 1
        if payload.get("sparse_version") != SPARSE_VERSION:
            status.sparse_out_of_date += 1
    status.pending_finish = pending.is_pending(ctx.marker_dir, doc_id)
    for name, count in ctx.db.query_all(
        "SELECT status, COUNT(*) FROM allowlist_entities WHERE doc_id = ? GROUP BY status",
        (doc_id,),
    ):
        status.allowlist[name] = int(count)
    row = ctx.db.query_one(
        "SELECT COUNT(*) FROM review_queue WHERE kind = ? AND status = 'open' AND doc_id = ?",
        (FAILED_PAGE_KIND, doc_id),
    )
    status.open_failed_pages = int(row[0]) if row else 0
    return status


def describe(status: DocumentStatus) -> list[str]:
    """The lines the command prints."""
    entries = ", ".join(f"{name} {count}" for name, count in sorted(status.allowlist.items()))
    return [
        f"document: {status.doc_id}",
        f"chunks: active {status.active}, held back {status.quarantined_pending}, "
        f"rejected {status.quarantined_rejected}, inactive {status.inactive} "
        f"(stored {status.stored})",
        f"document version: {status.doc_version}",
        f"unfinished cache step (pending-finish marker): {'YES' if status.pending_finish else 'no'}"
        "",
        f"sparse vectors out of date: {status.sparse_out_of_date} of {status.stored}",
        f"allow-list entries: {entries or 'none'}",
        f"failed pages waiting in the review queue: {status.open_failed_pages}",
    ]
