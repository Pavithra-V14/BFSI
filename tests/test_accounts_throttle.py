"""Login and registration throttling, with the in-process stand-in when Upstash is down."""

import fakeredis
import pytest

from app.accounts.audit import keyed_hash
from app.accounts.throttle import (
    LOGIN_PER_IP,
    LOGIN_PER_USERNAME,
    LOGIN_WINDOW_SECONDS,
    REGISTER_PER_IP,
    REGISTER_WINDOW_SECONDS,
    InProcessCounters,
    Throttle,
)
from app.cache.store import CacheStore
from tests.api_fixtures import AUDIT_KEY
from tests.test_cache_store import FakeClock, FlakyRedis


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def events():
    return []


@pytest.fixture
def throttle(redis_client, events):
    cache = CacheStore(redis_client, on_event=lambda name, fields: events.append(name))
    return Throttle(cache, AUDIT_KEY)


def attempts(throttle, username, ip, count):
    return [throttle.check_login(username, ip) for _ in range(count)]


def test_the_values_are_the_planned_ones():
    assert (LOGIN_PER_USERNAME, LOGIN_PER_IP, LOGIN_WINDOW_SECONDS) == (10, 30, 900)
    assert (REGISTER_PER_IP, REGISTER_WINDOW_SECONDS) == (5, 3600)


def test_ten_attempts_for_one_username_are_allowed_and_the_eleventh_is_not(throttle):
    results = attempts(throttle, "asha_rao", "203.0.113.5", 11)
    assert [r.allowed for r in results] == [True] * 10 + [False]
    assert results[-1].retry_after and 0 < results[-1].retry_after <= LOGIN_WINDOW_SECONDS


def test_every_attempt_counts_not_only_the_failures(throttle):
    # The throttle cannot know the outcome: it counts the attempt itself.
    attempts(throttle, "asha_rao", "203.0.113.5", 10)
    assert not throttle.check_login("asha_rao", "203.0.113.99").allowed  # a different address


def test_another_username_is_not_blocked_by_the_first(throttle):
    attempts(throttle, "asha_rao", "203.0.113.5", 11)
    assert throttle.check_login("ravi_k", "203.0.113.6").allowed


def test_one_address_is_blocked_after_30_attempts_across_many_usernames(throttle):
    results = [throttle.check_login(f"user{i}", "203.0.113.5") for i in range(31)]
    assert [r.allowed for r in results] == [True] * 30 + [False]
    assert throttle.check_login("fresh_user", "203.0.113.200").allowed  # another address is fine


def test_the_username_is_normalised_by_the_caller_so_case_variants_share_one_counter(throttle):
    # check_login receives the normalised name; the API normalises first (tested in test_api_auth).
    attempts(throttle, "asha_rao", "203.0.113.5", 10)
    assert not throttle.check_login("asha_rao", "203.0.113.5").allowed


def test_the_window_ends_and_the_counter_starts_again(redis_client):
    cache = CacheStore(redis_client)
    throttle = Throttle(cache, AUDIT_KEY)
    attempts(throttle, "asha_rao", "203.0.113.5", 11)
    for key in redis_client.keys("ctr:*"):
        redis_client.delete(key)  # what Redis does when the window's expiry passes
    assert throttle.check_login("asha_rao", "203.0.113.5").allowed


def test_the_counters_are_given_the_windows_expiry(throttle, redis_client):
    throttle.check_login("asha_rao", "203.0.113.5")
    ttls = [redis_client.ttl(key) for key in redis_client.keys("ctr:*")]
    assert ttls and all(0 < ttl <= LOGIN_WINDOW_SECONDS for ttl in ttls)


def test_no_name_and_no_address_is_ever_in_a_redis_key(throttle, redis_client):
    throttle.check_login("asha_rao", "203.0.113.5")
    throttle.check_register("203.0.113.5")
    keys = redis_client.keys("*")
    assert keys
    text = " ".join(keys)
    assert "asha_rao" not in text and "203.0.113.5" not in text and "203.0.113" not in text


def test_the_counter_subjects_are_keyed_hashes_that_differ_by_purpose(throttle, redis_client):
    throttle.check_login("asha_rao", "asha_rao")  # the same text as a name and as an address
    subjects = sorted(key.split(":")[-1] for key in redis_client.keys("ctr:rl:*"))
    assert len(subjects) == 2 and subjects[0] != subjects[1]  # different purposes, different hashes
    assert keyed_hash(AUDIT_KEY, "throttle-username", "asha_rao") in subjects
    assert keyed_hash(AUDIT_KEY, "throttle-ip", "asha_rao") in subjects


