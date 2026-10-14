"""The estimate, the confirmation code, the day's room and the cost guard (owner additions a, b)."""

from datetime import UTC, datetime

import pytest

from app.eval import constants as c
from app.eval import estimate as est
from app.eval import store
from app.eval.cases import load_cases
from app.eval.guard import CostGuard, NotConfirmed, OverEstimate, check_confirmation
from app.gateway.config import GatewayConfig
from app.gateway.limiter import Limits

KEYS = "k" * 12


def config(**overrides):
    values = {
        "groq_keys": [KEYS + "a"],
        "gemini_keys": [KEYS + "b"],
        "eval_openrouter_keys": [KEYS + "c", KEYS + "d"],
        "jev_key": KEYS + "e",
        "embedding_keys": [KEYS + "f"],
        "cohere_key": KEYS + "g",
    }
    values.update(overrides)
    return GatewayConfig(**values)


@pytest.fixture(scope="module")
def all_cases():
    return load_cases()[1]


def active(all_cases):
    return [x for x in all_cases if x.active]


# =============================================================================== the numbers ====
def test_a_greeting_makes_no_call_and_a_refusal_never_reaches_the_generator(all_cases):
    greeting = next(x for x in all_cases if x.id == "g01-hi")
    assert est.case_need(greeting).total_requests == 0
    cricket = next(x for x in all_cases if x.id == "o01-cricket")
    need = est.case_need(cricket)
    # triage (Jev) and one verdict on the refusal; no search, no generator, no judge text
    assert dict(need.requests) == {"jev": 2}


def test_an_answer_case_pays_for_search_generation_and_the_judge(all_cases):
    case = next(x for x in all_cases if x.id == "f01-penal-charge")
    need = est.case_need(case)
    assert need.requests["answer"] == 1 and need.requests["embedding"] == 1 + est.JUDGE_EMBED_CALLS
    assert need.requests["rerank"] == 1
    assert need.requests["judge"] == est.JUDGE_TEXT_CALLS
    assert need.requests["jev"] == 1 + est.JUDGE_VERDICT_CALLS  # triage plus the verdicts
    unjudged = est.case_need(case, judged=False)
    assert "judge" not in unjudged.requests and unjudged.requests["jev"] == 1


def test_a_multi_turn_case_pays_for_every_turn(all_cases):
    case = next(x for x in all_cases if x.id == "m01-stt")
    single = est.case_need(next(x for x in all_cases if x.id == "f04-stamp-duty"), judged=False)
    double = est.case_need(case, judged=False)
    assert double.requests["answer"] == single.requests["answer"] * 2 + 1  # one rewrite


def test_the_estimate_lists_every_provider_group_against_its_daily_limit(all_cases):
    estimate = est.estimate_run(active(all_cases), config())
    judge = estimate["providers"]["judge"]
    # two keys are in the pool, but the free allowance is counted ONCE (one account per provider)
    assert judge["keys"] == 2 and judge["daily_request_limit"] == 50
    assert judge["requests"] == sum(
        est.case_need(x).requests.get("judge", 0) for x in active(all_cases)
    )
    assert estimate["total_requests"] == sum(p["requests"] for p in estimate["providers"].values())
    assert estimate["cases"] == 20 and estimate["margin"] == 0.25


def test_a_run_that_cannot_fit_one_day_is_split_into_batches(all_cases):
    estimate = est.estimate_run(active(all_cases), config())
    # 100 judge requests a day at 80 % = 80; every answered case needs 14: five or six a batch
    assert len(estimate["batches"]) > 1
    flat = [cid for batch in estimate["batches"] for cid in batch]
    assert sorted(flat) == sorted(x.id for x in active(all_cases))  # nothing lost, nothing twice
    limits = est.daily_limits(config())
    cases = {x.id: x for x in all_cases}
    for batch in estimate["batches"]:
        need = est.Need()
        for cid in batch:
            need.merge(est.case_need(cases[cid]))
        assert est.fits(need, limits, c.DAILY_LIMIT_SAFETY) or len(batch) == 1


def test_more_keys_do_not_make_fewer_batches_because_free_quota_is_never_pooled(all_cases):
    """Owner decision (Phase 16, docs/READINESS.md): one key per provider from one account. A
    capacity plan that added up the allowances of several free accounts would plan a day that
    the provider's terms do not allow."""
    few = est.estimate_run(active(all_cases), config())
    many = est.estimate_run(
        active(all_cases), config(eval_openrouter_keys=[KEYS + str(i) for i in range(4)])
    )
    assert many["providers"]["judge"]["keys"] == 4
    assert many["providers"]["judge"]["daily_request_limit"] == 50
    assert len(many["batches"]) == len(few["batches"])
    assert est.ONE_KEY_PER_PROVIDER is True


def test_a_run_without_the_judge_needs_no_judge_requests_and_one_batch(all_cases):
    estimate = est.estimate_run(active(all_cases), config(), judged=False)
    assert "judge" not in estimate["providers"]
    assert len(estimate["batches"]) == 1 and estimate["too_big_for_one_day"] == []


def test_a_case_that_alone_passes_the_day_is_named():
    only = [x for x in load_cases()[1] if x.id == "f01-penal-charge"]
    tiny = config(eval_openrouter_keys=[KEYS + "c"])
    limits = est.DailyLimits(requests={"judge": 5}, tokens={}, keys={"judge": 1})
    batches, too_big = est.plan_batches(only, limits)
    assert batches == [["f01-penal-charge"]] and too_big == ["f01-penal-charge"]
    assert est.daily_limits(tiny).requests["judge"] == 50


