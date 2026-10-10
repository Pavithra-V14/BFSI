"""Admin routes of Phase 10 (spec 11.4, 13.1, 14, 19, 24). Every route here needs an admin.

- Users: a page of accounts (no password hash, no email) and an on/off switch for customer and staff
  accounts (never an admin, last admin protected).
- Review queue: list, and label from a fixed list of labels.
- Documents: counts per document from Qdrant (no text), readable anywhere.
- Entities: the allow-list; reading is allowed anywhere, approving or rejecting is refused on the
  host (Render) and allowed only on the admin machine (spec 24 [P]), enforced here on the server.
- Alerts: the recent alert events as counts by name, and a banner flag for the critical ones.
- Rollout stage (shadow mode): read and change the stage; list recent shadowed answers (masked).

Every list has a default and a maximum page size.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict

from app.accounts import audit, users
from app.api.deps import CurrentUser, Page, get_db, is_uuid, page_params, require_admin, services
from app.api.errors import ApiError
from app.api.rollout import STAGES
from app.db import entities as entity_store
from app.db import review_queue
from app.db.connection import Database
from app.vector.status import documents_overview

router = APIRouter(prefix="/admin", tags=["admin"])

DOC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
USER_PAGE = page_params(default=50, maximum=200)
REVIEW_PAGE = page_params(default=20, maximum=100)
DOCUMENT_PAGE = page_params(default=50, maximum=200)
ENTITY_PAGE = page_params(default=50, maximum=200)
SHADOW_PAGE = page_params(default=20, maximum=100)
ALERT_WINDOW = 1_000

# Alert events that make the banner show (Phase 12 adds the webhook and the dashboards).
CRITICAL_ALERTS = frozenset(
    {
        "masker_load_failed",
        "masker_unavailable",
        "cache_quota_exceeded",
        "cache_out_of_memory",
        "cache_read_only",
        "vector_store_rejected",
        "budget_charge_failed",
        "graph_failed",
        "sparse_version_mismatch",
        "persist_queue_overflow",
        "persist_job_dead_lettered",
    }
)


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "Not found.")


# ---- users ----------
@router.get("/users")
def list_users(
    page: Page = Depends(USER_PAGE),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    accounts, total = users.list_users(db, limit=page.limit, offset=page.offset)
    return {
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "users": [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": u.created_at,
            }
            for u in accounts
        ],
    }


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool


_USER_REFUSALS = {
    "not_found": (404, "Not found."),
    "target_is_admin": (409, "Admin accounts cannot be changed here."),
    "not_an_admin": (403, "You do not have access to this."),
}


@router.put("/users/{user_id}/active")
def set_user_active(
    body: ActiveBody,
    user_id: str = Path(max_length=64),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    if not is_uuid(user_id):
        raise _not_found()
    try:
        state = users.set_active_by_id(
            db, actor=current.user, target_id=user_id, active=body.active, now=datetime.now(UTC)
        )
    except users.RoleChangeRefused as refusal:
        status, message = _USER_REFUSALS.get(refusal.code, (409, "That change is not allowed."))
        raise ApiError(status, refusal.code, message) from None
    return {"id": user_id, "is_active": state}


# ---- review queue ----------
@router.get("/review")
def list_review(
    status: Literal["open", "labelled", "added_to_eval", "dismissed"] | None = Query(default=None),
    kind: Literal[
        "refused", "low_confidence", "thumbs_down", "two_unclear_turns", "failed_ingest_page"
    ]
    | None = Query(default=None),
    page: Page = Depends(REVIEW_PAGE),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    items, total = review_queue.list_items(
        db,
        status=status,
        kind=kind,
        limit=page.limit,
        offset=page.offset,
        now=datetime.now(UTC),
    )
    return {
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "labels": list(review_queue.LABELS),
        "overdue_after_working_days": review_queue.OVERDUE_WORKING_DAYS,
        "items": [
            {
                "id": i.id,
                "kind": i.kind,
                "status": i.status,
                "session_id": i.session_id,
                "message_id": i.message_id,
                "doc_id": i.doc_id,
                "details": i.details,
                "label": i.label,
                "labelled_at": i.labelled_at,
                "created_at": i.created_at,
                "overdue": i.overdue,
            }
            for i in items
        ],
    }


class LabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Literal[
        "correct_refusal", "should_have_answered", "wrong_answer", "bad_question", "other"
    ]
    status: Literal["labelled", "added_to_eval", "dismissed"] = "labelled"


@router.post("/review/{item_id}/label")
def label_review_item(
    body: LabelBody,
    item_id: str = Path(max_length=64),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    if not is_uuid(item_id):
        raise _not_found()
    now = datetime.now(UTC)
    with db.transaction(write=True):
        found = review_queue.label_item(
            db,
            item_id=item_id,
            label=body.label,
            status=body.status,
            user_id=current.user.id,
            now=now,
        )
        if not found:
            raise _not_found()
        audit.write_audit(
            db,
            actor=current.user.id,
            action="review_label",
            target=item_id,
            details={"label": body.label, "status": body.status},
            now=now,
        )
    return {"id": item_id, "label": body.label, "status": body.status}


# ---- documents and entities (counts and the allow-list; no document text) ----------
@router.get("/documents")
def list_documents(
    request: Request,
    page: Page = Depends(DOCUMENT_PAGE),
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    client = services(request).vector_client
    if client is None:
        raise ApiError(503, "service_unavailable", "The document store is not available.")
    everything = documents_overview(client)
    window = everything[page.offset : page.offset + page.limit]
    return {
        "total": len(everything),
        "limit": page.limit,
        "offset": page.offset,
        "documents": [d.as_dict() for d in window],
    }


@router.get("/documents/{doc_id}")
def document_status(
    request: Request,
    doc_id: str = Path(max_length=64, pattern=DOC_ID_PATTERN),
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    client = services(request).vector_client
    if client is None:
        raise ApiError(503, "service_unavailable", "The document store is not available.")
    found = documents_overview(client, doc_id=doc_id)
    if not found:
        raise _not_found()
    return found[0].as_dict()


@router.get("/entities")
def list_entities(
    doc_id: str | None = Query(default=None, max_length=64, pattern=DOC_ID_PATTERN),
    status: Literal["pending", "approved", "rejected"] | None = Query(default="pending"),
    page: Page = Depends(ENTITY_PAGE),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    rows, total = entity_store.list_entities(
        db, doc_id=doc_id, status=status, limit=page.limit, offset=page.offset
    )
    return {
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "entities": [
            {
                "id": r.id,
                "doc_id": r.doc_id,
                "text": r.text,
                "entity_type": r.entity_type,
                "status": r.status,
                "approved_at": r.approved_at,
                "access_role": r.access_role,
            }
            for r in rows
        ],
    }


class EntityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["approved", "rejected"]


@router.put("/entities/{entity_id}")
def review_entity(
    body: EntityBody,
    request: Request,
    entity_id: str = Path(max_length=64),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    if services(request).on_render:  # enforced on the server, whatever the client shows
        raise ApiError(
            403,
            "admin_machine_only",
            "Entity review is done on the admin machine, not on the hosted service.",
        )
    if not is_uuid(entity_id):
        raise _not_found()
    now = datetime.now(UTC)
    with db.transaction(write=True):
        found = entity_store.set_status(
            db, entity_id=entity_id, status=body.status, user_id=current.user.id, now=now
        )
        if not found:
            raise _not_found()
        audit.write_audit(
            db,
            actor=current.user.id,
            action="entity_review",
            target=entity_id,
            details={"status": body.status, "via": "api"},
            now=now,
        )
    return {"id": entity_id, "status": body.status}


# ---- alerts banner ----------
@router.get("/alerts")
def alerts(request: Request, current: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    """The recent alert events as counts by name (the last ALERT_WINDOW events; Phase 12 adds the
    webhook, persistence and the dashboards). Names and counts only."""
    events = list(services(request).events)[-ALERT_WINDOW:]
    counts: dict[str, int] = {}
    for name, _fields in events:
        counts[name] = counts.get(name, 0) + 1
    critical = sorted(name for name in counts if name in CRITICAL_ALERTS)
    return {
        "banner": bool(critical),
        "critical": critical,
        "events": [{"name": n, "count": c} for n, c in sorted(counts.items())],
        "window": len(events),
    }


# ---- rollout stage (shadow mode) ----------
class StageBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["shadow", "staff_beta", "open"]


@router.get("/shadow")
def get_stage(
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    return {"stage": services(request).rollout.stage(db), "stages": list(STAGES)}


@router.put("/shadow")
def set_stage(
    body: StageBody,
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    previous = services(request).rollout.set_stage(
        db, stage=body.stage, actor_id=current.user.id, now=datetime.now(UTC)
    )
    return {"stage": body.stage, "previous_stage": previous}


@router.get("/shadow/answers")
def shadow_answers(
    request: Request,
    page: Page = Depends(SHADOW_PAGE),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Recent answers that were generated and stored but hidden from their user (masked text only;
    the user appears only as a keyed hash)."""
    key = services(request).settings.audit_hash_key
    with db.transaction():
        total = db.query_one(
            "SELECT COUNT(*) FROM messages WHERE role = 'assistant' AND shadowed = ?", (True,)
        )
        rows = db.query_all(
            "SELECT m.id, m.session_id, m.created_at, m.content, m.citations, m.grounded, "
            "s.user_id, (SELECT q.content FROM messages q WHERE q.session_id = m.session_id "
            "AND q.role = 'user' AND q.seq < m.seq ORDER BY q.seq DESC LIMIT 1) "
            "FROM messages m JOIN sessions s ON s.id = m.session_id "
            "WHERE m.role = 'assistant' AND m.shadowed = ? "
            "ORDER BY m.created_at DESC, m.id LIMIT ? OFFSET ?",
            (True, page.limit, page.offset),
        )
    items = []
    for row in rows:
        try:
            citations = json.loads(row[4])
        except (TypeError, ValueError):
            citations = []
        items.append(
            {
                "message_id": row[0],
                "session_id": row[1],
                "created_at": str(row[2]),
                "question": row[7],
                "answer": row[3],
                "citations": citations if isinstance(citations, list) else [],
                "grounded": None if row[5] is None else bool(row[5]),
                "user": audit.keyed_hash(key, "shadow-user", row[6]),
            }
        )
    return {
        "total": int(total[0]) if total else 0,
        "limit": page.limit,
        "offset": page.offset,
        "answers": items,
    }
