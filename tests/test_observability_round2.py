"""Phase 12 hardening round: the rules the audit found missing or weak.

The webhook address stays out of every log line (also from a real HTTP call), a placeholder is a
whole value and not a word inside a real token, a warning is shown without alarm, an old in-process
event leaves the banner, the day's token total survives a restart, the early error results count
as errors, the daily bucket is a UTC day, every logged field goes through the allow-list, and the
decider statistics can be read while the gateway writes.
"""

import io
import json
import logging
import re
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.db.connection import connect
from app.db.migrate import run
from app.gateway.stats import DeciderStats
from app.observability import logs, metrics, scan, webhook
from app.observability.metrics import MetricsHub, RequestObservation
from app.pipeline.startup import MaskerHolder
from tests.api_chat_fixtures import ChatEnv
from tests.api_fixtures import MIGRATIONS
from tests.frontend_fixtures import html_bodies, json_response, page_text, signed_in
from tests.test_frontend import admin_api, open_section

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 11, 10, 30, tzinfo=UTC)
# Built from pieces: this file holds no text of the shape a secret scanner refuses.
TOKEN = "k3J9x2Lm8" + "Qw4Zp7R5vT"
HOST = "https://hooks.acme-corp.org/"
REAL_LOOKING = HOST + "incoming/" + TOKEN


def env_for(url):
    return {webhook.ENV_URL: url}


@pytest.fixture
def stream():
    """Route every log line through the JSON handler into a buffer, then put logging back."""
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    buffer = io.StringIO()
    logs.configure_logging("DEBUG", buffer)
    yield buffer
    for handler in list(root.handlers):
        if handler not in before_handlers:
            root.removeHandler(handler)
    root.setLevel(before_level)
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    scan.forget_secrets()


# ================================================================== the webhook address (H2) ====
def test_the_http_client_loggers_are_quiet_after_logging_is_configured(stream):
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


class _Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # the test server prints nothing
        pass


def test_a_real_http_call_to_the_webhook_leaves_its_address_out_of_every_log_line(stream):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/hooks/{TOKEN}"
        webhook.WebhookSender(url).send("BFSI assistant [critical] graph_failed: 1")  # no mock
    finally:
        server.shutdown()
        server.server_close()
    shown = stream.getvalue()
    assert TOKEN not in shown and "127.0.0.1" not in shown


def test_the_configured_address_is_scrubbed_exactly_whatever_its_host(stream):
    assert webhook.sender_from_env(env_for(REAL_LOOKING)) is not None
    logging.getLogger("app.test").warning("posting to %s failed", REAL_LOOKING)
    logging.getLogger("app.test").warning("path only: /incoming/%s", TOKEN)
    shown = stream.getvalue()
    assert TOKEN not in shown and "acme-corp" not in shown
    assert scan.scan_text("x " + REAL_LOOKING) and scan.scrub("x " + REAL_LOOKING) == "x [secret]"


def test_any_https_host_is_accepted():
    for host in ("hooks.acme-corp.org", "chat.internal.bank.in", "x.y.z.example-corp.com"):
        assert webhook.sender_from_env(env_for(f"https://{host}/a/{TOKEN}"))


@pytest.mark.parametrize(
    "path",
    [
        "incoming/" + "k3todoJ9x2Lm8Qw4Zp7R",  # a real token that merely contains "todo"
        "incoming/" + "fakeKey" + "9" * 3 + "Zq8Wm2",
        "services/" + "tbd" + "Qw4Zp7R5vT2",
    ],
)
def test_a_real_token_that_only_contains_a_placeholder_word_is_accepted(path):
    assert webhook.sender_from_env(env_for(HOST + path))


@pytest.mark.parametrize(
    "url",
    [
        HOST + "incoming/your-webhook-url",
        HOST + "incoming/todo",
        HOST + "incoming/xxxxxxxx",
        HOST + "incoming/<token>",
        "https://your-host.acme-corp.org/incoming/Qw4Zp7R5vT",
        "https://hooks.example.com/incoming/Qw4Zp7R5vT",
        HOST + "incoming/${TOKEN}",
    ],
)
def test_a_whole_value_placeholder_is_still_refused(url):
    with pytest.raises(webhook.WebhookConfigError, match="placeholder"):
        webhook.sender_from_env(env_for(url))


# ================================================================ every field logged (M5) ====
def test_a_logged_field_is_allow_listed_and_a_string_is_scrubbed(stream):
    fields = {"reason": "user@acme-corp.org", "query": "word", "count": 3, "error": "Boom"}
    shown = logs.fx(fields)
    assert shown == {"fx": {"count": 3, "error": "Boom"}}
    scan.register_secret(TOKEN)
    assert TOKEN not in json.dumps(logs.fx({"reason": TOKEN}))


