"""Login, registration and request throttling (spec 13.1, 13.2 "unbounded consumption").

Counters live in Upstash (app/cache/store.py). Names and addresses never go into a counter key:
the subject is a keyed hash (AUDIT_HASH_KEY, a different "purpose" for each counter). Every
login attempt counts, successful or not.

If Upstash cannot be reached, throttling does NOT switch off: a weaker counter held in this
process takes over (one instance on the free host, so it is a fair stand-in). It forgets on a
restart and is capped in size (oldest entries are dropped first). A throttle that fails open is
a free brute-force window, so this is the safer choice for a security control.

The values are [T] placeholders: 10 login attempts per username and 30 per client address in 15
minutes, 5 registrations per client address per hour.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.accounts.audit import keyed_hash
from app.cache.store import CacheStore

LOGIN_PER_USERNAME = 10  # [T]
LOGIN_PER_IP = 30  # [T]
LOGIN_WINDOW_SECONDS = 15 * 60  # [T]
REGISTER_PER_IP = 5  # [T]
REGISTER_WINDOW_SECONDS = 60 * 60  # [T]
IN_PROCESS_MAX_ENTRIES = 10_000


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after: int | None = None  # seconds until the window ends, when blocked
    scope: str | None = None  # which limit tripped: "ip" or "username"


class InProcessCounters:
    """Fixed-window counters in memory: the stand-in when Upstash is down."""

    def __init__(
        self, clock: Callable[[], float] = time.monotonic, max_entries: int = IN_PROCESS_MAX_ENTRIES
    ) -> None:
        self._clock = clock
        self._max = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, float]] = {}  # key -> (count, window ends at)

    def hit(self, key: str, limit: int, window_seconds: int) -> ThrottleDecision:
        now = self._clock()
        with self._lock:
            count, ends = self._entries.get(key, (0, 0.0))
            if now >= ends:
                count, ends = 0, now + window_seconds
            count += 1
            self._entries.pop(key, None)  # re-insert last, so the oldest are dropped first
            self._entries[key] = (count, ends)
            if len(self._entries) > self._max:
                for old in [k for k, (_, e) in self._entries.items() if now >= e]:
                    del self._entries[old]
                while len(self._entries) > self._max:
                    del self._entries[next(iter(self._entries))]
        if count <= limit:
            return ThrottleDecision(True)
        return ThrottleDecision(False, max(1, int(ends - now) + 1))


class Throttle:
    def __init__(
        self,
        cache: CacheStore | None,
        hash_key: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache = cache
        self._key = hash_key
        self._fallback = InProcessCounters(clock)

    def _subject(self, purpose: str, value: str) -> str:
        return keyed_hash(self._key, purpose, value)

    def _hit(
        self, scope: str, purpose: str, value: str, limit: int, window: int
    ) -> ThrottleDecision:
        subject = self._subject(purpose, value)
        if self._cache is not None:
            decision = self._cache.check_rate_limit(
                scope, subject, limit=limit, window_seconds=window
            )
            if decision.available:
                if decision.allowed:
                    return ThrottleDecision(True)
                return ThrottleDecision(False, decision.retry_after or window)
        return self._fallback.hit(f"{scope}:{subject}", limit, window)

    def check_login(self, username: str, client_ip: str) -> ThrottleDecision:
        """Count one login attempt for this username and this address. Blocked if either is over.

        The address is counted first. Once it is blocked the username is not counted at all, so a
        blocked flood with a new random username on every request creates no new counter anywhere
        (not in Upstash, not in this process).
        """
        by_ip = self._hit("login_ip", "throttle-ip", client_ip, LOGIN_PER_IP, LOGIN_WINDOW_SECONDS)
        if not by_ip.allowed:
            return ThrottleDecision(False, by_ip.retry_after or LOGIN_WINDOW_SECONDS, "ip")
        by_name = self._hit(
            "login_user", "throttle-username", username, LOGIN_PER_USERNAME, LOGIN_WINDOW_SECONDS
        )
        if not by_name.allowed:
            return ThrottleDecision(False, by_name.retry_after or LOGIN_WINDOW_SECONDS, "username")
        return ThrottleDecision(True)

    def check_register(self, client_ip: str) -> ThrottleDecision:
        return self._hit(
            "register_ip", "throttle-ip", client_ip, REGISTER_PER_IP, REGISTER_WINDOW_SECONDS
        )
