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

NOW = 1_800_000_000  # a fixed "current Unix time" for the corpus version


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


def test_the_corpus_version_starts_from_the_unix_time_and_every_bump_adds_one(store):
    assert store.corpus_version() == NOW  # never 0
    assert store.bump_corpus_version() == NOW + 1
    assert store.bump_corpus_version() == NOW + 2
    assert store.corpus_version() == NOW + 2


def test_a_first_bump_on_an_empty_cache_also_starts_from_the_time(store):
    assert store.bump_corpus_version() == NOW + 1


def test_a_lost_version_restarts_above_every_earlier_value(client):
    clock = {"now": NOW}
    store = CacheStore(client, wall_clock=lambda: clock["now"])
    old_key = exact_key("q", "customer", store.corpus_version(), "p")
    store.put_entry(old_key, {"a": 1}, [], corpus_version=NOW)
    for _ in range(5):
        store.bump_corpus_version()
    before = store.corpus_version()
    assert before == NOW + 5
    client.delete("corpus:version")  # evicted, or lost
    clock["now"] = NOW + 3_600  # an hour later
    restarted = store.corpus_version()
    assert restarted == NOW + 3_600 > before  # higher than anything used before
    # An entry stored under an older version cannot be reached again: the key differs.
    assert exact_key("q", "customer", restarted, "p") != old_key
    assert store.get_entry(exact_key("q", "customer", restarted, "p")).status == MISS


def test_an_existing_version_is_never_overwritten_by_the_start_value(server, store):
    store.bump_corpus_version()
    later = CacheStore(
        fakeredis.FakeRedis(server=server, decode_responses=True), wall_clock=lambda: NOW + 999
    )
    assert later.corpus_version() == NOW + 1  # SET ... NX left it alone


def test_the_version_is_shared_between_clients_of_the_same_server(server, store):
    other = CacheStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    store.bump_corpus_version()
    assert other.corpus_version() == NOW + 1


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
    assert stores[0].corpus_version() == NOW + 200


def test_a_garbled_version_is_reported_as_unavailable_not_as_a_number(store, client, events):
    client.set("corpus:version", "not-a-number")
    assert store.corpus_version() is None
    assert ("cache_corrupt", {"operation": "corpus_version"}) in events


# ---- counters and rate limits (12.4) ------------------------------------------------------------


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


def test_every_other_call_on_a_dead_cache_reports_unavailable(dead):
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
    assert store.corpus_version() == NOW  # the version works again too


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
