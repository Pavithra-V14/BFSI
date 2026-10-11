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
from collections.abc import Mapping
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


class CostGuard:
    def __init__(
        self,
        estimated_requests: int,
        estimated_tokens: int,
        *,
        margin: float = c.OVER_ESTIMATE_MARGIN,
        used_requests: int = 0,
        used_tokens: int = 0,
    ) -> None:
        self.estimated_requests = int(estimated_requests)
        self.estimated_tokens = int(estimated_tokens)
        self.margin = float(margin)
        self.requests = int(used_requests)
        self.tokens = int(used_tokens)
        # what this process used, by provider group (the daily limits are per group)
        self.usage: dict[str, list[int]] = {}

    @property
    def allowed_requests(self) -> int:
        return int(self.estimated_requests * (1.0 + self.margin))

    @property
    def allowed_tokens(self) -> int:
        return int(self.estimated_tokens * (1.0 + self.margin))

    def charge(self, *, requests: int = 0, tokens: int = 0, who: str = "answer") -> None:
        """Count a call (or a batch of them) for a provider group. Raises `OverEstimate` once past
        the margin."""
        requests, tokens = max(int(requests), 0), max(int(tokens), 0)
        self.requests += requests
        self.tokens += tokens
        row = self.usage.setdefault(who, [0, 0])
        row[0] += requests
        row[1] += tokens
        self.check()

    def check(self) -> None:
        if self.requests > self.allowed_requests:
            raise OverEstimate("requests", self.requests, self.allowed_requests)
        if self.tokens > self.allowed_tokens:
            raise OverEstimate("tokens", self.tokens, self.allowed_tokens)
