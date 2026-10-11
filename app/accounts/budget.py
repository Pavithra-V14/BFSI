"""Per-user request rate and daily token budget (spec 4.1 step 1, 5.4, 13.2).

The mechanism lives here and is ready for the callers: the chat endpoint (Phase 10) and the
pipeline (Phase 8) check the budget before any model call and charge the tokens afterwards.

- Rate limit: 20 requests a minute per user [T]. Applied to every signed-in request.
- Budget: customers 50,000 tokens a day (spec 5.4); staff and admin 200,000 [T]. The day is the
  UTC day, so it resets at 00:00 UTC.

If Upstash cannot be reached (or is not configured) the request is allowed and an alert event is
emitted: spec 11.1 says to answer normally when the cache is down, and the rate limiter was
decided to fail open (Phase 3). Login throttling is different (app/accounts/throttle.py).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.api.settings import ApiSettings
from app.cache.store import CacheStore, RateDecision
from app.observability.logs import fx

log = logging.getLogger("app.accounts")

REQUESTS_PER_MINUTE = 20  # [T]
RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    used: int | None  # None when the counter was unavailable
    budget: int
    available: bool = True


def utc_now() -> datetime:
    return datetime.now(UTC)


class UsageLimits:
    def __init__(
        self,
        cache: CacheStore | None,
        settings: ApiSettings,
        *,
        now: Callable[[], datetime] = utc_now,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._cache = cache
        self._settings = settings
        self._now = now
        self._on_event = on_event

    def _alert(self, name: str, **fields: Any) -> None:
        log.warning("accounts event %s", name, extra=fx(fields))
        if self._on_event:
            try:
                self._on_event(name, fields)
            except Exception:  # an alert hook that fails must never break a request
                log.error("accounts event handler failed for %s", name)

    def check_rate(self, user_id: str) -> RateDecision:
        """Count one request for this user. Allowed when the counter is unavailable."""
        if self._cache is None:
            self._alert("rate_limit_unavailable", scope="user")
            return RateDecision(True, None, REQUESTS_PER_MINUTE, None, available=False)
        return self._cache.check_rate_limit(
            "user", user_id, limit=REQUESTS_PER_MINUTE, window_seconds=RATE_WINDOW_SECONDS
        )

    def _day_counter(self, user_id: str) -> tuple[str, int]:
        """The counter name for today (UTC) and the seconds until 00:00 UTC."""
        now = self._now().astimezone(UTC)
        midnight = datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)
        return f"tokens:{user_id}:{now:%Y%m%d}", max(1, int((midnight - now).total_seconds()) + 1)

    def check_budget(self, user_id: str, role: str, estimated_tokens: int = 0) -> BudgetDecision:
        """Is there budget left today for a request of about `estimated_tokens`? Call this before
        any model call: an over-budget request is refused before it reaches a model."""
        budget = self._settings.daily_budget(role)
        name, _ = self._day_counter(user_id)
        used = self._cache.read_counter(name) if self._cache is not None else None
        if used is None:
            self._alert("budget_unavailable", scope="daily_tokens")
            return BudgetDecision(True, None, budget, available=False)
        return BudgetDecision(used + max(estimated_tokens, 1) <= budget, used, budget)

    def charge(self, user_id: str, tokens: int) -> int | None:
        """Add the tokens a request really used to today's total. Returns the new total, or None
        if the counter was unavailable (the use is then not counted: an accepted gap)."""
        if tokens <= 0:
            return None
        if self._cache is None:
            self._alert("budget_unavailable", scope="daily_tokens")
            return None
        name, seconds = self._day_counter(user_id)
        total = self._cache.incr_counter(name, seconds, tokens)
        if total is None:
            self._alert("budget_unavailable", scope="daily_tokens")
        return total
