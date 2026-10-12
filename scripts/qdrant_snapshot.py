"""Qdrant snapshots of the documents collection (spec 11.3). OWNER-RUN, REAL SERVICE.

    uv run python scripts/qdrant_snapshot.py list                 # names, sizes and times
    uv run python scripts/qdrant_snapshot.py create --run         # make one snapshot on the server
    uv run python scripts/qdrant_snapshot.py download NAME --run  # copy it to backups/qdrant/
    uv run python scripts/qdrant_snapshot.py prune --keep 5 --run # delete older snapshots

Nothing that changes anything happens without `--run`. It reads `QDRANT_URL` and `QDRANT_API_KEY`
from this window's settings, never from a file, and prints names, sizes and counts only. It works
on the production collection `documents` only; an `_eval` collection is not backed up here.

If the free cluster refuses snapshots (the plan may not include them), the command says so plainly
and exits with 2: the documented backup is then a re-ingest from `originals/` (spec 11.3 and
docs/runbooks/restore-documents.md).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLLECTION = "documents"
BACKUP_DIR = ROOT / "backups" / "qdrant"
REFUSED_EXIT = 2
REFUSED_TEXT = (
    "the server refused snapshots (the plan may not include them): the backup is a re-ingest "
    "from originals/ (docs/runbooks/restore-documents.md)"
)


class SnapshotRefused(Exception):
    """The server does not offer snapshots to this plan or key."""


def _client(env: Mapping[str, str]) -> Any:
    from app.vector.documents import client_from_env

    return client_from_env(env)


def _describe(item: Any) -> tuple[str, int, str]:
    return (
        str(getattr(item, "name", "")),
        int(getattr(item, "size", 0) or 0),
        str(getattr(item, "creation_time", "") or ""),
    )


def list_snapshots(client: Any) -> list[tuple[str, int, str]]:
    return sorted((_describe(s) for s in client.list_snapshots(COLLECTION)), key=lambda x: x[2])


def _is_refusal(error: Exception) -> bool:
    name = type(error).__name__.lower()
    status = getattr(error, "status_code", None)
    return isinstance(error, SnapshotRefused) or status in (401, 403) or "forbidden" in name


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
    client: Any = None,
    folder: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Qdrant snapshots of the documents collection")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    create = commands.add_parser("create")
    create.add_argument("--run", action="store_true")
    download = commands.add_parser("download")
    download.add_argument("name")
    download.add_argument("--run", action="store_true")
    prune = commands.add_parser("prune")
    prune.add_argument("--keep", type=int, default=5)
    prune.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    from app.gateway.embeddings import running_on_render

    if running_on_render(env):
        out("refused: snapshots are taken from the admin machine, never on Render")
        return 1
    if client is None:
        for name in ("QDRANT_URL", "QDRANT_API_KEY"):
            if not (env.get(name) or "").strip():
                out(f"not started: {name} is not set in this window")
                return 1
        client = _client(env)
    target = folder or BACKUP_DIR
    try:
        if args.command == "list":
            items = list_snapshots(client)
            out(f"{len(items)} snapshot(s) of '{COLLECTION}'")
            for name, size, made in items:
                out(f"  {name}  {size} bytes  {made}")
            return 0
        if args.command == "create":
            if not args.run:
                out("nothing was created: add --run to make a snapshot")
                return 0
            made = client.create_snapshot(COLLECTION)
            name, size, when = _describe(made)
            out(f"created {name} ({size} bytes) {when}")
            return 0
        if args.command == "download":
            if not args.run:
                out(f"nothing was downloaded: add --run to copy {args.name} to {target}")
                return 0
            known = {n for n, _s, _t in list_snapshots(client)}
            if args.name not in known:
                out("no such snapshot: run `list` and use a name from it")
                return 1
            target.mkdir(parents=True, exist_ok=True)
            path = target / Path(args.name).name  # a bare file name: no folder can be chosen
            size = _download(env, args.name, path)
            out(f"saved {path.name} ({size} bytes) in {target}")
            return 0
        items = list_snapshots(client)
        keep = max(args.keep, 1)
        old = items[: max(len(items) - keep, 0)]
        out(f"{len(items)} snapshot(s); keeping the newest {keep}; {len(old)} would be deleted")
        if not args.run:
            out("nothing was deleted: add --run")
            return 0
        for name, _size, _made in old:
            client.delete_snapshot(COLLECTION, name)
            out(f"deleted {name}")
        return 0
    except Exception as error:  # a class name only: an error text may hold an address
        if _is_refusal(error):
            out(REFUSED_TEXT)
            return REFUSED_EXIT
        out(f"stopped ({type(error).__name__}); nothing is shown of the address or the key")
        return 1


def _download(env: Mapping[str, str], name: str, path: Path) -> int:
    """Stream the snapshot with the same key; returns its size in bytes."""
    import httpx

    base = (env.get("QDRANT_URL") or "").rstrip("/")
    key = env.get("QDRANT_API_KEY") or ""
    url = f"{base}/collections/{COLLECTION}/snapshots/{name}"
    size = 0
    with httpx.stream("GET", url, headers={"api-key": key}, timeout=300.0) as reply:
        if reply.status_code in (401, 403):
            raise SnapshotRefused
        reply.raise_for_status()
        with path.open("wb") as handle:
            for chunk in reply.iter_bytes():
                handle.write(chunk)
                size += len(chunk)
    return size


if __name__ == "__main__":
    sys.exit(main())
