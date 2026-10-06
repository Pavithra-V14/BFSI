"""Phase 4 hardening: the database opens lazily, logout is not rate limited, a blocked address
creates no counters, throttle alerts, the password-hash gate, HEAD on /live, READ COMMITTED."""

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import timedelta

import fakeredis
import httpx
import pytest

from app.accounts import passwords, tokens
from app.accounts.gate import DEFAULT_LIMIT, HashBusy, HashGate
from app.accounts.throttle import (
    LOGIN_PER_IP,
    LOGIN_PER_USERNAME,
    InProcessCounters,
    Throttle,
)
from app.cache.store import CacheStore
from app.store_errors import DatabaseUnavailable
from tests.api_fixtures import AUDIT_KEY, PASSWORD, SOCKET_ADDRESS, Env, now
from tests.test_cache_store import FakeClock

# ---- the database opens lazily ---------------------------------------------------------------


def count_opens(env):
    opened = []
    original = env.app.state.services.db_factory

    def counting():
        opened.append(1)
        return original()

    env.app.state.services.db_factory = counting
    return opened


def database_down(env):
    def down():
        raise DatabaseUnavailable("OperationalError")

    env.app.state.services.db_factory = down


ANONYMOUS_REQUESTS = [
    ("GET", "/auth/me", {}),
    ("POST", "/auth/logout", {}),
    ("GET", "/health", {}),
    ("PUT", "/admin/users/abc/role", {"json": {"role": "staff"}}),
    ("GET", "/auth/me", {"headers": {"Authorization": "Bearer garbage"}}),
    ("GET", "/auth/me", {"headers": {"Authorization": "Bearer a.b.c"}}),
    ("GET", "/auth/me", {"headers": {"Authorization": "Basic abc"}}),
    ("GET", "/auth/me", {"headers": {"Authorization": ""}}),
]


@pytest.mark.parametrize(("method", "url", "options"), ANONYMOUS_REQUESTS)
def test_a_request_with_no_valid_token_never_opens_a_database_connection(env, method, url, options):
    opened = count_opens(env)
    response = env.client.request(method, url, **options)
    assert response.status_code == 401 and opened == []


@pytest.mark.parametrize(("method", "url", "options"), ANONYMOUS_REQUESTS)
def test_an_anonymous_request_gets_its_401_even_while_the_database_is_down(
    env, method, url, options
):
    database_down(env)
    response = env.client.request(method, url, **options)
    assert response.status_code == 401  # not a 503


def test_an_expired_or_forged_token_never_opens_a_connection(env):
    env.register()
    user_id = env.rows("SELECT id FROM users")[0][0]
    expired, _ = tokens.issue_token(env.settings, user_id, now() - timedelta(days=3))
    from tests.api_fixtures import OTHER_SECRET, make_settings

    forged, _ = tokens.issue_token(make_settings(jwt_secret=OTHER_SECRET), user_id, now())
    opened = count_opens(env)
    for token in (expired, forged):
        assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401
    assert opened == []


def test_a_valid_token_during_a_database_outage_is_a_503_not_a_401(env):
    env.register()
    token = env.token()
    database_down(env)
    response = env.client.get("/auth/me", headers=env.bearer(token))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_a_genuine_request_opens_exactly_one_connection(env):
    env.register()
    token = env.token()
    admin_token = env.admin_token()
    opened = count_opens(env)
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 200
    assert len(opened) == 1
    assert env.client.post("/auth/logout", headers=env.bearer(token)).status_code == 204
    assert len(opened) == 2  # one more, not two
    assert env.client.get("/health", headers=env.bearer(admin_token)).status_code == 200
    assert len(opened) == 3


def test_a_user_over_the_rate_limit_is_refused_before_the_database_is_opened(env):
    env.register()
    token = env.token()
    for _ in range(20):
        env.client.get("/auth/me", headers=env.bearer(token))
    opened = count_opens(env)
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 429
    assert opened == []


def test_a_throttled_login_and_an_invalid_registration_open_no_connection(env):
    for index in range(LOGIN_PER_USERNAME):
        env.login("asha_rao", f"wrong password {index}")
    opened = count_opens(env)
    assert env.login("asha_rao", "wrong password again").status_code == 429
    assert env.register("ab").status_code == 422  # a username that is too short
    assert opened == []


