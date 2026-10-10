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

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from app.accounts.tokens import utc_text
from app.api.deps import CurrentUser, authenticated, get_db, is_uuid
from app.api.errors import ApiError
from app.db import feedback
from app.db.connection import Database

router = APIRouter(tags=["feedback"])


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rating: Literal["up", "down"]


@router.post("/messages/{message_id}/feedback")
def give_feedback(
    body: FeedbackBody,
    message_id: str,
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
        # Each write is one statement that cannot collide with itself (app/db/feedback.py), so two
        # requests at the same moment neither fail nor make a second review item.
        feedback.upsert_rating(
            db, message_id=message_id, user_id=current.user.id, rating=body.rating, stamp=stamp
        )
        if body.rating == "down":
            question = db.query_one(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' "
                "AND seq < ? ORDER BY seq DESC LIMIT 1",
                (row[1], row[2]),
            )
            feedback.add_thumbs_down_item(
                db,
                message_id=message_id,
                session_id=row[1],
                question=question[0] if question else "",
                stamp=stamp,
            )
    return {"message_id": message_id, "rating": body.rating}
