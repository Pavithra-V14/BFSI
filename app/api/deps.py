"""What every route needs: the services, a database handle, the signed-in user (spec 4.1 step 1).

The client address is the socket address. X-Forwarded-For is NOT trusted: anyone can send it, and
trusting it would let an attacker pick the address the throttle sees. Phase 15 configures a
trusted proxy on the host (its options set explicitly and tested), and only then may this change.

The database is opened lazily, on first use. A request that is refused before it needs the
database (no token, a bad token, a throttled login) never opens a connection, so an anonymous
flood cannot use up connections and gets its 401 even while the database is down.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Header, Request

from app.accounts import tokens, users
from app.accounts.budget import UsageLimits
from app.accounts.gate import HashGate
from app.accounts.throttle import Throttle
from app.api.errors import ApiError
from app.api.settings import ApiSettings
from app.cache.store import CacheStore
from app.db.connection import Database

log = logging.getLogger("app.api")
EVENT_BUFFER = 1_000


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
    on_event: Callable[[str, dict[str, Any]], None] | None = None
    # The last EVENT_BUFFER events: what Phase 12's alerts and dashboards will read.
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_BUFFER))

    def emit(self, name: str, **fields: Any) -> None:
        """Record an alert event: names and counts only, never a name, an address or a secret."""
        self.events.append((name, fields))
        log.warning("api event %s %s", name, fields)
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


def client_ip(request: Request) -> str:
    """The address of the socket that connected. Never read from a header."""
    return request.client.host if request.client else "unknown"


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
    decision = svc.limits.check_rate(claims.user_id)
    if not decision.allowed:
        raise ApiError(
            429,
            "too_many_requests",
            "You are sending requests too quickly. Please wait a moment.",
            headers={"Retry-After": str(decision.retry_after or 60)},
        )
    return _current_user(db, claims)


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