def test_no_module_prints_a_whole_dict_of_event_fields_into_a_log_line():
    pattern = re.compile(r"""log(?:ger)?\.\w+\(\s*["'][^"']*%s[^"']*%s[^"']*["'],[^)]*\bfields\b""")
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text("utf-8")
        for match in pattern.finditer(text):
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: {match.group(0)[:60]}")
    assert offenders == []


# ======================================================== tokens of the day, early (M4, L4) ====
def test_the_day_total_starts_from_what_was_saved_before_the_restart():
    hub = MetricsHub(now=lambda: NOW)
    hub.seed_tokens(NOW, 1_000)
    assert hub.tokens_today() == 1_000
    hub.observe(
        RequestObservation(at=NOW.timestamp(), role="customer", kind="answer", tokens_in=50)
    )
    assert hub.tokens_today() == 1_050


def test_a_seed_belongs_to_its_own_day_only():
    clock = {"now": NOW}
    hub = MetricsHub(now=lambda: clock["now"])
    hub.seed_tokens(NOW, 1_000)
    clock["now"] = NOW + timedelta(days=1)
    assert hub.tokens_today() == 0


@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, observability=True)
    yield made
    made.close()


def test_start_up_reads_today_s_saved_hourly_rows_into_the_hub(env):
    observability = env.observability
    now = observability._now()
    db = observability._db_factory()
    try:
        metrics.add_hourly(
            db,
            {
                (metrics.hour_start(now), "all", "tokens_in"): 700.0,
                (metrics.hour_start(now), "all", "tokens_out"): 300.0,
                (metrics.hour_start(now) - timedelta(days=1), "all", "tokens_in"): 9_999.0,
            },
        )
    finally:
        db.close()
    observability.hub = MetricsHub(now=observability._now)  # a restarted process
    assert observability.hub.tokens_today() == 0
    observability.seed_tokens()
    assert observability.hub.tokens_today() == 1_000  # yesterday's rows are not today's


def test_starting_the_service_seeds_the_hub(env):
    observability = env.observability
    now = observability._now()
    db = observability._db_factory()
    try:
        metrics.add_hourly(db, {(metrics.hour_start(now), "all", "tokens_in"): 400.0})
    finally:
        db.close()
    observability.hub = MetricsHub(now=observability._now)
    observability._threads = []
    observability.start()
    try:
        assert observability.hub.tokens_today() == 400
    finally:
        observability.stop()


def test_a_chat_refused_while_the_service_starts_counts_as_an_error(env):
    _, token = env.user_token("asha_rao")
    env.rig.deps.masker = MaskerHolder(lambda: None)  # never started
    assert env.chat(token).status_code == 503
    (seen,) = env.observability.hub.window(86_400)
    assert seen.kind == "unavailable" and seen.reason == "starting_up"
    assert env.observability.hub.outcomes(86_400)["unavailable"] == 1


def test_a_chat_that_ran_the_graph_is_not_counted_twice(env):
    _, token = env.user_token("asha_rao")
    assert env.chat(token).status_code == 200
    assert len(env.observability.hub.window(86_400)) == 1


# ===================================================================== alerts and the banner ====
@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def alerts_body(env, admin):
    return env.client.get("/admin/alerts", headers=env.bearer(admin)).json()


def test_an_in_process_event_leaves_the_banner_after_a_day(env, admin):
    services = env.app.state.services
    base = {"now": 1_000_000.0}
    services.clock = lambda: base["now"]
    services.emit("persist_queued", queued=3)
    assert alerts_body(env, admin)["events"] == [
        {"name": "persist_queued", "count": 1, "codes": {}}
    ]
    base["now"] += 24 * 3600 - 60
    assert alerts_body(env, admin)["events"] == [
        {"name": "persist_queued", "count": 1, "codes": {}}
    ]
    base["now"] += 120
    body = alerts_body(env, admin)
    assert body["events"] == [] and body["window"] == 0


def test_a_warning_is_reported_apart_from_the_critical_names(env, admin):
    services = env.app.state.services
    services.emit("jev_unavailable")
    body = alerts_body(env, admin)
    assert body["banner"] is False and body["warnings"] == ["jev_unavailable"]
    services.emit("graph_failed", reason="KeyError")
    body = alerts_body(env, admin)
    assert body["banner"] is True and body["critical"] == ["graph_failed"]
    assert body["warnings"] == ["jev_unavailable"]


def alerts_api(**fields):
    api = admin_api()
    body = {"banner": False, "critical": [], "warnings": [], "events": [], "window": 0}
    body.update(fields)
    api.routes[("GET", "/admin/alerts")] = json_response(200, body)
    return api


