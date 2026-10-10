"""Counts per document, read from Qdrant (spec 6.3, Phase 10 document-status endpoints).

Counts and a few fixed fields only: never chunk text, a caption or a summary. It reads the payload
fields `doc_id`, `effective_to`, `quarantine`, `doc_version`, `sparse_version`, `access_role`,
`product_line` and `source` (a file name). Nothing here imports the ingestion package.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qdrant_client import QdrantClient, models

from app.store_errors import call_vector
from app.vector.documents import COLLECTION, chunk_is_active
from app.vector.sparse import SPARSE_VERSION

FIELDS = [
    "doc_id",
    "effective_to",
    "quarantine",
    "doc_version",
    "sparse_version",
    "access_role",
    "product_line",
    "source",
]
PAGE = 256


@dataclass
class DocumentCounts:
    doc_id: str
    access_role: str | None = None
    product_line: str | None = None
    source: str | None = None
    active: int = 0
    held_back: int = 0
    rejected: int = 0
    inactive: int = 0
    doc_version: int = 0
    sparse_out_of_date: int = 0

    @property
    def stored(self) -> int:
        return self.active + self.held_back + self.rejected + self.inactive

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "access_role": self.access_role,
            "product_line": self.product_line,
            "source": self.source,
            "chunks_active": self.active,
            "chunks_held_back": self.held_back,
            "chunks_rejected": self.rejected,
            "chunks_inactive": self.inactive,
            "chunks_stored": self.stored,
            "document_version": self.doc_version,
            "sparse_vectors_out_of_date": self.sparse_out_of_date,
        }


def _as_int(value: object) -> int:
    """A stored number, or 0 when the payload holds something else (never an error: one odd
    payload must not turn the admin list into a 500)."""
    try:
        return int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def documents_overview(
    client: QdrantClient, collection: str = COLLECTION, *, doc_id: str | None = None
) -> list[DocumentCounts]:
    """One entry per document (or only `doc_id`), sorted by id. A bounded scroll of payloads."""
    flt = None
    if doc_id is not None:
        flt = models.Filter(
            must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id))]
        )

    def work() -> dict[str, DocumentCounts]:
        found: dict[str, DocumentCounts] = {}
        offset = None
        while True:
            points, offset = client.scroll(
                collection,
                scroll_filter=flt,
                limit=PAGE,
                offset=offset,
                with_payload=FIELDS,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                name = str(payload.get("doc_id") or "")
                if not name:
                    continue
                counts = found.setdefault(name, DocumentCounts(name))
                counts.access_role = counts.access_role or payload.get("access_role")
                counts.product_line = counts.product_line or payload.get("product_line")
                counts.source = counts.source or payload.get("source")
                counts.doc_version = max(counts.doc_version, _as_int(payload.get("doc_version")))
                quarantine = payload.get("quarantine")
                if quarantine == "pending":
                    counts.held_back += 1
                elif quarantine == "rejected":
                    counts.rejected += 1
                elif chunk_is_active(payload):
                    counts.active += 1
                else:
                    counts.inactive += 1
                if payload.get("sparse_version") != SPARSE_VERSION:
                    counts.sparse_out_of_date += 1
            if offset is None:
                return found

    return sorted(call_vector(work).values(), key=lambda c: c.doc_id)
