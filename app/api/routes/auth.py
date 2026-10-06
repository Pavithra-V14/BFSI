"""Registration, login, logout, "who am I" (spec 13.1).

Every route here is a plain `def` on purpose: FastAPI runs it in a worker thread, so the slow
password hashing never blocks the event loop and other requests keep being served.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.accounts import audit, passwords, tokens, users
from app.api.deps import CurrentUser, Services, authenticated, client_ip, get_db, services
from app.api.errors import ApiError
from app.db.connection import Database
from app.db.identity import normalise_email, normalise_username

log = logging.getLogger("app.api")
router = APIRouter(prefix="/auth", tags=["auth"])

USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")
EMAIL_PATTERN = re.compile(r"[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+")
MAX_EMAIL_LENGTH = 254
BAD_LOGIN = ("bad_credentials", "Wrong username or password.")


class RegisterBody(BaseModel):
    """Exactly three fields. Anything else, a `role` included, is refused with a 422."""

    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=256)
    email: str = Field(max_length=512)
    password: str = Field(max_length=512)


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=256)
    password: str = Field(max_length=512)


def _now() -> datetime:
    return datetime.now(UTC)


def _too_many(decision) -> ApiError:  # type: ignore[no-untyped-def]
    return ApiError(
        429,
        "too_many_attempts",
        "Too many attempts. Please wait a while and try again.",
        headers={"Retry-After": str(decision.retry_after or 60)},
    )


@router.post("/register", status_code=201)
def register(
    body: RegisterBody, request: Request, db: Database = Depends(get_db)
) -> dict[str, str]:
    svc = services(request)
    decision = svc.throttle.check_register(client_ip(request))
    if not decision.allowed:
        raise _too_many(decision)
    username = normalise_username(body.username)
    email = normalise_email(body.email)
    if not USERNAME_PATTERN.fullmatch(username):
        raise ApiError(
            422,
            "invalid_username",
            "Use 3 to 32 letters, digits, dots, dashes or underscores, starting with a letter "
            "or digit.",
        )
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL_PATTERN.fullmatch(email):
        raise ApiError(422, "invalid_email", "That does not look like an email address.")
    problems = passwords.check_password_rule(body.password, username=username, email=email)
    if problems:
        raise ApiError(
            422,
            "invalid_password",
            "Choose a longer, less guessable password (10 to 128 characters, not your name, "
            "not a very common password).",
            extra={"reasons": problems},
        )
    password_hash = passwords.hash_password(body.password)
    try:
        with db.transaction(write=True):
            user = users.create_customer(
                db, username=username, email=email, password_hash=password_hash, now=_now()
            )
    except users.DuplicateAccount:
        # One message for a taken username and a taken email (known limitation: the fact that
        # the account exists can still be learned by registering).
        raise ApiError(409, "account_exists", "That username or email cannot be used.") from None
    return {"id": user.id, "username": user.username, "role": user.role}


@router.post("/login")
def login(body: LoginBody, request: Request, db: Database = Depends(get_db)) -> dict[str, str]:
    svc: Services = services(request)
    username = normalise_username(body.username)
    decision = svc.throttle.check_login(username, client_ip(request))
    if not decision.allowed:
        log.warning("login throttled")  # no name, no address
        raise _too_many(decision)
    user = users.get_by_username(db, username) if username else None
    # Always check a hash, so an unknown name takes as long as a wrong password.
    matches = passwords.verify_password(
        user.password_hash if user else passwords.dummy_hash(), body.password
    )
    now = _now()
    ok = bool(user and matches and user.is_active)
    if user is None:
        actor = "anonymous"
        target = audit.keyed_hash(svc.settings.audit_hash_key, "audit-username", username)
        reason = "unknown_user"
    else:
        actor = target = user.id
        reason = "success" if ok else ("inactive" if matches else "bad_password")
    with db.transaction(write=True):
        audit.write_audit(
            db,
            actor=actor,
            action="login",
            target=target,
            details={"result": "success" if ok else "failed", "reason": reason},
            now=now,
        )
        if ok and passwords.needs_rehash(user.password_hash):  # type: ignore[union-attr]
            users.update_password_hash(db, user.id, passwords.hash_password(body.password))  # type: ignore[union-attr]
    if not ok:
        raise ApiError(401, *BAD_LOGIN)
    token, claims = tokens.issue_token(svc.settings, user.id, now)  # type: ignore[union-attr]
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": tokens.utc_text(claims.expires_at),
    }


@router.post("/logout", status_code=204)
def logout(
    current: CurrentUser = Depends(authenticated), db: Database = Depends(get_db)
) -> Response:
    with db.transaction(write=True):
        tokens.revoke(db, current.claims, _now())
    return Response(status_code=204)


@router.get("/me")
def me(current: CurrentUser = Depends(authenticated)) -> dict[str, str]:
    user = current.user
    return {"id": user.id, "username": user.username, "role": user.role}