def test_the_connection_is_closed_at_the_end_of_the_request(env):
    env.register()
    token = env.token()
    closed = []
    original = env.app.state.services.db_factory

    def tracking():
        database = original()
        real_close = database.close
        database.close = lambda: (closed.append(1), real_close())[1]
        return database

    env.app.state.services.db_factory = tracking
    env.client.get("/auth/me", headers=env.bearer(token))
    assert closed == [1]


def test_a_request_that_never_used_the_database_has_nothing_to_close(env):
    opened = count_opens(env)
    assert env.client.get("/auth/me").status_code == 401
    assert opened == []


# ---- logout does not count against the per-user limit ------------------------------------------


def test_a_user_at_the_limit_can_still_log_out_and_revoke_the_token(env):
    env.register()
    token = env.token()
    codes = [env.client.get("/auth/me", headers=env.bearer(token)).status_code for _ in range(21)]
    assert codes[-1] == 429  # the user is at the limit
    out = env.client.post("/auth/logout", headers=env.bearer(token))
    assert out.status_code == 204  # logout still works
    assert env.rows("SELECT COUNT(*) FROM revoked_tokens") == [(1,)]
    for key in env.redis.keys("ctr:rl:user:*"):
        env.redis.delete(key)  # a new minute
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401  # revoked


def test_logging_out_never_adds_to_the_users_count(env):
    user_id = env.register().json()["id"]
    for _ in range(25):  # more than the limit, each with its own token
        token, _ = tokens.issue_token(env.settings, user_id, now())
        assert env.client.post("/auth/logout", headers=env.bearer(token)).status_code == 204
    assert env.redis.keys("ctr:rl:user:*") == []


def test_a_thief_using_up_the_limit_cannot_stop_the_owner_revoking_the_token(env):
    env.register()
    stolen = env.token()  # the thief and the owner hold the same token
    for _ in range(40):
        env.client.get("/auth/me", headers=env.bearer(stolen))  # the thief floods
    assert env.client.get("/auth/me", headers=env.bearer(stolen)).status_code == 429
    assert env.client.post("/auth/logout", headers=env.bearer(stolen)).status_code == 204


def test_logout_still_refuses_a_bad_or_revoked_token(env):
    env.register()
    token = env.token()
    assert env.client.post("/auth/logout", headers=env.bearer("garbage")).status_code == 401
    env.client.post("/auth/logout", headers=env.bearer(token))
    assert env.client.post("/auth/logout", headers=env.bearer(token)).status_code == 401


def test_every_other_signed_in_route_still_counts(env):
    token = env.admin_token()
    for url in ("/auth/me", "/health"):
        for key in env.redis.keys("ctr:rl:user:*"):
            env.redis.delete(key)
        codes = [env.client.get(url, headers=env.bearer(token)).status_code for _ in range(21)]
        assert codes[-1] == 429, url


# ---- a blocked address creates no counters -----------------------------------------------------


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_once_an_address_is_blocked_new_usernames_create_no_upstash_keys(redis_client):
    throttle = Throttle(CacheStore(redis_client), AUDIT_KEY)
    for index in range(LOGIN_PER_IP):
        assert throttle.check_login(f"user{index}", "203.0.113.5").allowed
    assert not throttle.check_login("user-x", "203.0.113.5").allowed  # the address is blocked
    keys_before = set(redis_client.keys("*"))
    for index in range(200):
        decision = throttle.check_login(f"random-name-{index}", "203.0.113.5")
        assert not decision.allowed and decision.scope == "ip"
    assert set(redis_client.keys("*")) == keys_before  # not one new key


def test_once_an_address_is_blocked_the_stand_in_grows_no_entries():
    throttle = Throttle(None, AUDIT_KEY, clock=FakeClock())
    for index in range(LOGIN_PER_IP):
        throttle.check_login(f"user{index}", "203.0.113.5")
    size = len(throttle._fallback._entries)
    for index in range(500):
        assert not throttle.check_login(f"random-name-{index}", "203.0.113.5").allowed
    assert len(throttle._fallback._entries) == size


