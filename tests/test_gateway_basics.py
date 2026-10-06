"""Gateway building blocks: limiter, key pools, breaker, backoff, cost, context, config."""

import json
import os

import pytest

from app.gateway import breaker as breaker_module
from app.gateway.backoff import MAX_ATTEMPTS, backoff_delay
from app.gateway.breaker import CircuitBreaker
from app.gateway.config import (
    DEFAULT_LIMITS,
    DEFAULT_PLANS,
    ENV_NAMES,
    GatewayConfig,
    parse_limits,
)
from app.gateway.context import DEFAULT_UNKNOWN_WINDOW, ContextWindows
from app.gateway.cost import PRICING_FILE, load_pricing, shadow_cost
from app.gateway.errors import ContextWindowExceeded
from app.gateway.keys import ConfigError, KeyPool, fingerprint, parse_keys
from app.gateway.limiter import Limits, SlidingWindowLimiter
from app.gateway.tokens import TokenCounter
from tests.helpers import FakeClock, fake_key

# ---- limiter ---------------------------------------------------------------------------


def test_limiter_requests_per_minute_window():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(Limits(rpm=2), clock)
    assert limiter.try_reserve(1) and limiter.try_reserve(1)
    assert limiter.try_reserve(1) is None
    clock.advance(59)
    assert limiter.try_reserve(1) is None
    clock.advance(2)
    assert limiter.try_reserve(1) is not None


def test_limiter_tokens_per_minute_and_correction():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(Limits(tpm=1000), clock)
    first = limiter.try_reserve(900)
    assert first is not None
    assert limiter.try_reserve(200) is None
    limiter.correct(first, 100)  # the real usage was much lower than the reservation
    assert limiter.try_reserve(200) is not None


def test_limiter_daily_caps():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(Limits(rpd=2, tpd=100), clock)
    assert limiter.try_reserve(10) and limiter.try_reserve(10)
    clock.advance(3600)
    assert limiter.try_reserve(10) is None  # two requests already used today
    clock.advance(86_400)
    assert limiter.try_reserve(10) is not None


def test_limiter_retry_in_and_can_ever_fit():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(Limits(rpm=1, tpm=100), clock)
    assert limiter.can_ever_fit(100)
    assert not limiter.can_ever_fit(101)
    limiter.try_reserve(10)
    clock.advance(20)
    assert limiter.retry_in(10) == pytest.approx(40, abs=0.01)
    clock.advance(41)
    assert limiter.retry_in(10) == 0.0


def test_limiter_no_limits_never_blocks():
    limiter = SlidingWindowLimiter(Limits(), FakeClock())
    assert all(limiter.try_reserve(10**6) for _ in range(100))


# ---- keys ------------------------------------------------------------------------------


def test_parse_keys_trims_dedupes_and_limits_to_four():
    assert parse_keys(" a , b,,a ,c", name="X") == ["a", "b", "c"]
    assert parse_keys(None, name="X") == []
    with pytest.raises(ConfigError) as caught:
        parse_keys("1,2,3,4,5", name="GROQ_API_KEYS")
    assert "GROQ_API_KEYS" in str(caught.value) and "1,2" not in str(caught.value)


def test_fingerprint_is_short_stable_and_hides_the_key():
    key = fake_key("groq")
    assert fingerprint(key) == fingerprint(key)
    assert len(fingerprint(key)) == 8
    assert key not in fingerprint(key) and fingerprint(key) != fingerprint(fake_key("groq", 2))


def test_round_robin_rotation_and_cooldown_of_one_key():
    clock = FakeClock()
    keys = [fake_key("g", i) for i in range(3)]
    pool = KeyPool("g", keys, Limits(), clock)
    seen = [pool.acquire(1).key for _ in range(6)]
    assert seen == keys + keys  # turns, in order
    first = fingerprint(keys[0])
    pool.cool_down(first, 30)
    assert [pool.acquire(1).key for _ in range(4)] == [keys[1], keys[2], keys[1], keys[2]]
    clock.advance(31)
    assert keys[0] in [pool.acquire(1).key for _ in range(3)]


