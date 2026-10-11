"""The dashboards' data (spec 5.3, 5.4, 15.6, 18). Admin only, read only, NUMBERS AND FIXED WORDS:
no question, no answer, no user, no session and no key (a key appears only as a count; the pool
health is a word).

- `/admin/dashboards/deciders`: per step the share of decisions each decider made over the last
  hour and day, and the current health of each provider (healthy, cooling down, circuit open).
- `/admin/dashboards/keys`: per provider the number of keys, how many are cooling down, the state.
- `/admin/dashboards/usage`: tokens per request (p50, p95, p99), the input/output split, tokens per
  stage, billed cost and shadow cost (always separate), cost per answer. For the last hour or day,
  all users or one role.
- `/admin/dashboards/cache`: the cache hit rate and the outcomes of the window.
- `/admin/dashboards/ingestion`: per-document counts (as `/admin/documents`), the last ingest,
  delete and reindex rows of the audit log and the open failed-page review items.
- `/admin/dashboards/daily`: the last N UTC days per role (customer, staff).

Live numbers come from this process (one instance); the daily series comes from the saved rollups.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import CurrentUser, get_db, require_admin, services
from app.api.errors import ApiError
from app.db.connection import Database
from app.observability import metrics
from app.vector.status import documents_overview

router = APIRouter(prefix="/admin/dashboards", tags=["dashboards"])

WINDOWS = {"hour": metrics.HOUR, "day": metrics.DAY}
MAX_DAYS = 90
DEFAULT_DAYS = 14
INGEST_ACTIONS = ("ingest", "delete", "reindex")


def _health(gateway: Any) -> dict[str, Any]:
    """The provider health of the gateway, or an empty report when it cannot give one."""
    report = getattr(gateway, "health", None)
    if report is None:
        return {}
    try:
        return dict(report())
    except Exception:
        return {}


def _hub(request: Request) -> metrics.MetricsHub:
    observability = services(request).observability
    if observability is None:
        raise ApiError(503, "service_unavailable", "The dashboards are not available right now.")
    return observability.hub


@router.get("/deciders")
def deciders(request: Request, current: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    hub = _hub(request)
    gateway = services(request).gateway
    steps: dict[str, Any] = {}
    stats = getattr(gateway, "stats", None)
    if stats is not None:
        for step in stats.steps():
            steps[step] = {
                "hour": stats.shares(step, metrics.HOUR),
                "day": stats.shares(step, metrics.DAY),
            }
    health = _health(gateway)
    return {
        "steps": steps,
        "health": health,
        "from_requests": {
            "hour": hub.decider_shares(metrics.HOUR),
            "day": hub.decider_shares(metrics.DAY),
        },
    }


@router.get("/keys")
def keys(request: Request, current: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    _hub(request)  # the same availability rule as the other panels
    health = _health(services(request).gateway)
    return {
        "providers": [
            {
                "provider": name,
                "keys": int(info.get("keys", 0)),
                "cooling": int(info.get("cooling", 0)),
                "state": str(info.get("state", "")),
                "tiers": {str(t): str(s) for t, s in (info.get("tiers") or {}).items()},
            }
            for name, info in sorted(health.items())
        ]
    }


@router.get("/usage")
def usage(
    request: Request,
    window: Literal["hour", "day"] = Query("day"),
    role: Literal["customer", "staff"] | None = Query(None),
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    result = _hub(request).usage(WINDOWS[window], role=role)
    return {"window": window, "role": role, **result}


@router.get("/cache")
def cache(
    request: Request,
    window: Literal["hour", "day"] = Query("day"),
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    outcomes = _hub(request).outcomes(WINDOWS[window])
    return {"window": window, "cache_configured": services(request).cache is not None, **outcomes}


@router.get("/ingestion")
def ingestion(
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    client = services(request).vector_client
    documents: list[dict[str, Any]] = []
    if client is not None:
        documents = [d.as_dict() for d in documents_overview(client)]
    marks = ", ".join("?" for _ in INGEST_ACTIONS)
    with db.transaction():
        rows = db.query_all(
            "SELECT action, target, created_at FROM audit_log "
            f"WHERE action IN ({marks}) ORDER BY created_at DESC, id LIMIT 20",
            INGEST_ACTIONS,
        )
        failed = db.query_one(
            "SELECT COUNT(*) FROM review_queue WHERE kind = 'failed_ingest_page' "
            "AND status = 'open'"
        )
    return {
        "documents": documents,
        "sparse_out_of_date": sum(int(d.get("sparse_vectors_out_of_date", 0)) for d in documents),
        "held_back": sum(int(d.get("chunks_held_back", 0)) for d in documents),
        "failed_pages_open": int(failed[0]) if failed else 0,
        "recent": [{"action": r[0], "document": r[1], "at": str(r[2])} for r in rows],
        "note": "Ingestion runs on the admin machine; the audit rows are kept 30 days.",
    }


@router.get("/daily")
def daily(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    return {"days": metrics.daily_series(db, days, now=datetime.now(UTC))}
