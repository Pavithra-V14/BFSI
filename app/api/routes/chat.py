"""The chat endpoint (spec 4, 8.3, 11.1, 19): one question in, one answer out.

`POST /chat {message, session_id?}` runs the pipeline for the signed-in user. Without a session id
a new session starts; with one the chat resumes (a session that is missing, someone else's or in a
bad format is the same 404).

Rules kept here:
- One count per chat: the route is not metered by the API's per-user limit (the pipeline's first
  stage counts it, once, together with the daily token budget).
- The request owns its database connection. If a transcript write that timed out is still running
  when the answer is ready, the connection is closed only when that write has ended.
- The rollout stage (shadow mode): a user who may not see answers gets the fixed notice with no
  answer text, citations, scores or reason; their turn was still generated and stored (flagged
  `shadowed`) for evaluation. The notice is the same whatever the pipeline decided.
- Responses are plain text and carry only the fields below. The correlation id is not returned
  (it is internal).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import is_uuid, open_user_context, services
from app.api.errors import ApiError
from app.api.rollout import SHADOW_NOTICE, answers_visible
from app.pipeline.state import Request as PipelineRequest

log = logging.getLogger("app.api")
router = APIRouter(tags=["chat"])

MAX_MESSAGE_CHARACTERS = 8_000
RETRY_AFTER_STARTING_UP = 5

# Pipeline result kinds that are not an answer: they become an error response.
_ERRORS = {
    "starting_up": (503, "starting_up"),
    "unavailable": (503, "service_unavailable"),
    "busy": (409, "busy"),
    "rate_limited": (429, "too_many_requests"),
    "over_budget": (429, "over_budget"),
    "session_not_found": (404, "not_found"),
}


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)
    session_id: str | None = Field(default=None, max_length=64)


def _error_for(result: Any) -> ApiError | None:
    mapped = _ERRORS.get(result.kind)
    if mapped is None:
        return None
    status, code = mapped
    headers: dict[str, str] = {}
    if result.kind == "rate_limited":
        headers["Retry-After"] = str(result.retry_after or 60)
    elif result.kind == "starting_up":
        headers["Retry-After"] = str(RETRY_AFTER_STARTING_UP)
    elif result.kind in ("unavailable", "busy"):
        headers["Retry-After"] = "2"
    message = "Not found." if result.kind == "session_not_found" else result.text
    return ApiError(status, code, message, headers=headers)


def _citations(result: Any) -> list[dict[str, Any]]:
    shown = ("doc_id", "source", "clause", "page", "page_end", "effective_date")
    return [{k: c.get(k) for k in shown if k in c} for c in result.citations if isinstance(c, dict)]


def _body(result: Any) -> dict[str, Any]:
    return {
        "answer_visible": True,
        "kind": result.kind,
        "text": result.text,
        "reason": result.reason,
        "citations": _citations(result),
        "warnings": list(result.warnings),
        "confidence": result.confidence,
        "degraded": list(result.degraded),
        "disclaimer": bool(result.disclaimer),
        "session_id": result.session_id,
    }


def _close_when_idle(db: Any, result: Any) -> None:
    """Close the request's connection now, or when the write that is still running has ended."""
    hook = getattr(result, "on_db_idle", None)
    if getattr(result, "db_busy", False) and callable(hook):
        hook(db.close)
    else:
        db.close()


@router.post("/chat")
async def chat(
    body: ChatBody, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    svc = services(request)
    current, db = await asyncio.to_thread(open_user_context, svc, authorization)
    result = None
    try:
        if svc.pipeline is None:
            raise ApiError(503, "service_unavailable", "The service is not available right now.")
        session_id = body.session_id
        if session_id is not None and not is_uuid(session_id):
            raise ApiError(404, "not_found", "Not found.")
        stage = await asyncio.to_thread(svc.rollout.stage, db)
        visible = answers_visible(stage, current.user.role)
        result = await svc.pipeline.answer(
            PipelineRequest(
                user_id=current.user.id,
                role=current.user.role,
                query=body.message,
                session_id=session_id,
                shadowed=not visible,
            ),
            db,
        )
        error = _error_for(result)
        if error is not None:
            raise error
        if not visible:
            return {
                "answer_visible": False,
                "kind": "shadow_notice",
                "text": SHADOW_NOTICE,
                "session_id": result.session_id,
            }
        return _body(result)
    finally:
        if result is not None:
            _close_when_idle(db, result)
        else:
            db.close()
