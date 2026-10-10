"""The Upstash cache primitives, against an in-process fake Redis (no network)."""

import json
import threading

import fakeredis
import pytest
import redis

from app.cache.store import (
    DEFAULT_TTL_SECONDS,
    HIT,
    MISS,
    UNAVAILABLE,
    UNAVAILABLE_SKIP_SECONDS,
    CacheConfigError,
    CacheStore,
    ChunkRef,
    client_from_env,
    exact_key,
    make_client,
    normalise_query,
    store_from_env,
    ttl_from_env,
)

NOW = 1_800_000_000  # a fixed "current Unix time" in seconds
NOW_MS = NOW * 1000  # the corpus version is seeded in milliseconds


class FakeClock:
    """A clock the tests move by hand."""

    def __init__(self, start=1_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def client(server):
    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def events():
    return []


@pytest.fixture
def store(client, events):
    return CacheStore(
        client, on_event=lambda name, fields: events.append((name, fields)), wall_clock=lambda: NOW
    )


class FlakyRedis:
    """A Redis that fails like a cache that cannot be reached, until `down` is set to False.
    Every attempted call is counted, so a test can see whether Upstash was even tried."""

    def __init__(self, inner=None, down=True):
        self.inner = inner
        self.down = down
        self.calls = 0

    def __getattr__(self, name):
        if name in ("inner", "down", "calls"):  # not set yet: avoid recursion
            raise AttributeError(name)
        self.calls += 1
        if self.down or self.inner is None:

            def fail(*args, **kwargs):
                raise redis.exceptions.ConnectionError(
                    "Error 111 connecting to secret-host.example:6379"
                )

            return fail
        return getattr(self.inner, name)


CHUNKS = [
    ChunkRef("chunk-a", "hash-a", "doc-1"),
    ChunkRef("chunk-b", "hash-b", "doc-1"),
    ChunkRef("chunk-c", "hash-c", "doc-2"),
]


# ---- the exact-cache key (10.1) -----------------------------------------------------------


def test_the_key_is_a_hash_with_no_query_text_in_it():
    key = exact_key("What is the waiting period?", "customer", 3, "prompt-v1")
    assert key.startswith("ans:") and len(key) == len("ans:") + 64
    assert "waiting" not in key.lower()


def test_the_same_inputs_always_give_the_same_key():
    assert exact_key("a b", "customer", 1, "p1") == exact_key("a b", "customer", 1, "p1")


@pytest.mark.parametrize(
    "changed",
    [
        ("another question", "customer", 1, "p1"),
        ("a b", "staff", 1, "p1"),  # scoped by role
        ("a b", "customer", 2, "p1"),  # a new corpus version
        ("a b", "customer", 1, "p2"),  # a new prompt version
    ],
)
def test_a_change_in_any_part_gives_a_different_key(changed):
    assert exact_key(*changed) != exact_key("a b", "customer", 1, "p1")


def test_the_query_is_normalised_before_hashing():
    base = exact_key("What is the waiting period", "customer", 1, "p1")
    assert exact_key("  what   IS the\twaiting period ", "customer", 1, "p1") == base
    assert exact_key("What is the waiting period?", "customer", 1, "p1") != base  # punctuation kept


def test_two_spellings_of_the_same_character_are_one_question_nfc():
    composed, decomposed = "caf\u00e9 cover", "cafe\u0301 cover"  # one character, or two
    assert composed != decomposed
    assert normalise_query(composed) == normalise_query(decomposed)
    assert exact_key(composed, "customer", 1, "p") == exact_key(decomposed, "customer", 1, "p")


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("limit of 10\u00b2", "limit of 102"),  # "10 squared" is not "102"
        ("plan \u2460", "plan 1"),  # a circled digit is not a plain digit
        ("\uff21\uff22\uff23 cover", "abc cover"),  # full-width letters are not folded
        ("rs\u2169", "rsx"),
    ],
)
def test_look_alike_forms_stay_different_questions(one, other):
    assert normalise_query(one) != normalise_query(other)
    assert exact_key(one, "customer", 1, "p") != exact_key(other, "customer", 1, "p")


def test_parts_cannot_be_shifted_between_fields():
    # "a", "bc" must not collide with "ab", "c".
    assert exact_key("a", "bc", 1, "p") != exact_key("ab", "c", 1, "p")


