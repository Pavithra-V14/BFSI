"""The FastAPI app (spec 2, 11.2, 13).

`create_app` builds the app from parts (tests pass fakes). `create_app_from_env` builds it from the
environment and REFUSES TO START on a missing, short or placeholder secret. Run it with:

    uv run uvicorn app.api.main:create_app_from_env --factory

The interactive docs and the OpenAPI file are switched off: they would publish the API's shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from app.accounts.budget import UsageLimits
from app.accounts.gate import HashGate
from app.accounts.throttle import Throttle
from app.api.deps import Services
from app.api.errors import install_error_handlers
from app.api.rollout import Rollout
from app.api.routes import (
    admin,
    admin_dashboards,
    admin_eval,
    admin_maintenance,
    admin_ops,
    auth,
    chat,
    feedback,
    health,
    sessions,
)
from app.api.security import SecurityMiddleware, cors_options
from app.api.settings import ApiSettings, SettingsError
from app.cache.store import CacheStore, store_from_env
from app.db.connection import Database, connect
from app.eval.sampling import SamplingConfigError
from app.gateway.embeddings import running_on_render
from app.observability import logs as observability_logs
from app.observability.service import from_env as observability_from_env
from app.observability.stage_printer import printer_from_env
from app.observability.webhook import WebhookConfigError

log = logging.getLogger("app.api")


class EventRelay:
    """A way to hand alert events to the app BEFORE the app exists.

    The cache store, the gateway, the usage limiter and the masker are built first and the app is
    built from them, so they cannot be given `Services.emit` directly. They get this relay; once
    the app exists `bind` points it at `Services.emit`, and any event that came in before that
    (a masker that failed to load while the app was being built) is passed on then. Events carry
    names and counts only."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._target: Callable[[str, dict[str, Any]], None] | None = None
        self._early: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, fields: dict[str, Any]) -> None:
        with self._lock:
            target = self._target
            if target is None:
                self._early.append((name, dict(fields)))
                return
        target(name, fields)

    def bind(self, target: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._target, early = target, self._early
            self._early = []
        for name, fields in early:
            target(name, fields)


class SecureFastAPI(FastAPI):
    """A FastAPI app whose outermost layer is the SecurityMiddleware.

    `add_middleware` would put it inside Starlette's error layer, and a 500 sent by that layer
    would then leave without the security headers. Wrapping the finished stack avoids that.
    """

    def build_middleware_stack(self) -> ASGIApp:
        # The correlation id is bound inside the security layer, so every log line a request
        # causes (including a refusal by the layers below) carries the same id.
        return SecurityMiddleware(
            observability_logs.CorrelationMiddleware(super().build_middleware_stack())
        )


def create_app(
    settings: ApiSettings,
    *,
    db_factory: Callable[[], Database],
    cache: CacheStore | None = None,
    vector_client: Any = None,
    gateway: Any = None,
    throttle: Throttle | None = None,
    limits: UsageLimits | None = None,
    pipeline: Any = None,
    on_render: bool = False,
    entity_review_enabled: bool = False,
    rollout_cache_seconds: float | None = None,
    observability: Any = None,
    evaluation: Any = None,
    ingest_runner: Callable[[list[str]], Any] | None = None,
    maintenance_runner: Callable[[list[str]], Any] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start-up check (spec 6.6, Phase 9): do the stored sparse vectors come from this
        # tokenizer version? Counts only; a mismatch is an alert event, never a refusal.
        if pipeline is not None:
            await asyncio.to_thread(pipeline.check_index)
        if observability is not None:  # alert writer, rollups and trace deletion (Phase 12)
            observability.start()
        sampler = getattr(evaluation, "sampler", None)
        if sampler is not None:  # the production sample's writer (Phase 13)
            sampler.start()
        try:
            yield
        finally:
            if sampler is not None:
                await asyncio.to_thread(sampler.stop)
            if observability is not None:
                await asyncio.to_thread(observability.stop)
            tracing = getattr(getattr(pipeline, "deps", None), "langfuse", None)
            if tracing is not None:  # hands over and flushes what the Langfuse SDK still holds
                await asyncio.to_thread(tracing.stop)

    app = SecureFastAPI(
        title="BFSI assistant API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.services = Services(
        settings=settings,
        db_factory=db_factory,
        throttle=throttle or Throttle(cache, settings.audit_hash_key),
        limits=limits or UsageLimits(cache, settings),
        hash_gate=HashGate(settings.password_hash_concurrency),
        cache=cache,
        vector_client=vector_client,
        gateway=gateway,
        pipeline=pipeline,
        on_render=on_render,
        entity_review_enabled=entity_review_enabled,
        observability=observability,
        evaluation=evaluation,
        ingest_runner=ingest_runner,
        maintenance_runner=maintenance_runner,
    )
    svc = app.state.services
    svc.rollout = Rollout(
        on_event=lambda name, fields: svc.emit(name, **fields),
        **({} if rollout_cache_seconds is None else {"cache_seconds": rollout_cache_seconds}),
    )
    if pipeline is not None and getattr(pipeline, "deps", None) is not None:
        previous = pipeline.deps.on_event

        def relay(name: str, fields: dict[str, Any]) -> None:
            svc.emit(name, **fields)
            if previous is not None:
                previous(name, fields)

        pipeline.deps.on_event = relay
    if pipeline is not None and observability is not None and getattr(pipeline, "deps", None):
        pipeline.deps.observer = observability.observe
        pipeline.deps.observer_early = observability.observe_early
    sampler = getattr(evaluation, "sampler", None)
    if pipeline is not None and sampler is not None and getattr(pipeline, "deps", None):
        from app.eval.sampling import chain

        pipeline.deps.observer = chain(pipeline.deps.observer, sampler.observe)
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(admin_ops.router)
    app.include_router(admin_dashboards.router)
    app.include_router(admin_maintenance.router)
    app.include_router(admin_eval.router)
    app.include_router(chat.router)
    app.include_router(sessions.router)
    app.include_router(feedback.router)
    app.include_router(health.router)
    options = cors_options(settings.cors_origin)
    if options is not None:  # no origin configured: no CORS header is ever sent
        app.add_middleware(CORSMiddleware, **options)
    return app


def create_app_from_env(env: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if env is None else env
    observability_logs.configure_logging()  # JSON lines with a correlation id, from the start
    try:
        settings = ApiSettings.from_env(env)
    except SettingsError as error:
        # The message names the variable and the rule, never a value.
        raise SystemExit(f"refusing to start: {error}") from None
    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("refusing to start: DATABASE_URL is not set")
    relay = EventRelay()
    cache: CacheStore | None = None
    if (env.get("UPSTASH_REDIS_URL") or "").strip():
        cache = store_from_env(env, on_event=relay)
    elif running_on_render(env):
        raise SystemExit("refusing to start: UPSTASH_REDIS_URL is not set")
    else:
        log.warning("no cache configured: counters stay in this process")
    vector_client = None
    if (env.get("QDRANT_URL") or "").strip():
        from app.vector.documents import client_from_env

        vector_client = client_from_env(env)
    gateway = None
    try:
        from app.gateway import Gateway, GatewayConfig

        gateway = Gateway(GatewayConfig.from_env(env), on_event=relay)
    except Exception as error:  # a wrong gateway setting must stop the start, with names only
        raise SystemExit(f"refusing to start: gateway settings: {type(error).__name__}") from None
    limits = UsageLimits(cache, settings, on_event=relay)
    pipeline = _build_pipeline(env, cache, vector_client, gateway, limits, relay)

    def db_factory() -> Database:
        return connect(database_url)

    try:
        observability = observability_from_env(env, db_factory, on_event=relay)
    except (ValueError, WebhookConfigError) as error:
        # The message names the variable and the rule, never a value (the address is a secret).
        raise SystemExit(f"refusing to start: {error}") from None
    observability.key_source = gateway  # the per-key call counts are saved with the other rollups
    printer = printer_from_env(env, running_on_render=running_on_render(env))
    if printer is not None and pipeline is not None:  # DEV_STAGE_TRACE=true, never on Render
        deps = pipeline.deps
        deps.on_stage_start = printer.stage_started
        deps.on_span = printer.stage_finished
        deps.on_finish = lambda ctx, result: printer.waterfall(
            list(ctx.trace.spans), (ctx.deps.clock() - ctx.started) * 1000.0
        )
    try:
        evaluation = _evaluation_access(env, relay)
    except SamplingConfigError as error:
        raise SystemExit(f"refusing to start: {error}") from None
    app = create_app(
        settings,
        db_factory=db_factory,
        observability=observability,
        evaluation=evaluation,
        cache=cache,
        vector_client=vector_client,
        gateway=gateway,
        limits=limits,
        pipeline=pipeline,
        on_render=running_on_render(env),
        entity_review_enabled=(env.get("ENTITY_REVIEW_ENABLED") or "").strip().lower() == "true",
    )
    # From here every event of the cache, gateway, limiter, masker and sink reaches /admin/alerts.
    relay.bind(lambda name, fields: app.state.services.emit(name, **fields))
    return app


def _evaluation_access(
    env: Mapping[str, str], relay: Callable[[str, dict[str, Any]], None] | None = None
) -> Any:
    """The evaluation databases this process can reach and, when `EVAL_DATABASE_URL` is set, the
    5 % production sample (Phase 13). The local test database exists only off Render."""
    from app.eval import store as eval_store
    from app.eval.access import EvalAccess
    from app.eval.sampling import sampler_from_env

    production = (env.get("EVAL_DATABASE_URL") or "").strip() or None
    on_render = running_on_render(env)
    local = None if on_render else eval_store.DEFAULT_LOCAL_FILE
    if production is None and (local is None or not local.is_file()):
        return None if on_render else EvalAccess(local_path=local, env=env)
    return EvalAccess(
        local_path=local,
        production_url=production,
        sampler=sampler_from_env(env, on_event=relay),
        env=env,
    )


def _build_pipeline(
    env: Mapping[str, str],
    cache: CacheStore | None,
    vector_client: Any,
    gateway: Any,
    limits: UsageLimits,
    relay: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """The query pipeline (Phase 8) with its dependencies, or None when no vector store is
    configured (chat then answers 503). The masker loads in the background: until it is ready
    a chat is "starting up, please retry". The Langfuse sink is off unless fully configured; a
    half-set or placeholder configuration refuses to start (names only)."""
    if vector_client is None:
        log.warning("no vector store configured: chat is not available")
        return None
    from app.gateway.tokens import TokenCounter
    from app.guardrails.domains import curated_domains
    from app.pipeline import langfuse_sink
    from app.pipeline.config import PipelineConfig, node_limits_for, slice_seconds_for
    from app.pipeline.run import Pipeline
    from app.pipeline.startup import MaskerHolder
    from app.pipeline.state import Deps

    try:
        sink = langfuse_sink.sink_from_env(env, on_event=relay)
    except langfuse_sink.SinkConfigError as error:
        raise SystemExit(f"refusing to start: {error}") from None
    holder = MaskerHolder(on_event=relay)
    holder.start()
    deps = Deps(
        gateway=gateway,
        qdrant=vector_client,
        cache=cache,
        masker=holder,
        counter=TokenCounter(),
        usage=limits,
        config=PipelineConfig(
            node_limits=node_limits_for(running_on_render(env)),
            slice_seconds=slice_seconds_for(running_on_render(env)),
        ),
        curated_domains=curated_domains(env),
        langfuse=sink,
    )
    return Pipeline(deps)
