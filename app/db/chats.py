"""Deleting a chat (spec 8.4, 13.6): the session, its messages, and the text copied from them.

A deleted chat must not live on in the review queue. The foreign keys already remove the messages
and their feedback and clear the review item's links, but a link cannot erase text, so the
review item's `details` (masked text) is cleared first, while the links still show which items
belong to the chat. The review row itself stays (its kind, status and label are not chat text).

The caller owns the transaction: nothing is committed here, so (when the connection is not in
autocommit mode) the clearing and the delete succeed or fail together. The 90-day retention
sweep (Phase 7) deletes sessions with this too.
"""

from __future__ import annotations

import sqlite3
from typing import Any

EMPTY_DETAILS = "{}"


def _placeholder(connection: Any) -> str:
    """SQLite uses ?, psycopg uses %s."""
    return "?" if isinstance(connection, sqlite3.Connection) else "%s"


class ForeignKeysOff(RuntimeError):
    """A SQLite connection has foreign keys off and they cannot be switched on right now."""


def ensure_foreign_keys(connection: Any) -> None:
    """On SQLite, run PRAGMA foreign_keys = ON (CLAUDE.md invariant) and check that it took.

    SQLite ignores the pragma inside an open transaction, and without foreign keys the delete
    would leave the messages (and their text) behind. So if it is still off afterwards, this
    raises instead of letting a chat "delete" half-happen. Postgres always enforces them.
    """
    if not isinstance(connection, sqlite3.Connection):
        return
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ForeignKeysOff(
            "foreign keys are off on this SQLite connection and cannot be switched on inside an "
            "open transaction; enable PRAGMA foreign_keys = ON when the connection is opened"
        )


def clear_review_details_for_session(connection: Any, session_id: str) -> int:
    """Blank `details` on every review item linked to the session or to one of its messages.
    Returns how many items were cleared."""
    p = _placeholder(connection)
    cursor = connection.execute(
        f"UPDATE review_queue SET details = {p} "
        f"WHERE session_id = {p} "
        f"OR message_id IN (SELECT id FROM messages WHERE session_id = {p})",
        (EMPTY_DETAILS, session_id, session_id),
    )
    return cursor.rowcount


def delete_session(connection: Any, session_id: str) -> bool:
    """Delete a chat: clear the review items' text, then delete the session (its messages and
    their feedback go with it). Returns True if the session existed. Does not commit.

    On SQLite it switches foreign keys on first (and raises ForeignKeysOff if that is impossible),
    because the cascade is what removes the messages. Authorisation (that this chat belongs to the
    caller) is the caller's job. "Does not commit" is only meaningful when the connection is not in
    autocommit mode: under psycopg autocommit=True the clearing and the delete are two separate
    commits (the safe direction: details are cleared first). Phase 10's endpoint must check the
    owner and run this in one transaction.
    """
    p = _placeholder(connection)
    ensure_foreign_keys(connection)
    clear_review_details_for_session(connection, session_id)
    cursor = connection.execute(f"DELETE FROM sessions WHERE id = {p}", (session_id,))
    return cursor.rowcount > 0
