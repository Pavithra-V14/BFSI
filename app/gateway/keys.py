"""Key pools (spec 5.2): up to four keys per provider, used in turn.

A 429 puts only that key on cool-down and the call moves to the next key. Keys
are only ever identified by a short fingerprint in logs and results.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.gateway.clock import Clock
from app.gateway.limiter import Limits, Reservation, SlidingWindowLimiter

MAX_KEYS_PER_PROVIDER = 4
DEFAULT_COOLDOWN_SECONDS = 60.0


class ConfigError(Exception):
    """A setting is wrong. The message names the variable, never a value."""


def fingerprint(key: str) -> str:
    """Eight hex characters of the key's SHA-256. Cannot be turned back into the key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def parse_keys(raw: str | None, *, name: str, max_keys: int = MAX_KEYS_PER_PROVIDER) -> list[str]:
    """Split a comma-separated variable into keys (blank and repeated ones dropped)."""
    keys: list[str] = []
    for part in (raw or "").split(","):
        key = part.strip()
        if key and key not in keys:
            keys.append(key)
    if len(keys) > max_keys:
        raise ConfigError(f"{name} holds more than {max_keys} keys")
    return keys


@dataclass
class Lease:
    key: str
    fingerprint: str
    reservation: Reservation


class _KeyState:
    def __init__(self, key: str, limits: Limits, clock: Clock) -> None:
        self.key = key
        self.fingerprint = fingerprint(key)
        self.limiter = SlidingWindowLimiter(limits, clock)
        self.cooldown_until = 0.0


class KeyPool:
    """strategy: 'round_robin' rotates across keys; 'ordered' prefers the first key."""

    def __init__(
        self,
        provider: str,
        keys: list[str],
        limits: Limits,
        clock: Clock,
        *,
        strategy: str = "round_robin",
        default_cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.provider = provider
        self._clock = clock
        self._strategy = strategy
        self._default_cooldown = default_cooldown
        self._states = [_KeyState(key, limits, clock) for key in keys]
        self._next = 0

    @property
    def size(self) -> int:
        return len(self._states)

    def can_ever_fit(self, tokens: int) -> bool:
        return any(s.limiter.can_ever_fit(tokens) for s in self._states)

    def _order(self) -> list[_KeyState]:
        if self._strategy == "ordered" or not self._states:
            return list(self._states)
        n = len(self._states)
        return [self._states[(self._next + i) % n] for i in range(n)]

    def acquire(self, estimated_tokens: int) -> Lease | None:
        """Reserve capacity on the next usable key, or None if every key is cooling or full."""
        now = self._clock.now()
        for state in self._order():
            if state.cooldown_until > now:
                continue
            reservation = state.limiter.try_reserve(estimated_tokens)
            if reservation is None:
                continue
            self._next = (self._states.index(state) + 1) % len(self._states)
            return Lease(state.key, state.fingerprint, reservation)
        return None

    def acquire_for(self, fp: str, estimated_tokens: int) -> Lease | None:
        """Reserve capacity on one particular key (the reasoning retry stays on its key)."""
        now = self._clock.now()
        for state in self._states:
            if state.fingerprint != fp:
                continue
            if state.cooldown_until > now:
                return None
            reservation = state.limiter.try_reserve(estimated_tokens)
            if reservation is None:
                return None
            return Lease(state.key, state.fingerprint, reservation)
        return None

    def all_cooling(self) -> bool:
        """True if every key is cooling down (after a failure or a 429), not merely full."""
        return self.size > 0 and len(self.cooling_fingerprints()) >= self.size

    def wait_time(self, estimated_tokens: int) -> float:
        """Seconds until some key could take a call of this size (0 if one could now)."""
        now = self._clock.now()
        waits = [
            max(max(0.0, s.cooldown_until - now), s.limiter.retry_in(estimated_tokens))
            for s in self._states
        ]
        return min(waits) if waits else 0.0

    def correct(self, lease: Lease, actual_tokens: int) -> None:
        for state in self._states:
            if state.fingerprint == lease.fingerprint:
                state.limiter.correct(lease.reservation, actual_tokens)

    def cool_down(self, fp: str, seconds: float | None = None) -> None:
        until = self._clock.now() + (self._default_cooldown if seconds is None else seconds)
        for state in self._states:
            if state.fingerprint == fp:
                state.cooldown_until = max(state.cooldown_until, until)

    def cooling_fingerprints(self) -> set[str]:
        """Fingerprints of the keys that are cooling down right now."""
        now = self._clock.now()
        return {s.fingerprint for s in self._states if s.cooldown_until > now}

    def status(self, estimated_tokens: int = 1) -> str:
        """'healthy' if some key could take a call now, else 'cooling_down'."""
        now = self._clock.now()
        for state in self._states:
            if state.cooldown_until <= now and state.limiter.retry_in(estimated_tokens) == 0.0:
                return "healthy"
        return "cooling_down"
