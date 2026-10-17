"""The Upstash cache primitives (spec 10.1, 10.3 items 1 and 2, 11.1, 12.4).

What is here: the exact-cache key, storing and fetching an entry, the corpus-version counter,
the reverse sets `chunk:{id}` and `doc:{id}` with invalidation, and windowed counters for rate
limits and budgets. What is not here: the read-time check, the edit and new-chunk rules, the
role-scoped versions and the pipeline's lookup and write (later phases).

If Upstash cannot be reached, nothing raises. Every call reports "unavailable" (a None, False or
an "unavailable" lookup) and an event is emitted, so the caller skips the cache and answers
normally (spec 11.1). After a failure the store does not call Upstash again for
UNAVAILABLE_SKIP_SECONDS [P], so a dead cache costs one timeout and not one per call. Reads and
rate limits keep skipping. Writes that keep the cache honest (the corpus-version bump and the
invalidations) never skip: they always try, and a failure emits "cache_staleness_write_failed".
Their callers must tell "not done" (None) from "done": ingest and delete treat None as "ingest
incomplete". A rate-limit check lets the request through when the counter is down (fail open)
and emits an alert event. Whether login throttling does the same is decided in Phase 4.

The corpus version starts from the current Unix time in milliseconds, never from 0, and each
bump adds 1. After a lost key the version restarts from the clock, which is above every earlier
value as long as bumps average fewer than 1,000 a second since the version was first created
(the seed was in milliseconds, so the counter can only outrun the clock at that rate). Seeding
and reading or incrementing run in one MULTI/EXEC transaction.

Entries hold no raw query text: the key is a hash, and the caller passes already-masked content.
Logs and events carry operation names and exception class names only, never a URL or a key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import redis

from app.gateway.embeddings import running_on_render
from app.isolation import is_local_url
from app.observability.logs import fx

log = logging.getLogger("app.cache")

DEFAULT_TTL_SECONDS = 86_400  # [P] 24 hours; TTL is only a backstop (spec 10.2)
REVERSE_SET_MARGIN_SECONDS = (
    3_600  # reverse sets outlive their entries, so they can still find them
)
UNAVAILABLE_SKIP_SECONDS = 30.0  # [P] how long Upstash is left alone after a failure
# A rate-limit subject is an id or a hash, never a name or an email address (spec 10.2: no PII).
SUBJECT_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
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
    """The normal form of a masked query: Unicode NFC, case-folded, spaces collapsed.

    [P] The spec says "normalised" without defining it. NFC only joins different spellings of the
    same character (an accent typed as one character or as two). It does not fold look-alikes, so
    "10²" and "102" stay different questions. Punctuation is kept on purpose, because figures and
    symbols can change an answer.
    """
    return " ".join(unicodedata.normalize("NFC", masked_query).casefold().split())


def exact_key(
    masked_query: str,
    role: str,
    corpus_version: int,
    prompt_version: str,
    role_map_version: str = "",
) -> str:
    """Spec 10.1: a hash of the normalised masked query, the role, the corpus version, the prompt
    version and (Phase 9, spec 10.3 item 7) a short hash of the role-to-visible-roles map. The key
    holds no query text, and a change in any part gives a new key."""
    parts = [normalise_query(masked_query), role, int(corpus_version), prompt_version]
    if role_map_version:  # the pipeline always passes it; an empty one keeps older callers' keys
        parts.append(role_map_version)
    digest = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{ANSWER_PREFIX}{digest}"


def chunk_set_key(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def doc_set_key(doc_id: str) -> str:
    return f"doc:{doc_id}"


_FAILED = object()

# What counts as "Upstash cannot be reached" (opens the skip window). Any other RedisError means the
# server answered with a refusal for one command (for example WRONGTYPE) and does not.
_CONNECTION_ERRORS = (
    redis.exceptions.ConnectionError,
    redis.exceptions.TimeoutError,
    redis.exceptions.BusyLoadingError,
    redis.exceptions.ClusterDownError,
    OSError,
)

INVALIDATE_BATCH = 500  # entries removed per command: a huge reverse set is never one DEL

# Refusals that are NOT about one key but about the whole server: they open the skip window like a
# connection problem, each with its own alert name so that "Upstash is out of quota" is not
# mistaken for "one key has the wrong type". Matched on the class first, then on the text the
# server sends (Upstash answers "ERR max daily request limit exceeded").
_QUOTA_WORDS = ("max daily request limit", "max request limit", "request limit exceeded")


def classify_refusal(error: Exception) -> str | None:
    """The alert name of a server-wide refusal, or None for a refusal of one command (a key of the
    wrong type, a bad argument), which is not an outage."""
    text = str(error).lower()
    if isinstance(error, redis.exceptions.ReadOnlyError) or text.startswith("readonly"):
        return "cache_read_only"
    if isinstance(error, redis.exceptions.OutOfMemoryError) or text.startswith("oom "):
        return "cache_out_of_memory"
    if any(word in text for word in _QUOTA_WORDS):
        return "cache_quota_exceeded"
    if isinstance(error, redis.exceptions.NoPermissionError) or text.startswith("noperm"):
        return "cache_permission_denied"
    return None


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive whole number")


class CacheStore:
    def __init__(
        self,
        client: redis.Redis,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        namespace: str = "",
    ) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise CacheConfigError("CACHE_TTL_SECONDS must be a positive whole number")
        self._client = client
        self.ttl_seconds = ttl_seconds
        self._on_event = on_event
        self._clock = clock
        self._wall_clock = wall_clock
        self._skip_until = 0.0  # while the clock is before this, Upstash is not called
        # A prefix put before every key this store touches. Empty in production. The owner-run
        # `scripts/cache_check.py` uses a unique throw-away one, so that it runs the real code on
        # the real server without ever touching a real key family.
        self._ns = namespace

    # ---- plumbing -------------------------------------------------------------------

    def _k(self, key: str) -> str:
        return f"{self._ns}{key}"

    def _emit(self, name: str, **fields: Any) -> None:
        log.warning("cache event %s", name, extra=fx(fields))
        if self._on_event:
            try:
                self._on_event(name, fields)
            except Exception:  # an alert hook that fails must never break a request
                log.error("cache event handler failed for %s", name)

    def _run(self, operation: str, work: Callable[[], Any], *, staleness: bool = False) -> Any:
        """Run one cache operation. A connection problem returns _FAILED and emits an event.

        After a failure Upstash is skipped for UNAVAILABLE_SKIP_SECONDS: calls return _FAILED at
        once, with no new event, and the first call after that window tries again.

        A staleness write (`staleness=True`: the corpus-version bump and the invalidations) is
        never skipped. It always tries, so a short blip cannot leave a changed document's answers
        in the cache, and when it fails it emits a "cache_staleness_write_failed" alert event.
        A staleness write that succeeds proves Upstash is back and ends the skip window.
        """
        if not staleness and self._clock() < self._skip_until:
            return _FAILED
        try:
            value = work()
        except _CONNECTION_ERRORS as error:
            # Upstash cannot be reached: leave it alone for a while (a dead cache costs one timeout)
            self._skip_until = self._clock() + UNAVAILABLE_SKIP_SECONDS
            self._emit("cache_unavailable", operation=operation, error=type(error).__name__)
            if staleness:
                self._emit(
                    "cache_staleness_write_failed",
                    operation=operation,
                    error=type(error).__name__,
                )
            return _FAILED
        except redis.exceptions.RedisError as error:
            outage = classify_refusal(error)
            if outage is not None:
                # The server refuses everything (read-only, out of memory, quota, no permission):
                # an outage with its own alert name. Leave it alone for a while.
                self._skip_until = self._clock() + UNAVAILABLE_SKIP_SECONDS
                self._emit(outage, operation=operation, error=type(error).__name__)
                if staleness:
                    self._emit(
                        "cache_staleness_write_failed",
                        operation=operation,
                        error=type(error).__name__,
                    )
                return _FAILED
            # Upstash answered, and said no to THIS command (a key of the wrong type, a refused
            # command). That is one bad key, not an outage: no skip window, so the other calls (and
            # the rate limiter) carry on, and an alert names the operation.
            self._emit("cache_error", operation=operation, error=type(error).__name__)
            if staleness:
                self._emit(
                    "cache_staleness_write_failed",
                    operation=operation,
                    error=type(error).__name__,
                )
            return _FAILED
        if staleness:
            self._skip_until = 0.0
        return value

    def preflight(self) -> bool:
        """Does Upstash accept the commands a staleness write needs? Ingest, delete and a review
        approval call this BEFORE their first write and refuse to start when it says no, so that
        nothing is changed while the cache cannot be told about it (Phase 9 [P]).

        It runs the same kinds of command the bump and the invalidation use (a MULTI/EXEC
        transaction with SET NX, a read, a scan and a delete) on one throw-away key that expires by
        itself. It never changes the corpus version and is never skipped because of the skip
        window. It returns False (and emits the usual alert) when any of them fails.
        """
        probe = self._k(f"preflight:{secrets.token_hex(8)}")

        def work() -> bool:
            pipe = self._client.pipeline(transaction=True)
            pipe.set(probe, "1", nx=True, ex=30)
            pipe.get(probe)
            ok = pipe.execute()[-1] == "1"
            list(self._client.sscan_iter(probe + ":none", count=1))
            self._client.delete(probe)
            return ok

        return self._run("preflight", work, staleness=True) is True

    # ---- corpus version (10.3 item 1) ----------------------------------------------

    def _version_pipeline(self):
        """One MULTI/EXEC transaction that first seeds the version from the current Unix time in
        milliseconds if (and only if) the key is missing. Seeding and the following read or
        increment run as one atomic unit, so a key that vanishes cannot be re-created by the
        increment at 1 in between."""
        pipe = self._client.pipeline(transaction=True)
        pipe.set(self._k(CORPUS_VERSION_KEY), int(self._wall_clock() * 1000), nx=True)
        return pipe

    def corpus_version(self) -> int | None:
        """The current corpus version, or None if unavailable (callers must skip the cache).

        If the key is missing (first use, or it was lost) it is created from the current Unix
        time in milliseconds.
        """

        def work() -> Any:
            pipe = self._version_pipeline()
            pipe.get(self._k(CORPUS_VERSION_KEY))
            return pipe.execute()[-1]

        value = self._run("corpus_version", work)
        if value is _FAILED:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            self._emit("cache_corrupt", operation="corpus_version")
            return None

    def bump_corpus_version(self) -> int | None:
        """Any ingest, edit or delete bumps the version. Returns the new value, or None.

        None means NOT DONE: the bump did not happen (and an alert event was emitted). It is never
        skipped because of the skip window. Ingest and delete must treat None as "ingest
        incomplete" and must not report success.
        """

        def work() -> Any:
            pipe = self._version_pipeline()
            pipe.incr(self._k(CORPUS_VERSION_KEY))
            return pipe.execute()[-1]

        value = self._run("bump_corpus_version", work, staleness=True)
        return None if value is _FAILED else int(value)

    # ---- entries and reverse sets (10.3 item 2) -------------------------------------

    def get_entry(self, key: str) -> Lookup:
        raw = self._run("get_entry", lambda: self._client.get(self._k(key)))
        if raw is _FAILED:
            return Lookup(UNAVAILABLE)
        if raw is None:
            return Lookup(MISS)
        try:
            entry = json.loads(raw)
        except ValueError:
            self._emit("cache_corrupt", operation="get_entry")
            self._run("delete_corrupt", lambda: self._client.delete(self._k(key)))
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
        Returns False if the cache was unavailable.

        The reverse sets are written first and the entry last. If the connection drops part way,
        the worst case is a reverse set that names an entry that does not exist (harmless), never
        an entry that no invalidation can find.
        """
        if not chunks:  # an entry that cites nothing could never be invalidated by a chunk
            self._emit("cache_error", operation="put_entry", error="EmptyChunkList")
            return False
        entry = {
            "payload": dict(payload),
            "chunks": [
                {"chunk_id": c.chunk_id, "content_hash": c.content_hash, "doc_id": c.doc_id}
                for c in chunks
            ],
            "corpus_version": corpus_version,
        }
        reverse_ttl = self.ttl_seconds + REVERSE_SET_MARGIN_SECONDS
        set_keys = sorted(
            {self._k(chunk_set_key(c.chunk_id)) for c in chunks}
            | {self._k(doc_set_key(c.doc_id)) for c in chunks}
        )

        def register() -> list[str]:
            """SADD the entry into every reverse set; returns the keys that hold the wrong type.
            Any OTHER refusal (out of memory, a quota, no permission ...) is raised as it is: it is
            not evidence that the key is of the wrong type, and a live set must never be deleted
            because of it."""
            pipe = self._client.pipeline(transaction=False)
            for set_key in set_keys:
                pipe.sadd(set_key, key)
                pipe.expire(set_key, reverse_ttl)
            results = pipe.execute(raise_on_error=False)
            wrong: list[str] = []
            for set_key, outcome in zip(set_keys, results[0::2], strict=True):
                if not isinstance(outcome, Exception):
                    continue
                if "WRONGTYPE" not in str(outcome).upper():
                    raise outcome
                wrong.append(set_key)
            return wrong

        def work() -> bool:
            refused = register()
            if refused:
                # A reverse set key that holds the wrong type can never have been made by this
                # code. Remove it and try once more; if it still refuses, the entry is NOT written:
                # an entry that no invalidation can find must never exist.
                for set_key in refused:
                    self._client.delete(set_key)
                refused = register()
            if refused:
                return False
            # the entry last, once every reverse set names it
            pipe = self._client.pipeline(transaction=False)
            pipe.set(self._k(key), json.dumps(entry, ensure_ascii=False), ex=self.ttl_seconds)
            pipe.execute()
            return True

        value = self._run("put_entry", work)
        if value is True:
            return True
        if value is False:
            self._emit("cache_error", operation="put_entry", error="ReverseSetRefused")
        return False

    def _invalidate(self, operation: str, set_key: str) -> int | None:
        """Remove the entries listed in a reverse set, in batches of INVALIDATE_BATCH (a reverse
        set can hold thousands: never one command, never the whole set in memory). The set key is
        removed last, so a run that stops half way is finished by running it again.

        A race, known and accepted: an entry registered into this set after the scan has passed
        it, and before the set key is removed, loses its reverse-set record. That is safe only
        because every caller bumps the corpus version BEFORE invalidating: such an entry sits under
        a key with the old version and can never be looked up again. Do not call this without a
        bump first."""

        def work() -> int:
            removed = 0
            batch: list[str] = []
            for member in self._client.sscan_iter(self._k(set_key), count=INVALIDATE_BATCH):
                batch.append(self._k(member))
                if len(batch) >= INVALIDATE_BATCH:
                    removed += int(self._client.delete(*batch))
                    batch = []
            if batch:
                removed += int(self._client.delete(*batch))
            self._client.delete(self._k(set_key))
            return removed

        value = self._run(operation, work, staleness=True)
        return None if value is _FAILED else value

    def invalidate_chunk(self, chunk_id: str) -> int | None:
        """Remove every entry that cited this chunk. Returns how many entries were removed.

        None means NOT DONE (an alert event was emitted). It is never skipped because of the skip
        window. Ingest and delete must treat None as "ingest incomplete".
        """
        return self._invalidate("invalidate_chunk", chunk_set_key(chunk_id))

    def invalidate_doc(self, doc_id: str) -> int | None:
        """Remove every entry that cited any chunk of this document. None means NOT DONE, with
        the same rule as invalidate_chunk."""
        return self._invalidate("invalidate_doc", doc_set_key(doc_id))

    def clear_cached_answers(self) -> int | None:
        """Remove EVERY cached answer and the reverse sets that list them (the owner's purge).
        Counters and rate limits are not touched. The corpus version is bumped first, so that an
        answer written while this runs sits under an old version and can never be found (the same
        rule as `_invalidate`). Returns how many answers were removed; None means NOT DONE (an
        alert event was emitted) and running it again finishes the job."""
        if self.bump_corpus_version() is None:
            return None

        def work() -> int:
            removed_answers = 0
            for prefix in (ANSWER_PREFIX, "chunk:", "doc:"):
                batch: list[str] = []
                for key in self._client.scan_iter(
                    match=self._k(prefix) + "*", count=INVALIDATE_BATCH
                ):
                    batch.append(key)
                    if len(batch) >= INVALIDATE_BATCH:
                        deleted = int(self._client.delete(*batch))
                        removed_answers += deleted if prefix == ANSWER_PREFIX else 0
                        batch = []
                if batch:
                    deleted = int(self._client.delete(*batch))
                    removed_answers += deleted if prefix == ANSWER_PREFIX else 0
            return removed_answers

        value = self._run("clear_cached_answers", work, staleness=True)
        return None if value is _FAILED else value

    # ---- counters (12.4) ---------------------------------------------------------------

    def incr_counter(self, name: str, window_seconds: int, amount: int = 1) -> int | None:
        """Add to a fixed-window counter and return its new value, or None if unavailable.

        The window starts at the first increment and the counter vanishes when it ends. Used for
        rate limits and for the daily token budget (the callers decide what to do on None).
        A window or an amount that is not a positive whole number is a bug in the caller and
        raises ValueError, because a window of zero would make the limit silently never apply.
        """
        _positive_int(window_seconds, "window_seconds")
        _positive_int(amount, "amount")
        key = self._k(f"{COUNTER_PREFIX}{name}")

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

        def healed() -> int:
            try:
                return work()
            except redis.exceptions.ResponseError as error:
                # A counter key of the wrong type (WRONGTYPE) can never have been made by this
                # code: start it again, so that this one subject keeps being counted instead of
                # being let through uncounted for ever.
                if "WRONGTYPE" not in str(error).upper():
                    raise
                self._client.delete(key)
                self._emit("cache_error", operation="incr_counter", error="WrongTypeHealed")
                return work()

        value = self._run("incr_counter", healed)
        return None if value is _FAILED else value

    def read_counter(self, name: str) -> int | None:
        """The current value of a counter (0 if it does not exist), or None if unavailable.
        Reads only: it never starts a window or changes a count."""
        raw = self._run(
            "read_counter", lambda: self._client.get(self._k(f"{COUNTER_PREFIX}{name}"))
        )
        if raw is _FAILED:
            return None
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            self._emit("cache_corrupt", operation="read_counter")
            return None

    def check_rate_limit(
        self, scope: str, subject: str, *, limit: int, window_seconds: int
    ) -> RateDecision:
        """Count one request against `limit` per window. If the counter is unavailable the
        request is allowed (fail open) and a "rate_limit_unavailable" event is emitted.

        `subject` must be an id or a hash (letters, digits and _ . : - only): a name or an email
        address would put personal data into a key name, so it is refused (ValueError).
        """
        _positive_int(limit, "limit")
        _positive_int(window_seconds, "window_seconds")
        if not SUBJECT_PATTERN.fullmatch(subject) or not SUBJECT_PATTERN.fullmatch(scope):
            raise ValueError("scope and subject must be ids or hashes, not names or addresses")
        key = f"{RATE_PREFIX}{scope}:{subject}"
        count = self.incr_counter(key, window_seconds)
        if count is None:
            self._emit("rate_limit_unavailable", scope=scope)
            return RateDecision(True, None, limit, None, available=False)
        if count <= limit:
            return RateDecision(True, count, limit, None)
        remaining = self._run(
            "rate_limit_ttl", lambda: self._client.ttl(self._k(f"{COUNTER_PREFIX}{key}"))
        )
        retry_after = None if remaining is _FAILED or remaining < 0 else int(remaining)
        return RateDecision(False, count, limit, retry_after)
