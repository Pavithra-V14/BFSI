"""Erase the system's data (Change round 1, step 6).

OWNER ONLY, on the admin machine, never on Render.

What is ERASED:
- the document vectors: every point of the production collection `documents` (the collection itself,
  its settings and its indexes stay, so the next ingest needs no set-up);
- the cache: every cached answer and its reverse sets in Upstash (counters and rate limits stay);
- the main database: every chat (sessions, messages, thumbs), every review-queue item, every
  customer and staff account, the alert rows, the hourly and daily metric rows, and the per-key
  call counts.

What is KEPT, and why:
- the admin accounts (you could not sign in again otherwise);
- the audit log (it is the record of this purge; its rows are the only history left);
- the allow-list entities (your decision: they are kept);
- the list of withdrawn document ids, the rollout stage and the other settings, and the pending
  trace removals (a privacy duty that must still be carried out);
- Langfuse traces and the evaluation database (they are other stores; remove them there).

The order is: vectors, cache, then the database in ONE transaction that ends with the audit row. A
step that fails is reported, and the command can be run again: every step is safe to repeat.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qdrant_client import models

from app.accounts.audit import write_audit
from app.db.connection import Database

PHRASE = "DELETE ALL DATA"
ACTOR_PATTERN = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")
PAGE = 256
MAX_PAGES = 20_000  # at most about five million points: a stop, not a limit anyone reaches

KEPT = (
    "admin accounts",
    "the audit log",
    "the allow-list entities",
    "the list of withdrawn documents, the rollout stage and the other settings",
    "pending trace removals",
)
NOT_TOUCHED = ("Langfuse traces", "the evaluation database", "the original PDF files")
WARNING = (
    "Warning: this erases all indexed knowledge (every document vector), every conversation, every "
    "customer and staff account, the review queue, the cached answers, the alert rows and the "
    "metrics. This cannot be undone."
)


@dataclass
class PurgeResult:
    counts: dict[str, int] = field(default_factory=dict)
    incomplete: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.incomplete


def _count(db: Database, sql: str) -> int:
    row = db.query_one(sql)
    return int(row[0]) if row else 0


def database_counts(db: Database) -> dict[str, int]:
    """What the database step would erase (a count of rows; nothing is read of their text)."""
    with db.transaction():
        return {
            "users": _count(db, "SELECT COUNT(*) FROM users WHERE role <> 'admin'"),
            "admins_kept": _count(db, "SELECT COUNT(*) FROM users WHERE role = 'admin'"),
            "chats": _count(db, "SELECT COUNT(*) FROM sessions"),
            "messages": _count(db, "SELECT COUNT(*) FROM messages"),
            "feedback": _count(db, "SELECT COUNT(*) FROM feedback_entries"),
            "review_items": _count(db, "SELECT COUNT(*) FROM review_queue"),
            "alert_rows": _count(db, "SELECT COUNT(*) FROM alert_events"),
            "metric_rows": _count(db, "SELECT COUNT(*) FROM metric_rollups"),
            "counter_rows": _count(db, "SELECT COUNT(*) FROM key_usage_daily"),
        }


def plan(db: Database, qdrant: Any, collection: str) -> dict[str, int]:
    """Everything the purge would erase, as counts. Changes nothing."""
    counts = database_counts(db)
    counts["chunks"] = int(qdrant.count(collection, exact=True).count)
    return counts


def delete_all_points(qdrant: Any, collection: str) -> int:
    """Remove every point of the collection, page by page (the collection stays). Returns how many
    were removed. Safe to repeat: a second run finds nothing."""
    removed = 0
    for _ in range(MAX_PAGES):
        points, _next = qdrant.scroll(
            collection, limit=PAGE, with_payload=False, with_vectors=False
        )
        if not points:
            break
        qdrant.delete(
            collection, points_selector=models.PointIdsList(points=[p.id for p in points])
        )
        removed += len(points)
    return removed


def erase_database(db: Database, *, actor: str, now: datetime) -> dict[str, int]:
    """The database step: one transaction, ending with the audit row. Returns the counts erased."""
    with db.transaction(write=True):
        counts = database_counts(db)
        for sql in (
            "DELETE FROM feedback_entries",
            "DELETE FROM messages",
            "DELETE FROM review_queue",
            "DELETE FROM sessions",
            "DELETE FROM users WHERE role <> 'admin'",
            "DELETE FROM alert_events",
            "DELETE FROM metric_rollups",
            "DELETE FROM key_usage_daily",
        ):
            db.execute(sql)
        write_audit(
            db,
            actor=actor,
            action="purge_all",
            target="database",
            details={k: v for k, v in counts.items() if k != "admins_kept"},
            now=now,
        )
    return counts


def purge(
    db: Database,
    qdrant: Any,
    collection: str,
    cache: Any,
    *,
    actor: str,
    now: datetime,
    on_step: Callable[[str], None] | None = None,
) -> PurgeResult:
    """Erase the vectors, the cached answers and the database rows (see the module note). A step
    that fails is named in `incomplete` and the next one still runs; nothing is half-written in the
    database (its step is one transaction)."""
    if not ACTOR_PATTERN.match(actor):
        raise ValueError("the actor name is not valid")
    result = PurgeResult()

    def say(text: str) -> None:
        if on_step:
            on_step(text)

    say("erasing the document vectors")
    try:
        result.counts["chunks"] = delete_all_points(qdrant, collection)
    except Exception as error:
        result.incomplete.append(f"the vector store did not finish ({type(error).__name__})")
    say("clearing the cached answers")
    if cache is None:
        result.incomplete.append("no cache is configured here, so no cached answer was cleared")
    else:
        removed = cache.clear_cached_answers()
        if removed is None:
            result.incomplete.append("the cache was not cleared (it could not be reached)")
        else:
            result.counts["cached_answers"] = removed
    say("erasing the database rows")
    try:
        result.counts.update(erase_database(db, actor=actor, now=now))
    except Exception as error:
        result.incomplete.append(f"the database was not changed ({type(error).__name__})")
    return result
