"""Audit rows (spec 12.1, 14): who, what, when, target, details. Append only: this module only
ever inserts (the 30-day clean-up in Phase 12 is the one thing that deletes).

No secret and no typed username may reach a row. A name that is not a known account is stored
as a keyed hash (HMAC-SHA256 with AUDIT_HASH_KEY), never as typed, because people sometimes type
their password into the username box. `details` keys that look like secrets are refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from typing import Any

from app.accounts.tokens import utc_text
from app.db.connection import Database

_FORBIDDEN_WORDS = ("password", "passwd", "token", "secret", "hash", "key", "authorization")


def keyed_hash(key: str, purpose: str, value: str) -> str:
    """A keyed hash of a value for one purpose ("audit-username", "throttle-ip" ...). Different
    purposes give different hashes of the same value, and nothing can be rebuilt without the key."""
    message = f"{purpose}\x00{value}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def write_audit(
    db: Database,
    *,
    actor: str,
    action: str,
    target: str | None,
    details: dict[str, Any] | None,
    now: datetime,
) -> str:
    """Insert one audit row and return its id. The caller commits."""
    details = details or {}
    for name in details:
        if any(word in name.lower() for word in _FORBIDDEN_WORDS):
            raise ValueError("audit details must not carry secrets")
    row_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO audit_log (id, actor, action, target, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (row_id, actor, action, target, json.dumps(details, sort_keys=True), utc_text(now)),
    )
    return row_id
