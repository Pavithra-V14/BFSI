"""The per-user rate limit and the daily token budget (spec 5.4, 13.2)."""

from datetime import UTC, datetime, timedelta

import fakeredis
import pytest

from app.accounts.budget import REQUESTS_PER_MINUTE, UsageLimits
from app.cache.store import CacheStore
from tests.api_fixtures import make_settings
from tests.test_cache_store import FakeClock, FlakyRedis

NOON = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


class Moving:
    """A clock for the UTC day that the test moves by hand."""

    def __init__(self, moment=NOON):
        self.moment = moment

    def __call__(self):
        return self.moment


@pytest.fixture
def redis_client():
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def events():
    return []


@pytest.fixture
def clock():
    return Moving()


@pytest.fixture
def limits(redis_client, events, clock):
    cache = CacheStore(redis_client)
    return UsageLimits(cache, make_settings(), now=clock, on_event=lambda n, f: events.append(n))


def test_twenty_requests_a_minute_per_user(limits):
    assert REQUESTS_PER_MINUTE == 20
    results = [limits.check_rate("user-1") for _ in range(21)]
    assert [r.allowed for r in results] == [True] * 20 + [False]
    assert limits.check_rate("user-2").allowed  # another user has their own count


def test_a_blocked_request_says_how_long_to_wait(limits):
    for _ in range(20):
        limits.check_rate("user-1")
    blocked = limits.check_rate("user-1")
    assert not blocked.allowed and 0 < blocked.retry_after <= 60


# ---- the daily budget ------------------------------------------------------------------------


def test_a_new_user_has_the_whole_budget(limits):
    decision = limits.check_budget("user-1", "customer")
    assert (decision.allowed, decision.used, decision.budget) == (True, 0, 50_000)


def test_customers_get_50000_and_staff_and_admins_200000(limits):
    assert limits.check_budget("u", "customer").budget == 50_000
    assert limits.check_budget("u", "staff").budget == 200_000
    assert limits.check_budget("u", "admin").budget == 200_000


def test_charging_adds_up_and_the_check_sees_it(limits):
    assert limits.charge("user-1", 12_000) == 12_000
    assert limits.charge("user-1", 3_000) == 15_000
    assert limits.check_budget("user-1", "customer").used == 15_000


def test_checking_never_changes_the_total(limits):
    limits.charge("user-1", 100)
    for _ in range(5):
        limits.check_budget("user-1", "customer")
    assert limits.check_budget("user-1", "customer").used == 100


def test_an_over_budget_request_is_refused_before_any_model_call(limits):
    limits.charge("user-1", 50_000)  # the whole customer budget is spent
    decision = limits.check_budget("user-1", "customer")
    assert decision.allowed is False and decision.used == 50_000


def test_the_last_token_of_the_budget_can_still_be_used_but_not_one_more(limits):
    limits.charge("user-1", 49_999)
    assert limits.check_budget("user-1", "customer").allowed is True
    limits.charge("user-1", 1)
    assert limits.check_budget("user-1", "customer").allowed is False


def test_an_estimate_larger_than_what_is_left_is_refused(limits):
    limits.charge("user-1", 40_000)
    assert limits.check_budget("user-1", "customer", estimated_tokens=10_000).allowed is True
    assert limits.check_budget("user-1", "customer", estimated_tokens=10_001).allowed is False


def test_one_users_spending_does_not_touch_another(limits):
    limits.charge("user-1", 50_000)
    assert limits.check_budget("user-2", "customer").allowed is True


def test_staff_may_spend_more_than_a_customer_could(limits):
    limits.charge("staff-1", 150_000)
    assert limits.check_budget("staff-1", "staff").allowed is True
    limits.charge("staff-1", 50_000)
    assert limits.check_budget("staff-1", "staff").allowed is False


def test_the_budget_follows_the_settings(redis_client, clock):
    small = UsageLimits(
        CacheStore(redis_client), make_settings(token_budget_customer=100), now=clock
    )
    small.charge("u", 100)
    assert small.check_budget("u", "customer").allowed is False


@pytest.mark.parametrize("tokens", [0, -5])
def test_charging_nothing_or_less_does_nothing(limits, tokens):
    assert limits.charge("user-1", tokens) is None
    assert limits.check_budget("user-1", "customer").used == 0


# ---- the day resets at 00:00 UTC --------------------------------------------------------------


def test_the_total_is_per_utc_day(limits, clock):
    limits.charge("user-1", 50_000)
    assert limits.check_budget("user-1", "customer").allowed is False
    clock.moment = datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC)
    assert limits.check_budget("user-1", "customer").allowed is False  # still the same day
    clock.moment = datetime(2026, 10, 7, 0, 0, 0, tzinfo=UTC)
    decision = limits.check_budget("user-1", "customer")
    assert decision.allowed is True and decision.used == 0  # a new day, a new budget


def test_the_day_is_utc_whatever_the_zone_of_the_clock(limits, clock):
    from datetime import timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    clock.moment = datetime(2026, 10, 7, 1, 0, tzinfo=ist)  # 19:30 UTC on 6 October
    limits.charge("user-1", 50_000)
    clock.moment = datetime(2026, 10, 7, 5, 0, tzinfo=ist)  # 23:30 UTC on 6 October: same day
    assert limits.check_budget("user-1", "customer").allowed is False
    clock.moment = datetime(2026, 10, 7, 6, 0, tzinfo=ist)  # 00:30 UTC on 7 October: next day
    assert limits.check_budget("user-1", "customer").allowed is True


def test_the_counter_expires_at_the_next_midnight(limits, redis_client, clock):
    limits.charge("user-1", 10)
    (key,) = redis_client.keys("ctr:tokens:*")
    assert key.endswith("20261006")
    ttl = redis_client.ttl(key)
    assert 12 * 3600 - 5 <= ttl <= 12 * 3600 + 5  # 12 hours until 00:00 UTC


# ---- Upstash down or absent: allowed, with an alert -------------------------------------------


def test_when_upstash_is_down_a_request_is_allowed_with_an_alert(events, clock):
    cache = CacheStore(FlakyRedis(), clock=FakeClock())
    limits = UsageLimits(cache, make_settings(), now=clock, on_event=lambda n, f: events.append(n))
    decision = limits.check_budget("user-1", "customer")
    assert decision.allowed is True and decision.available is False and decision.used is None
    assert "budget_unavailable" in events
    assert limits.charge("user-1", 100) is None
    assert limits.check_rate("user-1").allowed and not limits.check_rate("user-1").available


def test_with_no_cache_configured_the_same_applies(events, clock):
    limits = UsageLimits(None, make_settings(), now=clock, on_event=lambda n, f: events.append(n))
    assert limits.check_budget("u", "customer").allowed is True
    assert limits.charge("u", 5) is None
    assert limits.check_rate("u").allowed is True
    assert {"budget_unavailable", "rate_limit_unavailable"} <= set(events)


def test_a_failing_alert_hook_cannot_break_a_check(clock):
    def broken(name, fields):
        raise RuntimeError("webhook down")

    limits = UsageLimits(None, make_settings(), now=clock, on_event=broken)
    assert limits.check_budget("u", "customer").allowed is True
