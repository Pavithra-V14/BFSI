"""The security layer (spec 13.3): headers, a request size limit and CORS.

- Headers go on every response, errors and CORS answers included [P].
- The request size limit [T] is 16 KiB: a query is at most 100 tokens and the biggest body is a
  registration. It stops a declared oversize body at once and counts bytes as they stream in, so
  a body sent in chunks (with no Content-Length) cannot get past it.
- CORS: nothing is allowed unless one origin is configured (CORS_ALLOWED_ORIGIN). Then exactly
  that origin may call the API, with no cookies (the token travels in a header).
"""

from __future__ import annotations

import json
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 16 * 1024  # [T]

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (  # [P]
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Permissions-Policy", "geolocation=(), camera=(), microphone=()"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
    ("Cross-Origin-Resource-Policy", "same-site"),
)

CORS_METHODS = ["GET", "POST", "PUT", "DELETE"]
CORS_HEADERS = ["Authorization", "Content-Type"]


class SecurityMiddleware:
    """Adds the security headers and enforces the request size limit.

    It must be the outermost layer (see SecureFastAPI), so that even a 500 sent by Starlette's own
    error layer carries the headers.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        refused = False  # we have already answered 413: whatever the app sends now is dropped

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        async def send_from_app(message: Message) -> None:
            if not refused:
                await send_with_headers(message)

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            await self._too_large(send_with_headers)
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # Answer 413 now, and tell the app the client has gone, so it stops reading.
                    if not started and not refused:
                        refused = True
                        await self._too_large(send_with_headers)
                    return {"type": "http.disconnect"}
            return message

        await self.app(scope, counting_receive, send_from_app)

    @staticmethod
    async def _too_large(send: Send) -> None:
        body = json.dumps(
            {"error": {"code": "request_too_large", "message": "The request is too large."}}
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def cors_options(origin: str | None) -> dict[str, Any] | None:
    """The CORSMiddleware settings, or None when no origin is configured (every cross-origin
    request is then refused by the browser, because no CORS header is ever sent)."""
    if not origin:
        return None
    return {
        "allow_origins": [origin],
        "allow_methods": CORS_METHODS,
        "allow_headers": CORS_HEADERS,
        "allow_credentials": False,
        "max_age": 600,
    }
