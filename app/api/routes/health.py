"""Health (spec 11.2): a public liveness endpoint that reveals nothing, and a full admin-only one.

`/live` answers the same two words to everyone and touches no database, no cache and no model:
it exists for the uptime pinger. `/health` needs an admin and reports, for each dependency, only a
word: ok, unavailable, rejected (a setting is wrong), missing_collection or not_configured. No
address, no key, no error text.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.deps import CurrentUser, get_db, require_admin, services
from app.db.connection import Database
from app.store_errors import StoreRejected, StoreUnavailable, call_database, call_vector
from app.vector.documents import COLLECTION

log = logging.getLogger("app.api")
router = APIRouter(tags=["health"])


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


def _database(db: Database) -> str:
    try:
        call_database(lambda: db.query_one("SELECT 1"))
    except StoreRejected:
        return "rejected"
    except StoreUnavailable:
        return "unavailable"
    return "ok"


def _vector(client: Any) -> str:
    if client is None:
        return "not_configured"
    try:
        exists = call_vector(lambda: client.collection_exists(COLLECTION))
    except StoreRejected:
        return "rejected"
    except StoreUnavailable:
        return "unavailable"
    return "ok" if exists else "missing_collection"


@router.get("/health")
def health(
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    svc = services(request)
    cache = "not_configured"
    if svc.cache is not None:
        cache = "ok" if svc.cache.corpus_version() is not None else "unavailable"
    checks: dict[str, Any] = {
        "database": _database(db),
        "cache": cache,
        "vector_store": _vector(svc.vector_client),
    }
    providers: dict[str, Any] = {}
    if svc.gateway is not None:
        for name, info in svc.gateway.health().items():  # names, states and counts only
            providers[name] = {"state": info["state"], "keys": info["keys"]}
    configured = [value for value in checks.values() if value != "not_configured"]
    degraded = any(value != "ok" for value in configured) or any(
        p["state"] != "healthy" for p in providers.values()
    )
    return {"status": "degraded" if degraded else "ok", "checks": checks, "providers": providers}
