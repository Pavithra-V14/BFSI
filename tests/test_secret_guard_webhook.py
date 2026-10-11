"""The secret guard (a Claude Code hook) knows the alert webhook address (Phase 12): the name
ALERT_WEBHOOK_URL with a real-looking address, and the shape of Slack, Discord and Teams incoming
webhook addresses, are refused in any text Claude writes. A placeholder is not."""

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "secret_guard.py"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("secret_guard_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Built from pieces so that this file itself holds no address of the shape the guard refuses.
SLACK = (
    "https://hooks.slack.com/"
    + "services/"
    + "T0A1B2C3D/B4E5F6G7H/"
    + "q9w8e7r6t5y4u3i2o1p0aAbBcCdD"
)
DISCORD = (
    "https://discord.com/"
    + "api/webhooks/"
    + "112233445566778899/"
    + "kJhGfDsAqWeRtYuIoP0987zxcvbnm"
)
TEAMS = (
    "https://contoso."
    + "webhook.office.com/"
    + "webhookb2/"
    + "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
)
OTHER = "https://chat.acme-corp.org/" + "incoming/" + "k3J9x2Lm8Qw4Zp7R"


@pytest.mark.parametrize("address", [SLACK, DISCORD, TEAMS])
def test_a_chat_webhook_address_is_refused_wherever_it_appears(guard, address):
    hit = guard.scan_text(f"post the alert to {address} please")
    assert hit is not None and hit[0] == "chat-webhook-url"


@pytest.mark.parametrize("separator", ["=", ": ", " = "])
def test_the_variable_with_a_real_looking_address_is_refused(guard, separator):
    hit = guard.scan_text("ALERT_WEBHOOK_URL" + separator + OTHER)
    assert hit is not None and hit[0] == "alert-webhook-url"


def test_the_variable_in_a_quoted_assignment_is_refused(guard):
    assert guard.scan_text('os.environ["ALERT_WEBHOOK_URL"] = "' + OTHER + '"') is not None


@pytest.mark.parametrize(
    "text",
    [
        "ALERT_WEBHOOK_URL=",
        "ALERT_WEBHOOK_URL=https://example.com/hook",
        "ALERT_WEBHOOK_URL=https://hooks.example.test/your-webhook-address",
        "set ALERT_WEBHOOK_URL in your shell",
        "https://hooks.slack.com/services/XXXX/XXXX/XXXX",
        "the webhook is a chat address (https only) and is never logged",
    ],
)
def test_the_name_alone_and_placeholders_are_allowed(guard, text):
    assert guard.scan_text(text) is None


def test_the_guard_still_refuses_what_it_refused_before(guard):
    token = "sk-or-v1-" + "a1b2c3d4e5f6a7b8c9d0e1f2"
    assert guard.scan_text(f"key {token}")[0] == "openrouter-key"
