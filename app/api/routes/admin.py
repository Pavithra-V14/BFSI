"""Admin-only routes (spec 13.1: staff roles are assigned only by an admin).

Only the role change is here. User lists and the rest of user management are Phase 10.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from app.accounts import users
from app.api import diagnostics
from app.api.deps import (
    CurrentUser,
    client_address,
    forwarded_entries,
    get_db,
    require_admin,
    services,
)
from app.api.errors import ApiError
from app.db.connection import Database

router = APIRouter(prefix="/admin", tags=["admin"])

_REFUSALS = {
    "not_found": (404, "Not found."),
    "own_role": (409, "You cannot change your own role."),
    "last_admin": (409, "There must always be at least one active admin."),
    "not_an_admin": (403, "You do not have access to this."),
    "target_is_admin": (409, "Admin accounts cannot be changed here."),
}
USER_ID_PATTERN = re.compile(r"[A-Za-z0-9-]{1,64}")


class RoleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["customer", "staff"]  # admins are made only by the owner's command


@router.put("/users/{user_id}/role")
def change_role(
    body: RoleBody,
    user_id: str,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, str]:
    if not USER_ID_PATTERN.fullmatch(user_id):
        raise ApiError(404, "not_found", "Not found.")
    try:
        old, new = users.set_role(
            db, actor=current.user, target_id=user_id, new_role=body.role, now=datetime.now(UTC)
        )
    except users.RoleChangeRefused as refusal:
        status, message = _REFUSALS.get(refusal.code, (409, "That change is not allowed."))
        raise ApiError(status, refusal.code, message) from None
    return {"id": user_id, "role": new, "previous_role": old}


@router.get("/diagnostics")
def diagnostics_report(
    request: Request, current: CurrentUser = Depends(require_admin)
) -> dict[str, object]:
    """For the owner's first deployment (Run 28): what this request looks like from the API and
    what the process holds. The admin's OWN address only, no other user's. Reads no database."""
    svc = services(request)
    hops = svc.settings.trusted_proxy_hops
    address, source = client_address(request, hops)
    return {
        "forwarded_entries": len(forwarded_entries(request)),
        "trusted_proxy_hops": hops,
        "client_address_used": address,
        "address_source": source,
        # what the address WOULD be with one trusted proxy: the check of the first deploy
        # (docs/DEPLOY.md 2.6) compares it with the owner's real public address while the
        # setting is still 0
        "address_at_one_hop": client_address(request, 1)[0],
        "source_at_one_hop": client_address(request, 1)[1],
        "socket_address": request.client.host if request.client else "unknown",
        "memory_mb": diagnostics.memory_now_mb(),
        "peak_memory_mb": diagnostics.memory_peak_mb(),
        "hash_concurrency": svc.settings.password_hash_concurrency,
        "on_render": svc.on_render,
    }