def test_ordered_pool_prefers_the_first_key():
    clock = FakeClock()
    keys = [fake_key("a"), fake_key("b")]
    pool = KeyPool("embed", keys, Limits(), clock, strategy="ordered")
    assert [pool.acquire(1).key for _ in range(3)] == [keys[0]] * 3
    pool.cool_down(fingerprint(keys[0]), 30)
    assert pool.acquire(1).key == keys[1]


def test_pool_status_and_exhaustion():
    clock = FakeClock()
    pool = KeyPool("g", [fake_key("g")], Limits(rpm=1), clock)
    assert pool.status() == "healthy"
    assert pool.acquire(1) is not None
    assert pool.acquire(1) is None
    assert pool.status() == "cooling_down"


# ---- breaker and backoff ---------------------------------------------------------------


def test_breaker_opens_then_allows_one_probe_then_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker(clock, open_seconds=30)
    assert breaker.allow() and breaker.state == breaker_module.CLOSED
    breaker.record_failure()
    assert breaker.state == breaker_module.OPEN and not breaker.allow()
    clock.advance(31)
    assert breaker.state == breaker_module.HALF_OPEN
    assert breaker.allow() and not breaker.allow()  # exactly one probe
    breaker.record_failure()
    assert breaker.state == breaker_module.OPEN
    clock.advance(31)
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == breaker_module.CLOSED and breaker.allow()


def test_backoff_bounds_and_attempt_count():
    assert MAX_ATTEMPTS == 3
    for attempt in range(6):
        low = backoff_delay(attempt, rng=lambda: 0.0)
        high = backoff_delay(attempt, rng=lambda: 1.0)
        step = min(4.0, 0.5 * 2**attempt)
        assert low == pytest.approx(step / 2) and high == pytest.approx(step)
        assert high <= 4.0
    assert backoff_delay(0, rng=lambda: 1.0) == 0.5


# ---- tokens, cost, context -------------------------------------------------------------


def test_token_counter_falls_back_to_an_estimate_when_the_encoding_cannot_load():
    counter = TokenCounter("no-such-encoding")
    assert counter.count_text("abcdefgh") == 2
    assert counter.approximate
    assert counter.count_messages([{"role": "user", "content": "abcdefgh"}]) == 2 + 4 + 2


@pytest.mark.skipif(
    not os.environ.get("GATEWAY_TEST_REAL_TOKENIZER"),
    reason="downloads the o200k_base file on first use; unit tests must not use the network",
)
def test_real_tokenizer_when_explicitly_asked_for():
    counter = TokenCounter()
    count = counter.count_text("What is the waiting period for pre-existing conditions?")
    assert not counter.approximate
    assert 5 <= count <= 15


def test_pricing_file_is_valid_and_marks_placeholders():
    pricing = load_pricing(PRICING_FILE)
    assert pricing["default_reference"]["model"] == "groq/openai/gpt-oss-120b"
    assert "placeholder" in pricing["default_reference"]["status"]
    json.loads(PRICING_FILE.read_text(encoding="utf-8"))


def test_shadow_cost_sources():
    pricing = load_pricing()
    cost, source = shadow_cost("gemini/gemini-3.8-flash", 1_000_000, 1_000_000, pricing)
    assert (cost, source) == (pytest.approx(4.5), "pricing_table")
    cost, source = shadow_cost("openrouter/google/gemma-4-31b-it:free", 1_000_000, 0, pricing)
    assert source == "default_reference" and cost == pytest.approx(0.15)
    cost, source = shadow_cost("groq/openai/gpt-oss-20b", 1_000_000, 0, pricing)
    assert source == "litellm_map" and cost > 0


def test_context_window_overrides_and_smallest_window():
    windows = ContextWindows({"x/model": 5_000})
    assert windows.window("x/model") == (5_000, "override")
    assert windows.window("groq/openai/gpt-oss-120b")[1] == "litellm"
    assert windows.window("nobody/knows-this") == (DEFAULT_UNKNOWN_WINDOW, "default")
    assert windows.smallest(["x/model", "groq/openai/gpt-oss-120b"]) == 5_000
    assert windows.check(4_000, 400, ["x/model"]) == 5_000
    with pytest.raises(ContextWindowExceeded):
        windows.check(4_700, 400, ["x/model", "groq/openai/gpt-oss-120b"])


