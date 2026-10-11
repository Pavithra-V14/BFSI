"""Decider statistics for the 'decider in use' dashboard panel (spec 5.3).

Keeps recent decisions in memory so the share per decider over the last hour and
day can be read. The dashboard itself is built in a later phase.
"""

from __future__ import annotations

from collections import Counter, deque

from app.gateway.clock import Clock

HOUR = 3_600.0
DAY = 86_400.0


class DeciderStats:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._records: deque[tuple[float, str, str]] = deque()

    def record(self, step: str, decider: str) -> None:
        now = self._clock.now()
        self._records.append((now, step, decider))
        while self._records and now - self._records[0][0] > DAY:
            self._records.popleft()

    def steps(self) -> list[str]:
        """The steps that have a decision on record (last day)."""
        return sorted({name for _, name, _ in self._records})

    def shares(self, step: str, window_seconds: float = HOUR) -> dict[str, float]:
        now = self._clock.now()
        counts = Counter(
            decider
            for at, name, decider in self._records
            if name == step and now - at <= window_seconds
        )
        total = sum(counts.values())
        return {decider: count / total for decider, count in counts.items()} if total else {}
