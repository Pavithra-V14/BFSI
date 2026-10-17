"""The fixed reasons of the Alerts screen: every alert and event has one, the text is ours, and the
only thing taken from a running event is a reason CODE that looks like a class name."""

import re
from pathlib import Path

import pytest

from app.observability import reasons
from app.observability.alerts import EVENT_SEVERITY
from tests.api_chat_fixtures import ChatEnv

APP = Path(__file__).resolve().parents[1] / "app"
RULES = ("error_rate", "fallback_rate", "guardrail_spike", "budget_variance", "retention_overdue")
EMITTED = re.compile(r"""(?:emit|_emit|on_event|_event|_alert)\(\s*["']([a-z][a-z_]+)["']""")


def emitted_names():
    found = set()
    for path in APP.rglob("*.py"):
        if path.name == "reasons.py":
            continue
        found |= set(EMITTED.findall(path.read_text("utf-8")))
    return found


# ---- every name has its reason --------------------------------------------------------------
def test_every_alert_and_every_rule_has_a_reason():
    for name in [*EVENT_SEVERITY, *RULES]:
        assert name in reasons.REASONS, name


def test_every_event_the_code_emits_has_a_reason():
    missing = sorted(emitted_names() - set(reasons.REASONS))
    assert missing == [], f"events with no reason text: {missing}"


def test_a_reason_is_two_plain_sentences_without_markup_an_address_or_a_key():
    for name, (meaning, action) in reasons.REASONS.items():
        for text in (meaning, action):
            assert text.strip() and len(text) < 400, name
            assert "http" not in text and "@" not in text and "Traceback" not in text, name
            assert "`" not in text and "<" not in text and "[" not in text, name


def test_a_name_without_a_reason_gets_the_plain_notice_not_an_error():
    assert reasons.explain("brand_new_event") == {
        "meaning": reasons.NO_REASON[0],
        "action": reasons.NO_REASON[1],
    }


# ---- only a code gets through -----------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "database password is hunter2",  # a sentence
        "boom: connection to host=10.0.0.1 failed",  # a message with an address
        "a@b.example",
        "https://x.example/?key=abc",
        "",
        "7",
        7,
        None,
        ["TimeoutError"],
        "x" * 61,
    ],
)
def test_anything_that_is_not_a_code_is_dropped(value):
    assert reasons.clean_code(value) is None


def test_a_class_name_or_a_fixed_code_is_kept():
    assert reasons.clean_code("TimeoutError") == "TimeoutError"
    assert reasons.clean_code("budget_exhausted") == "budget_exhausted"


def test_the_codes_of_an_event_are_counted_and_only_the_most_common_few_are_kept():
    fields = [{"reason": "TimeoutError"}] * 3 + [{"error": "KeyError"}] * 2
    fields += [{"code": f"code_{n}"} for n in range(10)]
    fields += [{"reason": "has spaces and secrets"}, {"queued": 3}, "not a dict"]
    counts = reasons.reason_codes(fields)
    assert list(counts)[:2] == ["TimeoutError", "KeyError"] and counts["TimeoutError"] == 3
    assert len(counts) == reasons.MAX_CODES
    assert "has spaces and secrets" not in counts


# ---- through the API --------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, fast_hashing):
    made = ChatEnv(tmp_path, observability=True)
    yield made
    made.close()


@pytest.fixture
def admin(env):
    return env.user_token("boss", "admin")[1]


def alerts(env, admin):
    return env.client.get("/admin/alerts", headers=env.bearer(admin)).json()


def test_the_alerts_response_explains_every_name_it_shows(env, admin):
    services = env.app.state.services
    services.emit("persist_queued", queued=2, reason="TimeoutError")
    services.emit("all_keys_exhausted", provider="groq")
    body = alerts(env, admin)
    shown = {e["name"] for e in body["events"]} | {a["name"] for a in body["recent"]}
    assert shown and shown <= set(body["reasons"])
    for name in shown:
        assert set(body["reasons"][name]) == {"meaning", "action"}
        assert body["reasons"][name]["meaning"].strip()
    queued = next(e for e in body["events"] if e["name"] == "persist_queued")
    assert queued["codes"] == {"TimeoutError": 1}


def test_a_message_in_an_event_never_reaches_the_screen(env, admin):
    secret = "postgres://user:password@host/db failed for asha@example.com"
    env.app.state.services.emit("graph_failed", reason=secret)
    text = env.client.get("/admin/alerts", headers=env.bearer(admin)).text
    assert "password" not in text and "asha@example.com" not in text
