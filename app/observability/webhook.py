"""The chat webhook (spec 14: "alerts: a chat webhook plus a dashboard banner (no email)").

`ALERT_WEBHOOK_URL` is a SECRET: whoever has it can post into the team's chat. So:
- it must be an https address (an http one, a placeholder or an address that is not a web address
  is refused at start-up, naming the variable and never the value);
- it is never logged, printed, put in an event, an error text or a `repr`;
- it is read from the shell only (like every other secret) and its name is known to the secret
  guard.

`ALERT_WEBHOOK_FORMAT` chooses the body: `text` (default) is `{"text": "..."}` (Slack and Teams
incoming webhooks), `discord` is `{"content": "..."}`.

What is sent: the alert's name, severity, a count and the window, in one short line. Never text a
user wrote, a name, an id or an address. A send has a 3 second limit and is never retried (the next
alert is the retry); a failure is an event carrying the exception CLASS name only.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

ENV_URL = "ALERT_WEBHOOK_URL"
ENV_FORMAT = "ALERT_WEBHOOK_FORMAT"
FORMATS = ("text", "discord")
SEND_TIMEOUT_SECONDS = 3.0
_PLACEHOLDER = re.compile(
    r"your|example|placeholder|changeme|change-me|dummy|fake|sample|redacted|todo|tbd|xxxx|<|>|\$\{",
    re.IGNORECASE,
)
_NAME = re.compile(r"^[a-z0-9_]{1,60}$")

# Tests put an `httpx.MockTransport` here; the running service leaves it None (real network).
TRANSPORT: httpx.BaseTransport | None = None


class WebhookConfigError(ValueError):
    """The webhook cannot be used. The message names the variable and the rule, never a value."""


class WebhookSender:
    def __init__(self, url: str, fmt: str = "text") -> None:
        if fmt not in FORMATS:
            raise WebhookConfigError(f"{ENV_FORMAT} must be one of: {', '.join(FORMATS)}")
        self._url = url
        self._format = fmt

    def __repr__(self) -> str:  # the address is a secret
        return f"WebhookSender(format={self._format!r})"

    @property
    def format(self) -> str:
        return self._format

    def body(self, text: str) -> dict[str, str]:
        return {"content": text} if self._format == "discord" else {"text": text}

    def send(self, text: str) -> None:
        """Post one message. Raises on any failure (the caller turns it into an alert event)."""
        with httpx.Client(timeout=SEND_TIMEOUT_SECONDS, transport=TRANSPORT) as client:
            response = client.post(self._url, json=self.body(text))
        response.raise_for_status()


def message_for(name: str, severity: str, count: int, window_minutes: int | None) -> str:
    """One short line: names and numbers only."""
    safe = name if _NAME.match(name) else "alert"
    window = f" in {window_minutes} min" if window_minutes else ""
    return f"BFSI assistant [{severity}] {safe}: {count}{window}"


def sender_from_env(env: Mapping[str, str]) -> WebhookSender | None:
    """The sender, or None when no webhook is configured. A configured but unusable address is an
    error (`WebhookConfigError`): an alert channel that silently does nothing is worse."""
    url = (env.get(ENV_URL) or "").strip()
    fmt = (env.get(ENV_FORMAT) or "text").strip().lower() or "text"
    if not url:
        if (env.get(ENV_FORMAT) or "").strip():
            raise WebhookConfigError(f"{ENV_FORMAT} is set but {ENV_URL} is not")
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc or not parts.hostname:
        raise WebhookConfigError(f"{ENV_URL} must be an https address")
    if parts.username or parts.password:
        raise WebhookConfigError(f"{ENV_URL} must not carry a user name or password")
    if _PLACEHOLDER.search(url):
        raise WebhookConfigError(f"{ENV_URL} is still a placeholder")
    return WebhookSender(url, fmt)


def failure_event(error: BaseException) -> tuple[str, dict[str, Any]]:
    """The event for a failed send: the exception class name, nothing else."""
    return "alert_webhook_failed", {"reason": type(error).__name__}


Emit = Callable[[str, dict[str, Any]], None]

TEST_MESSAGE = "BFSI assistant [test] webhook check"


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """`python -m app.observability.webhook --test`: post one test message to the configured
    webhook and say whether it was accepted. Prints `sent`, or the exception CLASS name, or the
    name of the setting that is wrong; never the address. Nothing is stored; no banner changes."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m app.observability.webhook",
        allow_abbrev=False,
        description="Send one test message to the chat webhook (ALERT_WEBHOOK_URL).",
    )
    parser.add_argument("--test", action="store_true", help="really send the test message")
    args = parser.parse_args(argv)
    if not args.test:
        print("nothing sent: add --test to send one test message", file=sys.stderr)
        return 2
    if env is None:
        import os

        env = os.environ
    try:
        sender = sender_from_env(env)
    except WebhookConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if sender is None:
        print(f"error: {ENV_URL} is not set", file=sys.stderr)
        return 2
    try:
        sender.send(TEST_MESSAGE)
    except Exception as error:
        print(type(error).__name__, file=sys.stderr)
        return 1
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
