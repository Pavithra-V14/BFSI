"""What every route needs: the services, a database handle, the signed-in user (spec 4.1 step 1).

The client address is the socket address, unless TRUSTED_PROXY_HOPS (default 0) says how many
proxies stand in front of the API (Phase 15). X-Forwarded-For is never read at 0 hops: anyone can
send it, and trusting it would let an attacker pick the address the throttle sees. With N hops
the entry N places from the END is used, because each trusted proxy appends the address it saw;
an entry that is not an address, or a header with fewer entries than hops, falls back to the
socket's address. The server is started with --no-proxy-headers, so only this code reads it.

The database is opened lazily, on first use. A request that is refused before it needs the
database (no token, a bad token, a throttled login) never opens a connection, so an anonymous
flood cannot use up connections and gets its 401 even while the database is down.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Header, Query, Request

from app.accounts import tokens, users
from app.accounts.budget import UsageLimits
from app.accounts.gate import HashGate
from app.accounts.throttle import Throttle
from app.api.errors import ApiError
from app.api.rollout import Rollout
from app.api.settings import ApiSettings
from app.cache.store import CacheStore
from app.db.connection import Database
from app.observability.logs import fx

log = logging.getLogger("app.api")
EVENT_BUFFER = 1_000

# A session or message id as this system makes them (uuid4 text). Anything else is "not found"
# BEFORE any lookup, the same answer as for a missing or someone else's id.
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MAX_OFFSET = 100_000


def is_uuid(value: str | None) -> bool:
    return isinstance(value, str) and UUID_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class Page:
    limit: int
    offset: int


def page_params(default: int = 20, maximum: int = 100):
    """A dependency for a list route: `limit` (default and maximum given) and `offset`. A value
    over the maximum is refused with 422, never clipped silently."""

    def dependency(
        limit: int = Query(default, ge=1, le=maximum),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ) -> Page:
        return Page(limit, offset)

    return dependency


@dataclass
class Services:
    settings: ApiSettings
    db_factory: Callable[[], Database]
    throttle: Throttle
    limits: UsageLimits
    hash_gate: HashGate
    cache: CacheStore | None = None
    vector_client: Any = None
    gateway: Any = None
    pipeline: Any = None  # app.pipeline.run.Pipeline, or None (no chat)
    rollout: Rollout = field(default_factory=Rollout)
    on_render: bool = False  # the admin-machine-only actions are refused here (spec 24)
    entity_review_enabled: bool = False  # ENTITY_REVIEW_ENABLED=true: opt-in, default off [P]
    observability: Any = None  # app.observability.service.Observability, or None
    evaluation: Any = None  # app.eval.access.EvalAccess, or None (Phase 13)
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    # The last EVENT_BUFFER events: what Phase 12's alerts and dashboards will read.
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    # When each event in `events` happened (same length, same order), so the banner can let an
    # old one expire (24 hours) instead of showing it until the buffer is pushed out.
    event_times: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))
    clock: Callable[[], float] = time.time

    def emit(self, name: str, **fields: Any) -> None:
        """Record an alert event: names and counts only, never a name, an address or a secret."""
        self.events.append((name, fields))
        self.event_times.append(self.clock())
        log.warning("api event %s", name, extra=fx({"event": name, **fields}))
        if self.observability is not None:  # alert rules and persistence (never raises)
            try:
                self.observability.alerts.on_event(name, fields)
            except Exception:
                pass
        if self.on_event:
            try:
                self.on_event(name, fields)
            except Exception:  # an alert hook that fails must never break a request
                log.error("api event handler failed for %s", name)


@dataclass(frozen=True)
class CurrentUser:
    user: users.User
    claims: tokens.Claims


class LazyDatabase:
    """A database handle that opens its connection the first time it is used."""

    def __init__(self, factory: Callable[[], Database]) -> None:
        self._factory = factory
        self._database: Database | None = None

    def _open(self) -> Database:
        if self._database is None:
            self._database = self._factory()
        return self._database

    def __getattr__(self, name: str) -> Any:  # only reached for names not set in __init__
        return getattr(self._open(), name)

    @property
    def opened(self) -> bool:
        return self._database is not None

    def close(self) -> None:
        if self._database is not None:
            self._database.close()
            self._database = None


def services(request: Request) -> Services:
    return request.app.state.services


def get_db(request: Request) -> Iterator[Any]:
    db = LazyDatabase(services(request).db_factory)
    try:
        yield db
    finally:
        db.close()


def forwarded_entries(request: Request) -> list[str]:
    """The entries of every X-Forwarded-For header, in order, stripped, empty ones left out."""
    values = request.headers.getlist("x-forwarded-for")
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


def client_address(request: Request, hops: int) -> tuple[str, str]:
    """(the address to use, where it came from: "socket" or "forwarded").

    With `hops` = 0 the header is never read: the address is the socket's. With `hops` = N the
    entry N places from the END is used: each trusted proxy appends the address it saw, so the
    last entry is the one our own proxy wrote and anything before it is the client's claim. A
    header with fewer entries than hops, or an entry that is not an address, falls back to the
    socket's address (a request that did not come through the proxy is never believed)."""
    socket_address = request.client.host if request.client else "unknown"
    if hops <= 0:
        return socket_address, "socket"
    entries = forwarded_entries(request)
    if len(entries) < hops:
        return socket_address, "socket"
    candidate = entries[-hops]
    try:
        return str(ipaddress.ip_address(candidate)), "forwarded"
    except ValueError:
        return socket_address, "socket"