def test_a_different_key_gives_different_hashes():
    one = keyed_hash(AUDIT_KEY, "p", "value")
    assert keyed_hash(AUDIT_KEY + "x", "p", "value") != one
    assert keyed_hash(AUDIT_KEY, "q", "value") != one
    assert len(one) == 64 and "value" not in one


def test_registration_is_limited_to_five_an_hour_per_address(throttle):
    results = [throttle.check_register("203.0.113.5") for _ in range(6)]
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert results[-1].retry_after <= REGISTER_WINDOW_SECONDS
    assert throttle.check_register("203.0.113.6").allowed


def test_login_and_registration_counters_do_not_share_a_count(throttle):
    for _ in range(5):
        throttle.check_register("203.0.113.5")
    assert throttle.check_login("asha_rao", "203.0.113.5").allowed


# ---- Upstash down: the stand-in takes over, the throttle does not switch off ------------------


@pytest.fixture
def dead_throttle(events):
    cache = CacheStore(
        FlakyRedis(), on_event=lambda name, fields: events.append(name), clock=FakeClock()
    )
    clock = FakeClock()
    return Throttle(cache, AUDIT_KEY, clock=clock), clock


def test_with_upstash_down_logins_are_still_throttled(dead_throttle, events):
    throttle, _ = dead_throttle
    results = attempts(throttle, "asha_rao", "203.0.113.5", 11)
    assert [r.allowed for r in results] == [True] * 10 + [False]
    assert "rate_limit_unavailable" in events  # the outage is still reported


def test_with_upstash_down_the_ip_limit_and_registration_limit_still_hold(dead_throttle):
    throttle, _ = dead_throttle
    assert [throttle.check_login(f"u{i}", "203.0.113.5").allowed for i in range(31)][-1] is False
    assert [throttle.check_register("203.0.113.5").allowed for _ in range(6)][-1] is False


def test_with_no_cache_at_all_the_stand_in_is_used():
    throttle = Throttle(None, AUDIT_KEY, clock=FakeClock())
    results = attempts(throttle, "asha_rao", "203.0.113.5", 11)
    assert results[-1].allowed is False and results[-2].allowed is True


def test_the_stand_in_forgets_after_the_window():
    clock = FakeClock()
    throttle = Throttle(None, AUDIT_KEY, clock=clock)
    attempts(throttle, "asha_rao", "203.0.113.5", 11)
    clock.advance(LOGIN_WINDOW_SECONDS + 1)
    assert throttle.check_login("asha_rao", "203.0.113.5").allowed


def test_a_blocked_stand_in_reports_how_long_to_wait():
    clock = FakeClock()
    throttle = Throttle(None, AUDIT_KEY, clock=clock)
    attempts(throttle, "asha_rao", "203.0.113.5", 10)
    clock.advance(100)
    blocked = throttle.check_login("asha_rao", "203.0.113.5")
    assert not blocked.allowed and 790 <= blocked.retry_after <= 801


def test_when_upstash_comes_back_it_is_used_again(redis_client):
    flaky = FlakyRedis(inner=redis_client, down=True)
    cache_clock = FakeClock()
    throttle = Throttle(CacheStore(flaky, clock=cache_clock), AUDIT_KEY, clock=FakeClock())
    throttle.check_login("asha_rao", "203.0.113.5")  # counted in process
    flaky.down = False
    cache_clock.advance(31)  # the skip window is over
    throttle.check_login("asha_rao", "203.0.113.5")
    assert redis_client.keys("ctr:*")  # now counted in Upstash


def test_the_stand_in_memory_is_capped_and_drops_the_oldest_first():
    counters = InProcessCounters(clock=FakeClock(), max_entries=100)
    for index in range(250):
        counters.hit(f"key-{index}", 5, 60)
    assert len(counters._entries) == 100
    assert "key-249" in counters._entries and "key-0" not in counters._entries


def test_the_stand_in_drops_expired_entries_before_live_ones():
    clock = FakeClock()
    counters = InProcessCounters(clock=clock, max_entries=3)
    counters.hit("old-1", 5, 10)
    counters.hit("old-2", 5, 10)
    clock.advance(20)  # both expired
    counters.hit("live-1", 5, 1000)
    counters.hit("live-2", 5, 1000)  # now over the cap: the expired ones go first
    assert set(counters._entries) == {"live-1", "live-2"}


def test_the_stand_in_blocks_over_the_limit_and_allows_up_to_it():
    counters = InProcessCounters(clock=FakeClock())
    decisions = [counters.hit("k", 3, 60) for _ in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[3].retry_after >= 1


# The thread-safety tests (a test that fails without the lock, and one that shows it would) are
# in test_api_hardening.py.