# ---- config ----------------------------------------------------------------------------


def test_limits_default_and_override():
    assert DEFAULT_LIMITS["GROQ"] == Limits(rpm=30, tpm=8_000, rpd=1_000, tpd=200_000)
    assert DEFAULT_LIMITS["OPENROUTER"].rpd == 50
    limits = parse_limits("rpm=5, tpm=none", DEFAULT_LIMITS["GROQ"], name="GATEWAY_LIMITS_GROQ")
    assert limits == Limits(rpm=5, tpm=None, rpd=1_000, tpd=200_000)
    with pytest.raises(ConfigError):
        parse_limits("rps=5", Limits(), name="GATEWAY_LIMITS_GROQ")
    with pytest.raises(ConfigError):
        parse_limits("rpm=lots", Limits(), name="GATEWAY_LIMITS_GROQ")


def test_config_from_env_reads_names_and_defaults():
    env = {
        "GROQ_API_KEYS": f"{fake_key('groq', 1)}, {fake_key('groq', 2)}",
        "JEV_OPENROUTER_API_KEY": fake_key("jev"),
        "GEMINI_EMBEDDING_KEY_A": fake_key("embedA"),
        "GEMINI_MODEL": "gemini-test",
        "GATEWAY_LIMITS_GEMINI": "rpm=3",
        "GATEWAY_CONTEXT_OVERRIDES": "gemini/gemini-test=64000",
        "GATEWAY_ENABLE_MOCK": "true",
        "GATEWAY_TIMEOUT_SECONDS": "7",
    }
    config = GatewayConfig.from_env(env)
    assert len(config.groq_keys) == 2 and config.jev_key == fake_key("jev")
    assert config.embedding_keys == [fake_key("embedA")]
    assert config.models["GEMINI_MODEL"] == "gemini-test"
    assert config.models["GEMINI_DECIDE_MODEL"] == "gemini-test"  # defaults to the same
    assert config.models["JEV_MODEL"] == "typesafe/jev-1.13"
    assert config.models["GEMINI_EMBEDDING_MODEL"] == "gemini-embedding-2"
    assert config.limits["GEMINI"].rpm == 3 and config.limits["GEMINI"].tpm == 250_000
    assert config.context_overrides == {"gemini/gemini-test": 64_000}
    assert config.enable_mock and config.timeout_seconds == 7.0
    assert config.eval_judge_model is None


def test_plans_default_to_free_except_jev_and_can_be_set_by_name():
    assert DEFAULT_PLANS == {
        "GROQ": "free",
        "GEMINI": "free",
        "OPENROUTER": "free",
        "JEV": "paid",
        "EMBEDDING": "free",
    }
    config = GatewayConfig.from_env({"GATEWAY_PLAN_GROQ": "PAID", "GATEWAY_PLAN_JEV": " free "})
    assert config.plans["GROQ"] == "paid" and config.plans["JEV"] == "free"
    assert config.plans["GEMINI"] == "free"
    with pytest.raises(ConfigError) as caught:
        GatewayConfig.from_env({"GATEWAY_PLAN_GROQ": "trial"})
    assert "GATEWAY_PLAN_GROQ" in str(caught.value)


def test_config_defaults_have_the_spec_models_and_mock_off():
    config = GatewayConfig.from_env({})
    assert config.models["GROQ_MODEL"] == "openai/gpt-oss-120b"
    assert config.models["GROQ_DECIDE_MODEL"] == "openai/gpt-oss-20b"
    assert config.models["GEMINI_MODEL"] == "gemini-3.8-flash"
    assert config.models["OPENROUTER_MODEL"] == "google/gemma-4-31b-it:free"
    assert config.models["JEV_MODEL"] == "typesafe/jev-1.13"
    assert not config.enable_mock and config.timeout_seconds == 10.0


def test_no_jev_router_or_latest_alias_anywhere_in_the_config():
    config = GatewayConfig.from_env({})
    assert "router" not in config.models["JEV_MODEL"] and "latest" not in config.models["JEV_MODEL"]


def test_env_names_are_unique():
    assert len(ENV_NAMES) == len(set(ENV_NAMES))
