"""One turn's history: load the session, fold what no longer fits, return what the rewrite needs.

`prepare_history` is what Phase 8 calls before the rewrite step of an existing session. It makes at
most one summary call (spec 8.2: one call per overflow), given at most `fold_input_tokens` of the
oldest turns that do not fit the buffer. No database transaction is open while the model is called
(`load_session` has finished and committed before it, `save_summary` opens its own afterwards).
When the call fails or its answer is refused, the old summary and boundary stay and the returned
history simply holds the buffer.
"""

from __future__ import annotations

from typing import Any

from app.db.connection import Database
from app.guardrails.masking import Masker
from app.memory.buffer import History, build_history
from app.memory.config import MemoryConfig
from app.memory.store import load_session, save_summary
from app.memory.summary import Event, fold_summary


async def prepare_history(
    db: Database,
    gateway: Any,
    *,
    user_id: str,
    session_id: str,
    config: MemoryConfig,
    counter: Any,
    masker: Masker,
    on_event: Event | None = None,
) -> History:
    state = load_session(db, user_id, session_id)
    history = build_history(state, config, counter)
    if not history.fold_batch or history.boundary_id is None:
        return history
    result = await fold_summary(
        gateway,
        current_summary=history.summary,
        turns=history.fold_batch,
        config=config,
        counter=counter,
        masker=masker,
        on_event=on_event,
    )
    if not result.ok or result.summary is None:
        return history
    # compare-and-set, and refused if a folded message was flagged "source removed" meanwhile: the
    # history below is then built from what is stored now
    save_summary(
        db,
        user_id,
        session_id,
        result.summary,
        history.boundary_id,
        expected_upto=state.summary_upto_message_id,
        folded_ids=history.fold_ids,
    )
    return build_history(load_session(db, user_id, session_id), config, counter)
