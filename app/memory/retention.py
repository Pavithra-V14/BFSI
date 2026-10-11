"""The 90-day chat retention sweep (spec 8.4).

A session is deleted when its `last_active_at` is more than 90 days old (the age is counted from
the last activity, not from creation). Each deletion goes through `app.db.chats.delete_session`, so
the text copied into review items is cleared first and the messages and their feedback go with the
session. The sweep runs in ONE transaction and writes ONE audit row (`retention_sweep`: counts only,
never text), and none when there was nothing to delete.

The same command (Phase 12, owner-run, never automatic) also:
- removes the Langfuse traces of the chats it deleted: it queues their keyed session hash in
  `trace_deletions` (needs `LANGFUSE_USER_HASH_KEY` in the shell; without it nothing can be matched
  to a chat). The API's background worker works the queue off (within a minute of its next run);
- deletes `audit_log` rows older than 30 days, `alert_events` older than 30 days and hourly metric
  rollups older than 30 days, and writes ONE audit row (`audit_purge`: counts only). 30 days is the
  pilot value of spec 12.1 and is to be revisited in Phase 16.

Run: uv run python -m app.memory.retention [--dry-run]
`--dry-run` only counts (sessions and messages, old audit rows) and changes nothing, not even an
audit row. The database URL is read from DATABASE_URL in the shell, never from the command line.
Scheduling this (a daily job on the host) is Phase 15.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.db.chats import delete_session
from app.db.connection import Database, connect
from app.memory.config import RETENTION_DAYS
from app.memory.store import require_aware
from app.observability import traces

AUDIT_RETENTION_DAYS = 30  # [T] the pilot value (spec 12.1): to be revisited in Phase 16


@dataclass(frozen=True)
class SweepResult:
    sessions: int
    messages: int
    dry_run: bool


def cutoff(now: datetime, days: int = RETENTION_DAYS) -> str:
    return utc_text(now - timedelta(days=days))


def expired_session_ids(db: Database, now: datetime, days: int = RETENTION_DAYS) -> list[str]:
    rows = db.query_all(
        "SELECT id FROM sessions WHERE last_active_at < ? ORDER BY last_active_at, id",
        (cutoff(now, days),),
    )
    return [row[0] for row in rows]


def count_messages(db: Database, session_ids: list[str]) -> int:
    total = 0
    for start in range(0, len(session_ids), 200):
        batch = session_ids[start : start + 200]
        marks = ", ".join("?" for _ in batch)
        row = db.query_one(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({marks})", tuple(batch)
        )
        total += int(row[0]) if row else 0
    return total


def sweep(
    db: Database,
    *,
    now: datetime,
    actor: str,
    dry_run: bool = False,
    days: int = RETENTION_DAYS,
    trace_key: str | None = None,
) -> SweepResult:
    require_aware(now)
    limit = cutoff(now, days)
    with db.transaction(write=not dry_run):
        ids = expired_session_ids(db, now, days)
        if dry_run:
            return SweepResult(len(ids), count_messages(db, ids), True)
        deleted = messages = 0
        removed_ids: list[str] = []
        lock = " FOR UPDATE" if db.kind == "postgres" else ""
        for session_id in ids:
            # Re-checked under the lock, at the moment of deleting: a chat that was resumed after
            # the list was read has a newer last_active_at and stays.
            still_old = db.query_one(
                f"SELECT 1 FROM sessions WHERE id = ? AND last_active_at < ?{lock}",
                (session_id, limit),
            )
            if still_old is None:
                continue
            count = count_messages(db, [session_id])
            if trace_key is not None:  # queued BEFORE the chat goes, in the same transaction
                traces.enqueue(db, [traces.session_hash(trace_key, session_id)], now)
            if delete_session(db.raw, session_id):
                deleted += 1
                messages += count
                removed_ids.append(session_id)
        if deleted:  # no audit row when nothing was deleted
            write_audit(
                db,
                actor=actor,
                action="retention_sweep",
                target="sessions",
                details={"sessions": deleted, "messages": messages, "days": days},
                now=now,
            )
    return SweepResult(deleted, messages, False)


@dataclass(frozen=True)
class PurgeResult:
    audit_rows: int
    alert_rows: int
    hourly_rows: int
    dry_run: bool


def purge_old_rows(
    db: Database,
    *,
    now: datetime,
    actor: str,
    dry_run: bool = False,
    days: int = AUDIT_RETENTION_DAYS,
) -> PurgeResult:
    """Delete audit rows, alert rows and hourly rollups older than `days` (30 for the pilot), in
    ONE transaction with ONE audit row of counts. Daily rollups are kept: they hold counts only."""
    require_aware(now)
    limit = cutoff(now, days)
    with db.transaction(write=not dry_run):

        def count(table: str, where: str) -> int:
            row = db.query_one(f"SELECT COUNT(*) FROM {table} WHERE {where}", (limit,))
            return int(row[0]) if row else 0

        audit = count("audit_log", "created_at < ?")
        alerts = count("alert_events", "created_at < ?")
        hourly = count("metric_rollups", "period = 'hour' AND period_start < ?")
        if dry_run:
            return PurgeResult(audit, alerts, hourly, True)
        db.execute("DELETE FROM audit_log WHERE created_at < ?", (limit,))
        db.execute("DELETE FROM alert_events WHERE created_at < ?", (limit,))
        db.execute(
            "DELETE FROM metric_rollups WHERE period = 'hour' AND period_start < ?", (limit,)
        )
        if audit or alerts or hourly:  # no audit row when nothing was deleted
            write_audit(
                db,
                actor=actor,
                action="audit_purge",
                target="audit_log",
                details={"audit": audit, "alerts": alerts, "hourly": hourly, "days": days},
                now=now,
            )
    return PurgeResult(audit, alerts, hourly, False)


def os_actor() -> str:
    """The audit "who" for a command-line run: "cli:<operating-system login name>"."""
    try:
        name = getpass.getuser()
    except Exception:
        name = "unknown"
    return "cli:" + (re.sub(r"[^A-Za-z0-9._-]", "_", name)[:40] or "unknown")


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.memory.retention",
        allow_abbrev=False,
        description="Delete chats not used for 90 days (counted from the last activity).",
    )
    parser.add_argument("--dry-run", action="store_true", help="count only; change nothing")
    parser.add_argument(
        "--url-env",
        default="DATABASE_URL",
        help="name of the environment variable that holds the database URL "
        "(the URL itself is never taken from the command line)",
    )
    args = parser.parse_args(argv)
    url = os.environ.get(args.url_env, "").strip()
    if not url:
        print(f"error: the environment variable {args.url_env} is not set", file=sys.stderr)
        return 2
    trace_key = (os.environ.get("LANGFUSE_USER_HASH_KEY") or "").strip() or None
    moment = now or datetime.now(UTC)
    db = connect(url)
    try:
        result = sweep(db, now=moment, actor=os_actor(), dry_run=args.dry_run, trace_key=trace_key)
        purged = purge_old_rows(db, now=moment, actor=os_actor(), dry_run=args.dry_run)
        pending = traces.pending_count(db)
    finally:
        db.close()
    verb = "would delete" if result.dry_run else "deleted"
    print(f"{verb} {result.sessions} chats ({result.messages} messages)")
    print(
        f"{verb} {purged.audit_rows} audit rows, {purged.alert_rows} alert rows and "
        f"{purged.hourly_rows} hourly metric rows older than {AUDIT_RETENTION_DAYS} days"
    )
    if trace_key is None and result.sessions and not result.dry_run:
        print(
            "note: LANGFUSE_USER_HASH_KEY is not set, so the traces of these chats could not be "
            "queued for removal"
        )
    if pending:
        print(f"traces: {pending} chats queued for removal (the running API clears them)")
    return 0
