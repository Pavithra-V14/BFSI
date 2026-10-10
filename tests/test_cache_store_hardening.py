"""Phase 9 hardening of the cache store (spec 10.1 to 10.3, deferred Phase 3 findings 5, 7, 11, 13):
a refusal of one command is not an outage, a put never leaves an entry that no invalidation can
find, a huge reverse set is removed in batches, and the role map is part of the key."""

import fakeredis
import pytest
import redis

from app.cache.store import (
    INVALIDATE_BATCH,
    UNAVAILABLE_SKIP_SECONDS,
    CacheStore,
    ChunkRef,
    exact_key,
)
from app.pipeline.roles import VISIBLE_ROLES, role_map_version

NOW = 1_800_000_000.0
CHUNK = ChunkRef("chunk-a", "hash-a", "doc-1")


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def events():
    return []


@pytest.fixture
def store(client, events):
    return CacheStore(
        client,
        wall_clock=lambda: NOW,
        clock=Clock(),
        on_event=lambda name, fields: events.append((name, fields)),
    )


def names(events):
    return [name for name, _ in events]


# ---- a refusal is not an outage --------------------------------------------------------------
def test_a_wrong_type_key_is_one_refusal_and_does_not_open_the_skip_window(client, store, events):
    client.lpush("corpus:version", "x")  # the version key holds a list: GET is refused (WRONGTYPE)
    assert store.corpus_version() is None
    assert ("cache_error", {"operation": "corpus_version", "error": "ResponseError"}) in events
    assert "cache_unavailable" not in names(events)
    assert store._skip_until == 0.0  # no skip window
    # the other calls carry on at once, with no 30 s wait
    assert store.get_entry("ans:other").status == "miss"
    assert store.put_entry("ans:k", {"a": 1}, [CHUNK], corpus_version=1) is True


def test_a_refusal_does_not_make_the_rate_limiter_let_everyone_through(client, store, events):
    client.lpush("corpus:version", "x")
    assert store.corpus_version() is None  # one refusal
    first = store.check_rate_limit("chat", "user-b", limit=1, window_seconds=60)
    second = store.check_rate_limit("chat", "user-b", limit=1, window_seconds=60)
    assert first.available and first.allowed and first.count == 1
    assert second.available and not second.allowed and second.count == 2  # counted, then blocked
    assert "rate_limit_unavailable" not in names(events)


def test_a_counter_key_of_the_wrong_type_heals_and_keeps_counting(client, store, events):
    client.lpush("ctr:rl:chat:user-w", "x")
    decision = store.check_rate_limit("chat", "user-w", limit=2, window_seconds=60)
    assert decision.available and decision.count == 1 and decision.allowed
    assert store.check_rate_limit("chat", "user-w", limit=2, window_seconds=60).count == 2
    assert ("cache_error", {"operation": "incr_counter", "error": "WrongTypeHealed"}) in events
    assert store._skip_until == 0.0


class DeadClient:
    """Every command is a refused connection (a real outage)."""

    def __init__(self):
        self.calls = 0

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls += 1
            raise redis.exceptions.ConnectionError("connection refused")

        return fail

    def pipeline(self, *args, **kwargs):
        self.calls += 1
        raise redis.exceptions.ConnectionError("connection refused")


def test_a_real_connection_error_still_opens_the_30_second_skip_window():
    clock = Clock()
    dead = DeadClient()
    events = []
    store = CacheStore(dead, clock=clock, on_event=lambda n, f: events.append(n))
    assert store.get_entry("ans:k").status == "unavailable"
    assert events.count("cache_unavailable") == 1
    tried = dead.calls
    for _ in range(10):  # during the window: no call reaches the server
        assert store.get_entry("ans:k").status == "unavailable"
    assert dead.calls == tried
    clock.now += UNAVAILABLE_SKIP_SECONDS + 1  # after the window it tries again
    store.get_entry("ans:k")
    assert dead.calls == tried + 1


def test_a_connection_error_of_a_staleness_write_is_an_alert_and_never_skipped(events):
    clock = Clock()
    dead = DeadClient()
    store = CacheStore(dead, clock=clock, on_event=lambda n, f: events.append((n, f)))
    store.get_entry("ans:k")  # opens the window
    before = dead.calls
    assert store.invalidate_chunk("chunk-a") is None  # a staleness write ignores the window
    assert dead.calls > before and "cache_staleness_write_failed" in names(events)


