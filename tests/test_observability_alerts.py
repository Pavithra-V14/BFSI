"""Alerts and the chat webhook (spec 14, 18; Phase 12)."""

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.db.connection import connect
from app.db.migrate import run
from app.observability import alerts, webhook
from app.observability.alerts import AlertConfig, AlertManager, AlertStore
from app.observability.metrics import RequestObservation
from tests.api_fixtures import MIGRATIONS

START = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
# Built from pieces so this file holds no real-looking webhook address or credential URL.
GOOD_URL = "https://hooks.acme-corp.org/" + "incoming/" + "k3J9x2Lm8Qw4Zp7R"
CREDENTIAL_URL = "https://user:" + "pw1234567" + "@hooks.acme-corp.org/x"
SLACK_SHAPE = "https://hooks.slack.com/" + "services/T0A1B2C3D/B4E5F6G7H/q9w8e7r6t5y4u3i2o1p0"


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def database(tmp_path):
    url = f"sqlite:///{tmp_path / 'alerts.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    return url


class Recorder:
    """A webhook sender that records what it was asked to post."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.format = "text"

    def send(self, text):
        if self.fail:
            raise httpx.ConnectError("down")
        self.sent.append(text)


def manager(database, clock, sender=None, config=None, events=None, **extra):
    return AlertManager(
        store=AlertStore(lambda: connect(database)),
        sender=sender,
        config=config or AlertConfig(),
        now=clock,
        clock=lambda: clock.now.timestamp(),
        on_event=(lambda n, f: events.append((n, f))) if events is not None else None,
        background=False,
        **extra,
    )


def rows(database):
    db = connect(database)
    try:
        return db.query_all(
            "SELECT name, severity, event_count, notified FROM alert_events ORDER BY created_at, id"
        )
    finally:
        db.close()


def obs(at, kind="answer", **fields):
    return RequestObservation(at=at.timestamp(), role="customer", kind=kind, **fields)


def feed(mgr, clock, count, **fields):
    for _ in range(count):
        clock.advance(seconds=1)
        mgr.observe(obs(clock.now, **fields))


# ============================================================================ event alerts ====
def test_a_critical_event_is_persisted_and_sent(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    mgr.on_event("all_keys_exhausted", {"provider": "groq"})
    assert rows(database) == [("all_keys_exhausted", "critical", 1, 1)]
    assert sender.sent == ["BFSI assistant [critical] all_keys_exhausted: 1"]


def test_a_warning_event_is_persisted_for_the_banner_but_not_sent(database):
    clock, sender = Clock(), Recorder()
    manager(database, clock, sender).on_event("jev_unavailable", {})
    assert rows(database) == [("jev_unavailable", "warning", 1, 0)]
    assert sender.sent == []


def test_an_event_that_is_not_an_alert_is_ignored(database):
    clock = Clock()
    manager(database, clock, Recorder()).on_event("persist_queued", {"queued": 2})
    assert rows(database) == []


def test_a_critical_event_with_no_webhook_is_still_persisted(database):
    clock = Clock()
    manager(database, clock, None).on_event("graph_failed", {"reason": "KeyError"})
    assert rows(database) == [("graph_failed", "critical", 1, 0)]


# ======================================================================= one per name per hour ====
def test_one_alert_per_name_per_hour_and_later_ones_only_raise_the_count(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    for _ in range(5):
        mgr.on_event("all_keys_exhausted", {})
        clock.advance(minutes=5)
    assert rows(database) == [("all_keys_exhausted", "critical", 5, 1)]
    assert len(sender.sent) == 1


def test_after_the_hour_a_new_alert_is_made(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    mgr.on_event("all_keys_exhausted", {})
    clock.advance(minutes=61)
    mgr.on_event("all_keys_exhausted", {})
    assert len(rows(database)) == 2 and len(sender.sent) == 2


def test_the_hour_limit_survives_a_restart(database):
    clock, sender = Clock(), Recorder()
    manager(database, clock, sender).on_event("all_keys_exhausted", {})
    clock.advance(minutes=10)
    again = manager(database, clock, sender)  # a new process: nothing in memory
    again.on_event("all_keys_exhausted", {})
    assert len(rows(database)) == 1 and len(sender.sent) == 1


def test_two_different_names_are_independent(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    mgr.on_event("all_keys_exhausted", {})
    mgr.on_event("graph_failed", {})
    assert len(rows(database)) == 2 and len(sender.sent) == 2


# ============================================================================ the daily cap ====
def test_the_daily_cap_stops_the_webhook_but_not_the_banner(database):
    clock, sender = Clock(), Recorder()
    names = ["all_keys_exhausted", "graph_failed", "cache_read_only", "vector_store_rejected"]
    mgr = manager(database, clock, sender, AlertConfig(webhook_daily_cap=2))
    for name in names:
        mgr.on_event(name, {})
    assert len(sender.sent) == 2
    assert sorted(r[3] for r in rows(database)) == [0, 0, 1, 1]
    assert len(rows(database)) == 4  # all four are on the banner


def test_the_cap_starts_again_the_next_utc_day(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender, AlertConfig(webhook_daily_cap=1))
    mgr.on_event("all_keys_exhausted", {})
    mgr.on_event("graph_failed", {})
    assert len(sender.sent) == 1
    clock.advance(days=1)
    mgr.on_event("cache_read_only", {})
    assert len(sender.sent) == 2


# =================================================================================== the rules ====
def test_a_rule_does_not_fire_below_the_minimum_number_of_requests(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    feed(mgr, clock, 19, kind="unavailable")  # every request failed, but only 19 of them
    assert rows(database) == [] and sender.sent == []


def test_the_error_rate_rule_fires_at_the_minimum_and_is_critical(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    feed(mgr, clock, 14, kind="answer")
    feed(mgr, clock, 6, kind="unavailable")  # 6 of 20 = 30 % >= 25 %
    assert rows(database) == [("error_rate", "critical", 6, 1)]
    assert sender.sent == ["BFSI assistant [critical] error_rate: 6 in 15 min"]


def test_the_error_rate_stays_quiet_below_its_threshold(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 18, kind="answer")
    feed(mgr, clock, 2, kind="unavailable")  # 10 %
    assert rows(database) == []


def test_the_fallback_rate_is_a_warning_for_the_banner_only(database):
    clock, sender = Clock(), Recorder()
    mgr = manager(database, clock, sender)
    feed(mgr, clock, 10, kind="answer")
    feed(mgr, clock, 10, kind="answer", degraded=("lexical_rerank",))  # 50 % >= 30 %
    assert [r[:2] for r in rows(database)] == [("fallback_rate", "warning")]
    assert sender.sent == []


def test_the_guardrail_spike_counts_only_guardrail_refusals(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 12, kind="refusal", reason="out_of_domain")  # not a guardrail
    feed(mgr, clock, 8, kind="answer")
    assert rows(database) == []


def test_a_burst_of_guardrail_refusals_is_a_spike(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 10, kind="refusal", reason="blocked")
    feed(mgr, clock, 10, kind="answer")
    assert [r[0] for r in rows(database)] == ["guardrail_spike"]


def test_old_requests_fall_out_of_the_window(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 20, kind="unavailable")  # fires
    assert len(rows(database)) == 1
    clock.advance(hours=3)
    feed(mgr, clock, 25, kind="answer")  # the window now holds only fine requests
    assert len(rows(database)) == 1


def test_the_budget_variance_rule_uses_the_days_tokens_against_the_plan(database):
    clock = Clock()
    used = {"tokens": 100_000}
    config = AlertConfig(daily_token_plan=100_000, budget_variance=1.5)
    mgr = manager(database, clock, Recorder(), config, tokens_today=lambda: used["tokens"])
    mgr.observe(obs(clock.now))
    assert rows(database) == []
    used["tokens"] = 160_000
    mgr.observe(obs(clock.now))
    assert [r[:2] for r in rows(database)] == [("budget_variance", "warning")]


# ======================================================================= what is in a row ====
def test_a_row_holds_only_a_name_a_severity_a_count_and_a_time(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    mgr.on_event(
        "graph_failed",
        {"reason": "KeyError", "user": "asha_rao", "question": "what is my claim status"},
    )
    db = connect(database)
    try:
        columns = [r[1] for r in db.query_all("PRAGMA table_info(alert_events)")]
        stored = db.query_all("SELECT * FROM alert_events")
    finally:
        db.close()
    assert columns == ["id", "name", "severity", "event_count", "notified", "created_at"]
    assert "asha_rao" not in str(stored) and "claim status" not in str(stored)


def test_recent_alerts_are_listed_newest_first_with_no_ids(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    mgr.on_event("jev_unavailable", {})
    clock.advance(minutes=30)
    mgr.on_event("graph_failed", {})
    recent = mgr.recent()
    assert [a["name"] for a in recent] == ["graph_failed", "jev_unavailable"]
    assert set(recent[0]) == {"name", "severity", "count", "notified", "at"}


def test_alerts_older_than_the_banner_window_are_not_listed(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    mgr.on_event("graph_failed", {})
    clock.advance(hours=25)
    assert mgr.recent() == []


# ========================================================================= a failing channel ====
def test_a_failing_webhook_is_an_event_with_a_class_name_and_nothing_breaks(database):
    clock, events = Clock(), []
    mgr = manager(database, clock, Recorder(fail=True), events=events)
    mgr.on_event("all_keys_exhausted", {})
    assert events == [("alert_webhook_failed", {"reason": "ConnectError"})]
    assert rows(database) == [("all_keys_exhausted", "critical", 1, 0)]  # on the banner, not sent


def test_a_failing_channel_reports_itself_at_most_once_a_minute(database):
    clock, events = Clock(), []
    mgr = manager(database, clock, Recorder(fail=True), events=events)
    for name in ("all_keys_exhausted", "graph_failed", "cache_read_only"):
        mgr.on_event(name, {})
    assert len(events) == 1
    clock.advance(minutes=2)
    mgr.on_event("vector_store_rejected", {})
    assert len(events) == 2


def test_a_broken_database_never_raises_into_the_caller_and_is_reported_by_class_name():
    clock, events = Clock(), []

    def broken():
        raise RuntimeError("database is down")

    mgr = AlertManager(
        store=AlertStore(broken),
        sender=None,
        now=clock,
        clock=lambda: clock.now.timestamp(),
        on_event=lambda n, f: events.append((n, f)),
        background=False,
    )
    mgr.on_event("graph_failed", {})
    assert events == [("alert_webhook_failed", {"reason": "RuntimeError"})]


def test_the_queue_is_bounded_and_drops_instead_of_waiting(database):
    clock = Clock()
    mgr = AlertManager(store=AlertStore(lambda: connect(database)), now=clock, background=True)
    for _ in range(alerts.QUEUE_SIZE + 25):  # no thread started: nothing drains the queue
        mgr.on_event("graph_failed", {})
    assert mgr.dropped == 25


# ================================================================================== the config ====
def test_defaults_are_the_placeholders_and_env_overrides_by_name():
    config = alerts.config_from_env({"ALERT_ERROR_RATE": "0.5", "ALERT_MIN_REQUESTS": "30"})
    assert config.error_rate == 0.5 and config.min_requests == 30
    assert alerts.config_from_env({}) == AlertConfig()
    assert AlertConfig().min_requests == 20 and AlertConfig().per_name_seconds == 3600.0


@pytest.mark.parametrize("value", ["soon", "0", "-3"])
def test_a_bad_threshold_names_the_variable(value):
    with pytest.raises(ValueError, match="ALERT_ERROR_RATE"):
        alerts.config_from_env({"ALERT_ERROR_RATE": value})


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("ALERT_ERROR_RATE", "nan"),
        ("ALERT_ERROR_RATE", "inf"),
        ("ALERT_ERROR_RATE", "25"),  # a percentage typed where a share (0 to 1) belongs
        ("ALERT_FALLBACK_RATE", "1.5"),
        ("ALERT_GUARDRAIL_RATE", "0"),
        ("ALERT_MIN_REQUESTS", "5"),  # below the floor of 20: it would fire on noise
        ("ALERT_MIN_REQUESTS", "20.5"),
        ("ALERT_WINDOW_MINUTES", "-1"),
        ("ALERT_DAILY_TOKEN_PLAN", "-100"),
        ("ALERT_BUDGET_VARIANCE", "-inf"),
        ("ALERT_WEBHOOK_DAILY_CAP", "0"),
    ],
)
def test_an_unusable_threshold_is_refused_naming_only_the_variable(variable, value):
    with pytest.raises(ValueError) as caught:
        alerts.config_from_env({variable: value})
    assert variable in str(caught.value)


def test_a_refused_threshold_never_echoes_its_value():
    with pytest.raises(ValueError) as caught:
        alerts.config_from_env({"ALERT_ERROR_RATE": "7654.321"})
    assert "7654" not in str(caught.value) and "ALERT_ERROR_RATE" in str(caught.value)


def test_the_minimum_requests_floor_is_twenty_and_a_rate_of_one_is_allowed():
    config = alerts.config_from_env({"ALERT_MIN_REQUESTS": "20", "ALERT_ERROR_RATE": "1"})
    assert config.min_requests == 20 and config.error_rate == 1.0


# ============================================================================ counting once ====
def test_a_rule_that_stays_above_its_threshold_adds_only_the_rise_not_the_window(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 14, kind="answer")
    feed(mgr, clock, 6, kind="unavailable")
    assert rows(database) == [("error_rate", "critical", 6, 1)]
    feed(mgr, clock, 4, kind="unavailable")  # the window now holds 10 failures
    assert rows(database) == [("error_rate", "critical", 10, 1)]
    feed(mgr, clock, 3, kind="answer")  # still above 25 %, no new failure: nothing is added
    assert rows(database) == [("error_rate", "critical", 10, 1)]


def test_an_early_start_up_failure_counts_in_the_error_rate_window(database):
    clock = Clock()
    mgr = manager(database, clock, Recorder())
    feed(mgr, clock, 14, kind="answer")
    feed(mgr, clock, 6, kind="starting_up")
    assert [r[0] for r in rows(database)] == ["error_rate"]


# ================================================================================ the webhook ====
def test_the_text_format_is_a_text_body_and_discord_is_a_content_body():
    assert webhook.WebhookSender(GOOD_URL).body("hi") == {"text": "hi"}
    assert webhook.WebhookSender(GOOD_URL, "discord").body("hi") == {"content": "hi"}


def test_a_send_posts_the_body_with_a_short_timeout(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(204)

    monkeypatch.setattr(webhook, "TRANSPORT", httpx.MockTransport(handler))
    webhook.WebhookSender(GOOD_URL, "discord").send("BFSI assistant [critical] x: 1")
    assert seen == [(GOOD_URL, {"content": "BFSI assistant [critical] x: 1"})]
    assert webhook.SEND_TIMEOUT_SECONDS <= 3.0


def test_an_error_status_is_a_failure(monkeypatch):
    monkeypatch.setattr(
        webhook, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(500))
    )
    with pytest.raises(httpx.HTTPStatusError):
        webhook.WebhookSender(GOOD_URL).send("x")


def test_no_address_means_no_sender():
    assert webhook.sender_from_env({}) is None


@pytest.mark.parametrize(
    ("value", "rule"),
    [
        ("http://hooks.acme-corp.org/incoming/abcdef", "https"),
        ("not a web address", "https"),
        (CREDENTIAL_URL, "user name"),
        ("https://hooks.example.test/your-webhook", "placeholder"),
    ],
)
def test_an_unusable_address_is_refused_naming_the_variable_and_never_the_value(value, rule):
    with pytest.raises(webhook.WebhookConfigError) as caught:
        webhook.sender_from_env({"ALERT_WEBHOOK_URL": value})
    assert "ALERT_WEBHOOK_URL" in str(caught.value) and rule in str(caught.value)
    assert value not in str(caught.value) and "pw1234567" not in str(caught.value)


def test_a_format_without_an_address_and_an_unknown_format_are_refused():
    with pytest.raises(webhook.WebhookConfigError, match="ALERT_WEBHOOK_FORMAT"):
        webhook.sender_from_env({"ALERT_WEBHOOK_FORMAT": "discord"})
    with pytest.raises(webhook.WebhookConfigError, match="ALERT_WEBHOOK_FORMAT"):
        webhook.sender_from_env({"ALERT_WEBHOOK_URL": GOOD_URL, "ALERT_WEBHOOK_FORMAT": "xml"})


def test_the_address_is_in_no_repr_no_log_and_no_event(database, caplog, monkeypatch):
    sender = webhook.sender_from_env({"ALERT_WEBHOOK_URL": GOOD_URL})
    assert GOOD_URL not in repr(sender) and "k3J9x2Lm8Qw4Zp7R" not in repr(sender)
    monkeypatch.setattr(
        webhook, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(500))
    )
    clock, events = Clock(), []
    caplog.set_level(logging.DEBUG)
    mgr = manager(database, clock, sender, events=events)
    mgr.on_event("graph_failed", {})
    shown = " ".join(str(e) for e in events) + caplog.text
    assert "k3J9x2Lm8Qw4Zp7R" not in shown and "acme-corp" not in shown


def test_the_message_is_a_name_a_severity_and_numbers_only():
    text = webhook.message_for("error_rate", "critical", 6, 15)
    assert text == "BFSI assistant [critical] error_rate: 6 in 15 min"
    assert webhook.message_for("Bad Name with spaces", "warning", 1, None).endswith("alert: 1")


def test_the_scanner_knows_the_webhook_shapes_too():
    from app.observability.scan import scan_text

    assert [f.rule for f in scan_text(SLACK_SHAPE)] == ["webhook-url"]
