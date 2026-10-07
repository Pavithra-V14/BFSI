"""A role change ends the user's old sessions (owner decision, spec 8.4 / 13.1).

Summaries, facts and history can rest on documents that only the OLD role could see. When a user's
role changes, every session of theirs is closed (it refuses new messages from then on; the user
starts a fresh one) and its summary, summary boundary and session facts are cleared. The messages
stay in the transcript. This runs inside the role-change transaction (`app.accounts.users.set_role`)
so the new role and the cleared sessions appear together or not at all.

It uses plain SQL on the session table only, so the account code can call it without importing the
rest of the memory package.
"""

from __future__ import annotations

from typing import Any


def reset_sessions_for_role_change(db: Any, user_id: str) -> int:
    """Close and clear every session of the user; returns how many sessions. The caller commits."""
    cursor = db.execute(
        "UPDATE sessions SET status = 'closed', summary_text = NULL, "
        "summary_upto_message_id = NULL, session_facts = '{}' WHERE user_id = ?",
        (user_id,),
    )
    return max(int(cursor.rowcount or 0), 0)