# ---- entries, TTL and reverse sets (10.3 item 2) ---------------------------------------------


def test_a_stored_entry_comes_back_with_its_chunks_and_version(store):
    key = exact_key("q", "customer", 1, "p1")
    assert store.get_entry(key).status == MISS
    assert store.put_entry(key, {"answer": "30 days"}, CHUNKS, corpus_version=1)
    found = store.get_entry(key)
    assert found.status == HIT
    assert found.entry["payload"] == {"answer": "30 days"}
    assert found.entry["corpus_version"] == 1
    assert found.entry["chunks"][0] == {
        "chunk_id": "chunk-a",
        "content_hash": "hash-a",
        "doc_id": "doc-1",
    }


def test_the_entry_has_the_ttl_and_the_reverse_sets_outlive_it(store, client):
    key = exact_key("q", "customer", 1, "p1")
    store.put_entry(key, {"answer": "x"}, CHUNKS, corpus_version=1)
    assert DEFAULT_TTL_SECONDS == 86_400
    assert 0 < client.ttl(key) <= 86_400
    assert client.ttl("chunk:chunk-a") > client.ttl(key)
    assert client.smembers("chunk:chunk-a") == {key}
    assert client.smembers("doc:doc-1") == {key}
    assert client.smembers("doc:doc-2") == {key}


def test_the_ttl_is_configurable_and_validated(client):
    short = CacheStore(client, ttl_seconds=120)
    short.put_entry("ans:k", {}, [], corpus_version=0)
    assert 0 < client.ttl("ans:k") <= 120
    for bad in (0, -5, True, "86400", 1.5):
        with pytest.raises(CacheConfigError, match="CACHE_TTL_SECONDS"):
            CacheStore(client, ttl_seconds=bad)


def test_an_entry_with_no_chunks_is_still_stored(store):
    assert store.put_entry("ans:plain", {"answer": "x"}, [], corpus_version=1)
    assert store.get_entry("ans:plain").status == HIT


def test_invalidating_a_chunk_removes_only_entries_that_cited_it(store, client):
    key_ab = exact_key("q1", "customer", 1, "p")
    key_c = exact_key("q2", "customer", 1, "p")
    store.put_entry(key_ab, {"a": 1}, CHUNKS[:2], corpus_version=1)
    store.put_entry(key_c, {"a": 2}, CHUNKS[2:], corpus_version=1)
    assert store.invalidate_chunk("chunk-a") == 1
    assert store.get_entry(key_ab).status == MISS
    assert store.get_entry(key_c).status == HIT
    assert not client.exists("chunk:chunk-a")


def test_invalidating_a_document_removes_every_entry_that_cited_any_of_its_chunks(store):
    key_ab = exact_key("q1", "customer", 1, "p")
    key_b = exact_key("q2", "staff", 1, "p")
    key_c = exact_key("q3", "customer", 1, "p")
    store.put_entry(key_ab, {}, CHUNKS[:2], corpus_version=1)
    store.put_entry(key_b, {}, CHUNKS[1:2], corpus_version=1)
    store.put_entry(key_c, {}, CHUNKS[2:], corpus_version=1)
    assert store.invalidate_doc("doc-1") == 2
    assert [store.get_entry(k).status for k in (key_ab, key_b, key_c)] == [MISS, MISS, HIT]


def test_invalidating_something_nobody_cited_is_a_quiet_zero(store):
    assert store.invalidate_chunk("never-cited") == 0
    assert store.invalidate_doc("never-cited") == 0


def test_a_stale_reverse_set_member_does_not_break_a_later_invalidation(store):
    key = exact_key("q", "customer", 1, "p")
    store.put_entry(key, {}, CHUNKS[:2], corpus_version=1)
    store.invalidate_chunk("chunk-a")  # removes the entry; chunk-b's set still lists it
    assert store.invalidate_chunk("chunk-b") == 0  # already gone: counted as zero, no error


def test_a_corrupt_entry_is_a_miss_and_is_removed(store, client, events):
    client.set("ans:bad", "{not json")
    assert store.get_entry("ans:bad").status == MISS
    assert not client.exists("ans:bad")
    assert ("cache_corrupt", {"operation": "get_entry"}) in events
    client.set("ans:list", json.dumps([1, 2]))
    assert store.get_entry("ans:list").status == MISS


