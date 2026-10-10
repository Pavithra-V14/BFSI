"""Thumbs feedback rows and the review item a thumbs-down makes (spec 12.1, 11.4, 19).

Both writes are ONE statement that cannot collide with itself: two requests at the same moment (a
double click, on Postgres) leave one rating per user and answer, and one `thumbs_down` item per
answer, instead of one of them failing on the unique index. The indexes are in migrations 0006
(`feedback_entries (message_id, user_id)`) and 0007 (`review_queue (message_id)` for thumbs_down).
"""

from __future__ import annotations

import json
import uuid

from app.db.connection import Database


def upsert_rating(db: Database, *, message_id: str, user_id: str, rating: str, stamp: str) -> None:
    """Store the user's thumb for the answer; a second thumb changes the first."""
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (message_id, user_id) DO UPDATE SET "
        "rating = excluded.rating, created_at = excluded.created_at",
        (str(uuid.uuid4()), message_id, user_id, rating, stamp),
    )


def add_thumbs_down_item(
    db: Database, *, message_id: str, session_id: str, question: str, stamp: str
) -> None:
    """One `thumbs_down` item for the answer, with the masked question. A second one is a no-op."""
    details = {"reason": "thumbs_down", "question": question}
    db.execute(
        "INSERT INTO review_queue (id, kind, status, session_id, message_id, details, created_at) "
        "VALUES (?, 'thumbs_down', 'open', ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (str(uuid.uuid4()), session_id, message_id, json.dumps(details, sort_keys=True), stamp),
    )
