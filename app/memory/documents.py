"""Clearing session memory when a document is deleted (spec 8.4: "any summary built from it is
cleared"; owner decision for the facts).

- A summary is built from the turns folded into it, that is the messages up to
  `summary_upto_message_id`. If any of those messages cites the deleted document, the session's
  summary and boundary are cleared: the history is then rebuilt from the stored messages (the ones
  flagged "source removed" no longer feed it, see `buffer.eligible`) and folded again when needed.
  Sessions whose summary does not rest on the document are left alone. The boundary subselect is
  bound to the session, so one session's boundary is never read for another.
- Session facts: only `entities` are touched. Each entity carries the ids of the documents that
  approve it; the deleted document's id is removed from them and an entity left with none is
  dropped. `product_line` and `topic` are the customer's own subject, not a document's content, and
  stay.

It writes ONE audit row (`memory_clear`: the document id and two counts, never any text), and none
when nothing was cleared, so a repeated delete leaves no second row. The caller (the document
delete flow in `app.ingest.delete`) owns the transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from app.accounts.audit import write_audit
from app.db.connection import Database
from app.memory.facts import clean_entities
from app.memory.store import parse_facts, require_aware


@dataclass(frozen=True)
class MemoryCleared:
    summaries: int = 0  # sessions whose summary was cleared
    entities: int = 0  # entities removed from session facts


def _cites(citations_text: str, doc_id: str) -> bool:
    try:
        citations = json.loads(citations_text)
    except (TypeError, ValueError):
        return False
    return isinstance(citations, list) and any(
        isinstance(item, dict) and item.get("doc_id") == doc_id for item in citations
    )


def sessions_built_from(db: Database, doc_id: str) -> list[str]:
    rows = db.query_all(
        "SELECT m.session_id, m.citations FROM messages m "
        "JOIN sessions s ON s.id = m.session_id "
        "WHERE s.summary_upto_message_id IS NOT NULL AND m.citations LIKE ? "
        "AND m.seq <= (SELECT u.seq FROM messages u "
        "WHERE u.id = s.summary_upto_message_id AND u.session_id = s.id)",
        (f'%"{doc_id}"%',),
    )
    return sorted({row[0] for row in rows if _cites(row[1], doc_id)})


def clear_entities_for_document(db: Database, doc_id: str) -> int:
    """Remove the document from the entities of every session's facts. Returns how many entities
    were removed (an entity also approved by another document stays, without this one)."""
    rows = db.query_all(
        "SELECT id, session_facts FROM sessions WHERE session_facts LIKE ?", (f'%"{doc_id}"%',)
    )
    removed = 0
    for session_id, facts_text in rows:
        facts = parse_facts(facts_text)
        before = clean_entities(facts.get("entities"))
        after = []
        for entity in before:
            remaining = [d for d in entity["doc_ids"] if d != doc_id]
            if remaining:
                after.append({"name": entity["name"], "doc_ids": remaining})
        if after == before:
            continue
        removed += len(before) - len(after)
        if after:
            facts["entities"] = after
        else:
            facts.pop("entities", None)
        db.execute(
            "UPDATE sessions SET session_facts = ? WHERE id = ?",
            (json.dumps(facts, sort_keys=True), session_id),
        )
    return removed


def clear_memory_for_document(
    db: Database, doc_id: str, *, actor: str, now: datetime
) -> MemoryCleared:
    """Clear the summaries and the entities that rest on the document. The caller commits."""
    require_aware(now)
    sessions = sessions_built_from(db, doc_id)
    for session_id in sessions:
        db.execute(
            "UPDATE sessions SET summary_text = NULL, summary_upto_message_id = NULL WHERE id = ?",
            (session_id,),
        )
    entities = clear_entities_for_document(db, doc_id)
    cleared = MemoryCleared(len(sessions), entities)
    if cleared.summaries or cleared.entities:
        write_audit(
            db,
            actor=actor,
            action="memory_clear",
            target=doc_id,
            details={"summaries": cleared.summaries, "entities": cleared.entities},
            now=now,
        )
    return cleared
