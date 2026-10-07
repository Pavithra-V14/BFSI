"""The memory sizes (spec 8.2). Starting values, tuned at the Phase 16 baseline.

Only the verbatim buffer size is an environment setting: the spec says to sweep it (200, 400, 800
and 1,200 tokens) on the multi-turn evaluation set and keep the smallest size where rewrite
accuracy stops improving. That set does not exist yet, so the sweep belongs to Phase 16.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

BUFFER_VARIABLE = "MEMORY_BUFFER_TOKENS"
BUFFER_RANGE = (50, 4000)
RETENTION_DAYS = 90  # spec 8.4: a decision, not a setting


class MemoryConfigError(ValueError):
    """A memory setting is not a whole number in its range. The message names the variable."""


@dataclass(frozen=True)
class MemoryConfig:
    buffer_tokens: int = 400  # [T] verbatim buffer; swept at the baseline run
    summary_tokens: int = 150  # spec 8.2: the summary is at most this long
    answer_clip_tokens: int = 80  # spec 8.2: each past assistant answer is clipped to this
    fold_input_tokens: int = 1500  # [T] most history one summary call may be given

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> MemoryConfig:
        raw = (env.get(BUFFER_VARIABLE) or "").strip()
        if not raw:
            return cls()
        low, high = BUFFER_RANGE
        try:
            value = int(raw)
        except ValueError:
            value = low - 1
        if not low <= value <= high:
            raise MemoryConfigError(
                f"{BUFFER_VARIABLE} must be a whole number from {low} to {high}"
            )
        return cls(buffer_tokens=value)