# ---- put_entry: no entry that no invalidation can find ---------------------------------------
def test_a_reverse_set_of_the_wrong_type_is_replaced_and_the_entry_is_listed(client, store):
    client.set("chunk:chunk-a", "not a set")
    key = exact_key("q", "customer", 1, "p")
    assert store.put_entry(key, {"a": 1}, [CHUNK], corpus_version=1) is True
    assert client.smembers("chunk:chunk-a") == {key}
    assert client.get(key) is not None


def test_an_entry_is_not_written_when_a_reverse_set_cannot_be_registered(client, store, events):
    client.set("chunk:chunk-a", "not a set")
    real_delete = client.delete

    def keep_the_bad_key(*keys):  # a server that will not let the bad key go
        kept = [k for k in keys if k != "chunk:chunk-a"]
        return real_delete(*kept) if kept else 0

    client.delete = keep_the_bad_key
    key = exact_key("q", "customer", 1, "p")
    assert store.put_entry(key, {"a": 1}, [CHUNK], corpus_version=1) is False
    assert not client.exists(key)  # never an entry that no invalidation can find
    assert ("cache_error", {"operation": "put_entry", "error": "ReverseSetRefused"}) in events


# ---- invalidation in batches -----------------------------------------------------------------
def test_a_huge_reverse_set_is_removed_in_batches_never_in_one_command(client, store, monkeypatch):
    members = [f"ans:{n}" for n in range(1_200)]
    pipe = client.pipeline(transaction=False)
    for member in members:
        pipe.set(member, "1")
        pipe.sadd("chunk:big", member)
    pipe.execute()
    sizes = []
    real_delete = client.delete

    def spy(*keys):
        sizes.append(len(keys))
        return real_delete(*keys)

    monkeypatch.setattr(client, "delete", spy)
    assert store.invalidate_chunk("big") == 1_200
    assert max(sizes) <= INVALIDATE_BATCH and len([s for s in sizes if s > 1]) >= 3
    assert client.keys("ans:*") == [] and not client.exists("chunk:big")


def test_an_invalidation_that_stops_half_way_is_finished_by_running_it_again(client, store):
    members = [f"ans:{n}" for n in range(30)]
    pipe = client.pipeline(transaction=False)
    for member in members:
        pipe.set(member, "1")
        pipe.sadd("chunk:c", member)
    pipe.execute()
    real_delete = client.delete
    calls = {"n": 0}

    def flaky(*keys):
        calls["n"] += 1
        if calls["n"] == 1:
            raise redis.exceptions.ConnectionError("dropped")
        return real_delete(*keys)

    client.delete = flaky
    assert store.invalidate_chunk("c") is None  # not done
    assert client.exists("chunk:c")  # the set is still there: nothing is lost
    assert store.invalidate_chunk("c") == 30 and not client.exists("chunk:c")


# ---- the namespace (used only by the owner-run real check) -----------------------------------
def test_a_namespace_keeps_every_key_under_its_prefix(client):
    store = CacheStore(client, wall_clock=lambda: NOW, namespace="check:abc:")
    key = exact_key("q", "customer", 1, "p")
    store.corpus_version()
    store.put_entry(key, {"a": 1}, [CHUNK], corpus_version=1)
    store.check_rate_limit("chat", "u", limit=5, window_seconds=60)
    assert client.keys("*") and all(k.startswith("check:abc:") for k in client.keys("*"))
    plain = CacheStore(client, wall_clock=lambda: NOW)
    assert plain.get_entry(key).status == "miss"  # a store without the prefix sees none of it
    assert store.get_entry(key).status == "hit"
    assert store.invalidate_doc("doc-1") == 1 and store.get_entry(key).status == "miss"


# ---- the role map is part of the key ---------------------------------------------------------
def test_the_role_map_version_is_stable_and_changes_with_the_map():
    original = role_map_version()
    assert original == role_map_version()
    assert role_map_version(dict(reversed(list(VISIBLE_ROLES.items())))) == original  # order
    changed = {**VISIBLE_ROLES, "customer": ("customer", "staff")}
    assert role_map_version(changed) != original
    assert role_map_version({**VISIBLE_ROLES, "intern": ("customer",)}) != original


def test_the_exact_key_changes_with_the_role_map_version():
    base = exact_key("q", "customer", 1, "p", role_map_version())
    assert exact_key("q", "customer", 1, "p", "another") != base
    assert exact_key("q", "customer", 1, "p", role_map_version()) == base
