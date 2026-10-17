"""The per-key daily counts (the admin key panel): every call that reaches a provider is counted on
the key that made it, a restart does not lose today's numbers, and no key value leaves the pool."""

from app.gateway.keys import KeyPool, fingerprint, parse_keys
from app.gateway.limiter import Limits
from tests.helpers import MESSAGES, FakeBackend, FakeClock, fake_key, lit, make_gateway, ok, run

LIMITS = Limits(rpd=10, tpd=1000)


def pool(keys=("k-one", "k-two"), today=lambda: "2026-10-17"):
    return KeyPool("groq", list(keys), LIMITS, FakeClock(), today=today)


def by_fp(rows):
    return {row["fingerprint"]: row for row in rows}


# ---- the pool counts ---------------------------------------------------------------------
def test_each_call_is_counted_on_the_key_that_made_it():
    p = pool()
    p.note_call(fingerprint("k-one"), 30)
    p.note_call(fingerprint("k-one"), 20)
    p.note_call(fingerprint("k-two"), 5)
    rows = by_fp(p.usage_today())
    assert (rows[fingerprint("k-one")]["calls"], rows[fingerprint("k-one")]["tokens"]) == (2, 50)
    assert (rows[fingerprint("k-two")]["calls"], rows[fingerprint("k-two")]["tokens"]) == (1, 5)


def test_a_key_that_made_no_call_shows_zero_and_an_unknown_key_is_ignored():
    p = pool()
    p.note_call("ffffffff", 99)  # not a key of this pool
    assert [(r["calls"], r["tokens"]) for r in p.usage_today()] == [(0, 0), (0, 0)]


def test_a_new_utc_day_starts_from_zero():
    day = ["2026-10-17"]
    p = pool(today=lambda: day[0])
    p.note_call(fingerprint("k-one"), 10)
    day[0] = "2026-10-18"
    assert by_fp(p.usage_today())[fingerprint("k-one")]["calls"] == 0
    p.note_call(fingerprint("k-one"), 4)
    row = by_fp(p.usage_today())[fingerprint("k-one")]
    assert (row["calls"], row["tokens"]) == (1, 4)


def test_unsaved_counts_are_taken_once_and_come_back_when_the_save_fails():
    p = pool()
    p.note_call(fingerprint("k-one"), 10)
    taken = p.take_unsaved()
    assert taken == {("2026-10-17", fingerprint("k-one")): [1, 10]}
    assert p.take_unsaved() == {}  # nothing is counted twice
    p.note_call(fingerprint("k-one"), 2)  # a call made while the save was running
    p.restore_unsaved(taken)  # the save failed
    assert p.take_unsaved() == {("2026-10-17", fingerprint("k-one")): [2, 12]}


def test_a_restart_starts_from_the_saved_counts_and_adds_new_calls_on_top():
    p = pool()
    p.seed_today({fingerprint("k-one"): (7, 700)})
    p.note_call(fingerprint("k-one"), 3)
    row = by_fp(p.usage_today())[fingerprint("k-one")]
    assert (row["calls"], row["tokens"]) == (8, 703)
    assert p.take_unsaved() == {("2026-10-17", fingerprint("k-one")): [1, 3]}  # only the new call


# ---- the gateway -------------------------------------------------------------------------
def test_a_successful_call_is_counted_with_its_tokens_and_the_calls_left_are_shown():
    gateway = make_gateway(backend=FakeBackend(lambda model, key, n: ok(prompt=10, completion=5)))
    run(gateway.complete(MESSAGES))
    report = gateway.key_usage()["groq"]
    assert report["keys"] == 2 and report["daily_calls_per_key"] == 1000
    used = [k for k in report["per_key"] if k["calls"]]
    assert len(used) == 1 and used[0]["calls"] == 1 and used[0]["tokens"] == 15
    assert used[0]["calls_left"] == 999 and used[0]["tokens_left"] == 200_000 - 15


def test_a_rate_limited_call_counts_on_its_key_and_the_retry_on_the_next_one():
    groq_one = fake_key("groq", 1)

    def handler(model, key, n):
        if key == groq_one:
            raise lit("RateLimitError")
        return ok()

    gateway = make_gateway(backend=FakeBackend(handler))
    run(gateway.complete(MESSAGES))
    rows = gateway.key_usage()["groq"]["per_key"]
    assert [r["calls"] for r in rows] == [1, 1]  # the refused call and the call that worked


def test_a_provider_without_a_daily_limit_shows_none_not_a_made_up_number():
    gateway = make_gateway(backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    report = gateway.key_usage()
    assert report["gemini"]["daily_tokens_per_key"] is None or isinstance(
        report["gemini"]["daily_tokens_per_key"], int
    )
    for pool_report in report.values():
        for row in pool_report["per_key"]:
            if pool_report["daily_calls_per_key"] is None:
                assert row["calls_left"] is None


def test_the_report_and_the_saved_rows_name_a_key_only_by_its_fingerprint():
    gateway = make_gateway(backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    text = repr(gateway.key_usage()) + repr(gateway.take_key_usage())
    assert "secretvalue" not in text and "fake-groq" not in text
    assert fingerprint(fake_key("groq", 1)) in text or fingerprint(fake_key("groq", 2)) in text


def test_the_gateway_hands_over_the_unsaved_counts_and_takes_them_back():
    gateway = make_gateway(backend=FakeBackend())
    run(gateway.complete(MESSAGES))
    taken = gateway.take_key_usage()
    assert taken and all(len(row) == 5 for row in taken)
    assert gateway.take_key_usage() == []
    gateway.restore_key_usage(taken)
    assert sorted(gateway.take_key_usage()) == sorted(taken)


def test_seeding_the_gateway_from_saved_rows_raises_today_s_counts():
    gateway = make_gateway(backend=FakeBackend())
    fp = fingerprint(fake_key("groq", 1))
    gateway.seed_key_usage({"groq": {fp: (40, 4000)}, "no-such-pool": {fp: (1, 1)}})
    row = by_fp(gateway.key_usage()["groq"]["per_key"])[fp]
    assert (row["calls"], row["tokens"], row["calls_left"]) == (40, 4000, 960)


# ---- the key variable is read as a comma list --------------------------------------------
def test_a_pool_variable_with_spaces_commas_and_blanks_gives_a_clean_key_list():
    assert parse_keys("key1 , key2, key3", name="X") == ["key1", "key2", "key3"]
    assert parse_keys(" key1,, key2 ,\tkey1 ,", name="X") == ["key1", "key2"]
    assert parse_keys("   ", name="X") == [] and parse_keys("", name="X") == []
