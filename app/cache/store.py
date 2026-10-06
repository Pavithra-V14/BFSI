"""The Upstash cache primitives (spec 10.1, 10.3 items 1 and 2, 11.1, 12.4).

What is here: the exact-cache key, storing and fetching an entry, the corpus-version counter,
the reverse sets `chunk:{id}` and `doc:{id}` with invalidation, and windowed counters for rate
limits and budgets. What is not here: the read-time check, the edit and new-chunk rules, the
role-scoped versions and the pipeline's lookup and write (later phases).

If Upstash cannot be reached, nothing raises. Every call reports "unavailable" (a None, False or
an "unavailable" lookup) and an event is emitted, so the caller skips the cache and answers
normally (spec 11.1). A rate-limit check lets the request through when the counter is down (fail
open) and emits an alert event. Whether login throttling does the same is decided in Phase 4.

Entries hold no raw query text: the key is a hash, and the caller passes already-masked content.
Logs and events carry operation names and exception class names only, never a URL or a key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import redis

from app.gateway.embeddings import running_on_render
from app.isolation import is_local_url

log = logging.getLogger("app.cache")

DEFAULT_TTL_SECONDS = 86_400  # [P] 24 hours; TTL is only a backstop (spec 10.2)
REVERSE_SET_MARGIN_SECONDS = (
    3_600  # reverse sets outlive their entries, so they can still find them
)
CORPUS_VERSION_KEY = "corpus:version"
ANSWER_PREFIX = "ans:"
COUNTER_PREFIX = "ctr:"
RATE_PREFIX = "rl:"

HIT = "hit"
MISS = "miss"
UNAVAILABLE = "unavailable"


class CacheConfigError(Exception):
    """A setting is wrong. The message names the variable, never a value."""


@dataclass(frozen=True)
class ChunkRef:
    """A chunk an answer cited: what the read-time check and the reverse sets need."""

    chunk_id: str
    content_hash: str
    doc_id: str


@dataclass(frozen=True)
class Lookup:
    status: str  # HIT, MISS or UNAVAILABLE
    entry: dict[str, Any] | None = None


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    count: int | None  # None when the counter was unavailable
    limit: int
    retry_after: int | None  # seconds until the window ends, when blocked
    available: bool = True


def make_client(
    url: str | None, *, timeout: float = 2.0, env: Mapping[str, str] | None = None
) -> redis.Redis:
    """A Redis client for Upstash. Short timeouts, so a slow cache can never stall a query.

    A remote host needs rediss:// (TLS). A missing URL is an error. The URL is never printed.
    """
    address = (url or "").strip()
    if not address:
        raise CacheConfigError("UPSTASH_REDIS_URL is not set")
    if not is_local_url(address) and not address.lower().startswith("rediss://"):
        raise CacheConfigError("UPSTASH_REDIS_URL must start with rediss:// for a remote host")
    if running_on_render(env) and is_local_url(address):
        raise CacheConfigError("UPSTASH_REDIS_URL must not point at this machine on Render")
    return redis.Redis.from_url(
        address, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
    )


def client_from_env(env: Mapping[str, str] | None = None) -> redis.Redis:
    env = os.environ if env is None else env
    return make_client(env.get("UPSTASH_REDIS_URL"), env=env)


def ttl_from_env(env: Mapping[str, str] | None = None) -> int:
    """CACHE_TTL_SECONDS [P], default 86400 (24 hours). The TTL is only a backstop."""
    env = os.environ if env is None else env
    raw = (env.get("CACHE_TTL_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise CacheConfigError("CACHE_TTL_SECONDS must be a positive whole number") from None
    if value <= 0:
        raise CacheConfigError("CACHE_TTL_SECONDS must be a positive whole number")
    return value


def store_from_env(
    env: Mapping[str, str] | None = None,
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> CacheStore:
    return CacheStore(client_from_env(env), ttl_seconds=ttl_from_env(env), on_event=on_event)


def normalise_query(masked_query: str) -> str:
    """The normal form of a masked query: Unicode-compatible, case-folded, spaces collapsed.

    [P] The spec says "normalised" without defining it. Punctuation is kept on purpose, because
    figures and symbols can change an answer.
    """
    return " ".join(unicodedata.normalize("NFKC", masked_query).casefold().split())


def exact_key(masked_query: str, role: str, corpus_version: int, prompt_version: str) -> str:
    """Spec 10.1: a hash of the normalised masked query, the role, the corpus version and the
    prompt version. The key holds no query text, and a change in any part gives a new key."""
    parts = [normalise_query(masked_query), role, int(corpus_version), prompt_version]
    digest = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{ANSWER_PREFIX}{digest}"


def chunk_set_key(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def doc_set_key(doc_id: str) -> str:
    return f"doc:{doc_id}"


_FAILED = object()


class CacheStore:
    def __init__(
        self,
        client: redis.Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise CacheConfigError("CACHE_TTL_SECONDS must be a positive whole number")
        self._client = client
        self.ttl_seconds = ttl_seconds
        self._on_event = on_event

    # ---- plumbing -------------------------------------------------------------------

    def _emit(self, name: str, **fields: Any) -> None:
        log.warning("cache event %s %s", name, fields)
        if self._on_event:
            self._on_event(name, fields)

    def _run(self, operation: str, work: Callable[[], Any]) -> Any:
        """Run one cache operation. A connection problem returns _FAILED and emits an event."""
        try:
            return work()
        except (redis.exceptions.RedisError, OSError) as error:
            self._emit("cache_unavailable", operation=operation, error=type(error).__name__)
            return _FAILED

    # ---- corpus version (10.3 item 1) ----------------------------------------------

    def corpus_version(self) -> int | None:
        """The current corpus version (0 before the first bump), or None if unavailable."""
        value = self._run("corpus_version", lambda: self._client.get(CORPUS_VERSION_KEY))
        if value is _FAILED:
            return None
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            self._emit("cache_corrupt", operation="corpus_version")
            return None

    def bump_corpus_version(self) -> int | None:
        """Any ingest, edit or delete bumps the version. Returns the new value, or None."""
        value = self._run("bump_corpus_version", lambda: self._client.incr(CORPUS_VERSION_KEY))
        return None if value is _FAILED else int(value)

    # ---- entries and reverse sets (10.3 item 2) -------------------------------------

    def get_entry(self, key: str) -> Lookup:
        raw = self._run("get_entry", lambda: self._client.get(key))
        if raw is _FAILED:
            return Lookup(UNAVAILABLE)
        if raw is None:
            return Lookup(MISS)
        try:
            entry = json.loads(raw)
        except ValueError:
            self._emit("cache_corrupt", operation="get_entry")
            self._run("delete_corrupt", lambda: self._client.delete(key))
            return Lookup(MISS)
        return Lookup(HIT, entry) if isinstance(entry, dict) else Lookup(MISS)

    def put_entry(
        self,
        key: str,
        payload: Mapping[str, Any],
        chunks: Sequence[ChunkRef],
        *,
        corpus_version: int,
    ) -> bool:
        """Store an entry with its TTL, and list it in the reverse set of every chunk and
        document it cited. The entry records each chunk's id and content hash plus the doc ids.
        Returns False if the cache was unavailable (nothing is stored)."""
        entry = {
            "payload": dict(payload),
            "chunks": [
                {"chunk_id": c.chunk_id, "content_hash": c.content_hash, "doc_id": c.doc_id}
                for c in chunks
            ],
            "corpus_version": corpus_version,
        }
        reverse_ttl = self.ttl_seconds + REVERSE_SET_MARGIN_SECONDS

        def work() -> None:
            pipe = self._client.pipeline(transaction=False)
            pipe.set(key, json.dumps(entry, ensure_ascii=False), ex=self.ttl_seconds)
            for set_key in {chunk_set_key(c.chunk_id) for c in chunks} | {
                doc_set_key(c.doc_id) for c in chunks
            }:
                pipe.sadd(set_key, key)
                pipe.expire(set_key, reverse_ttl)
            pipe.execute()

        return self._run("put_entry", work) is not _FAILED

    def _invalidate(self, operation: str, set_key: str) -> int | None:
        def work() -> int:
            members = list(self._client.smembers(set_key))
            removed = int(self._client.delete(*members)) if members else 0
            self._client.delete(set_key)
            return removed

        value = self._run(operation, work)
        return None if value is _FAILED else value

    def invalidate_chunk(self, chunk_id: str) -> int | None:
        """Remove every entry that cited this chunk. Returns how many, or None if unavailable."""
        return self._invalidate("invalidate_chunk", chunk_set_key(chunk_id))

    def invalidate_doc(self, doc_id: str) -> int | None:
        """Remove every entry that cited any chunk of this document."""
        return self._invalidate("invalidate_doc", doc_set_key(doc_id))

    # ---- counters (12.4) ---------------------------------------------------------------

    def incr_counter(self, name: str, window_seconds: int, amount: int = 1) -> int | None:
        """Add to a fixed-window counter and return its new value, or None if unavailable.

        The window starts at the first increment and the counter vanishes when it ends. Used for
        rate limits and for the daily token budget (the callers decide what to do on None).
        """
        key = f"{COUNTER_PREFIX}{name}"

        def work() -> int:
            # One round trip, no MULTI/EXEC: each command is atomic by itself, and a managed
            # Redis does not have to support transactions for this to work.
            pipe = self._client.pipeline(transaction=False)
            pipe.incrby(key, amount)
            pipe.ttl(key)
            count, ttl = pipe.execute()
            if ttl is None or ttl < 0:  # first increment (or a lost expiry): start the window
                self._client.expire(key, window_seconds)
            return int(count)

        value = self._run("incr_counter", work)
        return None if value is _FAILED else value

    def check_rate_limit(
        self, scope: str, subject: str, *, limit: int, window_seconds: int
    ) -> RateDecision:
        """Count one request against `limit` per window. If the counter is unavailable the
        request is allowed (fail open) and a "rate_limit_unavailable" event is emitted."""
        key = f"{RATE_PREFIX}{scope}:{subject}"
        count = self.incr_counter(key, window_seconds)
        if count is None:
            self._emit("rate_limit_unavailable", scope=scope)
            return RateDecision(True, None, limit, None, available=False)
        if count <= limit:
            return RateDecision(True, count, limit, None)
        remaining = self._run("rate_limit_ttl", lambda: self._client.ttl(f"{COUNTER_PREFIX}{key}"))
        retry_after = None if remaining is _FAILED or remaining < 0 else int(remaining)
        return RateDecision(False, count, limit, retry_after)
