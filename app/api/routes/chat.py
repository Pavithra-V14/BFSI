"""The chat endpoint (spec 4, 8.3, 11.1, 19): one question in, one answer out.

`POST /chat {message, session_id?}` runs the pipeline for the signed-in user. Without a session id
a new session starts; with one the chat resumes (a session that is missing, someone else's or in a
bad format is the same 404).

Rules kept here:
- One count per chat, taken at the route's entry (`open_user_context`), so a refusal before the
  pipeline (a bad session id, a busy session, a service that is starting up, no vector store) is
  counted too. The pipeline is told not to count again (`rate_counted`); it still checks the
  daily token budget.
- The request owns its database connection. If a transcript write that timed out is still running
  when the answer is ready, or the request is cancelled, the connection is closed only when that
  write has ended.
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

# [T] The 100-token guardrail (spec 9.1) is what limits a question; this only bounds what is
# read from the wire. Even 2,000 characters that JSON escapes to 12 bytes each stay under the
# request body cap (app/api/security.py), so the cap never refuses legitimate text.
MAX_MESSAGE_CHARACTERS = 2_000
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
    session_id: str | None = None  # its format is checked by `is_uuid`: a bad one is the 404


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


def _close_late(task: asyncio.Future) -> None:
    """A request was cancelled while its connection was being opened: close it when it arrives."""
    if task.cancelled() or task.exception() is not None:
        return
    task.result()[1].close()


@router.post("/chat")
async def chat(
    body: ChatBody, request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    svc = services(request)
    opening = asyncio.ensure_future(asyncio.to_thread(open_user_context, svc, authorization))
    try:
        current, db = await asyncio.shield(opening)
    except asyncio.CancelledError:
        opening.add_done_callback(_close_late)  # the thread goes on; its connection is closed then
        raise
    result = None
    handed_over = False  # True once the pipeline owns closing the connection (close_db)
    try:
        if svc.pipeline is None:
            raise ApiError(503, "service_unavailable", "The service is not available right now.")
        session_id = body.session_id
        if session_id is not None and not is_uuid(session_id):
            raise ApiError(404, "not_found", "Not found.")
        stage = await asyncio.to_thread(svc.rollout.stage, db)
        visible = answers_visible(stage, current.user.role)
        handed_over = True
        result = await svc.pipeline.answer(
            PipelineRequest(
                user_id=current.user.id,
                role=current.user.role,
                query=body.message,
                session_id=session_id,
                shadowed=not visible,
                rate_counted=True,
            ),
            db,
            close_db=db.close,
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
        elif not handed_over:
            db.close()
