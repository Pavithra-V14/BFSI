"""API settings, read from environment variables by name (never from files).

The app refuses to start on a missing, short or placeholder secret, so a forgotten or
copy-pasted value can never put a guessable key into production. Messages name the variable and
the rule, never a value.

Two secrets, unrelated to each other (generate each with
`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`, 43 characters; a 64
character hex key from `openssl rand -hex 32` is accepted too):
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
MIN_DISTINCT_CHARACTERS_HEX = 12  # a hex key has only 16 symbols to choose from
RELATED_LENGTH = 12  # two keys sharing a piece this long are treated as built from each other
RUN_LENGTH = 7  # a run of this many neighbouring keys (or letters, or digits) is a keyboard run
DEFAULT_HASH_CONCURRENCY = 2  # [P] password hashes running at once
MAX_HASH_CONCURRENCY = 16
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
_HEX = re.compile(r"[0-9a-fA-F]+")
# Runs a person types without thinking: keyboard rows, the alphabet and the digits, both ways.
_SEQUENCES = tuple(
    text + text[::-1]
    for text in ("qwertyuiop", "asdfghjkl", "zxcvbnm", "abcdefghijklmnopqrstuvwxyz", "0123456789")
)


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
    password_hash_concurrency: int = DEFAULT_HASH_CONCURRENCY

    def __post_init__(self) -> None:
        check_secret(self.jwt_secret, "JWT_SECRET_KEY")
        check_secret(self.audit_hash_key, "AUDIT_HASH_KEY")
        if keys_are_related(self.jwt_secret, self.audit_hash_key):
            raise SettingsError(
                "JWT_SECRET_KEY and AUDIT_HASH_KEY must be two unrelated keys: not equal, not "
                "one inside the other, not reversed, not one with a few characters changed"
            )
        _positive(self.jwt_expire_minutes, "JWT_EXPIRE_MINUTES", MAX_JWT_EXPIRE_MINUTES)
        _positive(self.token_budget_customer, "TOKEN_BUDGET_CUSTOMER")
        _positive(self.token_budget_staff, "TOKEN_BUDGET_STAFF")
        _positive(self.password_hash_concurrency, "PASSWORD_HASH_CONCURRENCY", MAX_HASH_CONCURRENCY)
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
            password_hash_concurrency=_env_int(
                env, "PASSWORD_HASH_CONCURRENCY", DEFAULT_HASH_CONCURRENCY
            ),
        )


def check_secret(value: str, name: str) -> None:
    """Refuse an empty, short or guessable secret. Never shows the value.

    Guessable means: a placeholder word, too few different characters, a short pattern repeated,
    or a keyboard, alphabet or digit run. A random 43-character URL-safe key and a random
    64-character hex key both pass.
    """
    if not value:
        raise SettingsError(f"{name} is not set")
    if len(value) < MIN_SECRET_CHARACTERS:
        raise SettingsError(
            f"{name} is too short: it needs at least {MIN_SECRET_CHARACTERS} characters "
            "(32 random bytes)"
        )
    lowered = value.lower()
    minimum_distinct = (
        MIN_DISTINCT_CHARACTERS_HEX if _HEX.fullmatch(value) else MIN_DISTINCT_CHARACTERS
    )
    if (
        lowered in _PLACEHOLDER_EXACT
        or (lowered.startswith(_PLACEHOLDER_START) and not _is_generated_shape(value))
        or len(set(value)) < minimum_distinct
        or _repeats_a_pattern(value)
        or _has_a_run(lowered)
    ):
        raise SettingsError(
            f"{name} does not look random (a placeholder, a repeated pattern or a keyboard "
            "run): generate a new one with the command in .env.example"
        )


GENERATED_LENGTH = 43  # secrets.token_urlsafe(32): 32 random bytes as 43 URL-safe characters
GENERATED_MIN_DISTINCT = 20  # a random one has about 33; a typed placeholder has far fewer
_URLSAFE = re.compile(r"[A-Za-z0-9_-]+")


def _is_generated_shape(value: str) -> bool:
    """The exact shape `secrets.token_urlsafe(32)` makes. A random key of this shape can start with
    "xxx" or "todo" by chance (about 1 in 25,000), which must not make it a "placeholder"; every
    other rule (distinct characters, repeats, runs, related keys) still applies to it."""
    return (
        len(value) == GENERATED_LENGTH
        and _URLSAFE.fullmatch(value) is not None
        and len(set(value)) >= GENERATED_MIN_DISTINCT
    )


def _repeats_a_pattern(value: str) -> bool:
    """True if the whole value is one short unit repeated (with a partial copy at the end), or
    if a piece of RELATED_LENGTH characters appears twice."""
    size = len(value)
    for period in range(1, size // 2 + 1):
        if all(value[i] == value[i - period] for i in range(period, size)):
            return True
    seen = set()
    for start in range(size - RELATED_LENGTH + 1):
        piece = value[start : start + RELATED_LENGTH]
        if piece in seen:
            return True
        seen.add(piece)
    return False


def _has_a_run(lowered: str) -> bool:
    return any(
        text[i : i + RUN_LENGTH] in lowered
        for text in _SEQUENCES
        for i in range(len(text) - RUN_LENGTH + 1)
    )


def keys_are_related(first: str, second: str) -> bool:
    """True if one key is built from the other: equal, one inside the other, reversed, the same
    apart from a few characters, or sharing a piece of RELATED_LENGTH characters."""
    one, other = first.lower(), second.lower()
    if one == other or one in other or other in one or one == other[::-1]:
        return True
    if len(one) == len(other) and sum(a != b for a, b in zip(one, other, strict=True)) <= 4:
        return True
    pieces = {one[i : i + RELATED_LENGTH] for i in range(len(one) - RELATED_LENGTH + 1)}
    for candidate in (other, other[::-1]):
        for i in range(len(candidate) - RELATED_LENGTH + 1):
            if candidate[i : i + RELATED_LENGTH] in pieces:
                return True
    return False


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
