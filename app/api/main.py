"""The FastAPI app (spec 2, 11.2, 13).

`create_app` builds the app from parts (tests pass fakes). `create_app_from_env` builds it from the
environment and REFUSES TO START on a missing, short or placeholder secret. Run it with:

    uv run uvicorn app.api.main:create_app_from_env --factory

The interactive docs and the OpenAPI file are switched off: they would publish the API's shape.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from app.accounts.budget import UsageLimits
from app.accounts.throttle import Throttle
from app.api.deps import Services
from app.api.errors import install_error_handlers
from app.api.routes import admin, auth, health
from app.api.security import SecurityMiddleware, cors_options
from app.api.settings import ApiSettings, SettingsError
from app.cache.store import CacheStore, store_from_env
from app.db.connection import Database, connect
from app.gateway.embeddings import running_on_render

log = logging.getLogger("app.api")


class SecureFastAPI(FastAPI):
    """A FastAPI app whose outermost layer is the SecurityMiddleware.

    `add_middleware` would put it inside Starlette's error layer, and a 500 sent by that layer
    would then leave without the security headers. Wrapping the finished stack avoids that.
    """

    def build_middleware_stack(self) -> ASGIApp:
        return SecurityMiddleware(super().build_middleware_stack())


def create_app(
    settings: ApiSettings,
    *,
    db_factory: Callable[[], Database],
    cache: CacheStore | None = None,
    vector_client: Any = None,
    gateway: Any = None,
    throttle: Throttle | None = None,
    limits: UsageLimits | None = None,
) -> FastAPI:
    app = SecureFastAPI(
        title="BFSI assistant API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = Services(
        settings=settings,
        db_factory=db_factory,
        throttle=throttle or Throttle(cache, settings.audit_hash_key),
        limits=limits or UsageLimits(cache, settings),
        cache=cache,
        vector_client=vector_client,
        gateway=gateway,
    )
    install_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(health.router)
    options = cors_options(settings.cors_origin)
    if options is not None:  # no origin configured: no CORS header is ever sent
        app.add_middleware(CORSMiddleware, **options)
    return app


def create_app_from_env(env: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if env is None else env
    try:
        settings = ApiSettings.from_env(env)
    except SettingsError as error:
        # The message names the variable and the rule, never a value.
        raise SystemExit(f"refusing to start: {error}") from None
    database_url = (env.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise SystemExit("refusing to start: DATABASE_URL is not set")
    cache: CacheStore | None = None
    if (env.get("UPSTASH_REDIS_URL") or "").strip():
        cache = store_from_env(env)
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

        gateway = Gateway(GatewayConfig.from_env(env))
    except Exception as error:  # a wrong gateway setting must stop the start, with names only
        raise SystemExit(f"refusing to start: gateway settings: {type(error).__name__}") from None
    return create_app(
        settings,
        db_factory=lambda: connect(database_url),
        cache=cache,
        vector_client=vector_client,
        gateway=gateway,
    )
