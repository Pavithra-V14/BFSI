"""The evaluation database, the candidates, the test-collection ingest and the retention purge
(Phase 13)."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.db.migrate import run as migrate
from app.eval import constants as c
from app.eval import store
from app.eval.candidates import candidate_from_review
from app.eval.cases import load_cases

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    connection = store.open_db(url)
    yield connection
    connection.close()


def make_run(db, **overrides):
    values = {
        "environment": "local",
        "kind": "active",
        "dataset_version": "v1",
        "system_version": {"p": 1},
        "estimate": {"total_requests": 5, "batches": [["a"]]},
        "estimate_code": "AAAA-1111",
        "case_ids": ["a", "b"],
        "batches": 1,
        "created_by": "t",
        "now": NOW,
    }
    values.update(overrides)
    return store.create_run(db, **values)


# ================================================================================= cases ====
def test_the_set_round_trips_through_the_database_with_every_field(db):
    _version, cases = load_cases()
    store.sync_cases(db, cases)
    back = {x.id: x for x in store.list_cases(db)}
    assert len(back) == 60
    for case in cases:
        assert back[case.id].turns == case.turns and back[case.id].sources == case.sources
        assert back[case.id].setup == case.setup and back[case.id].forbidden == case.forbidden
        assert back[case.id].active == case.active and back[case.id].behaviour == case.behaviour


def test_syncing_again_updates_in_place_and_never_duplicates(db):
    _version, cases = load_cases()
    store.sync_cases(db, cases)
    changed = [type(cases[0])(**{**cases[0].__dict__, "active": True})] + cases[1:]
    store.sync_cases(db, changed)
    assert len(store.list_cases(db)) == 60
    assert {x.id: x.active for x in store.list_cases(db)}[cases[0].id] is True


def test_candidates_are_kept_apart_from_the_set(db):
    _version, cases = load_cases()
    store.sync_cases(db, cases)
    item = SimpleNamespace(
        id="11111111-2222-3333-4444-555555555555",
        kind="refused",
        doc_id=None,
        details={"question": "Who won the cricket match?"},
    )
    candidate = candidate_from_review(item, "correct_refusal")
    store.upsert_case(db, candidate, now=NOW)
    assert len(store.list_cases(db, status="set")) == 60
    assert [x.id for x in store.list_cases(db, status="candidate")] == [candidate.id]
    assert store.candidate_exists(db, item.id) and not store.candidate_exists(db, "other")


@pytest.mark.parametrize(
    ("label", "behaviour"),
    [
        ("correct_refusal", "refuse"),
        ("should_have_answered", "answer"),
        ("wrong_answer", "answer"),
        ("other", "answer"),
    ],
)
def test_a_label_decides_what_the_candidate_expects(label, behaviour):
    item = SimpleNamespace(id="aaaa", kind="thumbs_down", doc_id="d", details={"question": "q?"})
    candidate = candidate_from_review(item, label)
    assert candidate.behaviour == behaviour and candidate.status == "candidate"
    assert candidate.active is False and candidate.source_docs == ("d",)


def test_a_bad_question_or_an_item_without_a_question_is_not_a_candidate():
    item = SimpleNamespace(id="aaaa", kind="refused", doc_id=None, details={"question": "q?"})
    assert candidate_from_review(item, "bad_question") is None
    empty = SimpleNamespace(id="aaaa", kind="refused", doc_id=None, details={})
    assert candidate_from_review(empty, "correct_refusal") is None


def test_a_long_question_is_cut_to_the_case_limit():
    item = SimpleNamespace(id="aaaa", kind="refused", doc_id=None, details={"question": "x" * 900})
    assert len(candidate_from_review(item, "correct_refusal").turns[0]) == 500


# ================================================================================== runs ====
def test_a_run_holds_its_estimate_and_one_pending_item_per_case(db):
    run_id = make_run(db)
    run = store.get_run(db, run_id)
    assert run["status"] == "estimated" and run["estimate"]["total_requests"] == 5
    assert run["planned_items"] == 2 and run["confirmed_at"] is None
    assert store.pending_case_ids(db, run_id) == ["a", "b"]


def test_an_unknown_environment_or_kind_is_refused(db):
    with pytest.raises(c.EvalConfigError):
        make_run(db, environment="staging")
    with pytest.raises(c.EvalConfigError):
        make_run(db, kind="everything")


def test_an_item_checkpoint_removes_it_from_the_pending_list(db):
    run_id = make_run(db)
    store.save_item(
        db,
        run_id,
        "a",
        status="done",
        answer_kind="answer",
        answer_text="t",
        chunk_ids=["c1"],
        contexts=["p"],
        models=["m"],
        tokens=7,
        latency_ms=1.5,
    )
    assert store.pending_case_ids(db, run_id) == ["b"]
    (item,) = [i for i in store.get_items(db, run_id) if i["case_id"] == "a"]
    assert item["answer_text"] == "t" and item["contexts"] == ["p"] and item["tokens"] == 7


def test_a_not_scored_result_is_stored_as_none_never_zero(db):
    run_id = make_run(db)
    store.save_result(db, run_id, "a", "faithfulness", score=None, decider="not_scored")
    store.save_result(db, run_id, "a", "case_pass", score=0.0, decider="code")
    rows = {r["metric"]: r for r in store.get_results(db, run_id)}
    assert rows["faithfulness"]["score"] is None and rows["faithfulness"]["scored"] is False
    assert rows["case_pass"]["score"] == 0.0 and rows["case_pass"]["scored"] is True


def test_a_result_is_replaced_not_added_when_scored_again(db):
    run_id = make_run(db)
    for score in (0.2, 0.9):
        store.save_result(db, run_id, "a", "case_pass", score=score, decider="code")
    (row,) = store.get_results(db, run_id)
    assert row["score"] == 0.9


def test_only_known_run_fields_may_be_updated(db):
    run_id = make_run(db)
    with pytest.raises(ValueError):
        store.update_run(db, run_id, estimate_code="X")
    store.update_run(db, run_id, status="running", started_at=NOW)
    assert store.get_run(db, run_id)["status"] == "running"


def test_usage_adds_up_per_day_run_and_group(db):
    first, second = make_run(db), make_run(db)
    store.add_usage(db, first, "2026-10-12", "judge", requests=3, tokens=100)
    store.add_usage(db, first, "2026-10-12", "judge", requests=2, tokens=50)
    store.add_usage(db, second, "2026-10-12", "judge", requests=1, tokens=10)
    store.add_usage(db, second, "2026-10-13", "judge", requests=9, tokens=9)
    assert store.usage_for_day(db, "2026-10-12") == {"judge": (6, 160)}
    assert store.usage_for_day(db, "2026-10-14") == {}


def test_deleting_a_run_removes_its_items_results_and_usage(db):
    run_id = make_run(db)
    store.save_result(db, run_id, "a", "m", score=1.0, decider="code")
    store.add_usage(db, run_id, "2026-10-12", "answer", requests=1, tokens=1)
    with db.transaction(write=True):
        db.execute("DELETE FROM eval_runs WHERE id = ?", (run_id,))
    for table in ("eval_items", "eval_results", "eval_usage_days"):
        assert db.query_one(f"SELECT COUNT(*) FROM {table}") == (0,)


# ============================================================================== isolation ====
def test_the_evaluation_tables_hold_no_user_session_or_secret_column(db):
    names = [r[0] for r in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")]
    forbidden = {"user_id", "session_id", "username", "email", "password_hash", "api_key", "token"}
    for table in names:
        columns = {r[1] for r in db.query_all(f"PRAGMA table_info({table})")}
        assert not forbidden & columns, table


def test_the_evaluation_database_has_none_of_the_main_tables(db):
    names = {r[0] for r in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not {"users", "sessions", "messages", "audit_log", "review_queue"} & names


def test_the_production_collection_name_is_refused_for_a_run():
    for bad in ("documents", "", "  ", "documents "):
        with pytest.raises(c.EvalConfigError):
            c.check_eval_collection(bad)
    with pytest.raises(c.EvalConfigError):
        c.check_eval_collection("some-other-collection")
    assert c.check_eval_collection("documents_eval") == "documents_eval"
    assert c.check_eval_collection("release2_eval") == "release2_eval"


# ===================================================================== ingest, test side ====
def test_an_ingest_into_the_test_collection_needs_a_local_database(monkeypatch):
    from app.ingest import cli as ingest_cli
    from app.ingest.model import IngestError

    env = {
        "INGEST_COLLECTION": "documents_eval",
        "DATABASE_URL": "postgresql://user:pw@db.example-cloud.test/main?sslmode=require",
        "QDRANT_URL": "http://localhost:6333",
    }
    with pytest.raises(IngestError) as caught:
        ingest_cli.build_services(env, "stores")
    assert "local SQLite" in str(caught.value) and "pw@" not in str(caught.value)


def test_an_ingest_into_the_production_collection_name_is_the_default(monkeypatch):
    from app.ingest import cli as ingest_cli

    assert ingest_cli.eval_collection({}) is None
    assert ingest_cli.eval_collection({"INGEST_COLLECTION": "documents"}) is None
    assert ingest_cli.eval_collection({"INGEST_COLLECTION": "documents_eval"}) == "documents_eval"
    with pytest.raises(c.EvalConfigError):
        ingest_cli.eval_collection({"INGEST_COLLECTION": "live_documents"})


def test_the_test_collection_ingest_does_not_need_or_touch_the_cache():
    from app.ingest import cli as ingest_cli

    env = {"INGEST_COLLECTION": "documents_eval", "DATABASE_URL": "x", "QDRANT_URL": "y"}
    assert ingest_cli.missing_settings(env, "cache") == []  # no UPSTASH_REDIS_URL needed
    cache = ingest_cli._EvalCache()
    assert cache.preflight() is True and cache.bump_corpus_version() == 0
    assert cache.invalidate_chunk("c") == 0 and cache.invalidate_doc("d") == 0


def test_the_ensure_command_takes_only_a_test_collection_name(capsys):
    from app.vector import documents

    assert documents.main(["--ensure", "--collection", "live_documents"], {}) == 1
    assert "evaluation" in capsys.readouterr().err


# ======================================================================== the purge ====
def test_the_retention_command_purges_old_samples_when_the_address_is_set(
    tmp_path, monkeypatch, capsys
):
    from app.memory import retention

    main_url = f"sqlite:///{tmp_path / 'main.db'}"
    from tests.api_fixtures import MIGRATIONS

    migrate(main_url, MIGRATIONS, log=lambda line: None)
    eval_url = store.sqlite_url(tmp_path / "eval.db")
    migrate(eval_url, store.MIGRATIONS, log=lambda line: None)
    edb = store.open_db(eval_url)
    row = {"role": "customer", "result_kind": "answer", "question": "q", "answer": "a"}
    store.insert_sample(edb, row, now=NOW - timedelta(days=45))
    store.insert_sample(edb, row, now=NOW - timedelta(days=2))
    edb.close()
    monkeypatch.setenv("DATABASE_URL", main_url)
    monkeypatch.setenv("EVAL_DATABASE_URL", eval_url)
    monkeypatch.delenv("LANGFUSE_USER_HASH_KEY", raising=False)
    assert retention.main(["--dry-run"], now=NOW) == 0
    assert "would delete 1 masked evaluation samples" in capsys.readouterr().out
    assert retention.main([], now=NOW) == 0
    assert "deleted 1 masked evaluation samples" in capsys.readouterr().out
    edb = store.open_db(eval_url)
    assert store.count_samples(edb) == 1
    edb.close()
    main = sqlite3.connect(tmp_path / "main.db")
    details = main.execute("SELECT details FROM audit_log WHERE action = 'audit_purge'").fetchone()
    main.close()
    assert json.loads(details[0])["eval_samples"] == 1


def test_without_the_address_the_retention_command_leaves_samples_alone(
    tmp_path, monkeypatch, capsys
):
    from app.memory import retention
    from tests.api_fixtures import MIGRATIONS

    main_url = f"sqlite:///{tmp_path / 'main.db'}"
    migrate(main_url, MIGRATIONS, log=lambda line: None)
    monkeypatch.setenv("DATABASE_URL", main_url)
    monkeypatch.delenv("EVAL_DATABASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_USER_HASH_KEY", raising=False)
    assert retention.main([], now=NOW) == 0
    assert "evaluation samples" not in capsys.readouterr().out
