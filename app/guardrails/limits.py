"""Query limits (spec 4.1 step 2.1).

- At most 100 tokens, counted with the real tokenizer (the gateway's tiktoken counter). Over the
  limit means refuse with "please shorten"; the text is never truncated silently.
- At least 10 characters, but ONLY for the first message of a session (spec 4.3): a follow-up such
  as "and for seniors?" has no minimum beyond not being empty.
Both numbers are [T] starting values for the Phase 16 baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_QUERY_TOKENS = 100  # [T]
MIN_FIRST_MESSAGE_CHARACTERS = 10  # [T] first message of a session only


@dataclass(frozen=True)
class LimitResult:
    ok: bool
    reason: str | None  # None | "empty" | "too_short" | "too_long"
    tokens: int
    approximate: bool  # True when the tokenizer file was unavailable and the count is an estimate


def check_limits(
    text: str,
    *,
    first_message: bool,
    counter: Any,
    max_tokens: int = MAX_QUERY_TOKENS,
    min_characters: int = MIN_FIRST_MESSAGE_CHARACTERS,
) -> LimitResult:
    """`counter` has `count_text(text)` and `approximate` (app.gateway.tokens.TokenCounter)."""
    stripped = text.strip()
    if not stripped:
        return LimitResult(False, "empty", 0, False)
    tokens = counter.count_text(stripped)
    approximate = bool(getattr(counter, "approximate", False))
    if tokens > max_tokens:
        return LimitResult(False, "too_long", tokens, approximate)
    if first_message and len(stripped) < min_characters:
        return LimitResult(False, "too_short", tokens, approximate)
    return LimitResult(True, None, tokens, approximate)
