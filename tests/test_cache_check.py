"""scripts/cache_check.py (Phase 9, owner-run): a unique throw-away prefix only, never a real key
family, no flush, its keys removed in a finally, names and counts only, nothing without --run."""

import fakeredis
import pytest
import redis

from app.cache import store as cache_store
from scripts import cache_check

REAL = {
    "ans:0123456789abcdef": "an answer entry",
    "chunk:real-chunk": None,  # a set
    "doc:real-doc": None,  # a set
    "ctr:rl:chat:real-user": "7",
    "corpus:version": "1800000000000",
    "rl:chat:real-user": "1",
}


@pytest.fixture
def client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def real_keys(client):
    for name, value in REAL.items():
        if value is None:
            client.sadd(name, "member-1", "member-2")
        else:
            client.set(name, value)
    return dict.fromkeys(REAL)


class Down:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise redis.exceptions.ConnectionError("refused")

        return fail

    def pipeline(self, *args, **kwargs):
        raise redis.exceptions.ConnectionError("refused")


def untouched(client):
    assert client.get("ans:0123456789abcdef") == "an answer entry"
    assert client.smembers("chunk:real-chunk") == {"member-1", "member-2"}
    assert client.smembers("doc:real-doc") == {"member-1", "member-2"}
    assert client.get("ctr:rl:chat:real-user") == "7"
    assert client.get("corpus:version") == "1800000000000"
    assert client.get("rl:chat:real-user") == "1"


def test_every_check_passes_and_every_key_it_made_is_removed(client, real_keys):
    lines = []

    def report(name, status, **details):
        lines.append((name, status, details))

    prefix = cache_check.throwaway_prefix()
    problems = cache_check.run_check(client, prefix, report, dead_client=Down())
    assert problems == 0, [line for line in lines if line[1] == "FAIL"]
    assert {status for _, status, _ in lines} <= {"PASS", "DONE", "INFO"}
    assert len([1 for _, status, _ in lines if status == "PASS"]) >= 9
    assert sorted(client.keys("*")) == sorted(REAL)  # exactly the real keys: nothing of ours left
    untouched(client)  # and the real key families were never read into, written or deleted


def test_it_never_flushes_and_only_removes_keys_under_its_prefix(client, real_keys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the check must never flush")

    monkeypatch.setattr(client, "flushall", forbidden)
    monkeypatch.setattr(client, "flushdb", forbidden)
    deleted = []
    real_delete = client.delete

    def spy(*keys):
        deleted.extend(keys)
        return real_delete(*keys)

    monkeypatch.setattr(client, "delete", spy)
    prefix = cache_check.throwaway_prefix()
    cache_check.run_check(client, prefix, lambda *a, **k: None, dead_client=Down())
    assert deleted and all(k.startswith(prefix) for k in deleted)
    untouched(client)


def test_a_failure_in_the_middle_still_removes_the_keys(client, real_keys, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("something went wrong half way")

    monkeypatch.setattr(cache_store.CacheStore, "invalidate_chunk", boom)
    prefix = cache_check.throwaway_prefix()
    with pytest.raises(RuntimeError):
        cache_check.run_check(client, prefix, lambda *a, **k: None)
    assert sorted(client.keys("*")) == sorted(REAL)  # the finally removed what was made
    untouched(client)


@pytest.mark.parametrize(
    "bad",
    ["", "ans:", "check:", "check:abc:", "chunk:", "corpus:version", "check:ABCDEF123456:", "x"],
)
def test_only_a_unique_throwaway_prefix_is_allowed(bad):
    with pytest.raises(cache_check.RefusedPrefix):
        cache_check.guard_prefix(bad)
    with pytest.raises(cache_check.RefusedPrefix):
        cache_check.remove_prefixed(fakeredis.FakeRedis(), bad)


def test_the_made_up_prefix_is_unique_and_well_formed():
    first, second = cache_check.throwaway_prefix(), cache_check.throwaway_prefix()
    assert first != second
    assert cache_check.guard_prefix(first) == first
    assert cache_check.PREFIX_PATTERN.fullmatch(first)


def test_nothing_happens_without_run(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not connect without --run")

    monkeypatch.setattr(cache_store, "client_from_env", forbidden)
    assert cache_check.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was done. Add --run" in out and "throw-away" in out


def test_with_run_it_prints_names_and_counts_only_never_a_value_or_an_address(
    client, real_keys, capsys, monkeypatch
):
    secret_url = "rediss://default:" + "p4ssw0rd-not-printed" + "@private-host.example:6379"
    monkeypatch.setattr(cache_store, "client_from_env", lambda env: client)
    code = cache_check.main(["--run"], env={"UPSTASH_REDIS_URL": secret_url})
    out = capsys.readouterr().out
    assert code in (0, 1)  # (the "refused connection" check uses a real closed local port)
    assert "using a throw-away key prefix check:" in out
    for value in (*REAL.values(), "p4ssw0rd-not-printed", "private-host", "an answer entry"):
        if value and len(value) > 3:  # (a one-digit counter value would match any number)
            assert value not in out
    assert "throw-away keys removed" in out and "removed=" in out
    untouched(client)


def test_a_missing_url_is_a_setting_error_that_names_the_variable(capsys):
    code = cache_check.main(["--run"], env={})
    err = capsys.readouterr().err
    assert code == 1 and "UPSTASH_REDIS_URL is not set" in err
