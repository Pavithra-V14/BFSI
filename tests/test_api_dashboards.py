"""The dashboards' routes and the observability wiring of the API (spec 5.3, 5.4, 15.6, 18)."""

import json

import pytest

from tests.api_chat_fixtures import ChatEnv

ROUTES = [
    "/admin/dashboards/deciders",
    "/admin/dashboards/keys",
    "/admin/dashboards/usage",
    "/admin/dashboards/cache",
    "/admin/dashboards/ingestion",
    "/admin/dashboards/daily",
]


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, observability=True)
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def get(env, token, path):
    return env.client.get(path, headers=env.bearer(token))


# ===================================================================== who may look ====
@pytest.mark.parametrize("path", ROUTES)
def test_every_dashboard_route_is_admin_only(env, path):
    assert env.client.get(path).status_code == 401
    for role in ("customer", "staff"):
        _, token = env.user_token(f"user_{role}", role)
        assert get(env, token, path).status_code == 403, (path, role)
    _, boss = env.user_token("boss", "admin")
    assert get(env, boss, path).status_code == 200


def test_the_routes_are_not_available_without_the_observability_parts(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path)  # observability off
    try:
        token = made.user_token("boss", "admin")[1]
        for path in ("/admin/dashboards/deciders", "/admin/dashboards/usage"):
            assert made.client.get(path, headers=made.bearer(token)).status_code == 503
    finally:
        made.close()


@pytest.mark.parametrize(
    "query",
    ["days=0", "days=91", "days=abc"],
)
def test_the_daily_series_has_a_default_and_a_maximum(env, admin, query):
    assert get(env, admin, f"/admin/dashboards/daily?{query}").status_code == 422
    assert get(env, admin, "/admin/dashboards/daily?days=90").status_code == 200
    assert get(env, admin, "/admin/dashboards/daily").status_code == 200


@pytest.mark.parametrize("query", ["window=week", "window=", "role=admin", "role=anyone"])
def test_a_window_or_role_that_is_not_in_the_list_is_refused(env, admin, query):
    assert get(env, admin, f"/admin/dashboards/usage?{query}").status_code == 422


# ============================================================================ what they hold ====
def test_usage_after_a_chat_has_tokens_and_separate_costs_and_no_text(env, admin):
    _, token = env.user_token("asha_rao")
    assert env.chat(token).status_code == 200
    response = get(env, admin, "/admin/dashboards/usage?window=hour")
    body = response.json()
    assert body["window"] == "hour" and body["requests"] == 1
    assert {"billed_cost", "shadow_cost", "billed_per_answer", "shadow_per_answer"} <= set(body)
    assert "total_cost" not in body
    assert set(body["tokens_per_stage"]) and body["tokens_in"] >= 0
    for leaked in ("waiting period", "asha_rao", "30 days"):
        assert leaked not in response.text


def test_usage_can_be_cut_by_role(env, admin):
    _, customer = env.user_token("asha_rao")
    _, staff = env.user_token("ravi_kumar", "staff")
    env.chat(customer)
    env.chat(staff)
    assert get(env, admin, "/admin/dashboards/usage?role=customer").json()["requests"] == 1
    assert get(env, admin, "/admin/dashboards/usage?role=staff").json()["requests"] == 1
    assert get(env, admin, "/admin/dashboards/usage").json()["requests"] == 2


def test_the_cache_panel_has_rates_and_outcomes(env, admin):
    _, token = env.user_token("asha_rao")
    env.chat(token)
    body = get(env, admin, "/admin/dashboards/cache").json()
    assert body["requests"] == 1 and body["answered"] == 1
    assert 0.0 <= body["cache_hit_rate"] <= 1.0 and body["cache_configured"] is True


def test_the_decider_panel_has_health_per_provider_and_shares_per_step(env, admin):
    _, token = env.user_token("asha_rao")
    env.chat(token)
    body = get(env, admin, "/admin/dashboards/deciders").json()
    assert set(body) == {"steps", "health", "from_requests"}
    assert set(body["from_requests"]) == {"hour", "day"}
    for windows in body["steps"].values():
        assert set(windows) == {"hour", "day"}


def test_the_key_panel_shows_counts_and_states_and_no_key_or_fingerprint(env, admin):
    response = get(env, admin, "/admin/dashboards/keys")
    for provider in response.json()["providers"]:
        assert set(provider) == {"provider", "keys", "cooling", "state", "tiers"}
        assert isinstance(provider["keys"], int) and isinstance(provider["cooling"], int)
    assert "fingerprint" not in response.text