def test_an_allowed_address_still_counts_every_username(redis_client):
    throttle = Throttle(CacheStore(redis_client), AUDIT_KEY)
    for index in range(5):
        throttle.check_login(f"user{index}", "203.0.113.5")
    assert len(redis_client.keys("ctr:rl:login_user:*")) == 5
    assert len(redis_client.keys("ctr:rl:login_ip:*")) == 1


def test_the_decision_says_which_limit_tripped(redis_client):
    throttle = Throttle(CacheStore(redis_client), AUDIT_KEY)
    for _ in range(LOGIN_PER_USERNAME):
        throttle.check_login("asha_rao", "203.0.113.5")
    by_name = throttle.check_login("asha_rao", "203.0.113.6")
    assert not by_name.allowed and by_name.scope == "username"
    assert throttle.check_login("ravi_k", "203.0.113.6").scope is None


def test_when_both_limits_are_over_the_address_is_the_one_reported(redis_client):
    throttle = Throttle(CacheStore(redis_client), AUDIT_KEY)
    for _ in range(LOGIN_PER_IP):
        throttle.check_login("asha_rao", "203.0.113.5")
    assert throttle.check_login("asha_rao", "203.0.113.5").scope == "ip"


# ---- a throttled login: counted, alerted, not audited, nothing checked -------------------------


def throttle_the_user(env, attempts=LOGIN_PER_USERNAME):
    env.register()
    for index in range(attempts):
        env.login("asha_rao", f"wrong password {index}")


def test_a_throttled_attempt_does_not_verify_a_password_or_open_a_connection(env, monkeypatch):
    throttle_the_user(env)
    calls = []
    real_verify, real_dummy = passwords.verify_password, passwords.dummy_hash
    monkeypatch.setattr(
        passwords,
        "verify_password",
        lambda *a, **k: (calls.append("verify"), real_verify(*a, **k))[1],
    )

    def spy_dummy():
        calls.append("dummy")
        return real_dummy()

    spy_dummy.cache_clear = real_dummy.cache_clear  # the fixture clears this cache at the end
    monkeypatch.setattr(passwords, "dummy_hash", spy_dummy)
    opened = count_opens(env)
    response = env.login("asha_rao", PASSWORD)  # even the right password
    assert response.status_code == 429
    assert calls == [] and opened == []  # no hash function was called, no connection was opened


def test_an_attempt_that_is_not_throttled_does_verify_a_password(env, monkeypatch):
    env.register()
    calls = []
    real = passwords.verify_password
    monkeypatch.setattr(
        passwords, "verify_password", lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    )
    env.login("asha_rao", "not the password")
    assert calls == [1]  # the spy really sees verifications, so the test above can fail


def test_a_throttled_login_emits_an_alert_event_with_no_name_or_address(env, caplog):
    throttle_the_user(env)
    services = env.app.state.services
    before = len([e for e in services.events if e[0] == "login_throttled"])
    with caplog.at_level(logging.WARNING):
        env.login("asha_rao", PASSWORD)
        env.login("asha_rao", PASSWORD)
    events = [e for e in services.events if e[0] == "login_throttled"]
    assert len(events) == before + 2  # counted: one event per refused attempt
    assert events[-1] == ("login_throttled", {"scope": "username"})
    everything = repr(list(services.events)) + caplog.text
    assert "asha_rao" not in everything and SOCKET_ADDRESS not in everything


def test_the_alert_event_reaches_the_hook_for_phase_12(env):
    seen = []
    env.app.state.services.on_event = lambda name, fields: seen.append((name, fields))
    throttle_the_user(env)
    env.login("asha_rao", PASSWORD)
    assert ("login_throttled", {"scope": "username"}) in seen


def test_a_failing_alert_hook_cannot_break_a_throttled_login(env):
    def broken(name, fields):
        raise RuntimeError("the webhook is down")

    env.app.state.services.on_event = broken
    throttle_the_user(env)
    assert env.login("asha_rao", PASSWORD).status_code == 429


def test_the_address_limit_reports_scope_ip(env):
    for index in range(LOGIN_PER_IP):
        env.login(f"user_{index:02d}", "wrong password here")
    env.login("brand_new", "wrong password here")
    assert env.app.state.services.events[-1] == ("login_throttled", {"scope": "ip"})


def test_a_throttled_login_writes_no_audit_row(env):
    throttle_the_user(env)
    rows = env.rows("SELECT COUNT(*) FROM audit_log")
    for _ in range(5):
        env.login("asha_rao", PASSWORD)
    assert env.rows("SELECT COUNT(*) FROM audit_log") == rows