# ---- the corpus version (10.3 item 1) -------------------------------------------------------


def test_the_corpus_version_starts_from_the_unix_time_in_milliseconds(store):
    assert store.corpus_version() == NOW_MS  # never 0, and not seconds
    assert store.bump_corpus_version() == NOW_MS + 1
    assert store.bump_corpus_version() == NOW_MS + 2
    assert store.corpus_version() == NOW_MS + 2


def test_a_first_bump_on_an_empty_cache_also_starts_from_the_time(store):
    assert store.bump_corpus_version() == NOW_MS + 1


def test_a_lost_version_restarts_above_every_earlier_value(client):
    clock = {"now": NOW}
    store = CacheStore(client, wall_clock=lambda: clock["now"])
    old_key = exact_key("q", "customer", store.corpus_version(), "p")
    store.put_entry(old_key, {"a": 1}, [], corpus_version=NOW_MS)
    for _ in range(5):
        store.bump_corpus_version()
    before = store.corpus_version()
    assert before == NOW_MS + 5
    client.delete("corpus:version")  # evicted, or lost
    clock["now"] = NOW + 3_600  # an hour later
    restarted = store.corpus_version()
    assert restarted == (NOW + 3_600) * 1000 > before  # higher than anything used before
    # An entry stored under an older version cannot be reached again: the key differs.
    assert exact_key("q", "customer", restarted, "p") != old_key
    assert store.get_entry(exact_key("q", "customer", restarted, "p")).status == MISS


def test_more_bumps_than_elapsed_seconds_followed_by_key_loss_still_restarts_higher(client):
    """The failure of a seconds-based seed: 2,000 bumps in 10 minutes put the counter at
    start + 2,000, above the clock's start + 600, so a restart from the clock went backwards.
    Seeded in milliseconds, the clock is at start + 600,000 and stays ahead."""
    clock = {"now": NOW}
    store = CacheStore(client, wall_clock=lambda: clock["now"])
    store.corpus_version()
    last = None
    for _ in range(2_000):
        last = store.bump_corpus_version()
    assert last == NOW_MS + 2_000
    elapsed_seconds = 600
    assert 2_000 > elapsed_seconds  # more bumps than seconds that passed
    clock["now"] = NOW + elapsed_seconds
    client.delete("corpus:version")
    restarted = store.corpus_version()
    assert restarted == (NOW + elapsed_seconds) * 1000
    assert restarted > last  # a seconds-based seed would have given NOW + 600, far below `last`
    # And a single bump after the restart goes on from there, never backwards.
    assert store.bump_corpus_version() == restarted + 1


def test_the_seed_and_the_increment_run_in_one_transaction(client, monkeypatch):
    kinds = []
    original = client.pipeline

    def spy(*args, **kwargs):
        kinds.append(kwargs.get("transaction", args[0] if args else True))
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "pipeline", spy)
    store = CacheStore(client, wall_clock=lambda: NOW)
    store.corpus_version()
    store.bump_corpus_version()
    assert kinds == [True, True]  # MULTI/EXEC: a vanished key cannot be re-created at 1 between


def test_an_increment_can_never_create_the_key_at_one(client):
    # The key is missing: a bare INCR would give 1. The bump seeds first, so it never does.
    store = CacheStore(client, wall_clock=lambda: NOW)
    assert not client.exists("corpus:version")
    assert store.bump_corpus_version() > 1_000_000_000_000  # milliseconds, never 1


def test_an_existing_version_is_never_overwritten_by_the_start_value(server, store):
    store.bump_corpus_version()
    later = CacheStore(
        fakeredis.FakeRedis(server=server, decode_responses=True), wall_clock=lambda: NOW + 999
    )
    assert later.corpus_version() == NOW_MS + 1  # SET ... NX left it alone


def test_the_version_is_shared_between_clients_of_the_same_server(server, store):
    other = CacheStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    store.bump_corpus_version()
    assert other.corpus_version() == NOW_MS + 1


