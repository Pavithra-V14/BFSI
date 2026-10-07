"""The 90-day chat retention sweep (spec 8.4).

A session is deleted when its `last_active_at` is more than 90 days old (the age is counted from
the last activity, not from creation). Each deletion goes through `app.db.chats.delete_session`, so
the text copied into review items is cleared first and the messages and their feedback go with the
session. The sweep runs in ONE transaction and writes ONE audit row (`retention_sweep`: counts only,
never text), and none when there was nothing to delete.

Run: uv run python -m app.memory.retention [--dry-run]
`--dry-run` only counts (sessions and messages) and changes nothing, not even an audit row. The
database URL is read from DATABASE_URL in the shell, never from the command line. Scheduling this
(a daily job on the host) is Phase 15.
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
    db: Database, *, now: datetime, actor: str, dry_run: bool = False, days: int = RETENTION_DAYS
) -> SweepResult:
    with db.transaction(write=not dry_run):
        ids = expired_session_ids(db, now, days)
        messages = count_messages(db, ids)
        if dry_run or not ids:
            return SweepResult(len(ids), messages, dry_run)
        deleted = 0
        for session_id in ids:
            if delete_session(db.raw, session_id):
                deleted += 1
        write_audit(
            db,
            actor=actor,
            action="retention_sweep",
            target="sessions",
            details={"sessions": deleted, "messages": messages, "days": days},
            now=now,
        )
    return SweepResult(deleted, messages, False)


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
    db = connect(url)
    try:
        result = sweep(db, now=now or datetime.now(UTC), actor=os_actor(), dry_run=args.dry_run)
    finally:
        db.close()
    verb = "would delete" if result.dry_run else "deleted"
    print(f"{verb} {result.sessions} chats ({result.messages} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
