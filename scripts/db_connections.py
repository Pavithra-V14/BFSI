"""How many connections does the database hold, against its limit? OWNER-RUN, READ ONLY.

    uv run python scripts/db_connections.py

It reads `DATABASE_URL` from this window's settings (never a file), runs two read-only queries
and prints numbers only: the connections open now, their states, and the server's limit.

Why (spec 11.2, Phase 14 note): the API opens one database connection per chat request, so under
load the count follows the number of requests in flight. Run it while the deployed service is
being used (the load of Run 27 is NOT pointed at the deployed address; use a few browser tabs, or
the owner's own short real test) and compare the peak with the limit before deciding anything about
the pooled address. A SQLite address is refused: there is nothing to count.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
    database=None,
) -> int:
    env = os.environ if env is None else env
    if database is None:
        url = (env.get("DATABASE_URL") or "").strip()
        if not url:
            out("not started: DATABASE_URL is not set in this window")
            return 1
        if url.startswith("sqlite"):
            out("refused: this is a SQLite address; there is nothing to count")
            return 1
        from app.db.connection import connect

        try:
            database = connect(url)
        except Exception as error:  # a class name only: the message may hold the address
            out(f"could not connect ({type(error).__name__})")
            return 1
    try:
        limit = database.query_one("SHOW max_connections")
        states = database.query_all(
            "SELECT COALESCE(state, 'no state'), COUNT(*) FROM pg_stat_activity "
            "WHERE datname = current_database() GROUP BY 1 ORDER BY 1"
        )
    except Exception as error:
        out(f"the counts could not be read ({type(error).__name__})")
        return 1
    finally:
        try:
            database.close()
        except Exception:
            pass
    total = sum(int(n) for _s, n in states)
    maximum = int(limit[0]) if limit else None
    out(f"connections open now: {total}" + (f" of {maximum}" if maximum else ""))
    for state, count in states:
        out(f"  {state}: {count}")
    if maximum:
        out(f"share of the limit: {100 * total / maximum:.0f} %")
    return 0


if __name__ == "__main__":
    sys.exit(main())
