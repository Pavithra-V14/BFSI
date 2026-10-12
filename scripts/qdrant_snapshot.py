"""Qdrant snapshots of the documents collection (spec 11.3). OWNER-RUN, REAL SERVICE.

    uv run python scripts/qdrant_snapshot.py list                 # names, sizes and times
    uv run python scripts/qdrant_snapshot.py create --run         # make one snapshot on the server
    uv run python scripts/qdrant_snapshot.py download NAME --run  # copy it to backups/qdrant/
    uv run python scripts/qdrant_snapshot.py download --newest --run   # the newest one
    uv run python scripts/qdrant_snapshot.py prune --keep 5 --run # delete older snapshots
    uv run python scripts/qdrant_snapshot.py restore-test NAME --run  # prove a copy restores

The weekly task (docs/DEPLOY.md 9) runs `create --run`, then `download --newest --run`, then
`prune --keep 5 --run`, in that order: prune deletes nothing that has no downloaded copy.

Nothing that changes anything happens without `--run`. It reads `QDRANT_URL` and `QDRANT_API_KEY`
from this window's settings, never from a file, and prints names, sizes and counts only. It works
on the production collection `documents` only; an `_eval` collection is not backed up here.

Safety rules, in code:
- `prune` orders by the server's creation time and NEVER deletes a snapshot whose time it cannot
  read, never the newest `--keep` (at least one), and never one that has no downloaded copy of the
  same size in `backups/qdrant/`: take the copy first (`download`);
- `download` writes to `NAME.part` and renames it only when the size (and the server's checksum,
  when it gives one) match; a failed download leaves nothing behind;
- `restore-test` uploads a DOWNLOADED copy into the throw-away collection
  `documents_restore_test`, counts its points next to the live collection's, and removes it. It
  refuses when that collection already exists (it never touches a collection it did not make) and
  it never writes to `documents`.

Exit codes: 0 done, 1 stopped, 2 the cluster does not offer snapshots (the backup is then a
re-ingest from `originals/`: docs/runbooks/restore-documents.md), 3 the key was rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLLECTION = "documents"
RESTORE_TEST_COLLECTION = "documents_restore_test"  # made and removed by `restore-test` only
BACKUP_DIR = ROOT / "backups" / "qdrant"
UNSUPPORTED_EXIT = 2
KEY_REJECTED_EXIT = 3
UNSUPPORTED_STATUSES = (404, 405, 501)  # the cluster has no such route: no snapshots on this plan
KEY_STATUSES = (401, 403)  # the key was not accepted, or may not make snapshots
UNSUPPORTED_TEXT = (
    "the cluster does not offer snapshots (the plan may not include them): the backup is a "
    "re-ingest from originals/ (docs/runbooks/restore-documents.md)"
)
KEY_TEXT = (
    "the key was rejected (QDRANT_API_KEY): check the key and that it may make snapshots; "
    "this is not the same as a plan without snapshots"
)


class KeyRejected(Exception):
    """HTTP 401 or 403: the key was not accepted or has no right to snapshots."""


class Unsupported(Exception):
    """The cluster has no snapshot route."""


@dataclass(frozen=True)
class Snap:
    name: str
    size: int
    made: datetime | None  # None: the server gave no time we can read
    checksum: str | None


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _describe(item: Any) -> Snap:
    checksum = getattr(item, "checksum", None)
    return Snap(
        name=str(getattr(item, "name", "")),
        size=int(getattr(item, "size", 0) or 0),
        made=_time(getattr(item, "creation_time", None)),
        checksum=str(checksum).lower() if checksum else None,
    )


def list_snapshots(client: Any) -> list[Snap]:
    """Newest first; a snapshot without a readable time sorts last."""
    found = [_describe(s) for s in client.list_snapshots(COLLECTION)]
    return sorted(found, key=lambda s: (s.made is None, -(s.made.timestamp() if s.made else 0)))


def _status(error: BaseException) -> int | None:
    for holder in (error, getattr(error, "response", None)):
        status = getattr(holder, "status_code", None)
        if isinstance(status, int):
            return status
    return None


def classify(error: Exception) -> Exception | None:
    """KeyRejected or Unsupported for the statuses that say so, else None."""
    if isinstance(error, (KeyRejected, Unsupported)):
        return error
    status = _status(error)
    if status in KEY_STATUSES:
        return KeyRejected()
    if status in UNSUPPORTED_STATUSES:
        return Unsupported()
    return None


def _client(env: Mapping[str, str]) -> Any:
    from app.vector.documents import client_from_env

    return client_from_env(env)


def _fetch(env: Mapping[str, str], name: str) -> Iterator[bytes]:
    """Stream a snapshot from the server with the same key."""
    import httpx

    base = (env.get("QDRANT_URL") or "").rstrip("/")
    key = env.get("QDRANT_API_KEY") or ""
    url = f"{base}/collections/{COLLECTION}/snapshots/{quote(Path(name).name, safe='')}"
    with httpx.stream("GET", url, headers={"api-key": key}, timeout=300.0) as reply:
        if reply.status_code in KEY_STATUSES:
            raise KeyRejected
        if reply.status_code in UNSUPPORTED_STATUSES:
            raise Unsupported
        reply.raise_for_status()
        yield from reply.iter_bytes()


def _upload(env: Mapping[str, str], path: Path) -> None:
    """Upload a downloaded snapshot file into the restore-test collection (it is created by the
    upload) and wait until the server has restored it."""
    import httpx

    base = (env.get("QDRANT_URL") or "").rstrip("/")
    key = env.get("QDRANT_API_KEY") or ""
    url = f"{base}/collections/{RESTORE_TEST_COLLECTION}/snapshots/upload"
    with path.open("rb") as handle:
        reply = httpx.post(
            url,
            params={"priority": "snapshot", "wait": "true"},
            headers={"api-key": key},
            files={"snapshot": (path.name, handle, "application/octet-stream")},
            timeout=900.0,
        )
    if reply.status_code in KEY_STATUSES:
        raise KeyRejected
    if reply.status_code in UNSUPPORTED_STATUSES:
        raise Unsupported
    reply.raise_for_status()


def restore_test(
    client: Any,
    folder: Path,
    name: str,
    upload: Callable[[Path], None],
    *,
    run: bool,
    out: Callable[[str], None],
) -> int:
    """Restore a downloaded snapshot into `documents_restore_test`, count it, remove it."""
    path = folder / Path(name).name
    if not path.is_file() or path.stat().st_size == 0:
        out(f"no downloaded copy of {Path(name).name} in {folder}: run `download` first")
        return 1
    if client.collection_exists(RESTORE_TEST_COLLECTION):
        out(
            f"{RESTORE_TEST_COLLECTION} already exists: it was not made by this command and is "
            "not touched; delete it in the console if it is a leftover, then run this again"
        )
        return 1
    if not run:
        out(
            f"nothing was restored: add --run to upload {path.name} into "
            f"{RESTORE_TEST_COLLECTION}, count it and remove it"
        )
        return 0
    failed = False
    try:
        upload(path)
        out(f"uploaded {path.name} into {RESTORE_TEST_COLLECTION}")
        restored = client.count(RESTORE_TEST_COLLECTION, exact=True).count
        live = client.count(COLLECTION, exact=True).count
        out(f"points in {RESTORE_TEST_COLLECTION}: {restored}")
        out(f"points in {COLLECTION}: {live}")
        if restored == 0:
            out("the restored collection is EMPTY: this snapshot did not restore")
            failed = True
        elif restored != live:
            out(
                "the counts differ: expected when documents were ingested or withdrawn after "
                "the snapshot was taken; not a fault by itself"
            )
    finally:
        try:
            if client.collection_exists(RESTORE_TEST_COLLECTION):
                client.delete_collection(RESTORE_TEST_COLLECTION)
            out(f"removed {RESTORE_TEST_COLLECTION}")
        except Exception as error:  # the test collection stays: say so, with the class only
            out(
                f"COULD NOT REMOVE {RESTORE_TEST_COLLECTION} ({type(error).__name__}): delete it "
                "by hand in the console, it is only a test"
            )
            failed = True
    return 1 if failed else 0


def download(
    snap: Snap, target: Path, fetch: Callable[[str], Iterator[bytes]]
) -> tuple[Path, int, str]:
    """Copy one snapshot to `target`/NAME: to NAME.part first, renamed only after the size (and
    the server's checksum, when given) match. Returns (path, size, sha256). A failure leaves
    nothing behind and raises."""
    target.mkdir(parents=True, exist_ok=True)
    final = target / Path(snap.name).name  # a bare file name: no folder can be chosen
    part = final.with_name(final.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with part.open("wb") as handle:
            for chunk in fetch(snap.name):
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if snap.size and size != snap.size:
            raise ValueError("size")
        if snap.checksum and digest.hexdigest() != snap.checksum:
            raise ValueError("checksum")
        part.replace(final)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return final, size, digest.hexdigest()


def plan_prune(
    items: list[Snap], keep: int, folder: Path
) -> tuple[list[Snap], list[tuple[str, str]]]:
    """(to delete, [(name, why it is kept)] for the older ones that stay)."""
    keep = max(keep, 1)
    dated = [s for s in items if s.made is not None]  # already newest first
    older = dated[keep:]
    delete: list[Snap] = []
    kept: list[tuple[str, str]] = []
    for snap in older:
        copy = folder / Path(snap.name).name
        if not copy.is_file():
            kept.append((snap.name, "no downloaded copy"))
        elif not snap.size:
            # a server that gives no size cannot confirm that the copy is whole
            kept.append((snap.name, "the server gave no size to compare the copy with"))
        elif copy.stat().st_size != snap.size:
            kept.append((snap.name, "the downloaded copy has another size"))
        else:
            delete.append(snap)
    kept += [(s.name, "no readable creation time") for s in items if s.made is None]
    return delete, kept


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
    client: Any = None,
    folder: Path | None = None,
    fetch: Callable[[str], Iterator[bytes]] | None = None,
    upload: Callable[[Path], None] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Qdrant snapshots of the documents collection")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    create = commands.add_parser("create")
    create.add_argument("--run", action="store_true")
    get = commands.add_parser("download")
    get.add_argument("name", nargs="?", help="a name from `list`, or use --newest")
    get.add_argument("--newest", action="store_true", help="the newest snapshot (by time)")
    get.add_argument("--run", action="store_true")
    restore = commands.add_parser("restore-test")
    restore.add_argument("name", help="a downloaded snapshot (a name from `list`)")
    restore.add_argument("--run", action="store_true")
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
    streamer = fetch or (lambda name: _fetch(env, name))
    try:
        if args.command == "list":
            items = list_snapshots(client)
            out(f"{len(items)} snapshot(s) of '{COLLECTION}'")
            for s in items:
                when = s.made.isoformat() if s.made else "no time"
                copy = "copy here" if (target / Path(s.name).name).is_file() else "no copy"
                out(f"  {s.name}  {s.size} bytes  {when}  {copy}")
            return 0
        if args.command == "create":
            if not args.run:
                out("nothing was created: add --run to make a snapshot")
                return 0
            made = _describe(client.create_snapshot(COLLECTION))
            out(f"created {made.name} ({made.size} bytes)")
            return 0
        if args.command == "restore-test":
            uploader = upload or (lambda path: _upload(env, path))
            return restore_test(client, target, args.name, uploader, run=args.run, out=out)
        if args.command == "download":
            if bool(args.name) == bool(args.newest):
                out("give a snapshot name (from `list`) or --newest, not both and not neither")
                return 1
            known = {s.name: s for s in list_snapshots(client)}
            if args.newest:
                dated = [s for s in known.values() if s.made is not None]
                if not dated:
                    out("no snapshot with a readable creation time to download")
                    return 1
                args.name = dated[0].name  # `list_snapshots` already sorts the newest first
            if not args.run:
                out(f"nothing was downloaded: add --run to copy {args.name} to {target}")
                return 0
            if args.name not in known:
                out("no such snapshot: run `list` and use a name from it")
                return 1
            try:
                path, size, digest = download(known[args.name], target, streamer)
            except ValueError as error:
                out(f"not saved: the {error} of the copy does not match the server's; "
                    "nothing was kept")  # fmt: skip
                return 1
            out(f"saved {path.name} ({size} bytes, sha256 {digest[:16]}...) in {target}")
            return 0
        items = list_snapshots(client)
        delete, kept = plan_prune(items, args.keep, target)
        verb = f"deleting {len(delete)}" if args.run else f"{len(delete)} would be deleted"
        out(f"{len(items)} snapshot(s); keeping the newest {max(args.keep, 1)}; {verb}")
        for name, why in kept:
            out(f"  kept {name}: {why}")
        if not args.run:
            out("nothing was deleted: add --run")
            return 0
        for s in delete:
            client.delete_snapshot(COLLECTION, s.name)
            out(f"deleted {s.name}")
        return 0
    except Exception as error:  # a class name only: an error text may hold an address
        found = classify(error)
        if isinstance(found, KeyRejected):
            out(KEY_TEXT)
            return KEY_REJECTED_EXIT
        if isinstance(found, Unsupported):
            out(UNSUPPORTED_TEXT)
            return UNSUPPORTED_EXIT
        out(f"stopped ({type(error).__name__}); nothing is shown of the address or the key")
        return 1


if __name__ == "__main__":
    sys.exit(main())
