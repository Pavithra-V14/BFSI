"""Structured JSON logs with a correlation id (spec 14).

One JSON object per line: time, level, logger, `correlation_id`, a message and, when the call site
gave them, a few `fields`. The correlation id is recorded here so an operator can follow one
request; it is never shown to a user (the chat response does not carry it).

What a log line can NEVER hold:
- a request body, a header or a query string. Nothing logs them; the one logger that could
  (uvicorn's access log, which prints the request target) is rewritten to method, path (the part
  before `?`) and status, without the client address;
- a free-form field. A call site adds fields with `extra={"fx": {...}}`; only the names in
  `ALLOWED_FIELDS` pass, and a value passes only if it is a number, a boolean or a short
  identifier-like word (never a sentence);
- a traceback or an exception message. Only the exception CLASS name is written (`exc_type`);
- anything that looks like an e-mail address, a long number, a bearer token, a key or an address
  with credentials or a query string: the message text is scrubbed by the same patterns the scan
  command (`app.observability.scan`) uses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any, TextIO

from app.observability.scan import scrub

CORRELATION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bfsi_correlation_id", default=None
)

# The only field names a log line may carry beyond its fixed keys.
ALLOWED_FIELDS = frozenset(
    {
        "event", "name", "count", "stage", "code", "reason", "status", "kind", "severity",
        "store", "scope", "tier", "provider", "model", "decider", "key_fingerprint", "tokens",
        "duration_ms", "method", "path", "status_code", "attempts", "queued", "window",
    }
)  # fmt: skip
MAX_MESSAGE = 300
_WORD = re.compile(r"^[A-Za-z0-9_.:/\-]{0,64}$")


def bind_correlation(value: str | None) -> contextvars.Token:
    """Make `value` the correlation id of everything logged in this context (a request)."""
    return CORRELATION.set(value)


def reset_correlation(token: contextvars.Token) -> None:
    CORRELATION.reset(token)


def current_correlation() -> str | None:
    return CORRELATION.get()


def clean_fields(raw: Any) -> dict[str, Any]:
    """The allow-listed fields of a call site, with values that are not plain words dropped."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in ALLOWED_FIELDS:
            continue
        if isinstance(value, bool | int | float):
            out[name] = value
        elif isinstance(value, str) and _WORD.match(value):
            out[name] = value
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if record.name == "uvicorn.access":
            return self._access(record)
        body: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": current_correlation(),
            "message": scrub(record.getMessage())[:MAX_MESSAGE],
        }
        fields = clean_fields(getattr(record, "fx", None))
        if fields:
            body["fields"] = fields
        if record.exc_info and record.exc_info[0] is not None:
            body["exc_type"] = record.exc_info[0].__name__  # the class name, never the message
        return json.dumps(body, sort_keys=True, ensure_ascii=True)

    def _access(self, record: logging.LogRecord) -> str:
        """Uvicorn's request line, without the query string and without the client address."""
        args = record.args if isinstance(record.args, tuple) else ()
        method = str(args[1]) if len(args) > 1 else ""
        target = str(args[2]) if len(args) > 2 else ""
        status = args[4] if len(args) > 4 else None
        path = target.split("?", 1)[0].split("#", 1)[0]
        body = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": "access",
            "correlation_id": current_correlation(),
            "message": "request",
            "fields": clean_fields({"method": method, "path": path[:120], "status_code": status}),
        }
        return json.dumps(body, sort_keys=True, ensure_ascii=True)


class CorrelationFilter(logging.Filter):
    """Puts the correlation id on every record (also those of other libraries)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation()
        return True


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> logging.Handler:
    """Send every log line, ours and the libraries', through `JsonFormatter` on one handler.
    Safe to call again: the previous handler of this module is replaced, not doubled."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_bfsi_json", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    handler._bfsi_json = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper() if isinstance(level, str) else "INFO")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        library = logging.getLogger(name)
        library.handlers = []  # their own plain-text handlers would print the request line
        library.propagate = True
    return handler


class CorrelationMiddleware:
    """Gives every HTTP request its own correlation id for the log lines it causes. The id is not
    sent back to the client (it is internal) and is not read from a header (a client could choose
    it)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = bind_correlation(uuid.uuid4().hex)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_correlation(token)
