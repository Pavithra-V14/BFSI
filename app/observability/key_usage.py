"""The saved daily counts of each API key (migration 0011; the admin key panel).

The gateway counts every provider call per key in memory (`KeyPool.note_call`). The rollup writer
adds those counts here every few minutes, so a restart does not lose today's numbers. A key is only
ever a fingerprint (eight hex characters of its SHA-256): the rows hold no key and no text.
"""

from __future__ import annotations

import uuid

from app.db.connection import Database

Row = tuple[str, str, str, int, int]  # (day, pool, fingerprint, calls, tokens)


def add_usage(db: Database, rows: list[Row]) -> int:
    """Add the increments to the day's rows (one statement per row; a row is made on first use)."""
    with db.transaction(write=True):
        for day, pool, fingerprint, calls, tokens in rows:
            db.execute(
                "INSERT INTO key_usage_daily (id, day, provider, fingerprint, calls, tokens) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (day, provider, fingerprint) DO UPDATE SET "
                "calls = key_usage_daily.calls + excluded.calls, "
                "tokens = key_usage_daily.tokens + excluded.tokens",
                (str(uuid.uuid4()), day, pool, fingerprint, int(calls), int(tokens)),
            )
    return len(rows)


def load_day(db: Database, day: str) -> dict[str, dict[str, tuple[int, int]]]:
    """The saved counts of one UTC day: pool -> fingerprint -> (calls, tokens)."""
    with db.transaction():
        found = db.query_all(
            "SELECT provider, fingerprint, calls, tokens FROM key_usage_daily WHERE day = ?",
            (day,),
        )
    out: dict[str, dict[str, tuple[int, int]]] = {}
    for provider, fingerprint, calls, tokens in found:
        out.setdefault(provider, {})[fingerprint] = (int(calls), int(tokens))
    return out