def client_ip(request: Request) -> str:
    """The address a per-address limit counts: the socket's, or with TRUSTED_PROXY_HOPS the one
    our own proxy saw (see `client_address`)."""
    return client_address(request, services(request).settings.trusted_proxy_hops)[0]


def _sign_in_again() -> ApiError:
    return ApiError(
        401,
        "not_signed_in",
        "Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _claims_from(svc: Services, authorization: str | None) -> tokens.Claims:
    """The claims of a genuine, unexpired token, or a 401. No database is touched."""
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _sign_in_again()
    try:
        return tokens.decode_token(svc.settings, token.strip())
    except tokens.InvalidToken:
        raise _sign_in_again() from None


def _current_user(db: Any, claims: tokens.Claims) -> CurrentUser:
    """The account behind a genuine token. This is the first thing that touches the database:
    the token must not be revoked, and the account must still exist and be active. The role is
    read from the database here, never from the token."""
    if tokens.is_revoked(db, claims.jti):
        raise _sign_in_again()
    user = users.get_by_id(db, claims.user_id)
    if user is None or not user.is_active:
        raise _sign_in_again()
    return CurrentUser(user, claims)


def authenticated(
    request: Request,
    db: Any = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    """The signed-in user, or a 401.

    Order: the header and the token's signature and expiry first (no database), then the
    per-user rate limit (so garbage never reaches the limiter or the database), then the revoked
    list and the account (the first use of the database).
    """
    svc = services(request)
    claims = _claims_from(svc, authorization)
    refused = _rate_refused(svc, claims.user_id)
    if refused is not None:
        raise refused
    return _current_user(db, claims)


def _rate_refused(svc: Services, user_id: str) -> ApiError | None:
    """Count one request for this user; the refusal to send, or None when it is allowed."""
    decision = svc.limits.check_rate(user_id)
    if decision.allowed:
        return None
    return ApiError(
        429,
        "too_many_requests",
        "You are sending requests too quickly. Please wait a moment.",
        headers={"Retry-After": str(decision.retry_after or 60)},
    )


def open_user_context(svc: Services, authorization: str | None) -> tuple[CurrentUser, LazyDatabase]:
    """For a route that owns its database connection (the chat route, which must keep it open
    while a timed-out transcript write finishes). Like `authenticated`, the request is counted
    against the per-user limit ONCE, here at the entry, so every refusal that follows (a bad
    session id, a busy session, a service that is starting up) is counted too. The pipeline is
    told (`Request.rate_counted`) not to count it again.

    Returns the signed-in user and the (lazy) database; the CALLER closes it. Blocking: call it
    from a worker thread."""
    claims = _claims_from(svc, authorization)
    refused = _rate_refused(svc, claims.user_id)
    if refused is not None:
        raise refused
    db = LazyDatabase(svc.db_factory)
    try:
        return _current_user(db, claims), db
    except BaseException:
        db.close()
        raise


def authenticated_for_logout(
    request: Request,
    db: Any = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    """Like `authenticated`, but the request is NOT counted against the per-user limit.

    Someone holding a stolen token could otherwise use up the owner's 20 requests a minute and
    stop the owner from logging out and revoking it.
    """
    claims = _claims_from(services(request), authorization)
    return _current_user(db, claims)


def require_admin(current: CurrentUser = Depends(authenticated)) -> CurrentUser:
    if current.user.role != "admin":
        raise ApiError(403, "forbidden", "You do not have access to this.")
    return current
