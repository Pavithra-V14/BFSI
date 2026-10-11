"""Small helpers every view shares."""

from __future__ import annotations

from frontend import messages
from frontend.api import Reply


def friendly(reply: Reply) -> str:
    """Plain words for a refused or failed reply (never a stack trace, address or status line)."""
    if reply.kind == "timeout":
        return messages.TIMEOUT_TEXT
    if reply.kind == "waking":
        return messages.WAKING_GAVE_UP
    text = messages.ERROR_TEXT.get(reply.code or "")
    if text is None:
        text = reply.message or messages.GENERIC_ERROR
    if reply.retry_after and reply.code in ("too_many_requests", "too_many_attempts"):
        text = f"{text} Try again in about {reply.retry_after} seconds."
    return text


def number(value: object) -> int:
    """A count from the service as a whole number (0 when it is anything else)."""
    try:
        return int(value or 0)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
