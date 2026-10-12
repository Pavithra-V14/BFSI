"""The queries of Phase 13 on a real Postgres. Runs only in CI, on the throw-away service container
(TEST_POSTGRES_URL); skipped everywhere else (and never run on this machine).

Written without being run against Postgres: the first CI run is its first run. It covers the
evaluation database's own migration (a separate database in production, applied to an empty schema
here), the case upserts, the run and item bookkeeping with their foreign keys and cascades, the
per-day usage rows, the sample insert, the window read and the 30-day purge, and the drift and
calibration rows.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.migrate import run
from app.eval import store
from app.eval.cases import load_cases

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
EVAL_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations_eval"


@pytest.fixture
def url():
    import psycopg

    schema = "e_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    target = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(target, EVAL_MIGRATIONS, log=lambda line: None)
    yield target
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(url):
    database = connect(url)
    yield database
    database.close()


def make_run(db, **overrides):
    values = {
        "environment": "production",
        "kind": "samples",
        "dataset_version": "v1",
        "system_version": {"p": 1},
        "estimate": {"total_requests": 5},
        "estimate_code": "AAAA-1111",
        "case_ids": ["a", "b"],
        "batches": 1,
        "created_by": "ci",
        "now": NOW,
    }
    values.update(overrides)
    return store.create_run(db, **values)


def test_the_case_set_is_written_read_back_and_updated_in_place(db):
    _version, cases = load_cases()
    store.sync_cases(db, cases)
    store.sync_cases(db, cases)
    assert len(store.list_cases(db)) == 60
    assert len(store.list_cases(db, active_only=True)) == 20
    assert store.dataset_versions(db) == ["2026-10-v1"]


def test_a_run_its_items_and_its_results_and_the_cascade(db):
    run_id = make_run(db)
    store.save_item(
        db,
        run_id,
        "a",
        status="done",
        answer_kind="answer",
        answer_text="t",
        chunk_ids=["c"],
        contexts=["p"],
        models=["m"],
        tokens=5,
        latency_ms=2.5,
    )
    store.save_result(db, run_id, "a", "case_pass", score=1.0, decider="code")
    store.save_result(db, run_id, "a", "faithfulness", score=None, decider="not_scored")
    store.save_result(db, run_id, "a", "case_pass", score=0.0, decider="code")  # replaced
    store.update_run(db, run_id, status="running", started_at=NOW, tokens_used=5, requests_used=1)
    run_row = store.get_run(db, run_id)
    assert run_row["status"] == "running" and run_row["environment"] == "production"
    assert store.pending_case_ids(db, run_id) == ["b"]
    results = {r["metric"]: r for r in store.get_results(db, run_id)}
    assert results["case_pass"]["score"] == 0.0 and results["faithfulness"]["scored"] is False
    store.add_usage(db, run_id, "2026-10-12", "judge", requests=2, tokens=10)
    store.add_usage(db, run_id, "2026-10-12", "judge", requests=1, tokens=5)
    assert store.usage_for_day(db, "2026-10-12") == {"judge": (3, 15)}
    with db.transaction(write=True):
        db.execute("DELETE FROM eval_runs WHERE id = ?", (run_id,))
    for table in ("eval_items", "eval_results", "eval_usage_days"):
        assert db.query_one(f"SELECT COUNT(*) FROM {table}")[0] == 0


def test_a_run_of_an_unknown_environment_is_refused_by_the_table_as_well(db):
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction(write=True):
        db.execute(
            "INSERT INTO eval_runs (id, environment, kind, dataset_version, system_version, "
            "status, estimate, estimate_code, created_by) VALUES (?, 'staging', 'active', 'v', "
            "'{}', 'estimated', '{}', 'X', 'ci')",
            (uuid.uuid4().hex,),
        )


def test_samples_are_inserted_listed_judged_read_by_window_and_purged(db):
    row = {
        "role": "customer",
        "result_kind": "answer",
        "question": "q?",
        "answer": "a.",
        "chunk_ids": ["c1"],
        "contexts": ["passage"],
        "models": ["m1"],
        "prompt_hash": "ph",
        "cheap_checks": {"cited_idx": [0]},
        "tokens": 10,
        "latency_ms": 3.5,
    }
    old = store.insert_sample(db, row, now=NOW - timedelta(days=45))
    store.insert_sample(db, row, now=NOW - timedelta(days=2))
    store.insert_sample(db, row, now=NOW - timedelta(hours=1))
    waiting = store.unjudged_samples(db, 10)
    assert len(waiting) == 3 and waiting[0]["id"] == old and waiting[0]["contexts"] == ["passage"]
    run_id = make_run(db)
    store.mark_samples_judged(db, [old], run_id, now=NOW)
    assert len(store.unjudged_samples(db, 10)) == 2
    window = store.samples_between(db, NOW - timedelta(days=7), NOW)
    assert len(window) == 2 and window[0]["models"] == ["m1"]
    assert store.purge_samples(db, now=NOW, dry_run=True) == 1
    assert store.count_samples(db) == 3
    assert store.purge_samples(db, now=NOW) == 1
    assert store.count_samples(db) == 2


def test_drift_snapshots_and_calibration_rows(db):
    window = (NOW - timedelta(days=7), NOW)
    store.save_drift(
        db,
        environment="production",
        window=window,
        baseline=None,
        measures={"alerts": ["model_shift"]},
        now=NOW,
    )
    (snapshot,) = store.list_drift(db, "production")
    assert snapshot["measures"]["alerts"] == ["model_shift"]
    store.save_calibration(
        db,
        "claim_supported",
        items=6,
        judge_agreement=0.9,
        jev_agreement=0.95,
        threshold=0.85,
        trusted=True,
        now=NOW,
    )
    store.save_calibration(
        db,
        "claim_supported",
        items=6,
        judge_agreement=0.9,
        jev_agreement=0.5,
        threshold=0.85,
        trusted=False,
        now=NOW,
    )
    assert store.get_calibration(db)["claim_supported"]["trusted"] is False


def test_runs_are_listed_newest_first_and_filtered_by_environment(db):
    first = make_run(db, now=NOW - timedelta(days=2))
    second = make_run(db, now=NOW - timedelta(days=1))
    other = make_run(db, environment="local", now=NOW)
    assert [r["id"] for r in store.list_runs(db, environment="production")] == [second, first]
    assert [r["id"] for r in store.list_runs(db, environment="local")] == [other]
    assert len(store.list_runs(db, limit=2)) == 2


def test_the_items_of_a_run_come_back_with_their_saved_fields(db):
    run_id = make_run(db)
    store.save_item(
        db, run_id, "a", status="done", answer_kind="answer", answer_text="t",
        chunk_ids=["c"], contexts=["p"], models=["m"], cheap_checks={"cited_idx": [0]},
        tokens=5, latency_ms=2.5,
    )  # fmt: skip
    items = {i["case_id"]: i for i in store.get_items(db, run_id)}
    assert items["a"]["status"] == "done" and items["a"]["contexts"] == ["p"]
    assert items["a"]["cheap_checks"] == {"cited_idx": [0]} and items["a"]["tokens"] == 5
    assert items["b"]["status"] == "pending"


def test_a_candidate_is_found_by_its_review_item_and_listed_apart_from_the_set(db):
    from app.eval.candidates import candidate_from_review

    item = type("I", (), {"id": uuid.uuid4(), "kind": "refused", "doc_id": None,
                          "details": {"question": "What is the waiting period?"}})()  # fmt: skip
    candidate = candidate_from_review(item, "should_have_answered")
    assert store.candidate_exists(db, str(item.id)) is False
    store.upsert_case(db, candidate, now=NOW)
    assert store.candidate_exists(db, str(item.id)) is True
    (found,) = store.list_cases(db, status="candidate")
    assert found.review_item_id == str(item.id) and found.turns == candidate.turns
    assert store.list_cases(db, status="set") == []


def test_the_start_lock_is_one_atomic_update_and_the_heartbeat_column_exists(db):
    run_id = make_run(db, kind="active", environment="local")
    first = store.claim_run(db, run_id, now=NOW, stale_minutes=10, status="running")
    second = store.claim_run(db, run_id, now=NOW + timedelta(minutes=1), stale_minutes=10,
                             status="running")  # fmt: skip
    assert first is True and second is False
    stale = store.claim_run(db, run_id, now=NOW + timedelta(minutes=11), stale_minutes=10,
                            status="running")  # fmt: skip
    assert stale is True
    store.update_run(db, run_id, heartbeat_at=NOW)
    assert store.get_run(db, run_id)["heartbeat_at"] is not None


def test_a_calibration_row_keeps_its_counts_and_the_two_model_names(db):
    store.save_calibration(
        db, "claim_supported", items=6, judge_agreement=0.9, jev_agreement=0.95,
        threshold=0.85, trusted=True, counted=5, jev_judge_agreement=0.8,
        jev_model="typesafe/jev-1.13", judge_model="meta-llama/llama-4-maverick", now=NOW,
    )  # fmt: skip
    row = store.get_calibration(db)["claim_supported"]
    assert row["counted"] == 5 and row["jev_judge_agreement"] == 0.8
    assert row["jev_model"] == "typesafe/jev-1.13"
    assert row["judge_model"] == "meta-llama/llama-4-maverick"
