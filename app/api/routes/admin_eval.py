"""Evaluation routes (spec 15.4, 15.5, 24), admin only.

Reading the dashboard is available wherever an evaluation database is configured: the production
view on Render, the local (test) view on the admin machine. Starting a run is available only on
the admin machine, and only in two steps: an estimate (no model call; the numbers and a
confirmation code) and a start that must carry that code. A run itself is never executed inside
this process: it is started as its own command (`python -m app.eval run ...`) that reads the
evaluation keys from the admin machine's own shell, so the keys never enter the API.

No answer text and no key is returned: scores, counts, ids of cases, names of kinds.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.accounts import audit
from app.api.deps import CurrentUser, get_db, require_admin, services
from app.api.errors import ApiError
from app.db.connection import Database
from app.eval import constants as c
from app.eval import dashboard, store
from app.eval.access import EvalAccess
from app.eval.guard import NotConfirmed, check_confirmation, confirmation_expired

router = APIRouter(prefix="/admin/evaluation", tags=["admin"])

RUN_ID = r"^[0-9a-f]{32}$"
Environment = Literal["local", "production"]
ENVIRONMENT = Query("local")


def _access(request: Request) -> EvalAccess:
    access = getattr(services(request), "evaluation", None)
    if access is None:
        raise ApiError(503, "service_unavailable", "No evaluation database is configured.")
    return access


def _database(access: EvalAccess, environment: str) -> Database:
    db = access.database(environment)
    if db is None:
        raise ApiError(404, "not_found", "That evaluation view is not available here.")
    return db


@router.get("/environments")
def environments(request: Request, current: CurrentUser = Depends(require_admin)) -> dict[str, Any]:
    svc = services(request)
    access = getattr(svc, "evaluation", None)
    available = access.environments() if access is not None else []
    return {
        "environments": available,
        "can_run": bool(access is not None and not svc.on_render),
        "on_render": bool(svc.on_render),
    }


@router.get("/overview")
def overview(
    request: Request,
    environment: Environment = ENVIRONMENT,
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    db = _database(_access(request), environment)
    try:
        return dashboard.overview(db, environment)
    finally:
        db.close()


@router.get("/runs/{run_id}")
def run_detail(
    request: Request,
    run_id: str = Path(pattern=RUN_ID),
    environment: Environment = ENVIRONMENT,
    current: CurrentUser = Depends(require_admin),
) -> dict[str, Any]:
    db = _database(_access(request), environment)
    try:
        detail = dashboard.run_detail(db, run_id)
    finally:
        db.close()
    if detail is None:
        raise ApiError(404, "not_found", "No such run.")
    return detail


class EstimateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_set: Literal["active", "full", "variance"] = Field(alias="set", default="active")
    judged: bool = True


def _refuse_on_render(request: Request) -> None:
    if services(request).on_render:
        raise ApiError(
            409, "admin_machine_only", "Evaluation runs start only on the admin machine."
        )


@router.post("/estimates")
def make_estimate(
    body: EstimateBody,
    request: Request,
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """An estimate and its confirmation code. No model call is made."""
    _refuse_on_render(request)
    access = _access(request)
    if access.local_path is None:
        raise ApiError(503, "service_unavailable", "No local evaluation database is set up.")
    from app.db.migrate import run as migrate
    from app.eval.cases import load_cases, set_problems
    from app.eval.estimate import describe
    from app.eval.runner import prepare_run
    from app.gateway import GatewayConfig
    from app.gateway.keys import ConfigError

    try:
        config = GatewayConfig.for_evaluation(access.env if access.env is not None else os.environ)
    except ConfigError:
        raise ApiError(
            503, "evaluation_keys_missing", "The evaluation keys are not set on this machine."
        ) from None
    access.local_path.parent.mkdir(parents=True, exist_ok=True)
    migrate(store.sqlite_url(access.local_path), store.MIGRATIONS, log=lambda line: None)
    version, cases_all = load_cases()
    if set_problems(cases_all):
        raise ApiError(
            409, "invalid_set", "The evaluation set has problems; run the check command."
        )
    local = store.open_db(store.sqlite_url(access.local_path))
    try:
        store.sync_cases(local, cases_all)
        # only the main set: the red-team cases share this table under their own dataset
        # version and are never part of a main run (Phase 16, docs/TARGETS.md)
        cases = [
            x
            for x in store.list_cases(local, status="set", active_only=body.run_set != "full")
            if x.dataset_version == version
        ]
        run_id, estimate, code = prepare_run(
            local,
            cases=cases,
            kind=body.run_set,
            environment="local",
            dataset_version=version,
            config=config,
            created_by=f"admin:{current.user.username}"[:60],
            judged=body.judged,
        )
    finally:
        local.close()
    with db.transaction(write=True):
        audit.write_audit(
            db,
            actor=current.user.id,
            action="evaluation_run",
            target=run_id,
            details={"step": "estimate", "set": body.run_set, "cases": estimate["cases"]},
            now=datetime.now(UTC),
        )
    return {
        "run_id": run_id,
        "estimate": {k: v for k, v in estimate.items() if k != "batches"},
        "batches": len(estimate["batches"]),
        "lines": describe(estimate),
        "code": code,
    }


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_code: str = Field(min_length=4, max_length=16)


@router.post("/runs/{run_id}/start", status_code=202)
def start(
    body: StartBody,
    request: Request,
    run_id: str = Path(pattern=RUN_ID),
    current: CurrentUser = Depends(require_admin),
    db: Database = Depends(get_db),
) -> dict[str, Any]:
    """Start the run in its own process. The code must be the one of this run's estimate."""
    _refuse_on_render(request)
    access = _access(request)
    local = _database(access, "local")
    try:
        run = store.get_run(local, run_id)
    finally:
        local.close()
    if run is None:
        raise ApiError(404, "not_found", "No such run.")
    if run["status"] in (c.RUN_FINISHED, c.RUN_ABORTED):
        raise ApiError(409, "run_not_startable", "This run has already ended.")
    try:
        check_confirmation(
            body.confirm_code,
            run["estimate"],
            kind=run["kind"],
            dataset_version=run["dataset_version"],
        )
    except NotConfirmed:
        raise ApiError(
            409, "confirmation_mismatch", "That code does not match this run's estimate."
        ) from None
    moment = datetime.now(UTC)
    if confirmation_expired(run["created_at"], run.get("confirmed_at"), moment):
        raise ApiError(
            409,
            "confirmation_expired",
            f"That code is older than {c.CONFIRMATION_MINUTES} minutes. Make a new estimate.",
        )
    # One atomic step takes the run for this start: a second press, or a second tab, finds the
    # run taken and starts nothing (a run that crashed becomes free again after its heartbeat
    # has been silent for HEARTBEAT_STALE_MINUTES).
    local = _database(access, "local")
    try:
        taken = store.claim_run(
            local,
            run_id,
            now=moment,
            stale_minutes=c.HEARTBEAT_STALE_MINUTES,
            status=c.RUN_STARTING,
        )
    finally:
        local.close()
    if not taken:
        raise ApiError(409, "run_busy", "This run is already running or starting.")
    with db.transaction(write=True):
        audit.write_audit(
            db,
            actor=current.user.id,
            action="evaluation_run",
            target=run_id,
            details={"step": "start", "kind": run["kind"], "cases": run["planned_items"]},
            now=datetime.now(UTC),
        )
    try:
        access.starter(
            ["run", run_id, "--confirm", body.confirm_code.strip().upper(), "--from-page"]
        )
    except OSError:
        local = _database(access, "local")
        try:  # the process never started: give the run back
            back = c.RUN_PAUSED if run.get("confirmed_at") else c.RUN_ESTIMATED
            store.update_run(local, run_id, status=back)
        finally:
            local.close()
        raise ApiError(503, "service_unavailable", "The run could not be started here.") from None
    return {"run_id": run_id, "status": "starting"}
