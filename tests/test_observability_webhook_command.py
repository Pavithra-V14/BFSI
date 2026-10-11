"""The owner's `python -m app.observability.webhook --test` command (Run 18, step 3)."""

import json
import subprocess
import sys

import httpx

from app.observability import webhook

# Built from pieces so this file holds no real-looking webhook address.
GOOD_URL = "https://hooks.acme-corp.org/" + "incoming/" + "k3J9x2Lm8Qw4Zp7R"
PLAIN_HTTP = "http://" + "hooks.acme-corp.org/" + "x/abcdef"


def test_the_command_sends_one_message_and_prints_only_sent(monkeypatch, capsys):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    monkeypatch.setattr(webhook, "TRANSPORT", httpx.MockTransport(handler))
    code = webhook.main(["--test"], {"ALERT_WEBHOOK_URL": GOOD_URL})
    out = capsys.readouterr()
    assert code == 0 and out.out.strip() == "sent"
    assert seen == [{"text": webhook.TEST_MESSAGE}]
    assert "k3J9x2Lm8Qw4Zp7R" not in out.out + out.err


def test_the_discord_format_sends_content(monkeypatch):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(204)

    monkeypatch.setattr(webhook, "TRANSPORT", httpx.MockTransport(handler))
    env = {"ALERT_WEBHOOK_URL": GOOD_URL, "ALERT_WEBHOOK_FORMAT": "discord"}
    assert webhook.main(["--test"], env) == 0
    assert seen == [{"content": webhook.TEST_MESSAGE}]


def test_the_command_does_nothing_without_the_flag(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        webhook,
        "TRANSPORT",
        httpx.MockTransport(lambda request: calls.append(1) or httpx.Response(204)),
    )
    assert webhook.main([], {"ALERT_WEBHOOK_URL": GOOD_URL}) == 2
    assert "nothing sent" in capsys.readouterr().err and calls == []


def test_a_failure_prints_a_class_name_and_never_the_address(monkeypatch, capsys):
    monkeypatch.setattr(
        webhook, "TRANSPORT", httpx.MockTransport(lambda request: httpx.Response(404))
    )
    code = webhook.main(["--test"], {"ALERT_WEBHOOK_URL": GOOD_URL})
    out = capsys.readouterr()
    assert code == 1 and out.err.strip() == "HTTPStatusError"
    assert "acme-corp" not in out.out + out.err


def test_a_missing_or_wrong_setting_is_named(capsys):
    assert webhook.main(["--test"], {}) == 2
    assert "ALERT_WEBHOOK_URL is not set" in capsys.readouterr().err
    assert webhook.main(["--test"], {"ALERT_WEBHOOK_URL": PLAIN_HTTP}) == 2
    err = capsys.readouterr().err
    assert "ALERT_WEBHOOK_URL" in err and "https" in err and "acme-corp" not in err


def test_the_module_runs_as_a_command_and_refuses_without_the_flag():
    done = subprocess.run(
        [sys.executable, "-m", "app.observability.webhook"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2 and "add --test" in done.stderr
