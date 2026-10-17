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

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.accounts import audit, users
from app.api.commands import run_ingest_command
from app.api.deps import CurrentUser, Page, get_db, is_uuid, page_params, require_admin, services
from app.api.errors import ApiError
from app.api.rollout import STAGES, WITHDRAWN_NOTICE
from app.db import entities as entity_store
from app.db import review_queue
from app.db.connection import Database
from app.observability.reasons import explain, reason_codes
from app.vector.status import documents_overview

router = APIRouter(prefix="/admin", tags=["admin"])

DOC_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
USER_PAGE = page_params(default=50, maximum=200)
REVIEW_PAGE = page_params(default=20, maximum=100)
DOCUMENT_PAGE = page_params(default=50, maximum=200)
ENTITY_PAGE = page_params(default=50, maximum=200)
SHADOW_PAGE = page_params(default=20, maximum=100)
ALERT_WINDOW = 1_000
ALERT_EXPIRY_SECONDS = 24 * 3600  # an in-process event leaves the banner after a day

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
    user_id: str,
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


def _masked_again(request: Request, candidate: Any) -> Any:
    """The candidate with its question masked once more by the application's own masker (the
    review item holds the already-masked transcript text, so this changes nothing in the normal
    case; it is the second lock that keeps an identifier out of the evaluation database). None
    when the masker exists but is not ready. A deployment with no chat pipeline has no masker;
    the text is then the stored, masked copy."""
    import dataclasses

    pipeline = getattr(services(request), "pipeline", None)
    if pipeline is None:
        return candidate
    holder = pipeline.deps.masker
    if not getattr(holder, "ready", False):
        return None
    from app.guardrails.allowlist import AllowList
    from app.guardrails.scan import mask_for_storage

    turns = tuple(
        mask_for_storage(
            text, masker=holder.masker, allow=AllowList(), doc_ids=set(candidate.source_docs)
        ).text
        for text in candidate.turns
    )
    return dataclasses.replace(candidate, turns=turns)


def _add_evaluation_candidate(
    request: Request, db: Database, item_id: str, label: str, now: datetime
) -> str:
    """One click, "add to evaluation set" (spec 11.4): the masked question of the item becomes a
    CANDIDATE in the evaluation database. Returns what happened, as a word:
    added | already_added | not_added | no_evaluation_database | failed. Never raises."""
    access = getattr(services(request), "evaluation", None)
    environments = access.environments() if access is not None else []
    if not environments:
        return "no_evaluation_database"
    try:
        from app.eval import store as eval_store
        from app.eval.candidates import candidate_from_review

        item = review_queue.get_item(db, item_id, now=now)
        candidate = candidate_from_review(item, label) if item is not None else None
        if candidate is None:
            return "not_added"
        candidate = _masked_again(request, candidate)
        if candidate is None:
            return "failed"  # the masker is not ready: nothing is written (fail closed)
        target = "production" if "production" in environments else "local"
        eval_db = access.database(target)
        if eval_db is None:
            return "no_evaluation_database"
        try:
            if eval_store.candidate_exists(eval_db, item_id):
                return "already_added"
            eval_store.upsert_case(eval_db, candidate, now=now)
        finally:
            eval_db.close()
        return "added"
    except Exception:
        return "failed"


