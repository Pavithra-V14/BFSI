"""A fake API for the frontend tests: an `httpx.MockTransport`, and a runner for the real Streamlit
script (`streamlit.testing.v1.AppTest`). No network, no real service."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from streamlit.testing.v1 import AppTest

import frontend.api as frontend_api

ROOT = Path(__file__).resolve().parents[1]
APP = str(ROOT / "frontend" / "app.py")
# A made-up value that is easy to search for in a rendered page. Not a real credential.
FAKE_TOKEN = "fake-session-marker-" + "k" * 8
SESSION = "11111111-1111-4111-8111-111111111111"
MESSAGE = "22222222-2222-4222-8222-222222222222"
OTHER_MESSAGE = "33333333-3333-4333-8333-333333333333"

Handler = Callable[[httpx.Request], httpx.Response]


def json_response(status: int, body: Any = None, headers: dict[str, str] | None = None):
    return httpx.Response(status, json=body, headers=headers or {})


def error(status: int, code: str, message: str = "Plain words.", **headers: str):
    return json_response(status, {"error": {"code": code, "message": message}}, headers)


def answer_body(**changes: Any) -> dict[str, Any]:
    body = {
        "answer_visible": True,
        "kind": "answer",
        "text": "A waiting period of 30 days applies.",
        "reason": None,
        "citations": [{"doc_id": "doc-a", "source": "Policy", "clause": "4.2", "page": 7}],
        "warnings": [],
        "confidence": None,
        "degraded": [],
        "disclaimer": False,
        "session_id": SESSION,
        "message_id": MESSAGE,
    }
    body.update(changes)
    return body


class FakeApi:
    """Routes are `(METHOD, path) -> response | callable(request) -> response`."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any, str | None]] = []
        self.routes: dict[tuple[str, str], Any] = {
            ("GET", "/live"): json_response(200, {"status": "ok"}),
            ("POST", "/auth/login"): json_response(
                200, {"access_token": FAKE_TOKEN, "token_type": "bearer", "expires_at": "x"}
            ),
            ("GET", "/auth/me"): json_response(
                200, {"id": "u1", "username": "asha_rao", "role": "customer"}
            ),
            ("POST", "/auth/logout"): httpx.Response(204),
            ("POST", "/auth/register"): json_response(
                201, {"id": "u2", "username": "new_user", "role": "customer"}
            ),
            ("GET", "/sessions"): json_response(
                200, {"total": 0, "limit": 20, "offset": 0, "sessions": []}
            ),
            ("POST", "/chat"): json_response(200, answer_body()),
            ("POST", f"/messages/{MESSAGE}/feedback"): json_response(
                200, {"message_id": MESSAGE, "rating": "up"}
            ),
            ("GET", "/admin/alerts"): json_response(
                200, {"banner": False, "critical": [], "events": [], "window": 0}
            ),
        }
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content
        self.requests.append(
            (request.method, request.url.path, body, request.headers.get("authorization"))
        )
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return error(404, "not_found", "Not found.")
        return route(request) if callable(route) else route

    def calls(self, method: str, path: str) -> list[Any]:
        return [r for r in self.requests if r[0] == method and r[1] == path]


def _prepare(api: FakeApi, monkeypatch, env: dict[str, str] | None) -> None:
    monkeypatch.setattr(frontend_api, "TRANSPORT", api.transport)
    for name in ("API_BASE_URL", "ADMIN_MACHINE", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)


def signed_in(
    api: FakeApi,
    monkeypatch,
    *,
    role: str = "customer",
    username: str = "asha_rao",
    env: dict[str, str] | None = None,
) -> AppTest:
    """The app, already signed in (the session state a login would have made)."""
    _prepare(api, monkeypatch, env)
    at = AppTest.from_file(APP, default_timeout=20)
    at.session_state["fx_token"] = FAKE_TOKEN
    at.session_state["fx_user"] = {"id": "u1", "username": username, "role": role}
    return at


def signed_out(api: FakeApi, monkeypatch, env: dict[str, str] | None = None) -> AppTest:
    _prepare(api, monkeypatch, env)
    return AppTest.from_file(APP, default_timeout=20)


def _walk(block) -> list[Any]:
    found = []
    for child in block:
        found.append(child)
        children = getattr(child, "children", None)
        if children:
            found.extend(_walk(list(children.values())))
    return found


def page_text(at: AppTest) -> str:
    """Everything the page shows, as one string (every element's content, including HTML)."""
    parts: list[str] = []
    for element in _walk(list(at.main)) + _walk(list(at.sidebar)):
        proto = getattr(element, "proto", None)
        parts.append(str(proto) if proto is not None else str(element))
    return "\n".join(parts)


def html_bodies(at: AppTest) -> list[str]:
    return [str(e.proto.body) for e in at.get("html")]