def test_the_ingestion_panel_has_counts_and_the_last_audit_rows(env, admin):
    env.execute(
        "INSERT INTO audit_log (id, actor, action, target, details, created_at) "
        "VALUES ('a1', 'cli:owner', 'ingest', 'doc-a', '{}', '2026-10-10T09:00:00+00:00')"
    )
    env.execute(
        "INSERT INTO review_queue (id, kind, status, details, created_at) "
        "VALUES ('r1', 'failed_ingest_page', 'open', '{}', '2026-10-10T09:00:00+00:00')"
    )
    body = get(env, admin, "/admin/dashboards/ingestion").json()
    assert body["documents"] and body["failed_pages_open"] == 1
    assert body["recent"][0] == {
        "action": "ingest",
        "document": "doc-a",
        "at": "2026-10-10T09:00:00+00:00",
    }  # newest first
    assert "sparse_out_of_date" in body and "held_back" in body


# ======================================================================== the rollup pass ====
def test_a_rollup_pass_saves_the_hour_and_the_days_counts_and_the_series_reads_them(env, admin):
    _, token = env.user_token("asha_rao")
    env.chat(token)
    env.chat(token, "And what about accident claims?")
    written = env.observability.rollup_once()
    assert written > 0
    today = get(env, admin, "/admin/dashboards/daily?days=2").json()["days"][-1]
    customer = today["roles"]["customer"]
    assert customer["requests"] == 2.0 and customer["answered"] == 2.0
    assert {"questions", "chats_started", "active_users"} <= set(customer)  # from the tables
    assert "thumbs_up_share" in customer and "tokens_per_answer" in customer


def test_a_second_pass_does_not_count_the_same_requests_twice(env, admin):
    _, token = env.user_token("asha_rao")
    env.chat(token)
    env.observability.rollup_once()
    env.observability.rollup_once()
    today = get(env, admin, "/admin/dashboards/daily?days=1").json()["days"][-1]
    assert today["roles"]["customer"]["requests"] == 1.0
    assert today["roles"]["all"]["requests"] == 1.0


def test_the_saved_rows_hold_no_user_or_session_id(env):
    user_id, token = env.user_token("asha_rao")
    session_id = env.chat(token).json()["session_id"]
    env.observability.rollup_once()
    stored = json.dumps([list(map(str, r)) for r in env.rows("SELECT * FROM metric_rollups")])
    assert user_id not in stored and session_id not in stored


# ===================================================================== alerts through the API ====
def test_an_event_the_system_emits_becomes_a_persisted_alert_on_the_banner(env, admin):
    env.app.state.services.emit("all_keys_exhausted", provider="groq")
    body = get(env, admin, "/admin/alerts").json()
    assert body["banner"] is True and "all_keys_exhausted" in body["critical"]
    (recent,) = body["recent"]
    assert set(recent) == {"name", "severity", "count", "notified", "at"}
    assert recent["name"] == "all_keys_exhausted" and recent["severity"] == "critical"


def test_the_persisted_alert_survives_the_in_process_buffer(env, admin):
    env.app.state.services.emit("graph_failed", reason="KeyError")
    env.app.state.services.events.clear()  # a restart: the buffer is empty
    body = get(env, admin, "/admin/alerts").json()
    assert body["banner"] is True and body["critical"] == ["graph_failed"]


def test_a_warning_alert_is_listed_but_does_not_raise_the_banner(env, admin):
    env.app.state.services.emit("jev_unavailable")
    env.app.state.services.events.clear()
    body = get(env, admin, "/admin/alerts").json()
    assert body["banner"] is False and body["critical"] == []
    assert [a["severity"] for a in body["recent"]] == ["warning"]


def test_an_event_that_is_not_an_alert_is_not_persisted(env):
    env.app.state.services.emit("persist_queued", queued=3)
    assert env.rows("SELECT COUNT(*) FROM alert_events") == [(0,)]


def test_the_alert_rows_hold_no_text_a_user_wrote(env):
    _, token = env.user_token("asha_rao")
    env.chat(token, "My PAN is ABCDE1234F, please help with " + "claims")
    env.app.state.services.emit("graph_failed", reason="KeyError")
    stored = json.dumps([list(map(str, r)) for r in env.rows("SELECT * FROM alert_events")])
    assert "ABCDE1234F" not in stored and "asha_rao" not in stored


def test_the_pipeline_hands_each_finished_request_to_the_observer(env):
    _, token = env.user_token("asha_rao")
    env.chat(token)
    window = env.observability.hub.window(86_400)
    assert len(window) == 1 and window[0].role == "customer" and window[0].kind == "answer"
