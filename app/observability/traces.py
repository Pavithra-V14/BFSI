"""Removing the Langfuse traces of a deleted chat (spec 13.6: "related traces are removed by session
id").

A trace carries its chat only as a KEYED HASH (`sessionId`, made with `LANGFUSE_USER_HASH_KEY`;
see `app.pipeline.langfuse_sink`). To remove them this module
1. lists the traces of that hash (`GET /api/public/traces?sessionId=...`, paged), and
2. deletes each one (`DELETE /api/public/traces/{id}`).

A chat is queued in `trace_deletions` BEFORE its rows are deleted, holding only the hash. The queue
is worked off by `process_pending` (the API runs it in the background; the retention command runs it
once after a sweep). A failure keeps the row and counts an attempt; after `MAX_ATTEMPTS` the row
raises one `trace_deletion_failed` alert (critical: a privacy obligation) and stays for a later try.
Nothing here ever raises into a request.

The two Langfuse routes are the documented public ones; `docs/OWNER_RUNS.md` (Run 18) has a check
of them against the real service, because they are the part that cannot be tested without it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.accounts.audit import keyed_hash
from app.accounts.tokens import utc_text
from app.db.connection import Database

MAX_ATTEMPTS = 5
PAGE_SIZE = 100
MAX_PAGES = 10  # at most 1,000 traces of one chat are removed in one pass
TIMEOUT_SECONDS = 5.0
BATCH = 50  # chats worked off in one pass

# Tests put an `httpx.MockTransport` here; the running service leaves it None (real network).
TRANSPORT: httpx.BaseTransport | None = None


def session_hash(user_hash_key: str, session_id: str) -> str:
    """The `sessionId` the sink wrote on this chat's traces."""
    return keyed_hash(user_hash_key, "langfuse-session", session_id)


class TraceDeleter:
    def __init__(self, host: str, public_key: str, secret_key: str) -> None:
        self._host = host.rstrip("/")
        self._auth = (public_key, secret_key)

    @classmethod
    def from_config(cls, config: Any) -> "TraceDeleter":
        """From the Langfuse sink's configuration (host, public_key, secret_key)."""
        return cls(config.host, config.public_key, config.secret_key)

    def __repr__(self) -> str:  # no key, no host
        return "TraceDeleter()"

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=TIMEOUT_SECONDS, transport=TRANSPORT, auth=self._auth)

    def trace_ids(self, hash_value: str) -> list[str]:
        ids: list[str] = []
        with self._client() as client:
            for page in range(1, MAX_PAGES + 1):
                response = client.get(
                    f"{self._host}/api/public/traces",
                    params={"sessionId": hash_value, "limit": PAGE_SIZE, "page": page},
                )
                response.raise_for_status()
                body = response.json()
                found = [str(t.get("id")) for t in body.get("data", []) if t.get("id")]
                ids.extend(found)
                if len(found) < PAGE_SIZE:
                    break
        return ids

    def delete(self, trace_ids: Iterable[str]) -> int:
        removed = 0
        with self._client() as client:
            for trace_id in trace_ids:
                response = client.delete(f"{self._host}/api/public/traces/{trace_id}")
                if response.status_code == 404:  # already gone
                    continue
                response.raise_for_status()
                removed += 1
        return removed

    def delete_session(self, hash_value: str) -> int:
        return self.delete(self.trace_ids(hash_value))


# ---- the queue -------------------------------------------------------------------------------
def enqueue(db: Database, hashes: Iterable[str], now: datetime) -> int:
    """Remember chats whose traces must go. One row per hash; asking twice is harmless."""
    added = 0
    with db.transaction(write=True):
        for value in hashes:
            cursor = db.execute(
                "INSERT INTO trace_deletions (id, session_hash, attempts, alerted, created_at) "
                "VALUES (?, ?, 0, ?, ?) ON CONFLICT (session_hash) DO NOTHING",
                (str(uuid.uuid4()), value, False, utc_text(now)),
            )
            added += 1 if (getattr(cursor, "rowcount", 1) or 0) else 0
    return added


def pending_count(db: Database) -> int:
    row = db.query_one("SELECT COUNT(*) FROM trace_deletions")
    return int(row[0]) if row else 0


@dataclass(frozen=True)
class PassResult:
    removed_chats: int
    failed: int
    alerted: int


def process_pending(
    db: Database,
    deleter: TraceDeleter,
    now: datetime,
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    limit: int = BATCH,
) -> PassResult:
    """Work off the queue once. A row is removed only when its traces are gone."""
    with db.transaction():
        rows = db.query_all(
            "SELECT id, session_hash, attempts, alerted FROM trace_deletions "
            "ORDER BY created_at, id LIMIT ?",
            (int(limit),),
        )
    removed = failed = alerted = 0
    for row_id, hash_value, attempts, already_alerted in rows:
        try:
            deleter.delete_session(hash_value)
        except Exception as error:
            failed += 1
            attempts = int(attempts) + 1
            raise_alert = attempts >= max_attempts and not already_alerted
            with db.transaction(write=True):
                db.execute(
                    "UPDATE trace_deletions SET attempts = ?, last_attempt_at = ?, alerted = ? "
                    "WHERE id = ?",
                    (attempts, utc_text(now), bool(already_alerted or raise_alert), row_id),
                )
            if raise_alert:
                alerted += 1
                if on_event is not None:
                    try:
                        on_event(
                            "trace_deletion_failed",
                            {"reason": type(error).__name__, "attempts": attempts},
                        )
                    except Exception:
                        pass
            continue
        with db.transaction(write=True):
            db.execute("DELETE FROM trace_deletions WHERE id = ?", (row_id,))
        removed += 1
    return PassResult(removed, failed, alerted)


def now_utc() -> datetime:
    return datetime.now(UTC)