def test_a_warning_is_a_calm_line_not_a_red_banner(monkeypatch):
    api = alerts_api(warnings=["fallback_rate"])
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    shown = page_text(at)
    assert "fallback_rate" in shown and 'role="alert"' not in shown
    notices = [b for b in html_bodies(at) if "fx-notice " in b]
    assert notices and all("fx-notice-info" in n and "fx-notice-danger" not in n for n in notices)


def test_the_alerts_section_lists_the_last_day_with_severity_and_no_stale_caption(monkeypatch):
    recent = [
        {"name": "fallback_rate", "severity": "warning", "count": 7, "notified": False, "at": "t1"},
        {"name": "graph_failed", "severity": "critical", "count": 2, "notified": True, "at": "t2"},
    ]
    api = alerts_api(banner=True, critical=["graph_failed"], recent=recent)
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Alerts")
    shown = page_text(at)
    assert "fallback_rate" in shown and "graph_failed" in shown
    assert "warning" in shown and "critical" in shown
    captions = " ".join(c.value for c in at.caption)
    assert "survive a restart" in captions and "kept in memory" in captions
    assert "Phase" not in captions


def test_each_alert_and_event_has_a_view_reason_box_with_fixed_text_and_the_codes_seen(monkeypatch):
    recent = [
        {"name": "graph_failed", "severity": "critical", "count": 2, "notified": True, "at": "t2"}
    ]
    reasons = {
        "graph_failed": {"meaning": "A bug stopped a request.", "action": "Send the class name."},
        "persist_queued": {"meaning": "A turn is waiting.", "action": "No action."},
    }
    events = [{"name": "persist_queued", "count": 3, "codes": {"TimeoutError": 3}}]
    api = alerts_api(
        banner=True, critical=["graph_failed"], recent=recent, events=events, reasons=reasons
    )
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Alerts")
    labels = [e.label for e in at.expander]
    assert "View reason graph failed" in labels and "View reason persist queued" in labels
    text = " ".join(c.value for c in at.caption)
    assert "What it means A bug stopped a request." in text
    assert "What to do No action." in text
    assert "Reason codes seen" in text and "TimeoutError" in text
    assert "A bug stopped a request." in page_text(at)  # also in the table of the last day


def test_a_hostile_reason_text_from_the_service_is_shown_as_plain_text(monkeypatch):
    hostile = "see https://evil.example $x$ :warning: a@evil.example [t](u)"
    recent = [
        {"name": "graph_failed", "severity": "critical", "count": 1, "notified": 0, "at": "t"}
    ]
    api = alerts_api(
        recent=recent, reasons={"graph_failed": {"meaning": hostile, "action": hostile}}
    )
    at = open_section(signed_in(api, monkeypatch, role="admin", username="boss").run(), "Alerts")
    text = " ".join(c.value for c in at.caption)
    for bad in ("https://", "$x$", ":warning:", "a@evil", "](u)", "[t]"):
        assert bad not in text, bad


# ============================================================================ a UTC day (L5) ====
def test_the_daily_series_buckets_hourly_rows_by_their_utc_day(tmp_path):
    url = f"sqlite:///{tmp_path / 'day.db'}"
    run(url, MIGRATIONS, log=lambda line: None)
    db = connect(url)
    try:
        late = datetime(2026, 10, 10, 23, 0, tzinfo=UTC)
        early = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)
        metrics.add_hourly(db, {(late, "all", "requests"): 4.0, (early, "all", "requests"): 6.0})
        series = {d["day"]: d for d in metrics.daily_series(db, 3, now=NOW)}
        assert series["2026-10-10"]["roles"]["all"]["requests"] == 4.0
        assert series["2026-10-11"]["roles"]["all"]["requests"] == 6.0
    finally:
        db.close()


def test_the_postgres_day_bucket_names_utc_explicitly():
    class Fake:
        kind = "postgres"

    assert "AT TIME ZONE 'UTC'" in metrics._utc_day_expression(Fake())


# ================================================================== reading the stats (L3) ====
def test_the_decider_stats_can_be_read_while_the_gateway_writes():
    class Ticker:
        def __init__(self):
            self.value = 0.0

        def now(self):
            self.value += 0.001
            return self.value

    stats = DeciderStats(Ticker())
    failures = []
    done = threading.Event()

    def write():
        for index in range(30_000):
            stats.record(f"step{index % 7}", "primary")
        done.set()

    writer = threading.Thread(target=write)
    writer.start()
    try:
        while not done.is_set():
            stats.steps()
            stats.shares("step1", 3_600.0)
    except RuntimeError as error:  # "deque mutated during iteration"
        failures.append(type(error).__name__)
    writer.join()
    assert failures == []
