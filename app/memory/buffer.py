"""The history for the rewrite step (spec 8.2, 8.6): a running summary plus a verbatim buffer.

What feeds history (the poisoning rule, 8.6): every user question that passed the guardrails
(masked; a question flagged `history_excluded` is left out), and an assistant answer only when it is
grounded and its source has not been removed. Refusals, ungrounded answers and "source removed"
answers are left out. A whole kept answer line, label and clause references included, is clipped
to about 80 tokens, so the history carries cited facts and not free prose.

The verbatim buffer is rebuilt from the stored messages after the summary boundary, newest first,
within `buffer_tokens` ([T] 400). Turns that do not fit are the ones to fold into the summary, but
one summary call is given at most `fold_input_tokens` of them (the oldest first); the rest wait for
the next turn, so a long outage or a cleared summary can never build a prompt that is too big.
No session facts and no unclear-turn count are part of this text.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from app.memory.config import MemoryConfig
from app.memory.store import SessionState, StoredMessage
from app.memory.tokens import clip_to_tokens

USER_LABEL = "User"
ASSISTANT_LABEL = "Assistant"
MAX_CLAUSES_SHOWN = 5
MIN_TEXT_TOKENS = 8  # below this much room for the answer text, the whole line is clipped instead


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
    to_fold: list[Turn] = field(default_factory=list)  # every older eligible turn not in the buffer
    fold_batch: list[Turn] = field(default_factory=list)  # the oldest of them, within the cap
    boundary_id: str | None = None  # the new boundary once `fold_batch` has been folded in
    tokens: int = 0  # tokens of summary + recent

    @property
    def fold_ids(self) -> list[str]:
        return [turn.message_id for turn in self.fold_batch]


def eligible(message: StoredMessage) -> bool:
    """May this message feed history and the summary?"""
    if message.history_excluded:
        return False
    if message.role == "user":
        return True
    return message.grounded is True and not message.source_removed


def render_message(message: StoredMessage, config: MemoryConfig, counter: Any) -> str:
    if message.role == "user":
        return f"{USER_LABEL}: {' '.join(message.content.split())}"
    limit = config.answer_clip_tokens
    clauses: list[str] = []
    for citation in message.citations:
        clause = citation.get("clause")
        if isinstance(clause, str) and clause and clause not in clauses:
            clauses.append(clause)
    suffix = f" (clauses: {', '.join(clauses[:MAX_CLAUSES_SHOWN])})" if clauses else ""
    prefix = f"{ASSISTANT_LABEL}: "
    text = " ".join(message.content.split())
    room = limit - counter.count_text(prefix + suffix)
    if room >= MIN_TEXT_TOKENS:
        line = prefix + clip_to_tokens(text, room, counter) + suffix
    else:
        line = prefix + text + suffix
    if counter.count_text(line) > limit:  # the whole line, whatever the tokenizer made of it
        line = clip_to_tokens(line, limit, counter)
    return line


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
        # one call is given the oldest turns that fit the cap (at least one, clipped if it must)
        batch: list[Turn] = []
        spent = 0
        for turn in history.to_fold:
            if batch and spent + turn.tokens > config.fold_input_tokens:
                break
            if not batch and turn.tokens > config.fold_input_tokens:
                line = clip_to_tokens(turn.line, config.fold_input_tokens, counter)
                turn = replace(turn, line=line, tokens=counter.count_text(line))
            batch.append(turn)
            spent += turn.tokens
        history.fold_batch = batch
        # the boundary is the message just before the first turn that stays unfolded
        next_index = pool[len(batch)][0]
        history.boundary_id = messages[next_index - 1].id if next_index > 0 else None
    history.tokens = (counter.count_text(summary) if summary else 0) + (
        counter.count_text(history.recent) if history.recent else 0
    )
    return history
