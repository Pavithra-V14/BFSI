"""The purge (Change round 1, step 6): what it erases, what it keeps, and the gates in front of it.
Everything runs on a SQLite file, an in-memory vector store and a fake cache: never a real store."""

import json
import uuid
from datetime import UTC, datetime

import pytest

from app.maintenance import cli as purge_cli
from app.maintenance import purge as purge_module
from app.maintenance.purge import PHRASE
from tests.ingest_fixtures import Rig, para, parsed, title

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
COLLECTION = "documents"


def new_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    made.ingest(
        parsed(title("Section 2. Waiting periods"), para("A waiting period of 30 days applies."))
    )
    made.ingest(
        parsed(title("Section 1. Fees"), para("A fee of 2 percent applies to every loan.")),
        doc_id="doc-b",
    )
    yield made
    made.close()


def execute(rig, sql, params=()):
    with rig.db.transaction(write=True):
        rig.db.execute(sql, params)


def count(rig, table, where="1 = 1"):
    return rig.db.query_one(f"SELECT COUNT(*) FROM {table} WHERE {where}")[0]  # noqa: S608


def seed(rig):
    """Three accounts, a chat of each with a thumb, a review item, alert, metric and counter rows,
    and the things that must survive: an allow-list entity, a withdrawn id, a setting, an audit row
    and a pending trace removal."""
    ids = {}
    for name, role in (("boss", "admin"), ("asha", "customer"), ("ravi", "staff")):
        ids[name] = new_id()
        execute(
            rig,
            "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, 'h', ?)",
            (ids[name], name, f"{name}@example.test", role),
        )
        session, message = new_id(), new_id()
        execute(rig, "INSERT INTO sessions (id, user_id) VALUES (?, ?)", (session, ids[name]))
        execute(
            rig,
            "INSERT INTO messages (id, session_id, seq, role, content) "
            "VALUES (?, ?, 1, 'user', 'q')",
            (message, session),
        )
        execute(
            rig,
            "INSERT INTO feedback_entries (id, message_id, user_id, rating) VALUES (?, ?, ?, 'up')",
            (new_id(), message, ids[name]),
        )
    execute(
        rig,
        "INSERT INTO review_queue (id, kind, status) VALUES (?, 'refused', 'open')",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO alert_events (id, name, severity) VALUES (?, 'graph_failed', 'critical')",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO metric_rollups (id, period, period_start, role, metric, value) "
        "VALUES (?, 'hour', '2026-10-17T09:00:00+00:00', 'all', 'requests', 5)",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO key_usage_daily (id, day, provider, fingerprint, calls, tokens) "
        "VALUES (?, '2026-10-17', 'groq', 'aaaaaaaa', 3, 90)",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, 'doc-a', 'Rahul Sharma', 'person_in_policy', 'approved', 'customer')",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO withdrawn_documents (doc_id, reason_code) VALUES ('old-doc', 'outdated')",
    )
    execute(rig, "INSERT INTO app_settings (key, value) VALUES ('rollout_stage', 'staff_beta')")
    execute(
        rig,
        "INSERT INTO audit_log (id, actor, action, target, details) "
        "VALUES (?, 'cli:owner', 'ingest', 'doc-a', '{}')",
        (new_id(),),
    )
    execute(
        rig,
        "INSERT INTO trace_deletions (id, session_hash) VALUES (?, ?)",
        (new_id(), "ab" * 32),
    )
    redis = rig.redis
    redis.set("ans:abc", "x")
    redis.set("ans:def", "y")
    redis.sadd("chunk:c1", "ans:abc")
    redis.sadd("doc:doc-a", "ans:abc")
    redis.set("ctr:daily", "7")
    redis.set("rl:login:ip", "2")
    return ids


# ---- the plan changes nothing -------------------------------------------------------------------
def test_the_plan_counts_what_would_be_erased_and_changes_nothing(rig):
    seed(rig)
    before = {t: count(rig, t) for t in ("users", "sessions", "messages", "feedback_entries")}
    counts = purge_module.plan(rig.db, rig.qdrant, COLLECTION)
    assert counts["users"] == 2 and counts["admins_kept"] == 1
    assert counts["chats"] == 3 and counts["messages"] == 3 and counts["feedback"] == 3
    assert counts["review_items"] == counts["alert_rows"] == counts["metric_rows"] == 1
    assert counts["counter_rows"] == 1 and counts["chunks"] == len(rig.points()) + len(
        rig.points("doc-b")
    )
    assert {t: count(rig, t) for t in before} == before
    assert rig.redis.get("ans:abc") == "x"


# ---- the purge -----------------------------------------------------------------------------------
def run_purge(rig, actor="admin:boss"):
    return purge_module.purge(rig.db, rig.qdrant, COLLECTION, rig.cache, actor=actor, now=NOW)


