"""The evaluation routes and the review queue's "add to evaluation set" (Phase 13).

Admin only; a run is started only on the admin machine and only with the code of its estimate;
nothing the dashboard returns holds answer text; the two environments are never mixed."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.db.migrate import run as migrate
from app.eval import constants as c
from app.eval import store
from app.eval.access import EvalAccess
from app.eval.cases import load_cases
from tests.api_chat_fixtures import ChatEnv

# Made up from pieces: the values only have to be long enough to be noticed if they leaked.
KEYS = {
    name: "test-" + name.lower().replace("_", "-") + "-" + "z" * 12
    for name in (
        "EVAL_GROQ_API_KEYS",
        "EVAL_GEMINI_API_KEYS",
        "EVAL_OPENROUTER_API_KEYS",
        "JEV_OPENROUTER_API_KEY",
    )
}


class Starter:
    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))


@pytest.fixture
def paths(tmp_path):
    local = tmp_path / "eval_data" / "eval.db"
    local.parent.mkdir(parents=True)
    migrate(store.sqlite_url(local), store.MIGRATIONS, log=lambda line: None)
    production = tmp_path / "production_eval.db"
    migrate(store.sqlite_url(production), store.MIGRATIONS, log=lambda line: None)
    return local, production


def build(tmp_path, paths, *, on_render=False, with_keys=True, local=True, production=True):
    local_path, production_path = paths
    starter = Starter()
    access = EvalAccess(
        local_path=local_path if local else None,
        production_url=store.sqlite_url(production_path) if production else None,
        starter=starter,
        env=dict(KEYS) if with_keys else {},
    )
    env = ChatEnv(tmp_path, evaluation=access, on_render=on_render)
    return env, access, starter


@pytest.fixture
def env_and_access(tmp_path, fast_hashing, paths):
    env, access, starter = build(tmp_path, paths)
    yield env, access, starter
    env.close()


@pytest.fixture
def admin(env_and_access):
    return env_and_access[0].user_token("boss", "admin")[1]


def get(env, token, path, **params):
    return env.client.get(path, headers=env.bearer(token), params=params)


def post(env, token, path, body=None):
    return env.client.post(path, headers=env.bearer(token), json=body or {})


def audit_rows(env):
    return env.rows("SELECT action, target, details FROM audit_log WHERE action = 'evaluation_run'")


# ===================================================================== who may look ====
ROUTES = [
    ("get", "/admin/evaluation/environments", None),
    ("get", "/admin/evaluation/overview", None),
    ("get", f"/admin/evaluation/runs/{'a' * 32}", None),
    ("post", "/admin/evaluation/estimates", {"set": "active"}),
    ("post", f"/admin/evaluation/runs/{'a' * 32}/start", {"confirm_code": "ABCD-1234"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_every_evaluation_route_is_admin_only(env_and_access, method, path, body):
    env, _access, starter = env_and_access
    send = getattr(env.client, method)
    kwargs = {"json": body} if body is not None else {}
    assert send(path, **kwargs).status_code == 401
    for role in ("customer", "staff"):
        _id, token = env.user_token(f"user_{role}", role)
        assert send(path, headers=env.bearer(token), **kwargs).status_code == 403, (path, role)
    assert starter.calls == []


# ================================================================== the two views ====
def test_both_views_are_listed_and_a_run_can_be_started_off_render(env_and_access, admin):
    env, _access, _starter = env_and_access
    body = get(env, admin, "/admin/evaluation/environments").json()
    assert body == {"environments": ["local", "production"], "can_run": True, "on_render": False}


def test_on_render_only_the_production_view_exists_and_nothing_can_be_started(
    tmp_path, fast_hashing, paths
):
    env, _access, starter = build(tmp_path, paths, on_render=True, local=False)
    try:
        token = env.user_token("boss", "admin")[1]
        body = get(env, token, "/admin/evaluation/environments").json()
        assert body == {"environments": ["production"], "can_run": False, "on_render": True}
        assert get(env, token, "/admin/evaluation/overview", environment="local").status_code == 404
        assert post(env, token, "/admin/evaluation/estimates", {"set": "active"}).status_code == 409
        started = post(
            env, token, f"/admin/evaluation/runs/{'a' * 32}/start", {"confirm_code": "AAAA-1111"}
        )
        assert started.status_code == 409 and starter.calls == []
    finally:
        env.close()


def seed_run(path, *, environment="local", scores=(1.0, 0.0, 1.0), kind="active"):
    """A finished run with rule-based scores and one item, written straight to the database."""
    db = store.open_db(store.sqlite_url(path))
    version, cases = load_cases()
    store.sync_cases(db, cases)
    chosen = [x for x in cases if x.active][: len(scores)]
    run_id = store.create_run(
        db,
        environment=environment,
        kind=kind,
        dataset_version=version,
        system_version={"prompt_hash": "ph"},
        estimate={"total_requests": 10, "total_tokens": 100},
        estimate_code="AAAA-1111",
        case_ids=[x.id for x in chosen],
        batches=1,
        created_by="seed",
    )
    for case, score in zip(chosen, scores, strict=True):
        store.save_item(
            db,
            run_id,
            case.id,
            status="done",
            answer_kind="answer",
            answer_text="THE ANSWER TEXT",
            contexts=["a passage"],
            models=["m1"],
            cheap_checks={"stages_ms": {"generate": 120.0}},
            tokens=900,
            latency_ms=2000.0,
        )
        store.save_result(db, run_id, case.id, "case_pass", score=score, decider="code")
    store.update_run(
        db,
        run_id,
        status=c.RUN_FINISHED,
        finished_items=len(chosen),
        finished_at=datetime(2026, 10, 12, tzinfo=UTC),
    )
    db.close()
    return run_id


def test_the_overview_of_an_empty_database_is_an_honest_nothing(env_and_access, admin):
    env, _access, _starter = env_and_access
    body = get(env, admin, "/admin/evaluation/overview", environment="local").json()
    assert body["runs"] == [] and body["latest"]["golden_score"] is None
    assert body["band"]["width"] is None and body["cases"]["total"] == 0


def test_the_overview_reports_the_golden_score_and_never_mixes_the_environments(
    env_and_access, admin, paths
):
    env, _access, _starter = env_and_access
    seed_run(paths[0], environment="local", scores=(1.0, 0.0, 1.0, 1.0))
    local = get(env, admin, "/admin/evaluation/overview", environment="local").json()
    assert local["latest"]["golden_score"] == 0.75 and len(local["runs"]) == 1
    production = get(env, admin, "/admin/evaluation/overview", environment="production").json()
    assert production["runs"] == [] and production["latest"]["golden_score"] is None


def test_a_band_from_three_variance_runs_judges_the_latest_run(env_and_access, admin, paths):
    env, _access, _starter = env_and_access
    for scores in ((1.0, 1.0, 0.0, 1.0), (1.0, 0.0, 1.0, 1.0), (1.0, 1.0, 1.0, 0.0)):
        seed_run(paths[0], kind="variance", scores=scores)
    body = get(env, admin, "/admin/evaluation/overview", environment="local").json()
    assert body["band"]["n"] == 3 and body["band"]["mean"] == 0.75
    assert body["latest"]["verdict"] in ("noise", "better", "worse")
    assert len(body["series"]) == 3


def test_a_run_in_detail_has_numbers_and_ids_but_no_answer_text(env_and_access, admin, paths):
    env, _access, _starter = env_and_access
    run_id = seed_run(paths[0], scores=(1.0, 0.0))
    body = get(env, admin, f"/admin/evaluation/runs/{run_id}", environment="local").json()
    assert body["summary"]["golden_score"] == 0.5
    assert len(body["summary"]["failed_cases"]) == 1
    assert body["summary"]["stage_latency_ms"] == {"generate": 120.0}
    assert body["summary"]["model_mix"] == {"m1": 2}
    shown = json.dumps(body)
    assert "THE ANSWER TEXT" not in shown and "a passage" not in shown


def test_a_run_id_that_is_not_a_run_is_not_found_or_refused(env_and_access, admin):
    env, _access, _starter = env_and_access
    assert get(env, admin, f"/admin/evaluation/runs/{'a' * 32}").status_code == 404
    assert get(env, admin, "/admin/evaluation/runs/not-an-id").status_code == 422


# ============================================================================ estimate ====
def test_an_estimate_makes_no_model_call_and_returns_a_code_and_an_audit_row(env_and_access, admin):
    env, access, starter = env_and_access
    calls_before = len(env.gateway.calls)
    reply = post(env, admin, "/admin/evaluation/estimates", {"set": "active", "judged": True})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert len(body["run_id"]) == 32 and len(body["code"]) == 9 and body["batches"] >= 1
    assert body["estimate"]["cases"] == 20 and body["lines"]
    assert len(env.gateway.calls) == calls_before and starter.calls == []
    (row,) = audit_rows(env)
    assert row[0] == "evaluation_run" and row[1] == body["run_id"]
    assert json.loads(row[2])["step"] == "estimate"
    db = store.open_db(store.sqlite_url(access.local_path))
    assert store.get_run(db, body["run_id"])["status"] == c.RUN_ESTIMATED
    db.close()


def test_the_full_set_estimate_covers_sixty_cases(env_and_access, admin):
    env, _access, _starter = env_and_access
    body = post(env, admin, "/admin/evaluation/estimates", {"set": "full", "judged": False}).json()
    assert body["estimate"]["cases"] == 60 and body["estimate"]["judged"] is False


def test_an_estimate_without_evaluation_keys_is_refused_in_plain_words(
    tmp_path, fast_hashing, paths
):
    env, _access, _starter = build(tmp_path, paths, with_keys=False)
    try:
        token = env.user_token("boss", "admin")[1]
        reply = post(env, token, "/admin/evaluation/estimates", {"set": "active"})
        assert reply.status_code == 503 and "evaluation keys" in reply.json()["error"]["message"]
    finally:
        env.close()


def test_an_unknown_set_or_a_stray_field_is_refused(env_and_access, admin):
    env, _access, _starter = env_and_access
    assert post(env, admin, "/admin/evaluation/estimates", {"set": "everything"}).status_code == 422
    stray = post(env, admin, "/admin/evaluation/estimates", {"set": "active", "x": 1})
    assert stray.status_code == 422


# ================================================================================ start ====
def estimate_of(env, admin):
    body = post(
        env, admin, "/admin/evaluation/estimates", {"set": "active", "judged": False}
    ).json()
    return body["run_id"], body["code"]


def start(env, admin, run_id, code):
    return post(env, admin, f"/admin/evaluation/runs/{run_id}/start", {"confirm_code": code})


def test_a_start_with_the_wrong_code_starts_nothing(env_and_access, admin):
    env, _access, starter = env_and_access
    run_id, _code = estimate_of(env, admin)
    reply = start(env, admin, run_id, "ZZZZ-0000")
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "confirmation_mismatch"
    assert starter.calls == []
    assert [json.loads(r[2])["step"] for r in audit_rows(env)] == ["estimate"]


def test_a_start_with_the_right_code_runs_the_command_with_that_code(env_and_access, admin):
    env, _access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    reply = start(env, admin, run_id, code.lower())
    assert reply.status_code == 202 and reply.json() == {"run_id": run_id, "status": "starting"}
    assert starter.calls == [["run", run_id, "--confirm", code, "--from-page"]]
    assert [json.loads(r[2])["step"] for r in audit_rows(env)] == ["estimate", "start"]


def test_no_key_value_travels_to_the_command_or_the_reply(env_and_access, admin):
    env, _access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    reply = start(env, admin, run_id, code)
    shown = reply.text + json.dumps(starter.calls) + json.dumps(audit_rows(env), default=str)
    assert not any(value in shown for value in KEYS.values())


def test_a_run_that_is_running_or_ended_is_not_started_again(env_and_access, admin):
    env, access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    db = store.open_db(store.sqlite_url(access.local_path))
    store.update_run(db, run_id, status=c.RUN_RUNNING, heartbeat_at=datetime.now(UTC))
    db.close()
    reply = start(env, admin, run_id, code)
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "run_busy"
    assert starter.calls == []


def test_a_second_press_of_start_starts_one_process_only(env_and_access, admin):
    env, access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    first = start(env, admin, run_id, code)
    second = start(env, admin, run_id, code)
    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["error"]["code"] == "run_busy"
    assert len(starter.calls) == 1
    db = store.open_db(store.sqlite_url(access.local_path))
    assert store.get_run(db, run_id)["status"] == c.RUN_STARTING
    db.close()


def test_a_run_whose_process_went_silent_can_be_started_again(env_and_access, admin):
    env, access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    db = store.open_db(store.sqlite_url(access.local_path))
    stale = datetime.now(UTC) - timedelta(minutes=c.HEARTBEAT_STALE_MINUTES + 5)
    store.update_run(db, run_id, status=c.RUN_RUNNING, heartbeat_at=stale)
    db.close()
    assert start(env, admin, run_id, code).status_code == 202
    assert len(starter.calls) == 1


def test_a_code_older_than_its_window_starts_nothing(env_and_access, admin):
    env, access, starter = env_and_access
    run_id, code = estimate_of(env, admin)
    db = store.open_db(store.sqlite_url(access.local_path))
    old = datetime.now(UTC) - timedelta(minutes=c.CONFIRMATION_MINUTES + 5)
    with db.transaction(write=True):
        db.execute(
            "UPDATE eval_runs SET created_at = ? WHERE id = ?", (store.utc_text(old), run_id)
        )
    db.close()
    reply = start(env, admin, run_id, code)
    assert reply.status_code == 409 and reply.json()["error"]["code"] == "confirmation_expired"
    assert starter.calls == []


def test_a_spawn_that_fails_gives_the_run_back(env_and_access, admin):
    env, access, starter = env_and_access
    run_id, code = estimate_of(env, admin)

    def refuse(args):
        raise OSError("no process")

    access.starter = refuse
    assert start(env, admin, run_id, code).status_code == 503
    db = store.open_db(store.sqlite_url(access.local_path))
    assert store.get_run(db, run_id)["status"] == c.RUN_ESTIMATED
    db.close()


def test_a_start_for_an_unknown_run_is_not_found(env_and_access, admin):
    env, _access, starter = env_and_access
    reply = start(env, admin, "b" * 32, "AAAA-1111")
    assert reply.status_code == 404 and starter.calls == []


def test_the_code_must_be_short_text(env_and_access, admin):
    env, _access, _starter = env_and_access
    assert start(env, admin, "b" * 32, "x" * 100).status_code == 422


def test_a_process_that_cannot_be_spawned_is_a_plain_503(tmp_path, fast_hashing, paths):
    local_path, _production_path = paths

    def failing(args):
        raise OSError("no such interpreter")

    access = EvalAccess(local_path=local_path, starter=failing, env=dict(KEYS))
    env = ChatEnv(tmp_path, evaluation=access)
    try:
        token = env.user_token("boss", "admin")[1]
        run_id, code = estimate_of(env, token)
        assert start(env, token, run_id, code).status_code == 503
    finally:
        env.close()


# ================================================================ add to evaluation set ====
def add_review(env, kind="refused", doc_id=None, question="What is the waiting period?"):
    item = str(uuid.uuid4())
    env.execute(
        "INSERT INTO review_queue (id, kind, status, doc_id, details, created_at) "
        "VALUES (?, ?, 'open', ?, ?, ?)",
        (item, kind, doc_id, json.dumps({"question": question}), "2026-10-09T09:00:00+00:00"),
    )
    return item


def label(env, admin, item, label="should_have_answered", status="added_to_eval"):
    return post(env, admin, f"/admin/review/{item}/label", {"label": label, "status": status})


def candidates_in(path):
    db = store.open_db(store.sqlite_url(path))
    try:
        return store.list_cases(db, status="candidate")
    finally:
        db.close()


def test_adding_an_item_makes_a_masked_candidate_not_a_case_of_the_set(
    env_and_access, admin, paths
):
    env, _access, _starter = env_and_access
    item = add_review(env, doc_id="doc-a")
    reply = label(env, admin, item)
    assert reply.status_code == 200
    assert reply.json()["status"] == "added_to_eval"
    assert reply.json()["evaluation_candidate"] == "added"
    (candidate,) = candidates_in(paths[1])  # the production evaluation database
    assert candidate.status == "candidate" and candidate.origin == "review_queue"
    assert candidate.turns == ("What is the waiting period?",) and candidate.review_item_id == item
    assert candidate.active is False and candidate.behaviour == "answer"
    assert candidates_in(paths[0]) == []  # not written twice


def test_adding_twice_is_harmless(env_and_access, admin, paths):
    env, _access, _starter = env_and_access
    item = add_review(env)
    assert label(env, admin, item).json()["evaluation_candidate"] == "added"
    assert label(env, admin, item).json()["evaluation_candidate"] == "already_added"
    assert len(candidates_in(paths[1])) == 1


def test_a_correct_refusal_is_a_refusal_case_and_a_bad_question_is_not_added(
    env_and_access, admin, paths
):
    env, _access, _starter = env_and_access
    refused = add_review(env, kind="refused", question="Who won the cricket match?")
    assert label(env, admin, refused, "correct_refusal").json()["evaluation_candidate"] == "added"
    bad = add_review(env, question="asdf qwerty")
    assert label(env, admin, bad, "bad_question").json()["evaluation_candidate"] == "not_added"
    (candidate,) = candidates_in(paths[1])
    assert candidate.behaviour == "refuse" and candidate.category == "out_of_domain"


def test_labelling_without_adding_writes_no_candidate(env_and_access, admin, paths):
    env, _access, _starter = env_and_access
    item = add_review(env)
    reply = label(env, admin, item, status="labelled")
    assert reply.status_code == 200 and "evaluation_candidate" not in reply.json()
    assert candidates_in(paths[1]) == []


def test_without_an_evaluation_database_the_label_is_kept_and_the_reply_says_so(
    tmp_path, fast_hashing
):
    env = ChatEnv(tmp_path)  # no evaluation access at all
    try:
        token = env.user_token("boss", "admin")[1]
        item = add_review(env)
        reply = label(env, token, item)
        assert reply.status_code == 200
        assert reply.json()["evaluation_candidate"] == "no_evaluation_database"
        rows = env.rows("SELECT status FROM review_queue WHERE id = ?", (item,))
        assert rows == [("labelled",)], "never marked as added when it was not added"
        assert reply.json()["status"] == "labelled"
    finally:
        env.close()


def test_the_label_audit_row_is_written_either_way(env_and_access, admin):
    env, _access, _starter = env_and_access
    item = add_review(env)
    label(env, admin, item)
    rows = env.rows("SELECT action, target FROM audit_log WHERE action = 'review_label'")
    assert rows == [("review_label", item)]


def test_a_candidate_is_masked_again_before_it_is_written(env_and_access, admin, paths):
    """The review item holds the stored (masked) question; the candidate passes the application's
    own masker once more, so an identifier that got into the item still never reaches the
    evaluation database."""
    env, _access, _starter = env_and_access
    item = add_review(env, question="My PAN is ABCPE1234F. What is the waiting period?")
    reply = label(env, admin, item)
    assert reply.json()["evaluation_candidate"] == "added"
    (candidate,) = candidates_in(paths[1])
    assert "ABCPE1234F" not in " ".join(candidate.turns)
    assert "waiting period" in candidate.turns[0]


def test_a_run_and_its_audit_rows_name_the_admin_who_made_them(env_and_access, admin):
    env, access, _starter = env_and_access
    run_id, code = estimate_of(env, admin)
    start(env, admin, run_id, code)
    admin_id, username = env.rows("SELECT id, username FROM users WHERE username = 'boss'")[0]
    db = store.open_db(store.sqlite_url(access.local_path))
    try:
        assert store.get_run(db, run_id)["created_by"] == f"admin:{username}"
    finally:
        db.close()
    sql = "SELECT actor FROM audit_log WHERE action = 'evaluation_run'"
    assert {r[0] for r in env.rows(sql)} == {admin_id}


def test_a_candidate_is_not_written_when_the_masker_is_not_ready(env_and_access, admin, paths):
    env, _access, _starter = env_and_access
    holder = env.rig.pipeline.deps.masker
    ready = holder._ready
    holder._ready = type("E", (), {"is_set": staticmethod(lambda: False)})()
    try:
        item = add_review(env)
        reply = label(env, admin, item)
    finally:
        holder._ready = ready
    assert reply.json()["evaluation_candidate"] == "failed"
    assert reply.json()["status"] == "labelled"
    assert candidates_in(paths[1]) == []


def test_the_admin_page_never_puts_red_team_cases_into_a_main_estimate(env_and_access, admin):
    """The red-team cases share the case table under their own dataset version. After a red-team
    estimate has synced them, a 'full', 'active' or 'variance' estimate from the page still
    holds only the main set (Phase 16, the held-out half is for the gate only)."""
    from app.eval import redteam

    env, access, _starter = env_and_access
    local = store.open_db(store.sqlite_url(access.local_path))
    _version, red = redteam.load_redteam()
    store.sync_cases(local, red)  # what `estimate --set redteam_*` does on the command line
    assert len(store.list_cases(local, status="set")) == 40
    local.close()
    for which, expected in (("full", 60), ("active", 20), ("variance", 20)):
        body = post(
            env, admin, "/admin/evaluation/estimates", {"set": which, "judged": False}
        ).json()
        assert body["estimate"]["cases"] == expected, which
        db = store.open_db(store.sqlite_url(access.local_path))
        try:
            ids = [i["case_id"] for i in store.get_items(db, body["run_id"])]
            assert not any(i.startswith("rt-") for i in ids), which
            assert store.get_run(db, body["run_id"])["dataset_version"] == "2026-10-v1"
        finally:
            db.close()


def test_the_dashboard_shows_held_out_failures_as_a_count_and_development_ones_by_id(
    env_and_access, admin
):
    from app.eval import redteam

    env, access, _starter = env_and_access
    db = store.open_db(store.sqlite_url(access.local_path))
    _version, red = redteam.load_redteam()
    store.sync_cases(db, red)
    ids = [x.id for x in red if x.id.startswith("rt-held-")][:3] + [
        x.id for x in red if x.id.startswith("rt-dev-")
    ][:2]
    run_id = store.create_run(
        db, environment="local", kind="full", dataset_version="redteam-1", system_version={},
        estimate={}, estimate_code="x", case_ids=ids, batches=1, created_by="test",
    )  # fmt: skip
    for case_id in ids:
        store.save_item(db, run_id, case_id, status="done", answer_kind="answer")
        store.save_result(db, run_id, case_id, "case_pass", score=0.0, decider="code")
    store.update_run(db, run_id, status=c.RUN_FINISHED)
    db.close()
    summary = get(env, admin, f"/admin/evaluation/runs/{run_id}", environment="local").json()
    summary = summary["summary"]
    assert summary["held_out_ids_hidden"] is True and summary["failed_held_out_count"] == 3
    assert all(i.startswith("rt-dev-") for i in summary["failed_cases"])
    assert len(summary["failed_cases"]) == 2
    assert not any("rt-held-" in str(v) for v in summary.values())


def test_the_dashboard_case_totals_leave_the_red_team_cases_out(env_and_access, admin):
    from app.eval import redteam

    env, access, _starter = env_and_access
    db = store.open_db(store.sqlite_url(access.local_path))
    store.sync_cases(db, load_cases()[1])
    store.sync_cases(db, redteam.load_redteam()[1])
    db.close()
    cases = get(env, admin, "/admin/evaluation/overview", environment="local").json()["cases"]
    assert cases["total"] == 60 and cases["active"] == 20
    assert cases["redteam_cases"] == 40
    assert "redteam-1" not in cases["dataset_versions"]
    assert sum(cases["by_category"].values()) == 60
