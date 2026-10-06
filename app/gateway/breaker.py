"""Circuit breaker per provider and tier (spec 5.2).

It trips only when a provider's whole key pool is limited or failing, not on a
single bad key. After the open period one probe call is allowed.
"""

from __future__ import annotations

from app.gateway.clock import Clock

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, clock: Clock, *, open_seconds: float = 30.0) -> None:
        self._clock = clock
        self._open_seconds = open_seconds
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return CLOSED
        if self._clock.now() - self._opened_at >= self._open_seconds:
            return HALF_OPEN
        return OPEN

    def allow(self) -> bool:
        state = self.state
        if state == CLOSED:
            return True
        if state == HALF_OPEN and not self._probing:
            self._probing = True
            return True
        return False

    def record_success(self) -> None:
        self._opened_at = None
        self._probing = False

    def record_neutral(self) -> None:
        """The call ended for a reason that says nothing about the provider's health."""
        self._probing = False

    def record_failure(self) -> None:
        self._opened_at = self._clock.now()
        self._probing = False
