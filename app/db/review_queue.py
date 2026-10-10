"""The admin review queue (spec 11.4, 12.1 `review_queue`): list and label.

Refused answers, low-confidence answers, thumbs-down answers, two-unclear-turn sessions and failed
ingest pages land here (written by the pipeline, the feedback route and the ingestion commands).
An admin lists the open items and labels each one from a FIXED list of labels (no free text), then
sets its status: `labelled`, `added_to_eval` (the evaluation set itself is Phase 13) or `dismissed`.

`details` holds masked text only (the pass-2 copy); it is returned as stored. An item is overdue
when it is still open after OVERDUE_WORKING_DAYS working days (Saturday and Sunday do not count).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.accounts.tokens import utc_text
from app.db.connection import Database

KINDS = ("refused", "low_confidence", "thumbs_down", "two_unclear_turns", "failed_ingest_page")
STATUSES = ("open", "labelled", "added_to_eval", "dismissed")
LABELS = ("correct_refusal", "should_have_answered", "wrong_answer", "bad_question", "other")
SETTABLE_STATUSES = ("labelled", "added_to_eval", "dismissed")
OVERDUE_WORKING_DAYS = 2  # [T] placeholder from spec 11.4


@dataclass(frozen=True)
class ReviewItem:
    id: str
    kind: str
    status: str
    session_id: str | None
    message_id: str | None
    doc_id: str | None
    details: dict[str, Any]
    label: str | None
    labelled_by: str | None
    labelled_at: str | None
    created_at: str
    overdue: bool


def _parse_time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def is_overdue(created_at: str, status: str, now: datetime) -> bool:
    """Open for more than OVERDUE_WORKING_DAYS working days."""
    if status != "open":
        return False
    created = _parse_time(created_at)
    if created is None:
        return False
    deadline, counted = created, 0
    while counted < OVERDUE_WORKING_DAYS:
        deadline += timedelta(days=1)
        if deadline.weekday() < 5:
            counted += 1
    return now > deadline


def _item(row: tuple, now: datetime) -> ReviewItem:
    try:
        details = json.loads(row[6])
    except (TypeError, ValueError):
        details = {}
    status = row[2]
    return ReviewItem(
        id=row[0],
        kind=row[1],
        status=status,
        session_id=row[3],
        message_id=row[4],
        doc_id=row[5],
        details=details if isinstance(details, dict) else {},
        label=row[7],
        labelled_by=row[8],
        labelled_at=None if row[9] is None else str(row[9]),
        created_at=str(row[10]),
        overdue=is_overdue(str(row[10]), status, now),
    )


_COLUMNS = (
    "id, kind, status, session_id, message_id, doc_id, details, label, labelled_by, "
    "labelled_at, created_at"
)


def list_items(
    db: Database,
    *,
    status: str | None,
    kind: str | None,
    limit: int,
    offset: int,
    now: datetime,
) -> tuple[list[ReviewItem], int]:
    where, params = [], []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if kind is not None:
        where.append("kind = ?")
        params.append(kind)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db.transaction():
        total = db.query_one(f"SELECT COUNT(*) FROM review_queue{clause}", tuple(params))
        rows = db.query_all(
            f"SELECT {_COLUMNS} FROM review_queue{clause} "
            "ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
            (*params, int(limit), int(offset)),
        )
    return [_item(row, now) for row in rows], int(total[0]) if total else 0


def label_item(
    db: Database, *, item_id: str, label: str, status: str, user_id: str, now: datetime
) -> bool:
    """Label one item. False if there is no such item. The caller writes the audit row in the same
    transaction."""
    if label not in LABELS or status not in SETTABLE_STATUSES:
        raise ValueError("label or status is not one of the allowed values")
    cursor = db.execute(
        "UPDATE review_queue SET label = ?, status = ?, labelled_by = ?, labelled_at = ? "
        "WHERE id = ?",
        (label, status, user_id, utc_text(now), item_id),
    )
    return (cursor.rowcount or 0) > 0
