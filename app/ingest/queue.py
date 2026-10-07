"""Failed pages in the human-review queue (spec 11.4, 6.3).

A page that fails to parse or caption puts one `failed_ingest_page` row in `review_queue`
(document id, page number and a short reason: never page text). Running the same ingest again
retries the page; when a run succeeds the document's open rows are closed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from app.accounts.tokens import utc_text
from app.ingest.model import FailedPage

KIND = "failed_ingest_page"


def flag_failed_pages(db, doc_id: str, pages: list[FailedPage], now: datetime) -> int:
    """Add a row for each failed page that has no open row yet. Returns how many were added."""
    open_pages = set()
    for (details,) in db.query_all(
        "SELECT details FROM review_queue WHERE kind = ? AND status = 'open' AND doc_id = ?",
        (KIND, doc_id),
    ):
        try:
            open_pages.add(json.loads(details).get("page"))
        except (TypeError, ValueError, AttributeError):
            continue
    added = 0
    with db.transaction(write=True):
        for page in pages:
            if page.page in open_pages:
                continue
            db.execute(
                "INSERT INTO review_queue (id, kind, status, doc_id, details, created_at) "
                "VALUES (?, ?, 'open', ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    KIND,
                    doc_id,
                    json.dumps({"page": page.page, "reason": page.reason}, sort_keys=True),
                    utc_text(now),
                ),
            )
            added += 1
    return added


def resolve_failed_pages(db, doc_id: str, now: datetime) -> int:
    """Close the document's open failed-page rows after a run that had every page."""
    with db.transaction(write=True):
        cursor = db.execute(
            "UPDATE review_queue SET status = 'dismissed', label = 'resolved by re-ingest', "
            "labelled_at = ? WHERE kind = ? AND status = 'open' AND doc_id = ?",
            (utc_text(now), KIND, doc_id),
        )
    return max(cursor.rowcount or 0, 0)
