"""The allow-list of approved names, for the admin API (spec 9.2, 12.1 `allowlist_entities`).

The command-line review (`app.ingest review entities`) is the main way to approve entries; this is
the same table, read and changed by an admin through the API. Approving or rejecting records WHO
(the admin's user id) and WHEN. Nothing here imports the ingestion package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.accounts.tokens import utc_text
from app.db.connection import Database

STATUSES = ("pending", "approved", "rejected")
SETTABLE = ("approved", "rejected")


def is_single_word_person(entity_type: str, text: str) -> bool:
    """A person's name of one word (a surname alone). The command-line review approves such an
    entry only when the reviewer ticks it, and the API asks for the same confirmation: one shared
    rule, used by both (app/ingest/review.py imports it from here)."""
    return entity_type == "person_in_policy" and len(text.split()) == 1


@dataclass(frozen=True)
class EntityRow:
    id: str
    doc_id: str
    text: str
    entity_type: str
    status: str
    approved_by: str | None
    approved_at: str | None
    access_role: str


def list_entities(
    db: Database, *, doc_id: str | None, status: str | None, limit: int, offset: int
) -> tuple[list[EntityRow], int]:
    where, params = [], []
    if doc_id is not None:
        where.append("doc_id = ?")
        params.append(doc_id)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.transaction():
        total = db.query_one(f"SELECT COUNT(*) FROM allowlist_entities{clause}", tuple(params))
        rows = db.query_all(
            "SELECT id, doc_id, text, entity_type, status, approved_by, approved_at, access_role "
            f"FROM allowlist_entities{clause} ORDER BY doc_id, entity_type, text, id "
            "LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
    items = [
        EntityRow(r[0], r[1], r[2], r[3], r[4], r[5], None if r[6] is None else str(r[6]), r[7])
        for r in rows
    ]
    return items, int(total[0]) if total else 0


def get_entity(db: Database, entity_id: str) -> tuple[str, str] | None:
    """(entity_type, text) of one entry, or None."""
    row = db.query_one(
        "SELECT entity_type, text FROM allowlist_entities WHERE id = ?", (entity_id,)
    )
    return (row[0], row[1]) if row else None


def set_status(db: Database, *, entity_id: str, status: str, user_id: str, now: datetime) -> bool:
    """Approve or reject one entry, recording WHO (the admin's id, for a rejection too) and WHEN.
    False if there is no such entry. The caller writes the audit row in the same transaction."""
    if status not in SETTABLE:
        raise ValueError("status must be approved or rejected")
    cursor = db.execute(
        "UPDATE allowlist_entities SET status = ?, approved_by = ?, approved_at = ? WHERE id = ?",
        (status, user_id, utc_text(now), entity_id),
    )
    return (cursor.rowcount or 0) > 0
