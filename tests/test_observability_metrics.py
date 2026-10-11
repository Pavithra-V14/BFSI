"""The numbers behind the dashboards and their rollups (spec 5.4, 15.6, 18; Phase 12)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.accounts.tokens import utc_text
from app.db.connection import connect
from app.db.migrate import run
from app.observability import metrics
from app.observability.metrics import MetricsHub, RequestObservation
from tests.api_fixtures import MIGRATIONS

NOW = datetime(2026, 10, 11, 10, 30, tzinfo=UTC)


def obs(minutes_ago=0, role="customer", kind="answer", **fields):
    at = NOW - timedelta(minutes=minutes_ago)
    return RequestObservation(at=at.timestamp(), role=role, kind=kind, **fields)


@pytest.fixture
def hub():
    return MetricsHub(now=lambda: NOW)


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    connection = connect(url)
    yield connection
    connection.close()


# ================================================================================ the live hub ====
def test_percentiles_of_tokens_per_request(hub):
    for tokens in (100, 200, 300, 400, 1000):
        hub.observe(obs(tokens_in=tokens, tokens_out=0))
    usage = hub.usage(metrics.DAY)
    assert usage["requests"] == 5
    assert (
        usage["tokens_p50"] == 300 and usage["tokens_p95"] == 1000 and usage["tokens_p99"] == 1000
    )


def test_billed_and_shadow_cost_are_separate_numbers(hub):
    hub.observe(obs(billed=0.0, shadow=0.004, tokens_in=1000, tokens_out=200))
    hub.observe(obs(billed=0.002, shadow=0.001))
    usage = hub.usage(metrics.DAY)
    assert usage["billed_cost"] == 0.002 and usage["shadow_cost"] == 0.005
    assert usage["billed_per_answer"] == 0.001 and usage["shadow_per_answer"] == 0.0025
    assert "total_cost" not in usage  # never added together


def test_tokens_split_by_direction_and_by_stage(hub):
    hub.observe(obs(tokens_in=900, tokens_out=100, stage_tokens=(("triage", 300), ("answer", 700))))
    hub.observe(obs(tokens_in=50, tokens_out=50, stage_tokens=(("answer", 100),)))
    usage = hub.usage(metrics.DAY)
    assert (usage["tokens_in"], usage["tokens_out"]) == (950, 150)
    assert usage["tokens_per_stage"] == {"answer": 800, "triage": 300}


def test_the_window_and_the_role_filter_the_numbers(hub):
    hub.observe(obs(minutes_ago=10, role="customer", tokens_in=100))
    hub.observe(obs(minutes_ago=90, role="staff", tokens_in=400))
    assert hub.usage(metrics.HOUR)["requests"] == 1
    assert hub.usage(metrics.DAY)["requests"] == 2
    assert hub.usage(metrics.DAY, role="staff")["tokens_in"] == 400
    assert hub.usage(metrics.DAY, role="customer")["tokens_in"] == 100


def test_outcomes_and_rates(hub):
    for _ in range(6):
        hub.observe(obs(cached=True))
    for _ in range(2):
        hub.observe(obs(kind="refusal"))
    hub.observe(obs(kind="unavailable"))
    hub.observe(obs(low_confidence=True, degraded=("lexical_rerank",)))
    out = hub.outcomes(metrics.DAY)
    assert out["requests"] == 10 and out["answered"] == 7 and out["refused"] == 2
    assert out["unavailable"] == 1 and out["cache_hit_rate"] == 0.6
    assert out["degraded_rate"] == 0.1 and out["low_confidence_rate"] == 0.1


def test_decider_shares_per_step(hub):
    hub.observe(obs(deciders=(("triage", "jev"), ("answer", "groq"))))
    hub.observe(obs(deciders=(("triage", "jev"), ("answer", "gemini"))))
    hub.observe(obs(deciders=(("triage", "groq"),)))
    shares = hub.decider_shares(metrics.HOUR)
    assert shares["triage"] == {"groq": 0.3333, "jev": 0.6667}
    assert shares["answer"] == {"gemini": 0.5, "groq": 0.5}


def test_an_observation_holds_no_text_and_no_identity():
    names = set(RequestObservation.__dataclass_fields__)
    assert names == {
        "at", "role", "kind", "reason", "cached", "degraded", "low_confidence", "warnings",
        "tokens_in", "tokens_out", "billed", "shadow", "stage_tokens", "deciders",
    }  # fmt: skip
    for forbidden in ("user", "session", "query", "question", "answer", "text"):
        assert not any(forbidden in name for name in names)


def test_the_hub_is_bounded(hub):
    for _ in range(metrics.KEEP + 100):
        hub.observe(obs())
    assert len(hub.window(metrics.DAY)) == metrics.KEEP


# =========================================================================== the hourly rollup ====
def test_pending_increments_are_per_hour_role_and_metric(hub):
    hub.observe(obs(role="customer", tokens_in=100, tokens_out=20, billed=0.5))
    hub.observe(obs(role="staff", cached=True))
    pending = hub.take_pending()
    hour = datetime(2026, 10, 11, 10, tzinfo=UTC)
    assert pending[(hour, "all", "requests")] == 2.0
    assert pending[(hour, "customer", "tokens_in")] == 100.0
    assert pending[(hour, "staff", "cached")] == 1.0
    assert (hour, "staff", "tokens_in") not in pending  # zero values are not stored
    assert hub.take_pending() == {}  # taken once


def test_saving_adds_to_the_hour_and_never_counts_twice(hub, db):
    hub.observe(obs(tokens_in=100))
    metrics.add_hourly(db, hub.take_pending())
    hub.observe(obs(tokens_in=50))  # the same hour, a later pass (or a restarted process)
    metrics.add_hourly(db, hub.take_pending())
    total = metrics.hourly_totals(db, NOW.replace(hour=0, minute=0), NOW.replace(hour=23))
    assert total[("customer", "tokens_in")] == 150.0 and total[("all", "requests")] == 2.0


def test_a_failed_save_is_put_back_for_the_next_pass(hub):
    hub.observe(obs(tokens_in=70))
    taken = hub.take_pending()
    hub.restore(taken)
    hub.observe(obs(tokens_in=30))
    again = hub.take_pending()
    assert again[(datetime(2026, 10, 11, 10, tzinfo=UTC), "customer", "tokens_in")] == 100.0


def test_the_rollup_table_holds_numbers_and_fixed_words_only(db):
    columns = [r[1] for r in db.query_all("PRAGMA table_info(metric_rollups)")]
    assert columns == ["id", "period", "period_start", "role", "metric", "value"]


# ====================================================================== the daily counts ====
def add_user(db, role="customer", active=True):
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role, is_active) "
        "VALUES (?, ?, ?, 'h', ?, ?)",
        (user_id, f"u{user_id[:8]}", f"{user_id[:8]}@example.test", role, active),
    )
    return user_id


def add_session(db, user_id, at):
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, utc_text(at), utc_text(at)),
    )
    return session_id


def add_message(db, session_id, seq, role, at, *, grounded=None, citations="[]", fixed=False):
    message_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO messages (id, session_id, seq, role, content, citations, grounded, "
        "fixed_reply, created_at) VALUES (?, ?, ?, ?, 'masked', ?, ?, ?, ?)",
        (message_id, session_id, seq, role, citations, grounded, fixed, utc_text(at)),
    )
    return message_id


def add_feedback(db, message_id, user_id, rating, at):
    db.execute(
        "INSERT INTO feedback_entries (id, message_id, user_id, rating, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), message_id, user_id, rating, utc_text(at)),
    )


def test_the_daily_counts_per_role_come_from_the_tables(db):
    day = datetime(2026, 10, 10, 12, tzinfo=UTC)
    customer, staff, staff2 = add_user(db), add_user(db, "staff"), add_user(db, "staff")
    add_user(db, "staff", active=False)  # a seat that is switched off
    cited = '[{"doc_id": "doc-a"}]'
    s1 = add_session(db, customer, day)
    add_message(db, s1, 1, "user", day)
    answer = add_message(db, s1, 2, "assistant", day, grounded=True, citations=cited)
    add_message(db, s1, 3, "user", day)
    add_message(db, s1, 4, "assistant", day, grounded=False, fixed=True)  # a greeting or refusal
    add_feedback(db, answer, customer, "up", day)
    s2 = add_session(db, staff, day)
    add_message(db, s2, 1, "user", day)
    add_message(db, s2, 2, "assistant", day, grounded=True, citations=cited)
    add_session(db, staff2, day - timedelta(days=3))  # another day
    db.raw.commit()
    values = metrics.daily_from_tables(db, day)
    assert values[("customer", "active_users")] == 1.0
    assert values[("customer", "chats_started")] == 1.0
    assert values[("customer", "questions")] == 2.0
    assert values[("customer", "answers_verified")] == 1.0  # the greeting is not an answer
    assert values[("customer", "fixed_replies")] == 1.0
    assert values[("customer", "thumbs_up")] == 1.0 and values[("customer", "thumbs_down")] == 0.0
    assert values[("staff", "active_users")] == 1.0 and values[("staff", "questions")] == 1.0
    assert values[("staff", "seat_utilisation")] == 0.5  # 1 active of 2 active staff accounts


def test_daily_rows_are_replaced_and_read_back_with_ratios(db):
    day = datetime(2026, 10, 10, 12, tzinfo=UTC)
    metrics.set_daily(db, day, {("customer", "thumbs_up"): 3.0, ("customer", "thumbs_down"): 1.0})
    metrics.set_daily(db, day, {("customer", "thumbs_up"): 6.0, ("customer", "thumbs_down"): 2.0})
    series = metrics.daily_series(db, 7, now=NOW)
    (item,) = [d for d in series if d["day"] == "2026-10-10"]
    customer = item["roles"]["customer"]
    assert customer["thumbs_up"] == 6.0 and customer["thumbs_up_share"] == 0.75


def test_hourly_rows_are_summed_into_their_day_in_the_series(db):
    first = datetime(2026, 10, 11, 8, tzinfo=UTC)
    metrics.add_hourly(
        db,
        {
            (first, "customer", "requests"): 4.0,
            (first, "customer", "answered"): 4.0,
            (first, "customer", "cached"): 1.0,
            (first, "customer", "tokens_in"): 3000.0,
            (first, "customer", "tokens_out"): 1000.0,
            (first + timedelta(hours=1), "customer", "requests"): 6.0,
            (first + timedelta(hours=1), "customer", "answered"): 6.0,
            (first + timedelta(hours=1), "customer", "cached"): 4.0,
        },
    )
    (item,) = [d for d in metrics.daily_series(db, 3, now=NOW) if d["day"] == "2026-10-11"]
    customer = item["roles"]["customer"]
    assert customer["requests"] == 10.0 and customer["cached"] == 5.0
    assert customer["cache_hit_rate"] == 0.5 and customer["tokens_per_answer"] == 400.0


def test_the_ratios_and_the_missing_metrics():
    ratios = metrics.derived({"thumbs_up": 1.0, "thumbs_down": 3.0})
    assert ratios["thumbs_up_share"] == 0.25 and ratios["cache_hit_rate"] == 0.0
    assert metrics.derived({})["tokens_per_answer"] == 0.0
    produced = (
        set(metrics.derived({})) | set(metrics.DAILY_DB_METRICS) | set(metrics.HOURLY_METRICS)
    )
    for dropped in ("helpful_closed", "closed_with_helpful", "time_saved", "deflection"):
        assert not any(dropped in name for name in produced)


def test_no_user_or_session_id_is_stored_in_the_rollups(db):
    day = datetime(2026, 10, 10, 12, tzinfo=UTC)
    user = add_user(db)
    session = add_session(db, user, day)
    add_message(db, session, 1, "user", day)
    db.raw.commit()
    metrics.set_daily(db, day, metrics.daily_from_tables(db, day))
    stored = str(db.query_all("SELECT * FROM metric_rollups"))
    assert user not in stored and session not in stored


def test_billed_and_shadow_cost_per_answer_are_kept_apart_in_the_daily_ratios():
    ratios = metrics.derived({"answered": 4.0, "billed_cost": 0.02, "shadow_cost": 0.08})
    assert ratios["billed_per_answer"] == 0.005 and ratios["shadow_per_answer"] == 0.02


def test_hourly_totals_include_the_start_hour_and_exclude_the_end_hour(db):
    first = NOW.replace(hour=8, minute=0)
    metrics.add_hourly(
        db,
        {
            (first, "all", "requests"): 1.0,
            (first + timedelta(hours=1), "all", "requests"): 10.0,
        },
    )
    totals = metrics.hourly_totals(db, first, first + timedelta(hours=1))
    assert totals[("all", "requests")] == 1.0
