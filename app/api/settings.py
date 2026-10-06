"""API settings, read from environment variables by name (never from files).

The app refuses to start on a missing, short or placeholder secret, so a forgotten or
copy-pasted value can never put a guessable key into production. Messages name the variable and
the rule, never a value.

Two secrets, never equal to each other (generate each with
`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`, 43 characters):
- JWT_SECRET_KEY signs login tokens.
- AUDIT_HASH_KEY keys the hashes of usernames that appear in audit rows and throttle counters, so
  a typed name is never stored and the hashes cannot be rebuilt without the key.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

MIN_SECRET_CHARACTERS = 43  # token_urlsafe(32): 32 random bytes
MIN_DISTINCT_CHARACTERS = 16
DEFAULT_JWT_EXPIRE_MINUTES = 480  # [P] 8 hours
MAX_JWT_EXPIRE_MINUTES = 60 * 24 * 30
DEFAULT_TOKEN_BUDGET_CUSTOMER = 50_000  # spec 5.4
DEFAULT_TOKEN_BUDGET_STAFF = 200_000  # [T] placeholder; admin uses the staff value

_PLACEHOLDER_START = (
    "your",
    "change",
    "replace",
    "example",
    "placeholder",
    "insert",
    "paste",
    "todo",
    "xxx",
    "<",
    "${",
)
_PLACEHOLDER_EXACT = {"secret", "password", "changeme", "default", "test", "none", "null", "jwt"}
_ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")


class SettingsError(Exception):
    """A setting is wrong. The message names the variable and the rule, never a value."""


@dataclass(frozen=True)
class ApiSettings:
    # Secrets use repr=False so printing or logging the settings never shows one.
    jwt_secret: str = field(repr=False)
    audit_hash_key: str = field(repr=False)
    jwt_expire_minutes: int = DEFAULT_JWT_EXPIRE_MINUTES
    cors_origin: str | None = None
    token_budget_customer: int = DEFAULT_TOKEN_BUDGET_CUSTOMER
    token_budget_staff: int = DEFAULT_TOKEN_BUDGET_STAFF

    def __post_init__(self) -> None:
        check_secret(self.jwt_secret, "JWT_SECRET_KEY")
        check_secret(self.audit_hash_key, "AUDIT_HASH_KEY")
        if self.jwt_secret == self.audit_hash_key or (
            self.jwt_secret in self.audit_hash_key or self.audit_hash_key in self.jwt_secret
        ):
            raise SettingsError("JWT_SECRET_KEY and AUDIT_HASH_KEY must be two different keys")
        _positive(self.jwt_expire_minutes, "JWT_EXPIRE_MINUTES", MAX_JWT_EXPIRE_MINUTES)
        _positive(self.token_budget_customer, "TOKEN_BUDGET_CUSTOMER")
        _positive(self.token_budget_staff, "TOKEN_BUDGET_STAFF")
        if self.cors_origin is not None:
            check_origin(self.cors_origin)

    def daily_budget(self, role: str) -> int:
        return self.token_budget_customer if role == "customer" else self.token_budget_staff

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ApiSettings:
        env = os.environ if env is None else env
        origin = (env.get("CORS_ALLOWED_ORIGIN") or "").strip()
        return cls(
            jwt_secret=(env.get("JWT_SECRET_KEY") or "").strip(),
            audit_hash_key=(env.get("AUDIT_HASH_KEY") or "").strip(),
            jwt_expire_minutes=_env_int(env, "JWT_EXPIRE_MINUTES", DEFAULT_JWT_EXPIRE_MINUTES),
            cors_origin=origin or None,
            token_budget_customer=_env_int(
                env, "TOKEN_BUDGET_CUSTOMER", DEFAULT_TOKEN_BUDGET_CUSTOMER
            ),
            token_budget_staff=_env_int(env, "TOKEN_BUDGET_STAFF", DEFAULT_TOKEN_BUDGET_STAFF),
        )


def check_secret(value: str, name: str) -> None:
    """Refuse an empty, short, low-variety or placeholder secret. Never shows the value."""
    if not value:
        raise SettingsError(f"{name} is not set")
    if len(value) < MIN_SECRET_CHARACTERS:
        raise SettingsError(
            f"{name} is too short: it needs at least {MIN_SECRET_CHARACTERS} characters "
            "(32 random bytes)"
        )
    lowered = value.lower()
    if (
        lowered in _PLACEHOLDER_EXACT
        or lowered.startswith(_PLACEHOLDER_START)
        or len(set(value)) < MIN_DISTINCT_CHARACTERS
    ):
        raise SettingsError(f"{name} looks like a placeholder: generate a random one")


def check_origin(origin: str) -> None:
    if origin == "*" or not _ORIGIN.fullmatch(origin):
        raise SettingsError(
            "CORS_ALLOWED_ORIGIN must be one origin like https://app.example.com "
            "(scheme and host, no path, no wildcard, no list)"
        )


def _positive(value: object, name: str, maximum: int | None = None) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        raise SettingsError(f"{name} must be a positive whole number")


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a positive whole number") from None
