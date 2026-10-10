"""Session list, resume and delete-my-chats (spec 8.3, 8.4, 8.5, 13.6).

Rules:
- Every query is for the signed-in user only. A session that is someone else's, missing, or has a
  malformed id gives the same 404 (the id format is checked before any lookup).
- Every list has a default and a maximum page size; a larger value is refused, not clipped.
- On a resume, an assistant answer that was generated while its user was not allowed to see
  answers (`shadowed`) is replaced by the fixed notice for a non-admin, also after the stage has
  opened; an answer whose source has been removed shows a notice instead of its text. Citations
  of such messages are not returned. Text is plain; nothing is rendered as markup.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path, Response

from app.api.deps import CurrentUser, Page, authenticated, get_db, is_uuid, page_params
from app.api.errors import ApiError
from app.api.rollout import SHADOW_NOTICE, WITHDRAWN_NOTICE
from app.db.connection import Database
from app.memory import store

router = APIRouter(tags=["sessions"])

SESSION_PAGE = page_params(default=20, maximum=100)
MESSAGE_PAGE = page_params(default=50, maximum=200)


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Not found.")


def _check_id(session_id: str) -> str:
    if not is_uuid(session_id):
        raise _not_found()
    return session_id


def _shown(message: store.StoredMessage, *, is_admin: bool) -> dict[str, Any]:
    """One message as the user may see it."""
    text, citations, grounded = message.content, list(message.citations), message.grounded
    notice = None
    if message.role == "assistant" and message.shadowed and not is_admin:
        text, citations, grounded, notice = SHADOW_NOTICE, [], None, "shadowed"
    elif message.role == "assistant" and message.source_removed:
        text, citations, notice = WITHDRAWN_NOTICE, [], "source_removed"
    return {
        "id": message.id,
        "seq": message.seq,
        "role": message.role,
        "text": text,
        "citations": citations,
        "grounded": grounded,
        "source_removed": message.source_removed,
        "notice": notice,
        "created_at": message.created_at,
    }


@router.get("/sessions")
def list_my_sessions(
    page: Page = Depends(SESSION_PAGE),
    current: CurrentUser = Depends(authenticated),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    sessions, total = store.list_sessions_page(
        db, current.user.id, limit=page.limit, offset=page.offset
    )
    return {
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "last_active_at": s.last_active_at,
                "status": s.status,
            }
            for s in sessions
        ],
    }


@router.get("/sessions/{session_id}")
def resume_session(
    session_id: str = Path(max_length=64),
    page: Page = Depends(MESSAGE_PAGE),
    current: CurrentUser = Depends(authenticated),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    _check_id(session_id)
    try:
        info, messages, total = store.transcript(
            db, current.user.id, session_id, limit=page.limit, offset=page.offset
        )
    except store.SessionNotFound:
        raise _not_found() from None
    is_admin = current.user.role == "admin"
    return {
        "id": info.id,
        "title": info.title,
        "status": info.status,
        "created_at": info.created_at,
        "last_active_at": info.last_active_at,
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "messages": [_shown(m, is_admin=is_admin) for m in messages],
    }


@router.delete("/sessions/{session_id}", status_code=204)
def delete_my_session(
    session_id: str = Path(max_length=64),
    current: CurrentUser = Depends(authenticated),
    db: Database = Depends(get_db),
) -> Response:
    _check_id(session_id)
    if not store.delete_own_session(db, current.user.id, session_id):
        raise _not_found()
    return Response(status_code=204)


@router.delete("/sessions")
def delete_all_my_sessions(
    current: CurrentUser = Depends(authenticated), db: Database = Depends(get_db)
) -> dict[str, int]:
    """Delete every chat of the signed-in user (spec 13.6)."""
    deleted = 0
    while True:
        batch, _ = store.list_sessions_page(db, current.user.id, limit=100, offset=0)
        if not batch:
            return {"deleted": deleted}
        progress = 0
        for item in batch:
            if store.delete_own_session(db, current.user.id, item.id):
                progress += 1
        if progress == 0:  # nothing could be deleted: do not loop for ever
            return {"deleted": deleted}
        deleted += progress
