"""Thumbs feedback on an answer (spec 11.4, 12.1 `feedback_entries`, 19).

`POST /messages/{message_id}/feedback {rating: "up" | "down"}`.

- Only an ASSISTANT message of the signed-in user's own session can be rated; anything else
  (someone else's message, a user message, a malformed or unknown id, or an answer the user may
  not see because it was generated in shadow mode) is the same 404.
- One entry per user per message: the other thumb changes the entry (unique index, migration 0006).
- A thumbs-down also puts ONE `thumbs_down` item in the admin review queue, with the masked
  question; giving it twice does not add a second item.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict

from app.accounts.tokens import utc_text
from app.api.deps import CurrentUser, authenticated, get_db, is_uuid
from app.api.errors import ApiError
from app.db.connection import Database

router = APIRouter(tags=["feedback"])


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["up", "down"]


@router.post("/messages/{message_id}/feedback")
def give_feedback(
    body: FeedbackBody,
    message_id: str = Path(max_length=64),
    current: CurrentUser = Depends(authenticated),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    if not is_uuid(message_id):
        raise ApiError(404, "not_found", "Not found.")
    now = datetime.now(UTC)
    stamp = utc_text(now)
    with db.transaction(write=True):
        row = db.query_one(
            "SELECT m.id, m.session_id, m.seq, m.shadowed FROM messages m "
            "JOIN sessions s ON s.id = m.session_id "
            "WHERE m.id = ? AND s.user_id = ? AND m.role = 'assistant'",
            (message_id, current.user.id),
        )
        if row is None or (row[3] and current.user.role != "admin"):
            raise ApiError(404, "not_found", "Not found.")
        changed = db.execute(
            "UPDATE feedback_entries SET rating = ?, created_at = ? "
            "WHERE message_id = ? AND user_id = ?",
            (body.rating, stamp, message_id, current.user.id),
        )
        if (changed.rowcount or 0) == 0:
            db.execute(
                "INSERT INTO feedback_entries (id, message_id, user_id, rating, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), message_id, current.user.id, body.rating, stamp),
            )
        if body.rating == "down":
            existing = db.query_one(
                "SELECT 1 FROM review_queue WHERE message_id = ? AND kind = 'thumbs_down'",
                (message_id,),
            )
            if existing is None:
                question = db.query_one(
                    "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                    "AND seq < ? ORDER BY seq DESC LIMIT 1",
                    (row[1], row[2]),
                )
                details = {"reason": "thumbs_down", "question": question[0] if question else ""}
                db.execute(
                    "INSERT INTO review_queue "
                    "(id, kind, status, session_id, message_id, details, created_at) "
                    "VALUES (?, 'thumbs_down', 'open', ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        row[1],
                        message_id,
                        json.dumps(details, sort_keys=True),
                        stamp,
                    ),
                )
    return {"message_id": message_id, "rating": body.rating}
