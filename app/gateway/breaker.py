"""Circuit breaker per provider and tier (spec 5.2).

It trips only when a provider's whole key pool is limited or failing, not on a
single bad key. After the open period one probe call is allowed.

`acquire()` hands out a Permit. Only the call that holds the current probe permit can
release the probe flag; any other call finishing (even neutrally) leaves it alone, so a
second probe cannot be admitted by accident.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.gateway.clock import Clock

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


@dataclass(frozen=True)
class Permit:
    is_probe: bool
    epoch: int  # the breaker's epoch when this permit was handed out


class CircuitBreaker:
    def __init__(self, clock: Clock, *, open_seconds: float = 30.0) -> None:
        self._clock = clock
        self._open_seconds = open_seconds
        self._opened_at: float | None = None
        self._probing = False
        self._epoch = 0  # changes whenever the breaker opens or closes: old permits go stale

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return CLOSED
        if self._clock.now() - self._opened_at >= self._open_seconds:
            return HALF_OPEN
        return OPEN

    def acquire(self) -> Permit | None:
        """A permit to make one call, or None while the breaker is open or a probe is running."""
        state = self.state
        if state == CLOSED:
            return Permit(False, self._epoch)
        if state == HALF_OPEN and not self._probing:
            self._probing = True
            return Permit(True, self._epoch)
        return None

    def allow(self) -> bool:
        return self.acquire() is not None

    def record_success(self) -> None:
        self._opened_at = None
        self._probing = False
        self._epoch += 1

    def record_neutral(self, permit: Permit | None = None) -> None:
        """The call ended for a reason that says nothing about the provider's health.

        Only the holder of the current probe permit releases the probe.
        """
        if permit is not None and permit.is_probe and permit.epoch == self._epoch:
            self._probing = False

    def record_failure(self) -> None:
        self._opened_at = self._clock.now()
        self._probing = False
        self._epoch += 1
