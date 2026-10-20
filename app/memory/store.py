"""Sessions and messages in the main database (spec 8.3, 8.4, 12.1, 13.6).

Rules this module enforces in code:
- Every function takes the USER id. A session that belongs to someone else raises exactly the
  same `SessionNotFound` as a session that does not exist, so nothing tells the two apart. A CLOSED
  session refuses new messages, facts and summaries with that same error.
- Message text must be a `StoredQuery` (a user message) or a `StoredAnswer` (an assistant message),
  the masked types only `mask_for_storage` can make (see `app.guardrails.minted`). A plain string,
  the pass-1 `ModelCopy` or a hand-built look-alike is refused with a TypeError. A summary must be a
  `StoredSummary`, which only `fold_summary` can make.
- Two messages of one session never share a `seq`: the next number is read, the insert relies on
  the unique constraint `(session_id, seq)`, and a collision is retried with a fresh number.
- A message's `grounded` flag is stored with it; the history uses only grounded answers. A fixed
  reply (greeting, the unclear-turn ending, a refusal) is stored with `add_fixed_reply`, which is
  always `grounded = False`. A question that tripped the injection scan, or whose answer was a
  refusal, is stored with `history_excluded = True` and never feeds history or the summary.
- Every read runs inside `db.transaction()`, so on Postgres (autocommit off) no read leaves a
  transaction open. Nothing here holds a transaction while a model is called.
- `now` must be timezone-aware (a naive time would be read as local time and shift the 90-day
  boundary), and `last_active_at` never moves backwards.

One turn at a time per session: two requests in one session (two browser tabs) are not ordered
against each other; they get different message numbers but their user and assistant rows can
interleave. Phase 8 (the pipeline) and Phase 10 (the chat API) must serialise the turns of a
session, for example by refusing a second question while one is being answered.

Times are written as UTC text (`utc_text`). The caller owns the clock (`now`).
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.accounts.tokens import utc_text
from app.db.chats import delete_session
from app.db.connection import Database
from app.guardrails.minted import is_minted
from app.guardrails.scan import StoredAnswer, StoredQuery
from app.memory.stored_summary import StoredSummary

MAX_SEQ_RETRIES = 8
# After a lost race a writer waits a random time up to this many seconds times the number of
# attempts so far, so several writers on one session spread out instead of colliding again at once
# (the free-running writers test on a loaded runner ran out of retries without it).
SEQ_BACKOFF_SECONDS = 0.02
TITLE_CHARACTERS = 60
MAX_CITATIONS = 10
MAX_CLAUSE_CHARACTERS = 40


class SessionNotFound(Exception):
    """The session does not exist, belongs to someone else, or is closed (one error for all)."""

    def __init__(self) -> None:
        super().__init__("session not found")


class SeqCollision(RuntimeError):
    """The next message number kept colliding with other writers."""


@dataclass(frozen=True)
class SessionInfo:
    id: str
    title: str | None
    created_at: str
    last_active_at: str
    status: str


@dataclass(frozen=True)
class StoredMessage:
    id: str
    seq: int
    role: str
    content: str
    citations: tuple[dict[str, Any], ...]
    grounded: bool | None
    source_removed: bool
    created_at: str
    history_excluded: bool = False
    shadowed: bool = False  # generated while the user was not allowed to see answers
    fixed_reply: bool = False  # one of our own fixed texts, not a generated answer (0008)


@dataclass
class SessionState:
    """What a resume needs: the summary, the facts and the messages after the summary boundary."""

    info: SessionInfo
    summary_text: str | None
    summary_upto_message_id: str | None
    facts: dict[str, Any]
    messages: list[StoredMessage] = field(default_factory=list)


def require_user(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("a user id is required")
    return user_id


def require_aware(moment: datetime) -> datetime:
    """A naive time would be treated as local time and shift the retention boundary by hours."""
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return moment


def is_unique_violation(error: BaseException) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return "UNIQUE" in str(error).upper()
    try:
        import psycopg

        return isinstance(error, psycopg.errors.UniqueViolation)
    except ImportError:  # pragma: no cover
        return False


def parse_facts(text: str | None) -> dict[str, Any]:
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_SESSION_COLUMNS = (
    "id, title, created_at, last_active_at, status, summary_text, summary_upto_message_id, "
    "session_facts"
)


def _info(row: tuple) -> SessionInfo:
    return SessionInfo(row[0], row[1], str(row[2]), str(row[3]), row[4])


def owned_session(
    db: Database,
    user_id: str,
    session_id: str,
    *,
    lock: bool = False,
    writable: bool = False,
) -> tuple:
    """The session row, or SessionNotFound (also for someone else's session, and for a closed one
    when `writable`). `lock` takes a row lock on Postgres for a read-modify-write (SQLite takes the
    write lock with the transaction, so the caller opens it with write=True)."""
    require_user(user_id)
    suffix = " FOR UPDATE" if lock and db.kind == "postgres" else ""
    with db.transaction():
        row = db.query_one(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ? AND user_id = ?{suffix}",
            (session_id, user_id),
        )
    if row is None or (writable and row[4] != "active"):
        raise SessionNotFound
    return row


def create_session(
    db: Database, user_id: str, *, now: datetime, session_id: str | None = None
) -> str:
    """`session_id`: the id the answer already carries (the persist stage hands one out before the
    write, so a write that fails or is late still leaves the user with a conversation id)."""
    require_user(user_id)
    stamp = utc_text(require_aware(now))
    session_id = session_id or str(uuid.uuid4())
    with db.transaction():
        db.execute(
            "INSERT INTO sessions (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
            (session_id, user_id, stamp, stamp),
        )
    return session_id


def list_sessions(db: Database, user_id: str, *, limit: int = 50) -> list[SessionInfo]:
    """The user's sessions, most recently active first."""
    require_user(user_id)
    with db.transaction():
        rows = db.query_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE user_id = ? "
            "ORDER BY last_active_at DESC, id LIMIT ?",
            (user_id, max(1, min(int(limit), 200))),
        )
    return [_info(row) for row in rows]