def test_concurrent_bumps_are_never_lost(server):
    stores = [
        CacheStore(
            fakeredis.FakeRedis(server=server, decode_responses=True), wall_clock=lambda: NOW
        )
        for _ in range(8)
    ]

    def work(one):
        for _ in range(25):
            one.bump_corpus_version()

    threads = [threading.Thread(target=work, args=(one,)) for one in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert stores[0].corpus_version() == NOW_MS + 200


def test_a_garbled_version_is_reported_as_unavailable_not_as_a_number(store, client, events):
    client.set("corpus:version", "not-a-number")
    assert store.corpus_version() is None
    assert ("cache_corrupt", {"operation": "corpus_version"}) in events


# ---- counters and rate limits (12.4) ------------------------------------------------------------


def test_read_counter_reads_without_counting_or_starting_a_window(store, client):
    assert store.read_counter("tokens:u1") == 0  # a counter that does not exist yet
    assert not client.exists("ctr:tokens:u1")  # reading made nothing
    store.incr_counter("tokens:u1", 3_600, 120)
    for _ in range(3):
        assert store.read_counter("tokens:u1") == 120  # reading never adds
    assert 0 < client.ttl("ctr:tokens:u1") <= 3_600  # and never touches the window


def test_read_counter_on_a_dead_cache_is_unavailable(dead):
    assert dead.read_counter("tokens:u1") is None


def test_a_garbled_counter_reads_as_unavailable(store, client, events):
    client.set("ctr:tokens:u1", "not-a-number")
    assert store.read_counter("tokens:u1") is None
    assert ("cache_corrupt", {"operation": "read_counter"}) in events


def test_a_counter_adds_up_and_starts_its_window_on_the_first_increment(store, client):
    assert store.incr_counter("tokens:u1", 3_600, 120) == 120
    assert store.incr_counter("tokens:u1", 3_600, 30) == 150
    assert 0 < client.ttl("ctr:tokens:u1") <= 3_600


def test_the_window_is_not_stretched_by_later_increments(store, client):
    store.incr_counter("c", 100)
    client.expire("ctr:c", 10)  # pretend most of the window has passed
    store.incr_counter("c", 100)
    assert client.ttl("ctr:c") <= 10


def test_the_counter_disappears_when_the_window_ends(store, client):
    store.incr_counter("c", 1)
    client.delete("ctr:c")  # what Redis does when the expiry passes
    assert store.incr_counter("c", 60) == 1  # a fresh window


def test_a_counter_that_lost_its_expiry_gets_one_again(store, client):
    store.incr_counter("c", 60)
    client.persist("ctr:c")
    store.incr_counter("c", 60)
    assert client.ttl("ctr:c") > 0


@pytest.mark.parametrize("window", [0, -1, 1.5, "60", None, True])
def test_a_bad_window_is_refused_not_silently_ignored(store, window):
    with pytest.raises(ValueError, match="window_seconds"):
        store.incr_counter("c", window)
    with pytest.raises(ValueError, match="window_seconds"):
        store.check_rate_limit("chat", "user-1", limit=1, window_seconds=window)


@pytest.mark.parametrize("amount", [0, -5, 2.5, "3", None, True])
def test_a_bad_amount_is_refused(store, amount):
    with pytest.raises(ValueError, match="amount"):
        store.incr_counter("c", 60, amount)


@pytest.mark.parametrize("limit", [0, -1, 1.5, "5", None, True])
def test_a_bad_limit_is_refused(store, limit):
    with pytest.raises(ValueError, match="limit"):
        store.check_rate_limit("chat", "user-1", limit=limit, window_seconds=60)


def test_a_window_of_zero_cannot_switch_a_limit_off(store, client):
    with pytest.raises(ValueError):
        store.incr_counter("c", 0)
    assert not client.exists("ctr:c")  # nothing was counted or expired


@pytest.mark.parametrize(
    "subject",
    ["asha@example.test", "Asha Rao", "", "a/b", "name\n", "x" * 129, "\u00fcn\u00efcode"],
)
def test_a_rate_limit_subject_must_be_an_id_or_a_hash_not_a_name_or_an_address(store, subject):
    with pytest.raises(ValueError, match="ids or hashes"):
        store.check_rate_limit("chat", subject, limit=1, window_seconds=60)


@pytest.mark.parametrize(
    "subject",
    ["user-1", "3f2b8c1e-5d4a-4c3b-9a8e-1f2e3d4c5b6a", "9d8f" * 16, "ip:203.0.113.7", "u_1.2"],
)
def test_ids_and_hashes_are_accepted_as_subjects(store, subject):
    assert store.check_rate_limit("chat", subject, limit=1, window_seconds=60).allowed


def test_the_scope_is_checked_like_the_subject(store):
    with pytest.raises(ValueError):
        store.check_rate_limit("chat room", "user-1", limit=1, window_seconds=60)


def test_the_rate_limit_allows_up_to_the_limit_then_blocks_with_a_retry_time(store):
    decisions = [
        store.check_rate_limit("chat", "user-1", limit=3, window_seconds=60) for _ in range(5)
    ]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert [d.count for d in decisions] == [1, 2, 3, 4, 5]
    assert decisions[3].retry_after is not None and 0 < decisions[3].retry_after <= 60
    assert decisions[0].retry_after is None and all(d.available for d in decisions)


def test_rate_limits_are_kept_apart_per_scope_and_per_subject(store):
    store.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
    assert not store.check_rate_limit("chat", "user-1", limit=1, window_seconds=60).allowed
    assert store.check_rate_limit("chat", "user-2", limit=1, window_seconds=60).allowed
    assert store.check_rate_limit("login", "user-1", limit=1, window_seconds=60).allowed


# ---- Upstash down (11.1): never an error for the caller ---------------------------------------


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def dead_client():
    return FlakyRedis()


@pytest.fixture
def dead(dead_client, events, clock):
    return CacheStore(
        dead_client, on_event=lambda name, fields: events.append((name, fields)), clock=clock
    )


def test_a_lookup_on_a_dead_cache_says_unavailable_and_raises_nothing(dead, events):
    assert dead.get_entry("ans:k").status == UNAVAILABLE
    assert ("cache_unavailable", {"operation": "get_entry", "error": "ConnectionError"}) in events


DEAD_CALLS = {
    "corpus_version": (lambda s: s.corpus_version(), None),
    "bump_corpus_version": (lambda s: s.bump_corpus_version(), None),
    "put_entry": (lambda s: s.put_entry("ans:k", {}, CHUNKS, corpus_version=1), False),
    "invalidate_chunk": (lambda s: s.invalidate_chunk("chunk-a"), None),
    "invalidate_doc": (lambda s: s.invalidate_doc("doc-1"), None),
    "incr_counter": (lambda s: s.incr_counter("c", 60), None),
}


@pytest.mark.parametrize("name", sorted(DEAD_CALLS))
def test_each_call_reports_unavailable_through_its_own_failure_path(name, events):
    # A fresh store per call, so no skip window from an earlier failure can answer for it.
    call, expected = DEAD_CALLS[name]
    store = CacheStore(
        FlakyRedis(),
        on_event=lambda event, fields: events.append((event, fields)),
        clock=FakeClock(),
    )
    assert call(store) == expected
    assert events and events[0][0] == "cache_unavailable"  # it really tried and failed
    assert events[0][1]["error"] == "ConnectionError"


def test_a_dead_cache_looks_up_nothing_and_says_so_for_a_fresh_store(events):
    store = CacheStore(FlakyRedis(), clock=FakeClock())
    assert store.get_entry("ans:k").status == UNAVAILABLE


# ---- staleness writes are never skipped (10.3: invalidation is immediate) ---------------------

STALENESS = {
    "bump_corpus_version": lambda s: s.bump_corpus_version(),
    "invalidate_chunk": lambda s: s.invalidate_chunk("chunk-a"),
    "invalidate_doc": lambda s: s.invalidate_doc("doc-1"),
}
SKIPPED_READS = {
    "get_entry": lambda s: s.get_entry("ans:k"),
    "corpus_version": lambda s: s.corpus_version(),
    "incr_counter": lambda s: s.incr_counter("c", 60),
    "put_entry": lambda s: s.put_entry("ans:k", {}, [], corpus_version=1),
    "rate_limit": lambda s: s.check_rate_limit("chat", "user-1", limit=1, window_seconds=60),
}


@pytest.mark.parametrize("name", sorted(STALENESS))
def test_a_staleness_write_inside_the_skip_window_still_tries_and_alerts(
    name, dead, dead_client, events
):
    dead.get_entry("ans:k")  # a blip: the skip window is now open
    tried = dead_client.calls
    events.clear()
    assert STALENESS[name](dead) is None  # not done, and the caller can tell
    assert dead_client.calls > tried  # it tried anyway: the window did not skip it
    names = [event for event, _ in events]
    assert "cache_staleness_write_failed" in names
    alert = next(fields for event, fields in events if event == "cache_staleness_write_failed")
    assert alert == {"operation": name, "error": "ConnectionError"}  # names only


@pytest.mark.parametrize("name", sorted(SKIPPED_READS))
def test_reads_writes_of_entries_and_rate_limits_keep_skipping_inside_the_window(
    name, dead, dead_client
):
    dead.get_entry("ans:k")
    tried = dead_client.calls
    SKIPPED_READS[name](dead)
    assert dead_client.calls == tried  # not even tried: the window skipped it


@pytest.mark.parametrize("name", sorted(STALENESS))
def test_a_staleness_write_with_no_earlier_failure_also_alerts(name, dead, events):
    assert STALENESS[name](dead) is None
    assert [e for e, _ in events] == ["cache_unavailable", "cache_staleness_write_failed"]


def test_a_staleness_write_that_succeeds_inside_the_window_ends_the_window(client, events, clock):
    flaky = FlakyRedis(inner=client, down=True)
    store = CacheStore(
        flaky,
        on_event=lambda e, f: events.append((e, f)),
        clock=clock,
        wall_clock=lambda: NOW,
    )
    assert store.get_entry("ans:k").status == UNAVAILABLE  # the window opens
    flaky.down = False  # Upstash is back, but no time has passed
    assert store.get_entry("ans:k").status == UNAVAILABLE  # reads still skip
    assert store.bump_corpus_version() == NOW_MS + 1  # the bump tried, and worked
    assert store.get_entry("ans:k").status == MISS  # which proves Upstash is up: window over


def test_a_failed_invalidation_leaves_the_entry_in_place_so_a_retry_can_finish_it(
    server, client, events, clock
):
    store = CacheStore(client, wall_clock=lambda: NOW)
    key = exact_key("q", "customer", 1, "p")
    store.put_entry(key, {"a": 1}, CHUNKS[:1], corpus_version=1)
    flaky = FlakyRedis(inner=client, down=True)
    broken = CacheStore(flaky, on_event=lambda e, f: events.append((e, f)), clock=clock)
    assert broken.invalidate_doc("doc-1") is None  # not done
    assert store.get_entry(key).status == HIT  # still there: nothing was half-removed
    flaky.down = False
    assert broken.invalidate_doc("doc-1") == 1  # tried again at once, and done
    assert store.get_entry(key).status == MISS


# ---- put_entry: the reverse sets first, the entry last -----------------------------------------


class RecordingPipeline:
    """Collects the commands, then applies them one by one and stops at command `fail_at`."""

    def __init__(self, client, fail_at=None):
        self.client = client
        self.fail_at = fail_at
        self.commands = []
        self.refuse = set()  # command numbers that answer with a refusal (not a dropped line)

    def __getattr__(self, name):
        def add(*args, **kwargs):
            self.commands.append((name, args, kwargs))
            return self

        return add

    def execute(self, raise_on_error=True):
        results = []
        for index, (name, args, kwargs) in enumerate(self.commands):
            if index == self.fail_at:
                raise redis.exceptions.ConnectionError("connection dropped")
            try:
                if index in self.refuse:
                    raise redis.exceptions.ResponseError("WRONGTYPE refused")
                results.append(getattr(self.client, name)(*args, **kwargs))
            except redis.exceptions.ResponseError as error:
                if raise_on_error:
                    raise
                results.append(error)
        return results


class PipelineClient:
    """Hands out recording pipelines. `fail_at` drops the line at that command of pipeline number
    `fail_in` (0 = the one that registers the reverse sets, 1 = the one that writes the entry)."""

    def __init__(self, client, fail_at=None, fail_in=0):
        self.client = client
        self.fail_at = fail_at
        self.fail_in = fail_in
        self.pipelines = []

    @property
    def last(self):
        return self.pipelines[-1]

    @property
    def commands(self):
        return [command for pipe in self.pipelines for command in pipe.commands]

    def pipeline(self, *args, **kwargs):
        number = len(self.pipelines)
        pipe = RecordingPipeline(self.client, self.fail_at if number == self.fail_in else None)
        self.pipelines.append(pipe)
        return pipe

    def __getattr__(self, name):  # every other command goes straight to the real client
        return getattr(self.client, name)


def test_put_entry_writes_every_reverse_set_before_the_entry(client):
    wrapper = PipelineClient(client)
    store = CacheStore(wrapper, wall_clock=lambda: NOW)
    key = exact_key("q", "customer", 1, "p")
    assert store.put_entry(key, {"a": 1}, CHUNKS, corpus_version=1) is True
    names = [name for name, _, _ in wrapper.commands]
    assert names[-1] == "set"  # the entry is written last
    assert names.count("set") == 1 and set(names[:-1]) == {"sadd", "expire"}
    assert names[:-1].count("sadd") == 5  # chunk-a, chunk-b, chunk-c, doc-1, doc-2


@pytest.mark.parametrize("fail_at", range(0, 10))
def test_a_drop_at_any_point_never_leaves_an_entry_that_no_invalidation_can_find(client, fail_at):
    wrapper = PipelineClient(client, fail_at=fail_at)  # while the reverse sets are registered
    store = CacheStore(wrapper, wall_clock=lambda: NOW, clock=FakeClock())
    key = exact_key("q", "customer", 1, "p")
    assert store.put_entry(key, {"a": 1}, CHUNKS, corpus_version=1) is False
    assert not client.exists(key)  # the entry is written last, so it was never written
    # Whatever reverse sets were written name a key with no entry: invalidating is a harmless 0.
    healthy = CacheStore(client, wall_clock=lambda: NOW)
    assert healthy.invalidate_doc("doc-1") == 0 and healthy.invalidate_doc("doc-2") == 0


def test_a_drop_after_everything_but_the_last_command_is_the_worst_case_and_is_safe(client):
    wrapper = PipelineClient(client, fail_at=0, fail_in=1)  # the SET: the entry's own pipeline
    store = CacheStore(wrapper, wall_clock=lambda: NOW, clock=FakeClock())
    key = exact_key("q", "customer", 1, "p")
    store.put_entry(key, {"a": 1}, CHUNKS, corpus_version=1)
    assert client.smembers("chunk:chunk-a") == {key}  # the sets list it
    assert client.get(key) is None  # but there is no entry to serve


def test_every_dead_call_still_reports_unavailable_in_order(dead):
    # Kept as a smoke test of the whole surface on one store.
    assert dead.corpus_version() is None
    assert dead.bump_corpus_version() is None
    assert dead.put_entry("ans:k", {}, CHUNKS, corpus_version=1) is False
    assert dead.invalidate_chunk("chunk-a") is None
    assert dead.invalidate_doc("doc-1") is None
    assert dead.incr_counter("c", 60) is None


def test_after_a_failure_upstash_is_not_called_again_for_30_seconds(dead, dead_client, clock):
    assert UNAVAILABLE_SKIP_SECONDS == 30.0
    dead.get_entry("ans:k")
    tried = dead_client.calls
    assert tried >= 1
    for _ in range(20):  # a burst of requests during the outage
        assert dead.get_entry("ans:k").status == UNAVAILABLE
        assert dead.corpus_version() is None
        decision = dead.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
        assert decision.available is False
    assert dead_client.calls == tried  # not one more call: no timeout was paid again
    clock.advance(29.9)
    dead.get_entry("ans:k")
    assert dead_client.calls == tried  # still inside the window


def test_the_first_call_after_the_window_tries_again_and_a_failure_starts_a_new_window(
    dead, dead_client, clock, events
):
    dead.get_entry("ans:k")
    tried = dead_client.calls
    clock.advance(30.1)
    dead.get_entry("ans:k")
    assert dead_client.calls > tried  # it tried again
    assert len([e for e in events if e[0] == "cache_unavailable"]) == 2  # one event per failure
    tried = dead_client.calls
    dead.get_entry("ans:k")
    assert dead_client.calls == tried  # and the new window skips again


def test_the_same_store_recovers_when_upstash_comes_back(client, events, clock):
    flaky = FlakyRedis(inner=client, down=True)
    store = CacheStore(
        flaky,
        on_event=lambda name, fields: events.append((name, fields)),
        clock=clock,
        wall_clock=lambda: NOW,
    )
    assert store.put_entry("ans:k", {"answer": "x"}, [], corpus_version=1) is False
    flaky.down = False  # Upstash is back, but the store keeps away until the window ends
    assert store.get_entry("ans:k").status == UNAVAILABLE
    clock.advance(31)
    assert store.put_entry("ans:k", {"answer": "x"}, [], corpus_version=1) is True
    found = store.get_entry("ans:k")
    assert found.status == HIT and found.entry["payload"] == {"answer": "x"}
    assert store.corpus_version() == NOW_MS  # the version works again too


def test_the_rate_limit_fails_open_with_an_alert_event_every_time(dead, events):
    for _ in range(3):
        decision = dead.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
        assert decision.allowed and not decision.available and decision.count is None
    alerts = [e for e in events if e == ("rate_limit_unavailable", {"scope": "chat"})]
    assert len(alerts) == 3  # the skip window never hides the alert for a request let through


def test_events_and_logs_carry_no_address_or_error_text(dead, events, caplog):
    with caplog.at_level("DEBUG"):
        dead.get_entry("ans:k")
        dead.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
    everything = repr(events) + caplog.text
    assert "secret-host" not in everything and "111" not in everything


def test_an_alert_handler_that_raises_cannot_break_a_request(dead_client, clock, caplog):
    def broken(name, fields):
        raise RuntimeError("the alert webhook is down")

    store = CacheStore(dead_client, on_event=broken, clock=clock)
    assert store.get_entry("ans:k").status == UNAVAILABLE  # no exception reaches the caller
    assert store.check_rate_limit("chat", "user-1", limit=1, window_seconds=60).allowed
    assert "webhook" not in caplog.text  # the handler's own message is not copied into the log


# ---- the client factory ----------------------------------------------------------------------


def test_an_unset_url_is_an_error():
    for value in (None, "", "  "):
        with pytest.raises(CacheConfigError, match="UPSTASH_REDIS_URL is not set"):
            make_client(value)
    with pytest.raises(CacheConfigError):
        client_from_env({})


def test_a_remote_host_needs_tls_and_the_error_never_shows_the_url():
    url = "redis://default:not-a-real-password@example-host.upstash.test:6379"
    with pytest.raises(CacheConfigError) as caught:
        make_client(url)
    assert "rediss://" in str(caught.value)
    assert "not-a-real-password" not in str(caught.value) and "example-host" not in str(
        caught.value
    )


def test_a_tls_url_and_a_local_url_build_a_client_without_connecting():
    secure = make_client("rediss://default:not-a-real-password@example-host.upstash.test:6379")
    assert secure.connection_pool.connection_kwargs["socket_timeout"] == 2.0  # short: never stalls
    assert make_client("redis://localhost:6379/0") is not None


def test_the_ttl_setting_defaults_to_24_hours_and_is_validated():
    assert ttl_from_env({}) == ttl_from_env({"CACHE_TTL_SECONDS": "  "}) == 86_400
    assert ttl_from_env({"CACHE_TTL_SECONDS": " 600 "}) == 600
    for bad in ("0", "-1", "soon", "1.5", "nan"):
        with pytest.raises(CacheConfigError, match="CACHE_TTL_SECONDS"):
            ttl_from_env({"CACHE_TTL_SECONDS": bad})


def test_a_store_is_built_from_the_environment_without_connecting():
    env = {"UPSTASH_REDIS_URL": "redis://localhost:6379/0", "CACHE_TTL_SECONDS": "900"}
    assert store_from_env(env).ttl_seconds == 900
    with pytest.raises(CacheConfigError):
        store_from_env({"CACHE_TTL_SECONDS": "900"})  # no URL


def test_the_ttl_variable_is_documented_with_its_default_and_a_proposal_tag():
    from pathlib import Path

    lines = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8").splitlines()
    at = lines.index("CACHE_TTL_SECONDS=")  # the name is present and its value is empty
    block = []
    for line in reversed(lines[:at]):  # the comment block directly above the variable
        if not line.startswith("#"):
            break
        block.append(line)
    comment = " ".join(block)
    assert "86400" in comment and "[P]" in comment and "Where: Render and local" in comment


def test_a_local_cache_is_refused_on_render():
    with pytest.raises(CacheConfigError, match="Render"):
        make_client("redis://localhost:6379/0", env={"RENDER": "true"})