def test_a_throttled_registration_is_alerted_too(env):
    for index in range(5):
        env.register(f"user_{index}")
    env.register("user_9")
    assert ("register_throttled", {}) in list(env.app.state.services.events)


# ---- the password-hash gate ---------------------------------------------------------------------


def test_the_gate_allows_its_limit_and_refuses_the_next_at_once():
    gate = HashGate(2)
    assert DEFAULT_LIMIT == 2
    with gate.slot():
        with gate.slot():
            with pytest.raises(HashBusy):
                with gate.slot():
                    pass


def test_a_slot_is_given_back_when_the_block_ends_or_fails():
    gate = HashGate(1)
    with gate.slot():
        pass
    with pytest.raises(RuntimeError):
        with gate.slot():
            raise RuntimeError("the hash failed")
    with gate.slot():  # both slots were released
        pass


@pytest.mark.parametrize("limit", [0, -1, 17, True, 1.5, "2", None])
def test_a_bad_gate_limit_is_refused(limit):
    with pytest.raises(ValueError):
        HashGate(limit)


def test_the_gate_never_lets_more_than_its_limit_run_at_once():
    gate = HashGate(3)
    running = []
    peak = []
    lock = threading.Lock()

    def work():
        try:
            with gate.slot():
                with lock:
                    running.append(1)
                    peak.append(len(running))
                time.sleep(0.02)
                with lock:
                    running.pop()
        except HashBusy:
            pass

    threads = [threading.Thread(target=work) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(peak) <= 3


def slow(monkeypatch, seconds, which="verify_password"):
    real = getattr(passwords, which)

    def delayed(*args, **kwargs):
        time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(passwords, which, delayed)


async def burst(env, requests):
    transport = httpx.ASGITransport(app=env.app, client=(SOCKET_ADDRESS, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.gather(*(request(client) for request in requests))


def login_request(name):
    return lambda client: client.post("/auth/login", json={"username": name, "password": "x" * 12})


def test_a_burst_of_logins_beyond_the_limit_gets_503_for_the_extra_ones(env, monkeypatch):
    slow(monkeypatch, 0.6)
    responses = asyncio.run(burst(env, [login_request(f"user_{i}") for i in range(4)]))
    codes = sorted(r.status_code for r in responses)
    assert codes == [401, 401, 503, 503]  # two ran, two were refused at once
    busy = next(r for r in responses if r.status_code == 503)
    assert busy.json()["error"]["code"] == "busy" and busy.headers["retry-after"] == "2"


def test_the_gate_frees_up_and_the_next_login_works(env, monkeypatch):
    env.register()
    slow(monkeypatch, 0.3)
    asyncio.run(burst(env, [login_request(f"user_{i}") for i in range(4)]))
    assert env.login("asha_rao", PASSWORD).status_code == 200  # slots were given back


def test_a_busy_login_writes_no_audit_row_and_is_not_a_wrong_password(env, monkeypatch):
    slow(monkeypatch, 0.6)
    asyncio.run(burst(env, [login_request(f"user_{i}") for i in range(3)]))
    rows = env.rows("SELECT details FROM audit_log")
    assert len(rows) == 2  # only the two that were checked
    assert all(json.loads(row[0])["reason"] == "unknown_user" for row in rows)


def test_the_limit_is_the_setting(tmp_path, monkeypatch):
    env = Env(tmp_path, password_hash_concurrency=1)
    slow(monkeypatch, 0.5)
    responses = asyncio.run(burst(env, [login_request(f"user_{i}") for i in range(3)]))
    assert sorted(r.status_code for r in responses) == [401, 503, 503]  # one slot: one runs
    wide_path = tmp_path / "wide"
    wide_path.mkdir()
    wide = Env(wide_path, password_hash_concurrency=4)
    responses = asyncio.run(burst(wide, [login_request(f"user_{i}") for i in range(4)]))
    assert sorted(r.status_code for r in responses) == [401, 401, 401, 401]  # four slots


def test_registration_hashing_is_gated_too_and_a_busy_one_creates_nothing(env, monkeypatch):
    slow(monkeypatch, 0.6, which="hash_password")

    def register(name):
        return lambda client: client.post("/auth/register", json=env.body(name))

    responses = asyncio.run(burst(env, [register(f"user_{i}") for i in range(4)]))
    assert sorted(r.status_code for r in responses) == [201, 201, 503, 503]
    assert env.rows("SELECT COUNT(*) FROM users") == [(2,)]
    busy = next(r for r in responses if r.status_code == 503)
    assert busy.json()["error"]["code"] == "busy"


def test_the_gate_does_not_block_requests_that_do_not_hash(env, monkeypatch):
    env.register()
    token = env.token()
    slow(monkeypatch, 0.6)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=(SOCKET_ADDRESS, 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            tasks = [asyncio.create_task(login_request(f"user_{i}")(client)) for i in range(2)]
            await asyncio.sleep(0.1)  # both slots are now taken
            me = await client.get("/auth/me", headers=env.bearer(token))
            live = await client.get("/live")
            await asyncio.gather(*tasks)
            return me.status_code, live.status_code

    assert asyncio.run(scenario()) == (200, 200)


# ---- HEAD on /live ------------------------------------------------------------------------------


def test_head_on_live_answers_200_with_no_body_and_the_security_headers(env):
    from app.api.security import SECURITY_HEADERS

    response = env.client.head("/live")
    assert response.status_code == 200 and response.content == b""
    for name, value in SECURITY_HEADERS:
        assert response.headers.get(name) == value


def test_get_on_live_is_unchanged(env):
    assert env.client.get("/live").json() == {"status": "ok"}


@pytest.mark.parametrize("url", ["/health", "/auth/me", "/auth/login"])
def test_head_is_not_added_to_any_other_route(env, url):
    assert env.client.head(url).status_code == 405


# ---- READ COMMITTED on Postgres -----------------------------------------------------------------


def test_connect_sets_read_committed_on_every_postgres_connection(monkeypatch):
    import psycopg

    from app.db import connection

    made = []

    class Recorder:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.isolation_level = None

    def fake_connect(url, **kwargs):
        recorder = Recorder(**kwargs)
        made.append(recorder)
        return recorder

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    database = connection.connect("postgresql://someone:not-real@localhost:5432/app")
    assert database.kind == "postgres"
    assert made[0].isolation_level == psycopg.IsolationLevel.READ_COMMITTED
    assert made[0].kwargs["autocommit"] is False


def test_the_isolation_level_is_not_left_to_the_hosts_default(monkeypatch):
    import psycopg

    from app.db import connection

    class Recorder:
        isolation_level = psycopg.IsolationLevel.SERIALIZABLE  # as if the host defaulted to it

        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(psycopg, "connect", lambda url, **kw: Recorder())
    database = connection.connect("postgresql://someone:not-real@localhost:5432/app")
    assert database.raw.isolation_level == psycopg.IsolationLevel.READ_COMMITTED


def test_sqlite_connections_are_untouched_by_the_isolation_setting(tmp_path):
    from app.db.connection import connect

    database = connect(f"sqlite:///{tmp_path / 'x.db'}")
    assert isinstance(database.raw, sqlite3.Connection)
    database.close()


# ---- the in-process counters under threads (a test that fails without the lock) -----------------


class SlowEntries(dict):
    """A dict whose reads take a moment, so two threads that read before either writes lose an
    update unless the counter's lock holds them apart."""

    def get(self, key, default=None):
        value = super().get(key, default)
        time.sleep(0.0005)
        return value


class NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_threads(counters, threads=8, hits=25, limit=100):
    results = []

    def work():
        for _ in range(hits):
            results.append(counters.hit("shared", limit, 60).allowed)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for thread in pool:
        thread.start()
    for thread in pool:
        thread.join()
    return results


def test_exactly_the_limit_gets_through_under_threads_because_of_the_lock():
    counters = InProcessCounters(clock=FakeClock())
    counters._entries = SlowEntries()
    results = run_threads(counters)
    assert results.count(True) == 100  # 200 hits, limit 100: never more


def test_the_same_test_would_fail_without_the_lock():
    counters = InProcessCounters(clock=FakeClock())
    counters._entries = SlowEntries()
    counters._lock = NoLock()
    results = run_threads(counters)
    assert results.count(True) > 100  # updates were lost: this is what the lock prevents
