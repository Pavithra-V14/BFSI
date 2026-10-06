"""Sliding-window limiter, per key (spec 5.2).

Requests per minute and tokens per minute, plus optional per-day caps (the free
plans of Groq and OpenRouter have daily limits). Tokens are reserved before a
call and corrected afterwards with the real usage.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.gateway.clock import Clock

MINUTE = 60.0
DAY = 86_400.0


@dataclass(frozen=True)
class Limits:
    """None means no limit."""

    rpm: int | None = None
    tpm: int | None = None
    rpd: int | None = None
    tpd: int | None = None


@dataclass
class Reservation:
    time: float
    tokens: int


class SlidingWindowLimiter:
    def __init__(self, limits: Limits, clock: Clock) -> None:
        self.limits = limits
        self._clock = clock
        self._events: list[Reservation] = []

    def _prune(self, now: float) -> None:
        self._events = [e for e in self._events if now - e.time < DAY]

    def _fits(self, now: float, tokens: int) -> bool:
        lim = self.limits
        minute = [e for e in self._events if now - e.time < MINUTE]
        if lim.rpm is not None and len(minute) + 1 > lim.rpm:
            return False
        if lim.tpm is not None and sum(e.tokens for e in minute) + tokens > lim.tpm:
            return False
        if lim.rpd is not None and len(self._events) + 1 > lim.rpd:
            return False
        if lim.tpd is not None and sum(e.tokens for e in self._events) + tokens > lim.tpd:
            return False
        return True

    def can_ever_fit(self, tokens: int) -> bool:
        """False if one request of this size can never pass, even when idle."""
        lim = self.limits
        if lim.tpm is not None and tokens > lim.tpm:
            return False
        return not (lim.tpd is not None and tokens > lim.tpd)

    def try_reserve(self, tokens: int) -> Reservation | None:
        now = self._clock.now()
        self._prune(now)
        if not self._fits(now, tokens):
            return None
        reservation = Reservation(now, tokens)
        self._events.append(reservation)
        return reservation

    def correct(self, reservation: Reservation, actual_tokens: int) -> None:
        reservation.tokens = max(0, actual_tokens)

    def retry_in(self, tokens: int) -> float:
        """Seconds until a request of this size would pass (0 if it would pass now)."""
        now = self._clock.now()
        self._prune(now)
        if self._fits(now, tokens):
            return 0.0
        saved = list(self._events)
        try:
            for oldest in sorted(saved, key=lambda e: e.time):
                self._events.remove(oldest)
                if self._fits(now, tokens):
                    window = MINUTE if now - oldest.time < MINUTE else DAY
                    return max(0.0, oldest.time + window - now)
        finally:
            self._events = saved
        return DAY
