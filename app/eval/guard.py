"""The hard cost guard of a run (owner addition a).

A run is confirmed against an estimate. The guard counts what the run really uses (requests and
tokens, from every model call the system under test and the judge make) and stops the run, by
raising `OverEstimate`, as soon as the total passes the estimate by more than the margin (25 %).
It also refuses to let a run start without the confirmation code of that exact estimate.

The numbers it holds are per run and are written back to the run row after every case, so a
resumed run (a later batch, another day) continues counting from where the run was.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from app.eval import constants as c
from app.eval.estimate import confirmation_code


class OverEstimate(BaseException):  # noqa: N818
    """The run used more than the confirmed estimate plus the margin. Numbers only.

    A `BaseException` on purpose: the evaluation libraries wrap model calls in broad
    `except Exception` handlers and retries, and a guard stop must never be swallowed by one."""

    def __init__(self, kind: str, used: int, allowed: int) -> None:
        super().__init__(f"{kind} {used} is over the allowed {allowed}")
        self.kind = kind
        self.used = used
        self.allowed = allowed


class NotConfirmed(Exception):
    """No code, or the wrong code, for this estimate: nothing is called."""


def check_confirmation(
    given: str | None, estimate: Mapping[str, Any], *, kind: str, dataset_version: str
) -> None:
    expected = confirmation_code(estimate, kind=kind, dataset_version=dataset_version)
    if not given or not hmac.compare_digest(given.strip().upper(), expected):
        raise NotConfirmed("the confirmation code does not match this estimate")


def confirmation_expired(
    created_at: Any, confirmed_at: Any, now: datetime, minutes: int = c.CONFIRMATION_MINUTES
) -> bool:
    """True when a run that was never confirmed is older than its code's window. A run that was
    confirmed once continues its later batches under that confirmation."""
    if confirmed_at is not None:
        return False
    made = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return now - made > timedelta(minutes=minutes)


class CostGuard:
    def __init__(
        self,
        estimated_requests: int,
        estimated_tokens: int,
        *,
        margin: float = c.OVER_ESTIMATE_MARGIN,
        used_requests: int = 0,
        used_tokens: int = 0,
        on_charge: Callable[[str, int, int], None] | None = None,
    ) -> None:
        self.estimated_requests = int(estimated_requests)
        self.estimated_tokens = int(estimated_tokens)
        self.margin = float(margin)
        self.requests = int(used_requests)
        self.tokens = int(used_tokens)
        # what this process used, by provider group (the daily limits are per group)
        self.usage: dict[str, list[int]] = {}
        # called for EVERY charge (group, requests, tokens), before the margin is checked: this is
        # where the run's own usage rows are written, so a crash loses at most the call in flight
        self.on_charge = on_charge

    @property
    def allowed_requests(self) -> int:
        return int(self.estimated_requests * (1.0 + self.margin))

    @property
    def allowed_tokens(self) -> int:
        return int(self.estimated_tokens * (1.0 + self.margin))

    def charge(
        self, *, requests: int = 0, tokens: int = 0, who: str = "answer", defer: bool = False
    ) -> None:
        """Count a call for a provider group, at the moment it is made. Raises `OverEstimate` once
        past the margin, unless `defer`: the system under test is being run and must not be cut in
        the middle of a turn, so its calls are counted at once and the stop is `check()`ed at the
        end of the turn."""
        requests, tokens = max(int(requests), 0), max(int(tokens), 0)
        self.requests += requests
        self.tokens += tokens
        row = self.usage.setdefault(who, [0, 0])
        row[0] += requests
        row[1] += tokens
        if self.on_charge is not None and (requests or tokens):
            self.on_charge(who, requests, tokens)
        if not defer:
            self.check()

    def check(self) -> None:
        if self.requests > self.allowed_requests:
            raise OverEstimate("requests", self.requests, self.allowed_requests)
        if self.tokens > self.allowed_tokens:
            raise OverEstimate("tokens", self.tokens, self.allowed_tokens)