@router.post("/review/{item_id}/label")
def label_review_item(
    body: LabelBody,
    request: Request,
    item_id: str,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    if not is_uuid(item_id):
        raise _not_found()
    now = datetime.now(UTC)
    status = body.status
    outcome: str | None = None
    if status == "added_to_eval":
        # The candidate is made FIRST, and the item is marked "added to evaluation" only when it
        # really is in the evaluation set (added, or already there). Any other outcome leaves the
        # item merely "labelled", so the queue never says "added" about something that is not.
        outcome = _add_evaluation_candidate(request, db, item_id, body.label, now)
        if outcome not in ("added", "already_added"):
            status = "labelled"
    with db.transaction(write=True):
        found = review_queue.label_item(
            db,
            item_id=item_id,
            label=body.label,
            status=status,
            user_id=current.user.id,
            now=now,
        )
        if not found:
            raise _not_found()
        details = {"label": body.label, "status": status}
        if outcome is not None:
            details["evaluation_candidate"] = outcome
        audit.write_audit(
            db,
            actor=current.user.id,
            action="review_label",
            target=item_id,
            details=details,
            now=now,
        )
    reply = {"id": item_id, "label": body.label, "status": status}
    if outcome is not None:
        reply["evaluation_candidate"] = outcome
    return reply


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


class DeleteDocumentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_id: str = Field(min_length=1, max_length=64)  # the document id, typed again
    reason: Literal["outdated", "incorrect", "compliance", "replaced", "other"] = "other"


@router.post("/documents/{doc_id}/delete")
async def delete_document(
    body: DeleteDocumentBody,
    request: Request,
    doc_id: str = Path(max_length=64, pattern=DOC_ID_PATTERN),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Delete a document everywhere (Change round 1, step 4). Only on the admin machine: the same
    command as `python -m app.ingest delete`, started here as its own process with this machine's
    settings, after the document id has been typed again. The command keeps all its safeguards
    (withdrawn record, audit row, chats flagged, originals moved) and may be repeated."""
    svc = services(request)
    if svc.on_render:
        raise ApiError(
            409, "admin_machine_only", "Documents are deleted on the admin machine only."
        )
    if body.confirm_id.strip() != doc_id:
        raise ApiError(409, "confirmation_mismatch", "The document id typed does not match.")
    client = svc.vector_client
    if client is None:
        raise ApiError(503, "service_unavailable", "The document store is not available.")
    if not documents_overview(client, doc_id=doc_id):
        raise _not_found()
    with db.transaction(write=True):
        audit.write_audit(
            db,
            actor=current.user.id,
            action="document_delete_request",
            target=doc_id,
            details={"reason": body.reason},
            now=datetime.now(UTC),
        )
    runner = svc.ingest_runner or run_ingest_command
    result = await asyncio.to_thread(
        runner, ["delete", "--doc-id", doc_id, "--reason", body.reason, "--confirm-id", doc_id]
    )
    ok = result.code == 0
    if not ok:
        svc.emit("document_delete_failed", code=result.code)
    return {
        "doc_id": doc_id,
        "deleted": ok,
        "timed_out": bool(getattr(result, "timed_out", False)),
        "lines": [str(line) for line in result.lines],
    }


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
    # A one-word person entry (a surname alone) is approved only when this is true: the same
    # rule as the command-line review's "tick it".
    confirm_single_word: bool = False


@router.put("/entities/{entity_id}")
def review_entity(
    body: EntityBody,
    request: Request,
    entity_id: str,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    svc = services(request)
    if svc.on_render:  # enforced on the server, whatever the client shows
        raise ApiError(
            403,
            "admin_machine_only",
            "Entity review is done on the admin machine, not on the hosted service.",
        )
    if not svc.entity_review_enabled:  # opt-in: ENTITY_REVIEW_ENABLED=true on the admin machine
        raise ApiError(403, "entity_review_disabled", "Entity review is not switched on here.")
    if not is_uuid(entity_id):
        raise _not_found()
    now = datetime.now(UTC)
    with db.transaction(write=True):
        entry = entity_store.get_entity(db, entity_id)
        if entry is None:
            raise _not_found()
        if (
            body.status == "approved"
            and entity_store.is_single_word_person(*entry)
            and not body.confirm_single_word
        ):
            raise ApiError(
                409,
                "single_word_needs_confirmation",
                "A one-word name is approved only when you confirm it.",
            )
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
            details={
                "status": body.status,
                "via": "api",
                "single_word_confirmed": bool(body.confirm_single_word),
            },
            now=now,
        )
    return {"id": entity_id, "status": body.status}


# ---- alerts banner ----------
@router.get("/alerts")
def alerts(request: Request, current: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    """The recent alert events as counts by name (the last ALERT_WINDOW events; Phase 12 adds the
    webhook, persistence and the dashboards). Names and counts only."""
    svc = services(request)
    # Only events of the last 24 hours: an old one does not keep the banner up for ever.
    cutoff = svc.clock() - ALERT_EXPIRY_SECONDS
    paired = list(zip(svc.events, svc.event_times, strict=False))[-ALERT_WINDOW:]
    events = [item for item, at in paired if at >= cutoff]
    counts: dict[str, int] = {}
    seen_fields: dict[str, list[dict[str, Any]]] = {}
    for name, fields in events:
        counts[name] = counts.get(name, 0) + 1
        seen_fields.setdefault(name, []).append(fields)
    critical = {name for name in counts if name in CRITICAL_ALERTS}
    # The persisted alerts of the last 24 hours (they survive a restart): name, severity, count and
    # time only. A critical one keeps the banner up.
    recent = svc.observability.alerts.recent() if svc.observability is not None else []
    critical |= {a["name"] for a in recent if a["severity"] == "critical"}
    warnings = {a["name"] for a in recent if a["severity"] == "warning"} - critical
    # A fixed plain-words reason for every name on the screen (never an error message), and, for the
    # running service's events, how often each reason CODE (a class name or fixed code) was seen.
    names = set(counts) | critical | warnings | {a["name"] for a in recent}
    return {
        "banner": bool(critical),
        "critical": sorted(critical),
        "warnings": sorted(warnings),
        "events": [
            {"name": n, "count": c, "codes": reason_codes(seen_fields.get(n, []))}
            for n, c in sorted(counts.items())
        ],
        "window": len(events),
        "recent": recent,
        "reasons": {name: explain(name) for name in sorted(names)},
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
            "AND q.role = 'user' AND q.seq < m.seq ORDER BY q.seq DESC LIMIT 1), "
            "m.source_removed, m.fixed_reply "
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
        removed = bool(row[8])
        if removed:  # the document was withdrawn: the notice, never its text or citations
            answer, citations = WITHDRAWN_NOTICE, []
        else:
            answer = row[3]
            citations = citations if isinstance(citations, list) else []
        items.append(
            {
                "message_id": row[0],
                "session_id": row[1],
                "created_at": str(row[2]),
                "question": row[7],
                "answer": answer,
                "citations": citations,
                "grounded": None if row[5] is None else bool(row[5]),
                "kind": "fixed_reply" if row[9] else "answer",
                "source_removed": removed,
                "user": audit.keyed_hash(key, "shadow-user", row[6]),
            }
        )
    return {
        "total": int(total[0]) if total else 0,
        "limit": page.limit,
        "offset": page.offset,
        "answers": items,
    }
