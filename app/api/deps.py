"""What every route needs: the services, a database handle, the signed-in user (spec 4.1 step 1).

The client address is the socket address. X-Forwarded-For is NOT trusted: anyone can send it, and
trusting it would let an attacker pick the address the throttle sees. Phase 15 configures a
trusted proxy on the host, and only then may this change.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, Header, Request

from app.accounts import tokens, users
from app.accounts.budget import UsageLimits
from app.accounts.throttle import Throttle
from app.api.errors import ApiError
from app.api.settings import ApiSettings
from app.cache.store import CacheStore
from app.db.connection import Database

log = logging.getLogger("app.api")


@dataclass
class Services:
    settings: ApiSettings
    db_factory: Callable[[], Database]
    throttle: Throttle
    limits: UsageLimits
    cache: CacheStore | None = None
    vector_client: Any = None
    gateway: Any = None


@dataclass(frozen=True)
class CurrentUser:
    user: users.User
    claims: tokens.Claims


def services(request: Request) -> Services:
    return request.app.state.services


def get_db(request: Request) -> Iterator[Database]:
    db = services(request).db_factory()
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


def authenticated(
    request: Request,
    db: Database = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    """The signed-in user, or a 401. Every request is checked against the database: the token must
    be valid and not revoked, and the account must still exist and be active. The per-user rate
    limit is applied as soon as the token is known to be genuine."""
    svc = services(request)
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _sign_in_again()
    try:
        claims = tokens.decode_token(svc.settings, token.strip())
    except tokens.InvalidToken:
        raise _sign_in_again() from None
    decision = svc.limits.check_rate(claims.user_id)
    if not decision.allowed:
        raise ApiError(
            429,
            "too_many_requests",
            "You are sending requests too quickly. Please wait a moment.",
            headers={"Retry-After": str(decision.retry_after or 60)},
        )
    if tokens.is_revoked(db, claims.jti):
        raise _sign_in_again()
    user = users.get_by_id(db, claims.user_id)
    if user is None or not user.is_active:
        raise _sign_in_again()
    return CurrentUser(user, claims)


def require_admin(current: CurrentUser = Depends(authenticated)) -> CurrentUser:
    if current.user.role != "admin":
        raise ApiError(403, "forbidden", "You do not have access to this.")
    return current
