"""Deleting a chat (spec 8.4, 13.6): the session, its messages, and the text copied from them.

A deleted chat must not live on in the review queue. The foreign keys already remove the messages
and their feedback and clear the review item's links, but a link cannot erase text, so the
review item's `details` (masked text) is cleared first, while the links still show which items
belong to the chat. The review row itself stays (its kind, status and label are not chat text).

The caller owns the transaction: nothing is committed here, so the clearing and the delete
succeed or fail together. The 90-day retention sweep (Phase 7) deletes sessions with this too.
"""

from __future__ import annotations

import sqlite3
from typing import Any

EMPTY_DETAILS = "{}"


def _placeholder(connection: Any) -> str:
    """SQLite uses ?, psycopg uses %s."""
    return "?" if isinstance(connection, sqlite3.Connection) else "%s"


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
    their feedback go with it). Returns True if the session existed. Does not commit."""
    p = _placeholder(connection)
    clear_review_details_for_session(connection, session_id)
    cursor = connection.execute(f"DELETE FROM sessions WHERE id = {p}", (session_id,))
    return cursor.rowcount > 0
