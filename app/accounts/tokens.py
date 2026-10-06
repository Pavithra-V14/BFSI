"""Login tokens (spec 13.1): JWT with expiry, and revocation through `revoked_tokens`.

HS256 only, pinned on both sides: a token is signed with HS256 and decoded with exactly that
algorithm, so a token with `alg: none` or another algorithm is refused. A token holds only the
user id (`sub`), a token id (`jti`), the issue time and the expiry: no role and no personal data.
Every request also checks the revoked list and loads the account, so a role change, a
deactivation or a logout takes effect at once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.api.settings import ApiSettings
from app.db.connection import Database

ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("sub", "jti", "iat", "exp")


class InvalidToken(Exception):
    """The token cannot be used. The reason is a short code for the logs, never shown as such."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Claims:
    user_id: str
    jti: str
    issued_at: datetime
    expires_at: datetime


def utc_text(moment: datetime) -> str:
    """The one text form of a time that the app writes: UTC ISO 8601 with microseconds.

    Always the same shape, so plain text comparison in the database orders times correctly.
    """
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def issue_token(settings: ApiSettings, user_id: str, now: datetime) -> tuple[str, Claims]:
    claims = Claims(
        user_id=user_id,
        jti=uuid.uuid4().hex,
        issued_at=now,
        expires_at=now + timedelta(minutes=settings.jwt_expire_minutes),
    )
    token = jwt.encode(
        {
            "sub": claims.user_id,
            "jti": claims.jti,
            "iat": int(claims.issued_at.timestamp()),
            "exp": int(claims.expires_at.timestamp()),
        },
        settings.jwt_secret,
        algorithm=ALGORITHM,
    )
    return token, claims


def decode_token(settings: ApiSettings, token: str, now: datetime | None = None) -> Claims:
    """Check the signature, the algorithm and the expiry. Raises InvalidToken for anything else.

    The signature and expiry are checked against `now` (default: the real time).
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": list(REQUIRED_CLAIMS), "verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError:
        raise InvalidToken("invalid") from None
    current = now or datetime.now(UTC)
    try:
        issued = datetime.fromtimestamp(int(payload["iat"]), UTC)
        expires = datetime.fromtimestamp(int(payload["exp"]), UTC)
        user_id, jti = payload["sub"], payload["jti"]
    except (TypeError, ValueError, OverflowError, OSError):
        raise InvalidToken("invalid") from None
    # Our own check, whatever the JWT library does: both must be non-empty text.
    if not isinstance(user_id, str) or not isinstance(jti, str) or not user_id or not jti:
        raise InvalidToken("invalid")
    if current >= expires:
        raise InvalidToken("expired")
    if issued > current + timedelta(seconds=5):  # a token issued in the future
        raise InvalidToken("invalid")
    return Claims(user_id, jti, issued, expires)


def revoke(db: Database, claims: Claims, now: datetime) -> None:
    """Add the token to the revoked list until it would have expired anyway. Idempotent."""
    db.execute(
        "INSERT INTO revoked_tokens (jti, user_id, revoked_at, expires_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (jti) DO NOTHING",
        (claims.jti, claims.user_id, utc_text(now), utc_text(claims.expires_at)),
    )


def is_revoked(db: Database, jti: str) -> bool:
    return db.query_one("SELECT 1 FROM revoked_tokens WHERE jti = ?", (jti,)) is not None


def purge_expired_revocations(db: Database, now: datetime) -> int:
    """Delete revoked-token rows whose token has expired anyway. Returns how many were deleted.

    An expired token is refused by its expiry, so its row no longer protects anything. Run it
    from the scheduled clean-up (Phase 12). The caller commits.
    """
    cursor = db.execute("DELETE FROM revoked_tokens WHERE expires_at < ?", (utc_text(now),))
    return int(cursor.rowcount)
