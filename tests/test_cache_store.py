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
    return CacheStore(client, on_event=lambda name, fields: events.append((name, fields)))


class DeadRedis:
    """Every call fails like a cache that cannot be reached."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.exceptions.ConnectionError(
                "Error 111 connecting to secret-host.example:6379"
            )

        return fail

    def pipeline(self, *args, **kwargs):
        return self


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
    assert normalise_query("ＡＢＣ  Ⅻ") == "abc xii"  # compatibility forms are folded


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


def test_the_corpus_version_starts_at_zero_and_every_bump_adds_one(store):
    assert store.corpus_version() == 0
    assert store.bump_corpus_version() == 1
    assert store.bump_corpus_version() == 2
    assert store.corpus_version() == 2


def test_the_version_is_shared_between_clients_of_the_same_server(server, store):
    other = CacheStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    store.bump_corpus_version()
    assert other.corpus_version() == 1


def test_concurrent_bumps_are_never_lost(server):
    stores = [
        CacheStore(fakeredis.FakeRedis(server=server, decode_responses=True)) for _ in range(8)
    ]

    def work(one):
        for _ in range(25):
            one.bump_corpus_version()

    threads = [threading.Thread(target=work, args=(one,)) for one in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert stores[0].corpus_version() == 200


def test_a_garbled_version_is_reported_as_unavailable_not_as_zero(store, client, events):
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
def dead(events):
    return CacheStore(DeadRedis(), on_event=lambda name, fields: events.append((name, fields)))


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


def test_the_rate_limit_fails_open_with_an_alert_event(dead, events):
    decision = dead.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
    assert decision.allowed and not decision.available and decision.count is None
    assert ("rate_limit_unavailable", {"scope": "chat"}) in events


def test_events_and_logs_carry_no_address_or_error_text(dead, events, caplog):
    with caplog.at_level("DEBUG"):
        dead.get_entry("ans:k")
        dead.check_rate_limit("chat", "user-1", limit=1, window_seconds=60)
    everything = repr(events) + caplog.text
    assert "secret-host" not in everything and "111" not in everything


def test_a_cache_that_comes_back_works_again(server, events):
    store = CacheStore(DeadRedis())
    assert store.get_entry("ans:k").status == UNAVAILABLE
    healthy = CacheStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    assert healthy.get_entry("ans:k").status == MISS


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


def test_the_ttl_variable_is_documented_in_the_env_template():
    from pathlib import Path

    template = (Path(__file__).resolve().parents[1] / ".env.example").read_text("utf-8")
    assert "\nCACHE_TTL_SECONDS=\n" in template and "[P]" in template


def test_a_local_cache_is_refused_on_render():
    with pytest.raises(CacheConfigError, match="Render"):
        make_client("redis://localhost:6379/0", env={"RENDER": "true"})
