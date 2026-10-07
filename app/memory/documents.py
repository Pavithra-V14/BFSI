"""Clearing summaries when a document is deleted (spec 8.4: "any summary built from it is cleared").

A summary is built from the turns folded into it, that is the messages up to
`summary_upto_message_id`. If any of those messages cites the deleted document, the session's
summary and boundary are cleared: the history is then rebuilt from the stored messages (the ones
flagged "source removed" no longer feed it, see `buffer.eligible`) and folded again when needed.
Sessions whose summary does not rest on the document are left alone.

It writes ONE audit row (`summary_clear`: the document id and how many sessions, never any text),
and none when no summary was cleared, so a repeated delete leaves no second row. The caller (the
document delete flow in `app.ingest.delete`) owns the transaction.
"""

from __future__ import annotations

import json
from datetime import datetime

from app.accounts.audit import write_audit
from app.db.connection import Database


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
        "AND m.seq <= (SELECT u.seq FROM messages u WHERE u.id = s.summary_upto_message_id)",
        (f'%"{doc_id}"%',),
    )
    return sorted({row[0] for row in rows if _cites(row[1], doc_id)})


def clear_summaries_for_document(db: Database, doc_id: str, *, actor: str, now: datetime) -> int:
    """Clear every summary that rests on the document; returns how many sessions. The caller
    commits."""
    sessions = sessions_built_from(db, doc_id)
    for session_id in sessions:
        db.execute(
            "UPDATE sessions SET summary_text = NULL, summary_upto_message_id = NULL WHERE id = ?",
            (session_id,),
        )
    if sessions:
        write_audit(
            db,
            actor=actor,
            action="summary_clear",
            target=doc_id,
            details={"sessions": len(sessions)},
            now=now,
        )
    return len(sessions)
