"""The Dashboards admin section: a text table beside every chart, billed and shadow cost apart,
numbers from the service shown as text, and the same sign-out rule as every other call."""

import pytest

from tests.frontend_fixtures import FAKE_TOKEN, error, html_bodies, json_response, signed_in
from tests.test_frontend import admin_api, open_section
from tests.test_frontend_hardening import LABEL_SYNTAX, assert_clean, assert_signed_out

HOSTILE = "see https://evil.example $x$ :warning: a@evil.example"


def dashboards_api():
    api = admin_api()
    api.routes.update(
        {
            ("GET", "/admin/dashboards/deciders"): json_response(
                200,
                {
                    "steps": {
                        "triage": {"hour": {"jev": 0.75, "groq": 0.25}, "day": {"jev": 1.0}},
                        "answer": {"hour": {"gemini": 1.0}, "day": {"gemini": 0.5, "groq": 0.5}},
                    },
                    "health": {
                        "groq": {"keys": 2, "cooling": 1, "state": "cooling_down", "tiers": {}},
                        "gemini": {"keys": 1, "cooling": 0, "state": "healthy", "tiers": {}},
                    },
                    "from_requests": {"hour": {}, "day": {}},
                },
            ),
            ("GET", "/admin/dashboards/keys"): json_response(
                200,
                {
                    "providers": [
                        {
                            "provider": "groq",
                            "keys": 2,
                            "cooling": 1,
                            "state": "cooling_down",
                            "tiers": {"answer": "closed"},
                            "calls_today": 14,
                            "tokens_today": 9000,
                            "daily_calls_per_key": 1000,
                            "daily_tokens_per_key": 200000,
                            "per_key": [
                                {
                                    "fingerprint": "ab12cd34",
                                    "calls": 9,
                                    "tokens": 6000,
                                    "calls_left": 991,
                                    "tokens_left": 194000,
                                },
                                {
                                    "fingerprint": "ef56ab78",
                                    "calls": 5,
                                    "tokens": 3000,
                                    "calls_left": 995,
                                    "tokens_left": 197000,
                                },
                            ],  # fmt: skip
                        },
                        {
                            "provider": "gemini",
                            "keys": 1,
                            "cooling": 0,
                            "state": "healthy",
                            "tiers": {},
                            "calls_today": 2,
                            "tokens_today": 80,
                            "daily_calls_per_key": None,
                            "daily_tokens_per_key": None,
                            "per_key": [
                                {
                                    "fingerprint": "99aa00bb",
                                    "calls": 2,
                                    "tokens": 80,
                                    "calls_left": None,
                                    "tokens_left": None,
                                },
                            ],  # fmt: skip
                        },
                    ],
                    "day": "2026-10-17",
                },
            ),
            ("GET", "/admin/dashboards/usage"): json_response(
                200,
                {
                    "window": "day",
                    "role": None,
                    "requests": 12,
                    "tokens_p50": 3100.0,
                    "tokens_p95": 4800.0,
                    "tokens_p99": 5200.0,
                    "tokens_in": 30000,
                    "tokens_out": 6000,
                    "billed_cost": 0.0,
                    "shadow_cost": 0.0123,
                    "billed_per_answer": 0.0,
                    "shadow_per_answer": 0.001,
                    "tokens_per_stage": {"answer": 20000, "triage": 9000, HOSTILE: 7000},
                },
            ),
            ("GET", "/admin/dashboards/cache"): json_response(
                200,
                {
                    "window": "day",
                    "cache_configured": True,
                    "requests": 12,
                    "answered": 10,
                    "refused": 1,
                    "unavailable": 1,
                    "cache_hit_rate": 0.25,
                    "degraded_rate": 0.0833,
                    "low_confidence_rate": 0.0,
                },
            ),
            ("GET", "/admin/dashboards/ingestion"): json_response(
                200,
                {
                    "documents": [
                        {
                            "doc_id": "doc-a",
                            "chunks_active": 19,
                            "chunks_held_back": 1,
                            "sparse_vectors_out_of_date": 0,
                            "document_version": 2,
                        },
                    ],
                    "sparse_out_of_date": 0,
                    "held_back": 1,
                    "failed_pages_open": 2,
                    "recent": [{"action": "ingest", "document": "doc-a", "at": "2026-10-10T09:00"}],
                    "note": "Ingestion runs on the admin machine; the audit rows are kept 30 days.",
                },
            ),
            ("GET", "/admin/dashboards/daily"): json_response(
                200,
                {
                    "days": [
                        {
                            "day": "2026-10-10",
                            "roles": {
                                "customer": {
                                    "active_users": 4,
                                    "chats_started": 6,
                                    "questions": 11,
                                    "answers_verified": 8,
                                    "refused": 1,
                                    "thumbs_up_share": 0.75,
                                    "cache_hit_rate": 0.2,
                                    "tokens_per_answer": 3200.0,
                                    "billed_per_answer": 0.0,
                                    "shadow_per_answer": 0.001,
                                },
                                "staff": {
                                    "active_users": 2,
                                    "questions": 5,
                                    "seat_utilisation": 0.5,
                                },
                            },
                        }
                    ]
                },
            ),
        }
    )
    return api


def open_panel(api, monkeypatch, panel):
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    open_section(at, "Dashboards")
    if panel != "Decider in use":
        at.radio[1].set_value(panel).run()
    assert not at.exception, at.exception
    return at