def test_it_erases_vectors_cached_answers_chats_accounts_and_counters(rig):
    ids = seed(rig)
    chunks = len(rig.points()) + len(rig.points("doc-b"))
    result = run_purge(rig)
    assert result.complete and result.counts["chunks"] == chunks
    assert rig.qdrant.count(COLLECTION, exact=True).count == 0
    for table in ("sessions", "messages", "feedback_entries", "review_queue", "alert_events",
                  "metric_rollups", "key_usage_daily"):  # fmt: skip
        assert count(rig, table) == 0, table
    assert count(rig, "users") == 1 and count(rig, "users", f"id = '{ids['boss']}'") == 1
    assert result.counts["users"] == 2 and result.counts["cached_answers"] == 2
    for key in ("ans:abc", "ans:def", "chunk:c1", "doc:doc-a"):
        assert rig.redis.get(key) is None and not rig.redis.exists(key), key


def test_it_keeps_the_admin_the_audit_log_the_allow_list_the_lists_and_the_settings(rig):
    seed(rig)
    run_purge(rig)
    assert count(rig, "allowlist_entities") == 1
    assert count(rig, "withdrawn_documents") == 1
    assert count(rig, "app_settings") == 1
    assert count(rig, "trace_deletions") == 1  # a pending removal of traces is still owed
    assert count(rig, "audit_log", "action = 'ingest'") >= 1
    assert rig.redis.get("ctr:daily") == "7" and rig.redis.get("rl:login:ip") == "2"


def test_the_purge_is_recorded_in_the_audit_log_with_counts_and_no_text(rig):
    seed(rig)
    run_purge(rig, actor="admin:" + "b" * 32)
    rows = rig.db.query_all(
        "SELECT actor, action, target, details FROM audit_log WHERE action = 'purge_all'"
    )
    assert len(rows) == 1
    actor, action, target, details = rows[0]
    assert actor == "admin:" + "b" * 32 and target == "database"
    detail = json.loads(details)
    assert detail["users"] == 2 and detail["chats"] == 3 and "admins_kept" not in detail
    assert "example.test" not in details and "Rahul" not in details


def test_the_collection_and_its_set_up_stay_so_that_the_next_ingest_just_works(rig):
    run_purge(rig)
    assert rig.qdrant.collection_exists(COLLECTION)
    rig.ingest(parsed(title("Section 3"), para("Premiums are payable annually.")), doc_id="doc-c")
    assert rig.points("doc-c")


def test_the_corpus_version_is_raised_so_nothing_written_meanwhile_can_be_found(rig):
    before = rig.cache.corpus_version()
    run_purge(rig)
    assert rig.cache.corpus_version() > before


def test_it_is_safe_to_repeat_and_a_second_run_finds_nothing_to_erase(rig):
    seed(rig)
    run_purge(rig)
    again = run_purge(rig)
    assert again.complete and again.counts["chunks"] == 0 and again.counts["users"] == 0
    assert count(rig, "audit_log", "action = 'purge_all'") == 2  # each run leaves its own row


def test_a_vector_store_that_fails_is_named_and_the_other_steps_still_run(rig):
    seed(rig)

    class Broken:
        def scroll(self, *args, **kwargs):
            raise ConnectionError("CANARYHOST0 refused")

    result = purge_module.purge(
        rig.db, Broken(), COLLECTION, rig.cache, actor="admin:boss", now=NOW
    )
    assert not result.complete
    assert result.incomplete == ["the vector store did not finish (ConnectionError)"]
    assert "CANARYHOST0" not in repr(result)
    assert count(rig, "sessions") == 0  # the database step still ran