def test_the_estimate_is_described_in_plain_lines_with_the_margin(all_cases):
    lines = est.describe(est.estimate_run(active(all_cases), config()))
    text = "\n".join(lines)
    assert "requests" in text and "tokens" in text and "split into" in text
    assert "25 %" in text and "judge model" in text


# ============================================================================== the code ====
def test_the_confirmation_code_is_bound_to_the_exact_numbers(all_cases):
    base = est.estimate_run(active(all_cases), config())
    code = est.confirmation_code(base, kind="active", dataset_version="v1")
    assert est.confirmation_code(base, kind="active", dataset_version="v1") == code
    assert est.confirmation_code(base, kind="full", dataset_version="v1") != code
    assert est.confirmation_code(base, kind="active", dataset_version="v2") != code
    fewer = est.estimate_run(active(all_cases)[:10], config())
    assert est.confirmation_code(fewer, kind="active", dataset_version="v1") != code
    unjudged = est.estimate_run(active(all_cases), config(), judged=False)
    assert est.confirmation_code(unjudged, kind="active", dataset_version="v1") != code
    assert len(code) == 9 and code[4] == "-"


def test_a_missing_or_wrong_code_does_not_confirm(all_cases):
    estimate = est.estimate_run(active(all_cases), config())
    code = est.confirmation_code(estimate, kind="active", dataset_version="v1")
    check_confirmation(code, estimate, kind="active", dataset_version="v1")
    check_confirmation(code.lower(), estimate, kind="active", dataset_version="v1")
    for wrong in (None, "", "AAAA-0000", code + "0"):
        with pytest.raises(NotConfirmed):
            check_confirmation(wrong, estimate, kind="active", dataset_version="v1")


# ============================================================================ the guard ====
def test_the_guard_stops_a_run_that_passes_the_estimate_by_more_than_a_quarter():
    guard = CostGuard(100, 10_000)
    guard.charge(requests=100, tokens=10_000)  # exactly the estimate
    guard.charge(requests=25, tokens=2_500)  # exactly 25 % over: still allowed
    with pytest.raises(OverEstimate) as caught:
        guard.charge(requests=1)
    assert caught.value.kind == "requests" and caught.value.allowed == 125


def test_the_guard_counts_tokens_as_well_as_requests():
    guard = CostGuard(1000, 1000)
    with pytest.raises(OverEstimate) as caught:
        guard.charge(requests=1, tokens=1251)
    assert caught.value.kind == "tokens"


def test_a_stop_is_never_swallowed_by_a_broad_except():
    guard = CostGuard(1, 1)
    swallowed = False
    try:
        try:
            guard.charge(requests=5)
        except Exception:  # what an evaluation library does around a model call
            swallowed = True
    except OverEstimate:
        pass
    assert swallowed is False


def test_the_guard_keeps_usage_by_provider_group():
    guard = CostGuard(100, 100_000)
    guard.charge(requests=2, tokens=500, who="judge")
    guard.charge(requests=1, tokens=100, who="answer")
    guard.charge(requests=1, tokens=50, who="judge")
    assert guard.usage == {"judge": [3, 550], "answer": [1, 100]}


def test_a_resumed_guard_continues_from_what_was_used():
    guard = CostGuard(100, 1000, used_requests=120, used_tokens=0)
    with pytest.raises(OverEstimate):
        guard.charge(requests=6)  # 126 > 125


# =============================================================================== settings ====
def test_the_limits_read_the_pool_sizes_and_the_provider_limits():
    limits = est.daily_limits(config(groq_keys=["a" * 12, "b" * 12]))
    # two keys are listed (the pool exists for rotation), one key's allowance is counted
    assert limits.keys["answer"] == 2 and limits.requests["answer"] == 1000
    assert limits.tokens["answer"] == 200_000 and limits.keys["jev"] == 1


def test_a_provider_without_a_published_daily_limit_has_none():
    limits = est.daily_limits(config())
    assert limits.requests["rerank"] is None and limits.tokens["jev"] is None


def test_the_daily_room_counts_what_runs_already_used_today(tmp_path):
    from app.eval import runner

    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    from app.db.migrate import run as migrate

    migrate(store.sqlite_url(tmp_path / "e.db"), store.MIGRATIONS, log=lambda line: None)
    version, all_cases = load_cases()
    store.sync_cases(db, all_cases)
    chosen = [x for x in all_cases if x.id in ("f01-penal-charge", "f04-stamp-duty")]
    cfg = config()
    run_id, estimate, code = runner.prepare_run(
        db, cases=chosen, kind="active", environment="local", dataset_version=version,
        config=cfg, created_by="test",
    )  # fmt: skip
    run = store.get_run(db, run_id)
    cases = {x.id: x for x in all_cases}
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    assert runner.day_room_problem(db, run, cases, cfg, now=now) is None
    store.add_usage(db, run_id, "2026-10-12", "judge", requests=95, tokens=1)
    problem = runner.day_room_problem(db, run, cases, cfg, now=now)
    assert problem and "judge" in problem and "tomorrow" in problem
    other_day = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    assert runner.day_room_problem(db, run, cases, cfg, now=other_day) is None
    db.close()


def test_limits_object_has_the_fields_the_estimate_reads():
    lim = Limits(rpm=1, rpd=7)
    assert lim.rpd == 7 and lim.tpd is None
