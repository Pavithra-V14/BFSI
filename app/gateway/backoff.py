"""Exponential backoff with jitter (spec 5.2 [P]: base 0.5 s, max 4 s, 3 attempts)."""

from __future__ import annotations

import random
from collections.abc import Callable

BASE_SECONDS = 0.5
MAX_SECONDS = 4.0
MAX_ATTEMPTS = 3


def backoff_delay(
    attempt: int,
    *,
    base: float = BASE_SECONDS,
    cap: float = MAX_SECONDS,
    rng: Callable[[], float] = random.random,
) -> float:
    """Delay before retry number `attempt` (0, 1, 2, ...): 0.5, 1, 2 ... capped at `cap`.

    Equal jitter: the result is between half of the exponential step and all of it.
    """
    step = min(cap, base * (2**attempt))
    return step / 2 + rng() * step / 2
