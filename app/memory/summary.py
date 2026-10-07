"""Folding old turns into the running summary (spec 8.2, 8.6).

One decide-tier call per overflow, from masked text only, using `prompts/summary.v1.md`. The input
is the current summary plus the folded turns as the buffer renders them: masked user questions and
grounded answers clipped to about 80 tokens with their clause references (spec 8.6: "user
questions plus cited facts, not free model prose").

What comes back is untrusted model text. Before it is saved it is:
1. made plain text (HTML and links removed);
2. masked again (the summary is stored, so it follows the same rule as a stored message);
3. scanned with the injection patterns (the wider chunk list). On a hit the OLD summary is kept,
   the boundary does not move, and an alert event is emitted (pattern names only, never text);
4. clipped to the summary limit (150 tokens).
A failed call, an empty reply or a hit all mean the same: nothing changes, the turns stay out of
this turn's history, and the next turn tries again.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.gateway.errors import GatewayError
from app.guardrails.context import neutralise
from app.guardrails.injection import scan_chunk
from app.guardrails.masking import Masker
from app.guardrails.plain_text import to_plain_text
from app.guardrails.prompts import get_prompt
from app.memory.buffer import Turn
from app.memory.config import MemoryConfig
from app.memory.tokens import clip_to_tokens

Event = Callable[[str, dict[str, Any]], None]
SUMMARY_OUTPUT_ALLOWANCE = 60  # output room above the summary limit (formatting, a short lead-in)


@dataclass(frozen=True)
class FoldResult:
    ok: bool
    summary: str | None = None
    reason: str | None = None  # provider_failed | empty | injection_pattern


def assemble_summary(current_summary: str, turns: Sequence[Turn]) -> str:
    """The summary prompt: the static instructions, then the current summary, then the old turns
    (angle brackets neutralised like any text that is placed in a prompt)."""
    prompt = get_prompt("summary")
    old = "\n".join(turn.line for turn in turns)
    return (
        f"{prompt.render()}\n\nCURRENT SUMMARY\n{neutralise(current_summary) or '(none)'}"
        f"\n\nOLD TURNS\n{neutralise(old)}"
    )


async def fold_summary(
    gateway: Any,
    *,
    current_summary: str,
    turns: Sequence[Turn],
    config: MemoryConfig,
    counter: Any,
    masker: Masker,
    on_event: Event | None = None,
) -> FoldResult:
    def emit(name: str, **fields: Any) -> None:
        if on_event:
            on_event(name, fields)

    messages = [{"role": "user", "content": assemble_summary(current_summary, turns)}]
    try:
        result = await gateway.complete(
            messages,
            tier="decide",
            max_tokens=config.summary_tokens + SUMMARY_OUTPUT_ALLOWANCE,
            max_wait_s=0.0,
        )
    except GatewayError as error:
        emit("summary_fold_failed", reason=type(error).__name__)
        return FoldResult(False, None, "provider_failed")
    text = to_plain_text(getattr(result, "text", "") or "")
    text = masker.mask(text).text
    if not text.strip():
        return FoldResult(False, None, "empty")
    hits = scan_chunk(text)
    if hits:
        emit("summary_rejected_injection", patterns=list(hits))
        return FoldResult(False, None, "injection_pattern")
    return FoldResult(True, clip_to_tokens(text, config.summary_tokens, counter), None)
