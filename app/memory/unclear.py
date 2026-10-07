"""Consecutive unclear turns (spec 4.3): "Two unclear turns in a row end with a fixed message and a
flag in the review queue."

The count lives in `session_facts["unclear_turns"]` (owner decision) and is never passed to a model
(`facts.facts_for_prompt` drops it). A clear turn resets it. The second unclear turn in a row:
- returns the fixed message (`messages.UNCLEAR_END`),
- writes ONE `two_unclear_turns` row in the review queue (the session id and a count; no text),
- and starts counting again from zero.
All of it in one transaction, so the count and the review row never disagree. A closed session
refuses (the same error as a missing one).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.accounts.tokens import utc_text
from app.db.connection import Database
from app.memory.messages import UNCLEAR_END
from app.memory.store import owned_session, parse_facts, require_aware

LIMIT = 2  # unclear turns in a row that end the exchange (spec 4.3)
KEY = "unclear_turns"


@dataclass(frozen=True)
class UnclearOutcome:
    count: int  # the count after this turn (0 after an ending or a clear turn)
    ended: bool  # True on the turn that reached the limit
    message: str | None  # the fixed ending, when ended


def record_turn(
    db: Database, user_id: str, session_id: str, *, unclear: bool, now: datetime
) -> UnclearOutcome:
    stamp = utc_text(require_aware(now))
    with db.transaction(write=True):
        row = owned_session(db, user_id, session_id, lock=True, writable=True)
        facts = parse_facts(row[7])
        previous = facts.get(KEY, 0)
        previous = previous if isinstance(previous, int) and previous >= 0 else 0
        count = previous + 1 if unclear else 0
        ended = count >= LIMIT
        if ended:
            count = 0
            db.execute(
                "INSERT INTO review_queue (id, kind, status, session_id, details, created_at) "
                "VALUES (?, 'two_unclear_turns', 'open', ?, ?, ?)",
                (str(uuid.uuid4()), session_id, json.dumps({"unclear_turns": LIMIT}), stamp),
            )
        if count:
            facts[KEY] = count
        else:
            facts.pop(KEY, None)
        db.execute(
            "UPDATE sessions SET session_facts = ? WHERE id = ? AND user_id = ?",
            (json.dumps(facts, sort_keys=True), session_id, user_id),
        )
    return UnclearOutcome(count, ended, UNCLEAR_END if ended else None)
