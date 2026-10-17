"""The maintenance command line (the admin machine only; never Render).

    uv run python -m app.maintenance purge --dry-run    show what would be erased; change nothing
    uv run python -m app.maintenance purge              ask to type the phrase, then erase

`purge` erases the data of the system (see `app.maintenance.purge` for exactly what and what is
kept). It reads DATABASE_URL, QDRANT_URL, QDRANT_API_KEY and UPSTASH_REDIS_URL from the shell, like
every other command here. It always works on the production collection `documents`: an evaluation
collection is never touched. The output holds names and counts only.

`--confirm-phrase "DELETE ALL DATA"` gives the confirmation up front (the admin page uses it, with
no keyboard to answer a prompt); without it the phrase is asked for.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.maintenance import purge as purge_module
from app.maintenance.purge import ACTOR_PATTERN, KEPT, NOT_TOUCHED, PHRASE, WARNING


class MaintenanceError(Exception):
    """A fixable problem; the message is safe to print (names and numbers only)."""


def os_actor() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = "unknown"
    return "cli:" + (re.sub(r"[^A-Za-z0-9._-]", "_", name)[:40] or "unknown")


def build_services(env: Mapping[str, str]) -> dict[str, Any]:
    """The real stores, from the shell. Only the owner's own run reaches this."""
    from app.cache.store import store_from_env
    from app.db.connection import connect
    from app.vector.documents import COLLECTION, client_from_env

    missing = [name for name in ("DATABASE_URL", "QDRANT_URL") if not (env.get(name) or "").strip()]
    if missing:
        raise MaintenanceError("these settings are not set: " + ", ".join(missing))
    cache = store_from_env(env) if (env.get("UPSTASH_REDIS_URL") or "").strip() else None
    db = connect((env.get("DATABASE_URL") or "").strip())
    return {
        "db": db,
        "qdrant": client_from_env(env),
        "collection": COLLECTION,
        "cache": cache,
        "close": db.close,
    }


def _print_plan(counts: Mapping[str, int], out: Callable[[str], None]) -> None:
    labels = (
        ("chunks", "document chunks (vectors)"),
        ("chats", "chats"),
        ("messages", "messages"),
        ("feedback", "thumbs"),
        ("users", "customer and staff accounts"),
        ("review_items", "review-queue items"),
        ("alert_rows", "alert rows"),
        ("metric_rows", "metric rows"),
        ("counter_rows", "per-key call count rows"),
    )
    out("would be erased:")
    for key, label in labels:
        out(f"  {counts.get(key, 0):>8}  {label}")
    out("  and every cached answer")
    out("kept: " + "; ".join(KEPT) + f" ({counts.get('admins_kept', 0)} admin account(s))")
    out("not touched: " + ", ".join(NOT_TOUCHED))


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    services_factory: Callable[[Mapping[str, str]], dict[str, Any]] = build_services,
    confirm: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda text: print(text, file=sys.stderr),
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.maintenance", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    purge = commands.add_parser("purge", help="erase the system's data (asks you to type a phrase)")
    purge.add_argument("--dry-run", action="store_true", help="show the counts; change nothing")
    purge.add_argument("--confirm-phrase", help=f'the phrase "{PHRASE}", given up front')
    purge.add_argument("--actor", help="who asked, for the audit row (default: your login name)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    from app.gateway.embeddings import running_on_render

    try:
        if running_on_render(env):
            raise MaintenanceError("this runs only on the admin machine, never on Render")
        actor = args.actor or os_actor()
        if not ACTOR_PATTERN.match(actor):
            raise MaintenanceError("--actor must be letters, digits and : . _ - only")
        services = services_factory(env)
        try:
            counts = purge_module.plan(services["db"], services["qdrant"], services["collection"])
            _print_plan(counts, out)
            if args.dry_run:
                out("dry run: nothing was changed")
                return 0
            out(WARNING)
            given = (
                args.confirm_phrase
                if args.confirm_phrase is not None
                else confirm(f"Type {PHRASE} to confirm: ")
            )
            if given.strip() != PHRASE:
                out("not confirmed; nothing was erased")
                return 1
            result = purge_module.purge(
                services["db"],
                services["qdrant"],
                services["collection"],
                services["cache"],
                actor=actor,
                now=now(),
                on_step=out,
            )
        finally:
            services["close"]()
    except KeyboardInterrupt:
        err("interrupted: run the same command again to finish it")
        return 130
    except MaintenanceError as error:
        err(f"error: {error}")
        return 1
    except Exception as error:  # class names only: a store's message can hold an address
        err(
            f"error: the command stopped ({type(error).__name__}); no key, address or text is shown"
        )
        return 1
    erased = ", ".join(f"{value} {key}" for key, value in sorted(result.counts.items()))
    out("erased: " + erased)
    if result.incomplete:
        for line in result.incomplete:
            err(f"  {line}")
        err("purge incomplete: run the same command again to finish it")
        return 1
    out("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