def list_sessions_page(
    db: Database, user_id: str, *, limit: int, offset: int
) -> tuple[list[SessionInfo], int]:
    """One page of the user's sessions, most recently active first, and the total number."""
    require_user(user_id)
    with db.transaction():
        total = db.query_one("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,))
        rows = db.query_all(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE user_id = ? "
            "ORDER BY last_active_at DESC, id LIMIT ? OFFSET ?",
            (user_id, int(limit), int(offset)),
        )
    return [_info(row) for row in rows], int(total[0]) if total else 0


def transcript(
    db: Database, user_id: str, session_id: str, *, limit: int, offset: int
) -> tuple[SessionInfo, list[StoredMessage], int]:
    """One page of the WHOLE transcript of a session of this user (not only the messages after the
    summary boundary), oldest first, and the total number of messages. A session of someone else
    raises the same SessionNotFound as a missing one."""
    row = owned_session(db, user_id, session_id)
    with db.transaction():
        total = db.query_one("SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,))
        rows = db.query_all(
            "SELECT id, seq, role, content, citations, grounded, source_removed, created_at, "
            "history_excluded, shadowed, fixed_reply FROM messages WHERE session_id = ? "
            "ORDER BY seq LIMIT ? OFFSET ?",
            (session_id, int(limit), int(offset)),
        )
    return _info(row), [_message(r) for r in rows], int(total[0]) if total else 0


def _message(row: tuple) -> StoredMessage:
    try:
        citations = json.loads(row[4])
    except (TypeError, ValueError):
        citations = []
    citations = [c for c in citations if isinstance(c, dict)] if isinstance(citations, list) else []
    grounded = None if row[5] is None else bool(row[5])
    return StoredMessage(
        row[0],
        int(row[1]),
        row[2],
        row[3],
        tuple(citations),
        grounded,
        bool(row[6]),
        str(row[7]),
        bool(row[8]),
        bool(row[9]) if len(row) > 9 else False,
        bool(row[10]) if len(row) > 10 else False,
    )


def load_session(db: Database, user_id: str, session_id: str) -> SessionState:
    """Resume (spec 8.3): the summary, the facts and the messages after `summary_upto_message_id`
    (all of them when there is no summary yet). The verbatim buffer is built from these by
    `app.memory.buffer`; the last turns are not stored twice."""
    require_user(user_id)
    with db.transaction():
        row = db.query_one(
            f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )
        if row is None:
            raise SessionNotFound
        floor = 0
        if row[6]:
            boundary = db.query_one(
                "SELECT seq FROM messages WHERE id = ? AND session_id = ?", (row[6], session_id)
            )
            floor = int(boundary[0]) if boundary else 0
        rows = db.query_all(
            "SELECT id, seq, role, content, citations, grounded, source_removed, created_at, "
            "history_excluded, shadowed, fixed_reply FROM messages "
            "WHERE session_id = ? AND seq > ? ORDER BY seq",
            (session_id, floor),
        )
    return SessionState(
        _info(row), row[5], row[6], parse_facts(row[7]), [_message(r) for r in rows]
    )


def clean_citations(citations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The spec 8.5 shape: a list of objects with the `doc_id` of the cited document and, when
    known, its clause and page. Anything else is dropped; text is never kept here."""
    out: list[dict[str, Any]] = []
    for item in list(citations)[:MAX_CITATIONS]:
        doc_id = item.get("doc_id") if isinstance(item, Mapping) else None
        if not isinstance(doc_id, str) or not doc_id:
            raise ValueError("every citation needs a doc_id")
        entry: dict[str, Any] = {"doc_id": doc_id}
        clause, page = item.get("clause"), item.get("page")
        if isinstance(clause, str) and clause.strip():
            entry["clause"] = clause.strip()[:MAX_CLAUSE_CHARACTERS]
        if isinstance(page, int) and not isinstance(page, bool):
            entry["page"] = page
        out.append(entry)
    return out


def _next_seq(db: Database, session_id: str) -> int:
    with db.transaction():
        row = db.query_one(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?", (session_id,)
        )
    return int(row[0]) if row else 1


def _add_message(
    db: Database,
    user_id: str,
    session_id: str,
    role: str,
    text: str,
    citations: list[dict[str, Any]],
    grounded: bool | None,
    history_excluded: bool,
    now: datetime,
    shadowed: bool = False,
    fixed_reply: bool = False,
    seq: int | None = None,
    owned: bool = False,
) -> StoredMessage:
    """`seq` is the number to try first (the one after the message just written in the same
    transaction): the unique rule on (session, seq) is still the judge. `owned=True` says the caller
    has just checked, in this transaction, that the session is the user's and open."""
    if not text.strip():
        raise ValueError("a message cannot be empty")
    stamp = utc_text(require_aware(now))
    if not owned:
        owned_session(db, user_id, session_id, writable=True)
    nested = db.in_transaction  # a larger transaction is open: the caller retries the whole write
    title = " ".join(text.split())[:TITLE_CHARACTERS] if role == "user" else None
    # never backwards: a late write from a slow request must not make the chat look older
    newest = "GREATEST(last_active_at, ?)" if db.kind == "postgres" else "MAX(last_active_at, ?)"
    for attempt in range(MAX_SEQ_RETRIES):
        number = seq if (seq is not None and attempt == 0) else _next_seq(db, session_id)
        message_id = str(uuid.uuid4())
        try:
            with db.transaction():
                db.execute(
                    "INSERT INTO messages (id, session_id, seq, role, content, citations, "
                    "grounded, history_excluded, created_at, shadowed, fixed_reply) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        session_id,
                        number,
                        role,
                        text,
                        json.dumps(citations, sort_keys=True),
                        grounded,
                        history_excluded,
                        stamp,
                        bool(shadowed),
                        bool(fixed_reply),
                    ),
                )
                db.execute(
                    f"UPDATE sessions SET last_active_at = {newest}, "
                    "title = COALESCE(title, ?) WHERE id = ?",
                    (stamp, title, session_id),
                )
        except Exception as error:
            if is_unique_violation(error):
                if nested:
                    raise  # a spoiled open transaction is rolled back and the caller retries
                # another writer took this number: wait a little, at random, then read a fresh one
                time.sleep(random.uniform(0, SEQ_BACKOFF_SECONDS * (attempt + 1)))
                continue
            raise
        return StoredMessage(
            message_id,
            number,
            role,
            text,
            tuple(citations),
            grounded,
            False,
            stamp,
            history_excluded,
            bool(shadowed),
            bool(fixed_reply),
        )
    raise SeqCollision("could not find a free message number")


def add_user_message(
    db: Database,
    user_id: str,
    session_id: str,
    stored: StoredQuery,
    *,
    now: datetime,
    history_excluded: bool = False,
    seq: int | None = None,
    owned: bool = False,
) -> StoredMessage:
    """`history_excluded=True` for a question that tripped the injection scan or whose answer was
    a refusal: it is kept in the transcript and never feeds history or the summary."""
    if not is_minted(stored, StoredQuery):
        raise TypeError("a user message must be a StoredQuery (made by mask_for_storage)")
    return _add_message(
        db,
        user_id,
        session_id,
        "user",
        stored.text,
        [],
        None,
        bool(history_excluded),
        now,
        seq=seq,
        owned=owned,
    )


def add_assistant_message(
    db: Database,
    user_id: str,
    session_id: str,
    stored: StoredAnswer,
    *,
    citations: Sequence[Mapping[str, Any]],
    grounded: bool,
    now: datetime,
    shadowed: bool = False,
    seq: int | None = None,
    owned: bool = False,
) -> StoredMessage:
    if not is_minted(stored, StoredAnswer):
        raise TypeError("an assistant message must be a StoredAnswer (made by mask_for_storage)")
    if not isinstance(grounded, bool):
        raise TypeError("grounded must be True or False")
    return _add_message(
        db,
        user_id,
        session_id,
        "assistant",
        stored.text,
        clean_citations(citations),
        grounded,
        False,
        now,
        shadowed=bool(shadowed),
        seq=seq,
        owned=owned,
    )


def add_fixed_reply(
    db: Database,
    user_id: str,
    session_id: str,
    stored: StoredAnswer,
    *,
    now: datetime,
    shadowed: bool = False,
    seq: int | None = None,
    owned: bool = False,
) -> StoredMessage:
    """A fixed reply (a greeting, the unclear-turn ending, a refusal, the low-confidence text):
    always `grounded = False` and never cited, so it can never feed history."""
    if not is_minted(stored, StoredAnswer):
        raise TypeError("a fixed reply must be a StoredAnswer (made by mask_for_storage)")
    return _add_message(
        db,
        user_id,
        session_id,
        "assistant",
        stored.text,
        [],
        False,
        False,
        now,
        shadowed=bool(shadowed),
        fixed_reply=True,
        seq=seq,
        owned=owned,
    )


def close_session(db: Database, user_id: str, session_id: str) -> None:
    owned_session(db, user_id, session_id)
    with db.transaction():
        db.execute(
            "UPDATE sessions SET status = 'closed' WHERE id = ? AND user_id = ?",
            (session_id, user_id),
        )


def save_summary(
    db: Database,
    user_id: str,
    session_id: str,
    summary: StoredSummary,
    upto_message_id: str,
    *,
    expected_upto: str | None,
    folded_ids: Sequence[str],
) -> bool:
    """Save a new summary and boundary. True if saved. It is refused (False) when:
    - the boundary is no longer the one the caller read (compare-and-set: two turns folding at
      once cannot overwrite each other), or
    - one of the folded messages has been flagged "source removed" since the caller read it (a
      document was deleted while the model was working: the summary would rest on it).
    The boundary message must belong to this session (ValueError otherwise). The folded messages
    are row-locked on Postgres, so a document delete that flagged them but has not committed yet
    is waited for, and its clearing of summaries then sees what is saved here."""
    if not is_minted(summary, StoredSummary):
        raise TypeError("a summary must be a StoredSummary (made by fold_summary)")
    if not upto_message_id:
        raise ValueError("a summary needs the id of the last message folded into it")
    with db.transaction(write=True):
        owned_session(db, user_id, session_id, lock=True, writable=True)
        boundary = db.query_one(
            "SELECT 1 FROM messages WHERE id = ? AND session_id = ?", (upto_message_id, session_id)
        )
        if boundary is None:
            raise ValueError("the boundary message is not in this session")
        ids = list(dict.fromkeys(folded_ids))
        if ids:
            marks = ", ".join("?" for _ in ids)
            lock = " FOR UPDATE" if db.kind == "postgres" else ""
            rows = db.query_all(
                "SELECT source_removed FROM messages "
                f"WHERE session_id = ? AND id IN ({marks}){lock}",
                (session_id, *ids),
            )
            if any(bool(row[0]) for row in rows):
                return False
        where = (
            "summary_upto_message_id IS NULL"
            if expected_upto is None
            else "summary_upto_message_id = ?"
        )
        params: tuple = (summary.text, upto_message_id, session_id, user_id)
        if expected_upto is not None:
            params += (expected_upto,)
        cursor = db.execute(
            "UPDATE sessions SET summary_text = ?, summary_upto_message_id = ? "
            f"WHERE id = ? AND user_id = ? AND {where}",
            params,
        )
        return cursor.rowcount == 1


def delete_own_session(db: Database, user_id: str, session_id: str) -> bool:
    """A user deletes their own chat (spec 13.6): the session, its messages and feedback, and the
    text copied into review items. False for a missing session AND for someone else's (the caller
    cannot tell them apart); someone else's session is left untouched."""
    require_user(user_id)
    with db.transaction(write=True):
        row = db.query_one(
            "SELECT 1 FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
        )
        if row is None:
            return False
        return delete_session(db.raw, session_id)
