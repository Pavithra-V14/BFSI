"""Real-Upstash check of the cache code (Phase 9). Off by default; the owner runs it by hand.

It runs the project's own `CacheStore` against your REAL Upstash, but every key it touches lives
under one unique throw-away prefix (`check:<12 hex>:`), so it can never read, write or delete a
real key family (`ans:`, `chunk:`, `doc:`, `ctr:`, `rl:`, `corpus:version`). It never flushes
anything. Every key it made is removed in a `finally`, whatever happens, and it only ever removes
keys that start with its own prefix. It prints names, counts and PASS/FAIL: never a key's value, an
address or a secret.

What it checks on the real server (what the in-memory test double cannot prove):
  1. the corpus version is seeded from the clock (milliseconds) and a bump adds exactly 1,
  2. an entry can be stored and read back, with a time to live, and is listed in the reverse set
     of every chunk and document it cited,
  3. invalidating a chunk removes its entries and the set; a reverse set of 600 entries is removed
     in batches (more than one command), and nothing is left,
  4. a reverse-set key of the WRONG TYPE is refused by the server as an error for that one command,
     and the store heals it, still registers the entry, and does NOT open the 30 s skip window,
  5. a counter key of the wrong type heals and keeps counting,
  6. a rate limit counts 1, 2 and blocks the 3rd with a retry time,
  7. a refused connection (a local closed port, not Upstash) IS classed as an outage and does open
     the skip window.

Run it in your own terminal:
    uv run python scripts/cache_check.py --run
It reads UPSTASH_REDIS_URL from the shell (never from a file). Nothing happens without --run.
About 2,000 Redis commands in all (the Upstash free tier allows 10,000 a day).
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import redis  # noqa: E402

from app.cache import store as cache_store  # noqa: E402
from app.cache.store import CacheStore, ChunkRef, exact_key  # noqa: E402

PREFIX_PATTERN = re.compile(r"check:[0-9a-f]{12}:")
MANY = 600  # more than one invalidation batch (500)


class RefusedPrefix(ValueError):
    """The check was about to use a prefix that could be a real key family."""


def throwaway_prefix() -> str:
    return f"check:{secrets.token_hex(6)}:"


def guard_prefix(prefix: str) -> str:
    """Only a unique throw-away prefix is allowed: never an empty one, never a real family."""
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise RefusedPrefix("the check only uses a unique throw-away key prefix")
    return prefix


def line(name: str, status: str, **details: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{name:<44} {status}" + (f"  {extra}" if extra else ""))


def remove_prefixed(client: redis.Redis, prefix: str) -> int:
    """Delete every key under the prefix, and only those. Returns how many were removed."""
    guard_prefix(prefix)
    removed = 0
    batch: list[str] = []
    for key in client.scan_iter(match=f"{prefix}*", count=200):
        name = key if isinstance(key, str) else key.decode()
        if not name.startswith(prefix):  # never delete anything that is not ours
            continue
        batch.append(name)
        if len(batch) >= 200:
            removed += int(client.delete(*batch))
            batch = []
    if batch:
        removed += int(client.delete(*batch))
    return removed


def run_check(
    client: redis.Redis,
    prefix: str,
    report: Callable[..., None] = line,
    *,
    dead_client: redis.Redis | None = None,
) -> int:
    """Run every check under `prefix`, then remove the prefix's keys. Returns the problem count."""
    guard_prefix(prefix)
    problems = 0
    events: list[str] = []

    def result(label: str, ok: bool, **details: object) -> None:
        nonlocal problems
        if not ok:
            problems += 1
        report(label, "PASS" if ok else "FAIL", **details)

    def key(name: str) -> str:
        return f"{prefix}{name}"

    try:
        store = CacheStore(
            client,
            namespace=prefix,
            on_event=lambda name, fields: events.append(name),
        )
        # 1. corpus version
        first = store.corpus_version()
        now_ms = int(time.time() * 1000)
        result(
            "corpus version seeded from the clock (ms)",
            first is not None and abs(first - now_ms) < 120_000,
        )
        bumped = store.bump_corpus_version()
        result("a bump adds exactly 1", first is not None and bumped == first + 1)

        # 2. an entry, its time to live and its reverse sets
        chunks = [
            ChunkRef("chunk-a", "hash-a", "doc-1"),
            ChunkRef("chunk-b", "hash-b", "doc-1"),
        ]
        entry_key = exact_key("check question", "customer", int(bumped or 0), "p1")
        stored = store.put_entry(
            entry_key, {"answer": "x"}, chunks, corpus_version=int(bumped or 0)
        )
        lookup = store.get_entry(entry_key)
        ttl = client.ttl(key(entry_key))
        result("an entry is stored and read back", stored and lookup.status == "hit")
        result("the entry has a time to live", isinstance(ttl, int) and ttl > 0, ttl_seconds=ttl)
        listed = [
            client.sismember(key("chunk:chunk-a"), entry_key),
            client.sismember(key("chunk:chunk-b"), entry_key),
            client.sismember(key("doc:doc-1"), entry_key),
        ]
        result("it is listed in every reverse set", all(listed), sets=sum(bool(x) for x in listed))

        # 3. invalidation: one entry, then a reverse set that needs more than one batch
        removed = store.invalidate_chunk("chunk-a")
        gone = store.get_entry(entry_key).status == "miss" and not client.exists(
            key("chunk:chunk-a")
        )
        result("invalidating a chunk removes its entry", removed == 1 and gone, removed=removed)
        pipe = client.pipeline(transaction=False)
        for number in range(MANY):
            member = f"ans:many-{number}"
            pipe.set(key(member), "1", ex=300)
            pipe.sadd(key("chunk:chunk-many"), member)
        pipe.execute()
        removed = store.invalidate_chunk("chunk-many")
        left = sum(1 for _ in client.scan_iter(match=key("ans:many-*"), count=200))
        result(
            "a large reverse set is removed in batches",
            removed == MANY and left == 0 and not client.exists(key("chunk:chunk-many")),
            removed=removed,
            left=left,
        )

        # 4. a reverse-set key of the wrong type: a refusal of one command, not an outage
        client.set(key("chunk:chunk-w"), "not a set", ex=300)
        events.clear()
        wrong = ChunkRef("chunk-w", "hash-w", "doc-w")
        healed = store.put_entry(
            exact_key("wrong type", "customer", 1, "p1"), {"answer": "y"}, [wrong], corpus_version=1
        )
        registered = client.sismember(
            key("chunk:chunk-w"), exact_key("wrong type", "customer", 1, "p1")
        )
        after = store.corpus_version()  # a normal call, right away: no skip window
        result(
            "a wrong-type reverse set heals, the entry is listed",
            healed and bool(registered),
        )
        result(
            "a refusal does not open the 30 s skip window",
            after is not None and "cache_unavailable" not in events,
            events=",".join(sorted(set(events))) or "none",
        )

        # 5. a counter of the wrong type heals and keeps counting
        client.lpush(key("ctr:rl:chat:user-w"), "x")
        client.expire(key("ctr:rl:chat:user-w"), 300)
        decision = store.check_rate_limit("chat", "user-w", limit=5, window_seconds=60)
        result(
            "a wrong-type counter heals and counts",
            decision.available and decision.count == 1 and decision.allowed,
            count=decision.count,
        )

        # 6. a rate limit
        counts = [
            store.check_rate_limit("chat", "user-c", limit=2, window_seconds=60) for _ in range(3)
        ]
        result(
            "the rate limit counts 1, 2 and blocks the 3rd",
            [d.count for d in counts] == [1, 2, 3]
            and [d.allowed for d in counts] == [True, True, False]
            and counts[2].retry_after is not None,
            retry_after=counts[2].retry_after,
        )

        # 7. a refused connection (a closed local port, not Upstash) is an outage
        if dead_client is not None:
            dead_events: list[str] = []
            dead = CacheStore(
                dead_client, namespace=prefix, on_event=lambda n, f: dead_events.append(n)
            )
            answer = dead.corpus_version()
            skipped_until = getattr(dead, "_skip_until", 0.0)
            result(
                "a refused connection opens the 30 s skip window",
                answer is None and skipped_until > 0.0 and "cache_unavailable" in dead_events,
            )
        report("30 s skip window length", "INFO", seconds=cache_store.UNAVAILABLE_SKIP_SECONDS)
    finally:
        try:
            removed = remove_prefixed(client, prefix)
            leftover = sum(1 for _ in client.scan_iter(match=f"{prefix}*", count=200))
            report(
                "throw-away keys removed",
                "DONE" if leftover == 0 else "FAILED",
                removed=removed,
                left=leftover,
            )
            if leftover:
                problems += 1
        except Exception as error:  # report, never hide: the owner removes them by hand
            report("throw-away keys removed", "FAILED", error=type(error).__name__)
            problems += 1
    return problems


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Real-Upstash check on a throw-away key prefix.")
    parser.add_argument("--run", action="store_true", help="run the check (REAL Upstash)")
    args = parser.parse_args(argv)
    if not args.run:
        print(__doc__)
        print("Nothing was done. Add --run to check your Upstash.")
        return 0
    env = os.environ if env is None else env
    try:
        client = cache_store.client_from_env(env)
        prefix = guard_prefix(throwaway_prefix())
        print(f"using a throw-away key prefix {prefix}")
        dead = redis.Redis.from_url(
            "redis://127.0.0.1:1", socket_timeout=1.0, socket_connect_timeout=1.0
        )
        problems = run_check(client, prefix, dead_client=dead)
    except cache_store.CacheConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # names only, never a URL or a key
        print(f"error: the check stopped ({type(error).__name__})", file=sys.stderr)
        return 1
    print("all checks passed" if not problems else f"{problems} check(s) failed")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