def kinds(at):
    """The element types of the main page, in order (a table is `dataframe`)."""
    return [getattr(e, "type", None) for e in at.main]


@pytest.mark.parametrize("panel", ["Decider in use", "Tokens and cost", "Daily metrics"])
def test_a_table_comes_before_every_chart(monkeypatch, panel):
    at = open_panel(dashboards_api(), monkeypatch, panel)
    order = kinds(at)
    assert "dataframe" in order and "vega_lite_chart" in order, order
    assert order.index("dataframe") < order.index("vega_lite_chart")  # text first, then the chart
    # the table holds numbers: the chart is a picture of the same ones
    assert any(ch.isdigit() for ch in at.dataframe[0].value.to_string())


def test_the_deciders_table_has_each_step_decider_and_share(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Decider in use")
    text = "\n".join(t.value.to_string() for t in at.dataframe)
    assert "triage" in text and "jev" in text and "75.0 %" in text
    assert "cooling_down" in text and "healthy" in text


def test_the_key_panel_shows_counts_and_never_a_key(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Key pools")
    text = at.dataframe[0].value.to_string()
    assert "groq" in text and "cooling down" in text and "calls today" in text
    assert FAKE_TOKEN not in text
    assert any("never shown" in c.value for c in at.caption)


def test_the_key_panel_shows_the_pool_size_calls_today_and_what_is_left_per_key(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Key pools")
    assert any(m.label == "Keys in all pools" and m.value == "3" for m in at.metric)
    per_key = at.dataframe[1].value.to_string()
    assert "ab12cd34" in per_key and "991" in per_key and "194000" in per_key
    assert "not set" in per_key  # gemini has no configured daily limit
    assert FAKE_TOKEN not in per_key


def test_billed_and_shadow_cost_are_in_separate_columns_with_their_own_headings(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Tokens and cost")
    text = "\n".join(t.value.to_string() for t in at.dataframe)
    assert "billed cost (USD)" in text and "shadow cost (USD, reference price)" in text
    assert "total cost" not in text.lower()
    assert any("never added together" in c.value for c in at.caption)


def test_stage_names_from_the_service_are_neutralised_in_the_table_and_the_chart(monkeypatch):
    from frontend.views import dashboards

    drawn = []
    real = dashboards.st.bar_chart

    def record(data, *args, **kwargs):
        drawn.append(data)
        return real(data, *args, **kwargs)

    monkeypatch.setattr(dashboards.st, "bar_chart", record)
    at = open_panel(dashboards_api(), monkeypatch, "Tokens and cost")
    labels = [row["stage"] for data in drawn for row in data if "stage" in row]
    assert labels and not any(LABEL_SYNTAX.search(label) for label in labels)
    assert any("evil" in label for label in labels)  # the word stays, the syntax goes
    assert_clean(at)


def test_the_window_and_role_are_sent_to_the_service(monkeypatch):
    api = dashboards_api()
    at = open_panel(api, monkeypatch, "Tokens and cost")
    [s for s in at.selectbox if s.label == "Who"][0].set_value("Staff").run()
    [s for s in at.selectbox if s.label == "Window"][0].set_value("Last hour").run()
    usage = [q for path, q in api.queries if path == "/admin/dashboards/usage"]
    assert usage[0] == {"window": "day"}  # the first look: the last day, everyone
    assert usage[-1] == {"window": "hour", "role": "staff"}


def test_the_cache_panel_shows_rates_as_percentages(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Cache")
    text = at.dataframe[0].value.to_string()
    assert "25.0 %" in text and "cache hit rate" in text


def test_the_ingestion_panel_shows_counts_documents_and_recent_actions(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Ingestion health")
    text = "\n".join(t.value.to_string() for t in at.dataframe)
    assert "failed pages waiting" in text and "doc-a" in text and "ingest" in text


def test_the_daily_panel_has_one_row_per_role_and_the_thumbs_share(monkeypatch):
    at = open_panel(dashboards_api(), monkeypatch, "Daily metrics")
    text = at.dataframe[0].value.to_string()
    assert "customer" in text and "staff" in text and "75.0 %" in text and "50.0 %" in text
    for dropped in ("helpful", "time saved"):
        assert dropped not in text.lower()


def test_an_expired_sign_in_on_a_dashboard_call_signs_out(monkeypatch):
    api = dashboards_api()
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    open_section(at, "Dashboards")
    api.routes[("GET", "/admin/dashboards/deciders")] = error(
        401, "not_signed_in", "Please sign in again."
    )
    at.radio[1].set_value("Key pools").run()
    at.radio[1].set_value("Decider in use").run()
    assert_signed_out(at)


def test_the_section_is_listed_for_admins_and_absent_for_customers(monkeypatch):
    api = dashboards_api()
    admin = signed_in(api, monkeypatch, role="admin", username="boss").run()
    assert "Dashboards" in list(admin.radio[0].options)
    customer = signed_in(api, monkeypatch, role="customer").run()
    assert not customer.radio
    assert not [
        r
        for r in api.requests
        if r[1].startswith("/admin/dashboards") and r[3] and "customer" in r[1]
    ]


def test_a_panel_error_is_shown_in_plain_words(monkeypatch):
    api = dashboards_api()
    api.routes[("GET", "/admin/dashboards/cache")] = error(
        503, "service_unavailable", "The dashboards are not available right now."
    )
    at = open_panel(api, monkeypatch, "Cache")
    assert "temporarily unavailable" in "\n".join(html_bodies(at))
