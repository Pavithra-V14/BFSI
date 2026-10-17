"""The purge screen's routes (Change round 1, step 6). Admin only, and only on the admin machine.

`GET /admin/maintenance/purge-plan` counts what a purge would erase (nothing is read of any text).
`POST /admin/maintenance/purge` erases it, after the phrase is typed: the route writes its own audit
row, then runs `python -m app.maintenance purge` as its own process with this machine's settings
(the stores' keys stay in that process), and waits for it. Both are refused on Render. The command
keeps its own safeguards and writes its own audit row; see `app.maintenance.purge` for what is
erased and what is kept.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.accounts import audit
from app.api.commands import run_maintenance_command
from app.api.deps import CurrentUser, get_db, require_admin, services
from app.api.errors import ApiError
from app.db.connection import Database
from app.maintenance import purge
from app.vector.documents import COLLECTION

router = APIRouter(prefix="/admin/maintenance", tags=["admin"])


def _admin_machine_only(request: Request) -> Any:
    svc = services(request)
    if svc.on_render:
        raise ApiError(409, "admin_machine_only", "Data is erased on the admin machine only.")
    return svc


@router.get("/purge-plan")
def purge_plan(
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    svc = _admin_machine_only(request)
    if svc.vector_client is None:
        raise ApiError(503, "service_unavailable", "The document store is not available.")
    return {
        "counts": purge.plan(db, svc.vector_client, COLLECTION),
        "phrase": purge.PHRASE,
        "warning": purge.WARNING,
        "kept": list(purge.KEPT),
        "not_touched": list(purge.NOT_TOUCHED),
    }


class PurgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phrase: str = Field(min_length=1, max_length=60)  # the phrase, typed in full


@router.post("/purge")
async def purge_all(
    body: PurgeBody,
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    svc = _admin_machine_only(request)
    if body.phrase.strip() != purge.PHRASE:
        raise ApiError(409, "confirmation_mismatch", "The phrase typed does not match.")
    with db.transaction(write=True):
        audit.write_audit(
            db,
            actor=current.user.id,
            action="purge_all_request",
            target="database",
            details={},
            now=datetime.now(UTC),
        )
    runner = svc.maintenance_runner or run_maintenance_command
    result = await asyncio.to_thread(
        runner,
        ["purge", "--confirm-phrase", purge.PHRASE, "--actor", f"admin:{current.user.id}"],
    )
    ok = result.code == 0
    if not ok:
        svc.emit("purge_failed", code=result.code)
    return {
        "erased": ok,
        "timed_out": bool(getattr(result, "timed_out", False)),
        "lines": [str(line) for line in result.lines],
    }
