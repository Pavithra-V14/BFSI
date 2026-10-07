"""The history for the rewrite step (spec 8.2, 8.6): a running summary plus a verbatim buffer.

What feeds history (the poisoning rule, 8.6): every user question (masked), and an assistant answer
only when it is grounded and its source has not been removed. Refusals, ungrounded answers and
"source removed" answers are left out. Each kept answer is clipped to about 80 tokens, with the
clause references of its citations, so the history carries cited facts and not free prose.

The verbatim buffer is rebuilt from the stored messages after the summary boundary, newest first,
within `buffer_tokens` ([T] 400). Turns that do not fit are the ones to fold into the summary.
No session facts and no unclear-turn count are part of this text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.memory.config import MemoryConfig
from app.memory.store import SessionState, StoredMessage
from app.memory.tokens import clip_to_tokens

USER_LABEL = "User"
ASSISTANT_LABEL = "Assistant"


@dataclass(frozen=True)
class Turn:
    message_id: str
    seq: int
    role: str
    line: str  # the rendered line, e.g. "User: ..."
    tokens: int


@dataclass
class History:
    summary: str = ""  # the running summary (clipped to the summary limit)
    recent: str = ""  # the verbatim buffer, oldest first, one line per message
    kept: list[Turn] = field(default_factory=list)
    to_fold: list[Turn] = field(default_factory=list)  # older eligible turns that did not fit
    boundary_id: str | None = None  # the new summary boundary once to_fold has been folded in
    tokens: int = 0  # tokens of summary + recent


def eligible(message: StoredMessage) -> bool:
    """May this message feed history and the summary?"""
    if message.role == "user":
        return True
    return message.grounded is True and not message.source_removed


def render_message(message: StoredMessage, config: MemoryConfig, counter: Any) -> str:
    if message.role == "user":
        return f"{USER_LABEL}: {' '.join(message.content.split())}"
    text = clip_to_tokens(message.content, config.answer_clip_tokens, counter)
    clauses = []
    for citation in message.citations:
        clause = citation.get("clause")
        if isinstance(clause, str) and clause and clause not in clauses:
            clauses.append(clause)
    suffix = f" (clauses: {', '.join(clauses)})" if clauses else ""
    return f"{ASSISTANT_LABEL}: {text}{suffix}"


def build_history(state: SessionState, config: MemoryConfig, counter: Any) -> History:
    summary = clip_to_tokens(state.summary_text or "", config.summary_tokens, counter)
    messages = state.messages
    pool: list[tuple[int, Turn]] = []  # (index in messages, turn) of every eligible message
    for index, message in enumerate(messages):
        if eligible(message):
            line = render_message(message, config, counter)
            pool.append(
                (
                    index,
                    Turn(message.id, message.seq, message.role, line, counter.count_text(line)),
                )
            )
    history = History(summary=summary)
    if not pool:
        history.tokens = counter.count_text(summary) if summary else 0
        return history

    # newest first, whole messages, within the buffer limit (the newest is always kept, clipped)
    start = len(pool)
    used = 0
    while start > 0:
        turn = pool[start - 1][1]
        if used + turn.tokens > config.buffer_tokens and start < len(pool):
            break
        used += turn.tokens
        start -= 1
    # do not begin the buffer with an answer whose question was left out, if the question fits
    if start > 0 and start < len(pool) and pool[start][1].role == "assistant":
        previous = pool[start - 1][1]
        if previous.role == "user" and used + previous.tokens <= config.buffer_tokens:
            used += previous.tokens
            start -= 1
        elif start + 1 < len(pool):  # never drop the newest message itself
            used -= pool[start][1].tokens
            start += 1
    kept = [turn for _, turn in pool[start:]]
    lines = [turn.line for turn in kept]
    if len(kept) == 1 and kept[0].tokens > config.buffer_tokens:
        lines = [clip_to_tokens(kept[0].line, config.buffer_tokens, counter)]
    history.kept = kept
    history.recent = "\n".join(lines)
    history.to_fold = [turn for _, turn in pool[:start]]
    if history.to_fold:
        first_kept_index = pool[start][0] if start < len(pool) else len(messages)
        history.boundary_id = messages[first_kept_index - 1].id if first_kept_index > 0 else None
    history.tokens = (counter.count_text(summary) if summary else 0) + (
        counter.count_text(history.recent) if history.recent else 0
    )
    return history
