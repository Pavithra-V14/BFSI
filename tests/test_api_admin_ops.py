"""The admin routes of Phase 10: users, review queue, documents, entities, alerts, rollout stage and
the hidden answers. Every one is admin only; every list has a default and a maximum page size."""

import json
import uuid

import pytest

from app.api.routes.admin_ops import CRITICAL_ALERTS
from tests.api_chat_fixtures import QUESTION, ChatEnv

MISSING = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, entity_review_enabled=True)
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def new_id():
    return str(uuid.uuid4())


def add_review(env, kind="refused", status="open", created="2026-10-09T09:00:00+00:00", **fields):
    item = new_id()
    env.execute(
        "INSERT INTO review_queue (id, kind, status, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (item, kind, status, json.dumps({"question": "a masked question"}), created),
    )
    return item


def add_entity(env, doc_id="doc-a", text="Rahul Sharma", status="pending"):
    entity = new_id()
    env.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, ?, ?, 'person_in_policy', ?, 'customer')",
        (entity, doc_id, text, status),
    )
    return entity


# ---- every route needs an admin ----------
ROUTES = [
    ("get", "/admin/users", None),
    ("put", f"/admin/users/{MISSING}/active", {"active": False}),
    ("get", "/admin/review", None),
    ("post", f"/admin/review/{MISSING}/label", {"label": "other"}),
    ("get", "/admin/documents", None),
    ("get", "/admin/documents/doc-a", None),
    ("get", "/admin/entities", None),
    ("put", f"/admin/entities/{MISSING}", {"status": "approved"}),
    ("get", "/admin/alerts", None),
    ("get", "/admin/shadow", None),
    ("put", "/admin/shadow", {"stage": "open"}),
    ("get", "/admin/shadow/answers", None),
]


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
@pytest.mark.parametrize("role", ["customer", "staff"])
def test_every_admin_route_refuses_a_customer_and_a_staff_user(env, method, url, body, role):
    _, token = env.user_token("asha_rao", role)
    response = getattr(env.client, method)(
        url, headers=env.bearer(token), **({"json": body} if body is not None else {})
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(("method", "url", "body"), ROUTES)
def test_every_admin_route_needs_a_signed_in_user(env, method, url, body):
    response = getattr(env.client, method)(url, **({"json": body} if body is not None else {}))
    assert response.status_code == 401


def test_a_staff_token_gets_403_on_the_hidden_answers(env):
    _, token = env.user_token("sam_staff", "staff")
    response = env.client.get("/admin/shadow/answers", headers=env.bearer(token))
    assert response.status_code == 403


# ---- users ----------
def test_the_user_list_shows_no_password_hash_and_no_email(env, admin):
    env.make_user("ravi_k")
    response = env.client.get("/admin/users", headers=env.bearer(admin))
    body = response.json()
    assert body["total"] == 2 and body["limit"] == 50
    for user in body["users"]:
        assert set(user) == {"id", "username", "role", "is_active", "created_at"}
    assert "argon2" not in response.text and "example.test" not in response.text


def test_the_user_list_is_paged_with_a_maximum(env, admin):
    for number in range(3):
        env.make_user(f"user_{number}")
    headers = env.bearer(admin)
    assert len(env.client.get("/admin/users?limit=2", headers=headers).json()["users"]) == 2
    assert env.client.get("/admin/users?limit=201", headers=headers).status_code == 422
    assert env.client.get("/admin/users?limit=0", headers=headers).status_code == 422


def test_an_admin_can_turn_a_customer_or_staff_account_off_and_on_with_an_audit_row(env, admin):
    customer = env.make_user("ravi_k")
    staff = env.make_user("sam_staff", "staff")
    headers = env.bearer(admin)
    for target in (customer, staff):
        off = env.client.put(
            f"/admin/users/{target}/active", json={"active": False}, headers=headers
        )
        assert off.status_code == 200 and off.json() == {"id": target, "is_active": False}
    assert env.rows("SELECT is_active FROM users WHERE id = ?", (customer,)) == [(0,)]
    assert (
        env.client.put(
            f"/admin/users/{customer}/active", json={"active": True}, headers=headers
        ).json()["is_active"]
        is True
    )
    actions = [r[0] for r in env.rows("SELECT action FROM audit_log WHERE target = ?", (customer,))]
    assert "account_deactivated" in actions and "account_activated" in actions


def test_a_deactivated_account_loses_access_at_once(env, admin):
    customer, token = env.user_token("ravi_k")
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 200
    env.client.put(
        f"/admin/users/{customer}/active", json={"active": False}, headers=env.bearer(admin)
    )
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_an_admin_account_is_never_changed_here_not_even_ones_own(env, admin):
    other_admin = env.make_user("second_boss", "admin")
    mine = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    headers = env.bearer(admin)
    for target in (other_admin, mine):
        response = env.client.put(
            f"/admin/users/{target}/active", json={"active": False}, headers=headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "target_is_admin"
    assert env.rows("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1") == [(2,)]


def test_an_unknown_or_malformed_user_id_is_404(env, admin):
    headers = env.bearer(admin)
    for target in (MISSING, "not-a-uuid"):
        response = env.client.put(
            f"/admin/users/{target}/active", json={"active": False}, headers=headers
        )
        assert response.status_code == 404


# ---- review queue ----------
def test_the_review_list_filters_pages_and_marks_overdue_items(env, admin):
    old = add_review(env, "refused", created="2026-10-01T09:00:00+00:00")  # a Thursday, long ago
    fresh = add_review(env, "thumbs_down", created="2099-01-02T09:00:00+00:00")
    add_review(env, "low_confidence", status="labelled")
    headers = env.bearer(admin)
    everything = env.client.get("/admin/review", headers=headers).json()
    assert everything["total"] == 3 and everything["limit"] == 20
    assert everything["labels"] == [
        "correct_refusal", "should_have_answered", "wrong_answer", "bad_question", "other",
    ]  # fmt: skip
    by_id = {i["id"]: i for i in everything["items"]}
    assert by_id[old]["overdue"] is True and by_id[fresh]["overdue"] is False
    assert [
        i["id"]
        for i in env.client.get("/admin/review?kind=thumbs_down", headers=headers).json()["items"]
    ] == [fresh]
    labelled = env.client.get("/admin/review?status=labelled", headers=headers).json()
    assert labelled["total"] == 1 and labelled["items"][0]["overdue"] is False  # only open ones
    assert env.client.get("/admin/review?kind=nonsense", headers=headers).status_code == 422
    assert env.client.get("/admin/review?limit=101", headers=headers).status_code == 422


def test_labelling_uses_a_fixed_list_and_writes_an_audit_row(env, admin):
    item = add_review(env)
    headers = env.bearer(admin)
    admin_id = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    response = env.client.post(
        f"/admin/review/{item}/label",
        json={"label": "should_have_answered", "status": "added_to_eval"},
        headers=headers,
    )
    assert response.status_code == 200
    row = env.rows("SELECT status, label, labelled_by FROM review_queue WHERE id = ?", (item,))[0]
    # no evaluation database is set up here: the item is labelled, NOT marked as added
    assert row == ("labelled", "should_have_answered", admin_id)
    assert response.json()["status"] == "labelled"
    audit = env.rows("SELECT actor, details FROM audit_log WHERE action = 'review_label'")
    assert audit and audit[0][0] == admin_id
    assert json.loads(audit[0][1]) == {
        "label": "should_have_answered",
        "status": "labelled",
        "evaluation_candidate": "no_evaluation_database",
    }


@pytest.mark.parametrize(
    "body",
    [
        {"label": "a free text label"},
        {"label": ""},
        {"label": "other", "status": "open"},
        {"label": "other", "note": "x"},
        {},
    ],
)
def test_a_label_outside_the_fixed_list_is_refused(env, admin, body):
    item = add_review(env)
    response = env.client.post(f"/admin/review/{item}/label", json=body, headers=env.bearer(admin))
    assert response.status_code == 422
    assert env.rows("SELECT label, status FROM review_queue WHERE id = ?", (item,)) == [
        (None, "open")
    ]


def test_labelling_an_unknown_item_is_404(env, admin):
    for target in (MISSING, "not-a-uuid"):
        response = env.client.post(
            f"/admin/review/{target}/label", json={"label": "other"}, headers=env.bearer(admin)
        )
        assert response.status_code == 404


# ---- documents (counts only) ----------
def test_the_document_status_shows_counts_and_never_text(env, admin):
    response = env.client.get("/admin/documents", headers=env.bearer(admin))
    body = response.json()
    assert body["total"] == 1 and body["documents"][0]["doc_id"] == "doc-a"
    document = body["documents"][0]
    assert document["chunks_active"] == 3 and document["chunks_stored"] == 3
    assert document["chunks_held_back"] == 0 and document["sparse_vectors_out_of_date"] == 0
    assert document["access_role"] == "customer" and document["document_version"] == 1
    assert "waiting period" not in response.text.lower() and "30 days" not in response.text
    one = env.client.get("/admin/documents/doc-a", headers=env.bearer(admin))
    assert one.status_code == 200 and one.json()["chunks_active"] == 3
    assert env.client.get("/admin/documents/nope", headers=env.bearer(admin)).status_code == 404
    assert env.client.get("/admin/documents/Bad_Id!", headers=env.bearer(admin)).status_code == 422


def test_the_document_list_has_a_page_maximum(env, admin):
    headers = env.bearer(admin)
    assert env.client.get("/admin/documents?limit=201", headers=headers).status_code == 422
    assert (
        env.client.get("/admin/documents?limit=1&offset=5", headers=headers).json()["documents"]
        == []
    )


# ---- entities ----------
def test_pending_entities_are_listed_and_an_admin_approves_one_with_who_and_when(env, admin):
    entity = add_entity(env)
    add_entity(env, text="Already", status="approved")
    headers = env.bearer(admin)
    listed = env.client.get("/admin/entities?doc_id=doc-a", headers=headers).json()
    assert listed["total"] == 1 and listed["entities"][0]["id"] == entity  # pending by default
    assert env.client.get("/admin/entities?status=approved", headers=headers).json()["total"] == 1
    response = env.client.put(
        f"/admin/entities/{entity}", json={"status": "approved"}, headers=headers
    )
    assert response.status_code == 200
    admin_id = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    row = env.rows(
        "SELECT status, approved_by, approved_at FROM allowlist_entities WHERE id = ?", (entity,)
    )[0]
    assert row[0] == "approved" and row[1] == admin_id and row[2]
    audit = env.rows(
        "SELECT actor FROM audit_log WHERE action = 'entity_review' AND target = ?", (entity,)
    )
    assert audit == [(admin_id,)]


def test_a_rejected_entity_records_who_rejected_it(env, admin):
    entity = add_entity(env)
    env.client.put(
        f"/admin/entities/{entity}", json={"status": "rejected"}, headers=env.bearer(admin)
    )
    admin_id = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    row = env.rows(
        "SELECT status, approved_by, approved_at FROM allowlist_entities WHERE id = ?", (entity,)
    )[0]
    assert row[0] == "rejected" and row[1] == admin_id and row[2]


def test_entity_review_is_refused_on_the_host_but_reading_is_allowed(tmp_path, fast_hashing):
    env = ChatEnv(tmp_path, on_render=True)
    try:
        admin = env.user_token("boss", "admin")[1]
        entity = add_entity(env)
        headers = env.bearer(admin)
        assert env.client.get("/admin/entities", headers=headers).status_code == 200
        response = env.client.put(
            f"/admin/entities/{entity}", json={"status": "approved"}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "admin_machine_only"
        assert env.rows("SELECT status FROM allowlist_entities WHERE id = ?", (entity,)) == [
            ("pending",)
        ]
        assert env.rows("SELECT COUNT(*) FROM audit_log WHERE action = 'entity_review'") == [(0,)]
    finally:
        env.close()


def test_an_unknown_entity_is_404_and_the_status_is_checked(env, admin):
    headers = env.bearer(admin)
    assert (
        env.client.put(
            f"/admin/entities/{MISSING}", json={"status": "approved"}, headers=headers
        ).status_code
        == 404
    )
    entity = add_entity(env)
    assert (
        env.client.put(
            f"/admin/entities/{entity}", json={"status": "pending"}, headers=headers
        ).status_code
        == 422
    )
    assert env.client.get("/admin/entities?limit=201", headers=headers).status_code == 422


# ---- alerts ----------
def test_alerts_are_counts_by_name_and_the_banner_shows_for_critical_ones(env, admin):
    svc = env.app.state.services
    headers = env.bearer(admin)
    quiet = env.client.get("/admin/alerts", headers=headers).json()
    assert quiet["banner"] is False and quiet["critical"] == []
    svc.emit("login_throttled", scope="user")
    svc.emit("login_throttled", scope="user")
    body = env.client.get("/admin/alerts", headers=headers).json()
    assert body["banner"] is False and {"name": "login_throttled", "count": 2} in body["events"]
    svc.emit("persist_queue_overflow")
    svc.emit("persist_job_dead_lettered", reason="ValueError")
    body = env.client.get("/admin/alerts", headers=headers).json()
    assert body["banner"] is True
    assert body["critical"] == ["persist_job_dead_lettered", "persist_queue_overflow"]
    assert "ValueError" not in json.dumps(body)  # names and counts only


def test_the_critical_list_is_the_one_the_owner_approved():
    assert CRITICAL_ALERTS == {
        "masker_load_failed", "masker_unavailable", "cache_quota_exceeded",
        "cache_out_of_memory", "cache_read_only", "vector_store_rejected",
        "budget_charge_failed", "graph_failed", "sparse_version_mismatch",
        "persist_queue_overflow", "persist_job_dead_lettered",
    }  # fmt: skip


def test_pipeline_alerts_reach_the_banner_data(env, admin):
    from app.gateway.errors import EmbeddingUnavailable

    env.rig.gateway.embed_error = EmbeddingUnavailable([("gemini", "rate_limited")])
    _, token = env.user_token("asha_rao")
    env.chat(token)
    body = env.client.get("/admin/alerts", headers=env.bearer(admin)).json()
    assert "stage_failed" in [e["name"] for e in body["events"]]


# ---- the rollout stage ----------
def test_the_stage_is_read_and_changed_with_an_audit_row(env, admin):
    headers = env.bearer(admin)
    env.execute("DELETE FROM app_settings")
    assert env.client.get("/admin/shadow", headers=headers).json() == {
        "stage": "shadow",
        "stages": ["shadow", "staff_beta", "open"],
    }
    response = env.client.put("/admin/shadow", json={"stage": "staff_beta"}, headers=headers)
    assert response.json() == {"stage": "staff_beta", "previous_stage": "shadow"}
    assert env.client.get("/admin/shadow", headers=headers).json()["stage"] == "staff_beta"
    assert env.rows("SELECT value FROM app_settings WHERE key = 'rollout_stage'") == [
        ("staff_beta",)
    ]
    audit = env.rows("SELECT details FROM audit_log WHERE action = 'rollout_stage_change'")
    assert json.loads(audit[0][0]) == {"from": "shadow", "to": "staff_beta"}
    for body in ({"stage": "everyone"}, {"stage": "open", "x": 1}, {}):
        assert env.client.put("/admin/shadow", json=body, headers=headers).status_code == 422


def test_hidden_answers_are_listed_for_the_admin_masked_and_with_a_hashed_user(env, admin):
    env.set_stage_in_database("shadow")
    customer_id, token = env.user_token("asha_rao")
    env.chat(token, "My name is Rahul Sharma. " + QUESTION)
    response = env.client.get("/admin/shadow/answers", headers=env.bearer(admin))
    body = response.json()
    assert body["total"] == 1 and body["limit"] == 20
    item = body["answers"][0]
    assert item["answer"].startswith("A waiting period of 30 days")
    assert item["citations"] and item["grounded"] is True
    assert "Rahul" not in response.text and "Sharma" not in response.text  # masked
    assert customer_id not in response.text and "asha_rao" not in response.text
    assert len(item["user"]) == 64  # a keyed hash, not the account
    assert set(item) == {
        "message_id", "session_id", "created_at", "question", "answer", "citations", "grounded",
        "source_removed", "kind", "user",
    }  # fmt: skip


def test_the_hidden_answers_are_paged_with_a_maximum_and_leave_visible_ones_out(env, admin):
    env.set_stage_in_database("open")
    _, token = env.user_token("asha_rao")
    env.chat(token)  # visible: not listed
    body = env.client.get("/admin/shadow/answers", headers=env.bearer(admin)).json()
    assert body["total"] == 0 and body["answers"] == []
    assert (
        env.client.get("/admin/shadow/answers?limit=101", headers=env.bearer(admin)).status_code
        == 422
    )