def test_a_database_that_fails_changes_nothing_and_says_so(rig, monkeypatch):
    seed(rig)

    def boom(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr(purge_module, "write_audit", boom)  # the last statement of the transaction
    result = run_purge(rig)
    assert result.incomplete == ["the database was not changed (RuntimeError)"]
    assert count(rig, "sessions") == 3 and count(rig, "users") == 3  # rolled back as one


def test_without_a_cache_it_says_so_instead_of_pretending(rig):
    seed(rig)
    result = purge_module.purge(rig.db, rig.qdrant, COLLECTION, None, actor="admin:boss", now=NOW)
    assert result.incomplete == ["no cache is configured here, so no cached answer was cleared"]


def test_an_actor_name_with_odd_characters_is_refused(rig):
    for actor in ("", "a b", "x;drop", "é", "a" * 81):
        with pytest.raises(ValueError):
            run_purge(rig, actor=actor)


def test_the_cache_clear_that_cannot_reach_the_cache_reports_none(rig, monkeypatch):
    rig.redis.set("ans:abc", "x")
    monkeypatch.setattr(rig.cache, "bump_corpus_version", lambda: None)
    assert rig.cache.clear_cached_answers() is None
    assert rig.redis.get("ans:abc") == "x"  # nothing is removed before the version is raised


# ---- the command line ---------------------------------------------------------------------------
def make_services(rig):
    return lambda env: {
        "db": rig.db,
        "qdrant": rig.qdrant,
        "collection": COLLECTION,
        "cache": rig.cache,
        "close": lambda: None,
    }


def run_cli(rig, argv, env=None, confirm=None, services=None):
    out, err = [], []
    code = purge_cli.main(
        argv,
        env={} if env is None else env,
        services_factory=services or make_services(rig),
        confirm=confirm or (lambda prompt: pytest.fail("a prompt was not expected")),
        out=out.append,
        err=err.append,
        now=lambda: NOW,
    )
    return code, out, err


def test_a_dry_run_shows_the_counts_and_changes_nothing(rig):
    seed(rig)
    code, out, _ = run_cli(rig, ["purge", "--dry-run"])
    text = "\n".join(out)
    assert code == 0 and "would be erased" in text and "dry run: nothing was changed" in text
    assert "kept:" in text and "1 admin account(s)" in text and "Langfuse traces" in text
    assert count(rig, "sessions") == 3 and rig.qdrant.count(COLLECTION, exact=True).count > 0


def test_a_wrong_phrase_erases_nothing(rig):
    seed(rig)
    for typed in ("", "delete all data", "DELETE ALL", "yes"):
        code, out, _ = run_cli(rig, ["purge"], confirm=lambda prompt, t=typed: t)
        assert code == 1 and "not confirmed; nothing was erased" in out[-1]
    assert count(rig, "sessions") == 3
    assert count(rig, "audit_log", "action = 'purge_all'") == 0


def test_the_phrase_typed_at_the_prompt_erases_and_prints_the_counts(rig):
    seed(rig)
    prompts = []

    def typed(prompt):
        prompts.append(prompt)
        return "  " + PHRASE + " "

    code, out, err = run_cli(rig, ["purge", "--actor", "cli:owner"], confirm=typed)
    assert code == 0 and not err and out[-1] == "done"
    assert prompts == [f"Type {PHRASE} to confirm: "]
    assert any(line.startswith("erased:") and "users" in line for line in out)
    assert count(rig, "sessions") == 0


def test_the_phrase_given_up_front_needs_no_prompt(rig):
    seed(rig)
    code, out, _ = run_cli(rig, ["purge", "--confirm-phrase", PHRASE, "--actor", "admin:boss"])
    assert code == 0 and out[-1] == "done" and count(rig, "sessions") == 0


def test_a_wrong_phrase_given_up_front_erases_nothing(rig):
    seed(rig)
    code, out, _ = run_cli(rig, ["purge", "--confirm-phrase", "please"])
    assert code == 1 and count(rig, "sessions") == 3


def test_it_refuses_on_render_and_never_builds_a_service(rig):
    def forbidden(env):
        raise AssertionError("no store may be opened on Render")

    code, _, err = run_cli(rig, ["purge", "--dry-run"], env={"RENDER": "true"}, services=forbidden)
    assert code == 1 and "never on Render" in err[0]


def test_an_odd_actor_is_refused_before_any_store_is_opened(rig):
    def forbidden(env):
        raise AssertionError("not opened")

    code, _, err = run_cli(rig, ["purge", "--actor", "bad actor"], services=forbidden)
    assert code == 1 and "--actor" in err[0]


def test_an_unexpected_error_prints_a_class_name_not_its_message(rig):
    def broken(env):
        raise RuntimeError("CANARYSTORE0 postgres://user:password@host/db")

    code, _, err = run_cli(rig, ["purge", "--dry-run"], services=broken)
    text = "\n".join(err)
    assert (
        code == 1
        and "RuntimeError" in text
        and "CANARYSTORE0" not in text
        and "password" not in text
    )


def test_an_incomplete_purge_exits_non_zero_and_says_to_run_it_again(rig):
    seed(rig)
    services = make_services(rig)

    def without_cache(env):
        return {**services(env), "cache": None}

    code, out, err = run_cli(rig, ["purge", "--confirm-phrase", PHRASE], services=without_cache)
    assert code == 1 and "run the same command again" in err[-1]


def test_the_missing_settings_are_named_and_never_their_values():
    with pytest.raises(purge_cli.MaintenanceError) as caught:
        purge_cli.build_services({})
    assert "DATABASE_URL" in str(caught.value) and "QDRANT_URL" in str(caught.value)


def test_the_command_has_no_way_to_name_another_collection():
    parser_args = purge_cli.__doc__
    assert "always works on the production collection" in parser_args
    import inspect

    assert "INGEST_COLLECTION" not in inspect.getsource(purge_cli)
