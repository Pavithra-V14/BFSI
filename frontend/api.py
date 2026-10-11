"""The frontend's only way to the API: one small HTTP client.

Rules kept here:
- The token travels in the `Authorization` header and nowhere else. It is never logged, never part
  of a `repr`, never put in a URL, and never returned in a `Reply`.
- Nothing from a request or a reply (question, answer, token, password) is logged.
- A result is always a `Reply`; the client never raises for a refused or an unreachable API. A
  service that is still waking up (a free Render instance) is its own kind, `waking`, so the page
  can show a calm waiting state and retry instead of an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from frontend.config import Settings

# Tests put an `httpx.MockTransport` here; the running service leaves it None (real network).
TRANSPORT: httpx.BaseTransport | None = None

WAKING_STATUSES = (502, 503, 504)


class SessionExpired(Exception):
    """The service no longer accepts this sign-in (expired, revoked, account switched off). Raised
    by the client for EVERY call that was made with a token, so one place (the app) signs out."""


@dataclass(frozen=True)
class Reply:
    """What came back. `kind` is `ok`, `error` (the API refused, with a code and a plain message),
    `waking` (the service did not answer yet: still starting), or `timeout` (it did not finish)."""

    kind: str
    status: int = 0
    data: Any = None
    code: str | None = None
    message: str | None = None
    retry_after: int | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def waking(self) -> bool:
        return self.kind == "waking"


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return max(1, int(response.headers.get("retry-after", "")))
    except ValueError:
        return None


def _reply_from(response: httpx.Response) -> Reply:
    try:
        body = response.json() if response.content else None
    except ValueError:
        body = None
    if response.status_code < 400:
        return Reply("ok", response.status_code, body)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        message = error.get("message")
        return Reply(
            "error",
            response.status_code,
            code=error["code"],
            message=message if isinstance(message, str) else None,
            retry_after=_retry_after(response),
            data=error,
        )
    if response.status_code in WAKING_STATUSES:
        # a gateway page from the host, not one of the API's own error bodies: still starting up
        return Reply("waking", response.status_code, retry_after=_retry_after(response))
    return Reply("error", response.status_code, code="http_error", message=None)


class ApiClient:
    def __init__(self, settings: Settings, token: str | None = None) -> None:
        self._base = settings.api_base_url
        self._timeout = settings.request_timeout
        self._token = token

    def __repr__(self) -> str:  # never shows the token
        return f"ApiClient(base={self._base!r}, signed_in={self._token is not None})"

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Reply:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        timeout = httpx.Timeout(self._timeout, connect=10.0)
        try:
            with httpx.Client(
                base_url=self._base, timeout=timeout, transport=TRANSPORT, follow_redirects=False
            ) as client:
                response = client.request(method, path, json=json, params=params, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            return Reply("waking")  # nothing answered at all: most likely a cold start
        except httpx.TimeoutException:
            return Reply("timeout")  # the request got there, the answer did not come in time
        except httpx.HTTPError:
            # the connection broke after it was made: the request may have been received, so it
            # is NOT treated as "waking" (which would ask the question again)
            return Reply("error", code="connection_error")
        reply = _reply_from(response)
        if self._token and reply.kind == "error" and reply.code == "not_signed_in":
            raise SessionExpired
        return reply

    # ---- account ------------------------------------------------------------------------
    def live(self) -> Reply:
        return self.request("GET", "/live")

    def login(self, username: str, password: str) -> Reply:
        return self.request(
            "POST", "/auth/login", json={"username": username, "password": password}
        )

    def register(self, username: str, email: str, password: str) -> Reply:
        return self.request(
            "POST",
            "/auth/register",
            json={"username": username, "email": email, "password": password},
        )

    def me(self) -> Reply:
        return self.request("GET", "/auth/me")

    def logout(self) -> Reply:
        return self.request("POST", "/auth/logout")

    # ---- chat and sessions --------------------------------------------------------------
    def chat(self, message: str, session_id: str | None) -> Reply:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        return self.request("POST", "/chat", json=body)

    def sessions(self, limit: int = 20, offset: int = 0) -> Reply:
        return self.request("GET", "/sessions", params={"limit": limit, "offset": offset})

    def session(self, session_id: str, limit: int = 200, offset: int = 0) -> Reply:
        return self.request(
            "GET", f"/sessions/{session_id}", params={"limit": limit, "offset": offset}
        )

    def delete_session(self, session_id: str) -> Reply:
        return self.request("DELETE", f"/sessions/{session_id}")

    def delete_all_sessions(self) -> Reply:
        return self.request("DELETE", "/sessions")

    def feedback(self, message_id: str, rating: str) -> Reply:
        return self.request("POST", f"/messages/{message_id}/feedback", json={"rating": rating})

    # ---- admin --------------------------------------------------------------------------
    def users(self, limit: int = 50, offset: int = 0) -> Reply:
        return self.request("GET", "/admin/users", params={"limit": limit, "offset": offset})

    def set_active(self, user_id: str, active: bool) -> Reply:
        return self.request("PUT", f"/admin/users/{user_id}/active", json={"active": active})

    def set_role(self, user_id: str, role: str) -> Reply:
        return self.request("PUT", f"/admin/users/{user_id}/role", json={"role": role})

    def review(
        self, status: str | None = None, kind: str | None = None, limit: int = 20, offset: int = 0
    ) -> Reply:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        if kind:
            params["kind"] = kind
        return self.request("GET", "/admin/review", params=params)

    def label(self, item_id: str, label: str, status: str = "labelled") -> Reply:
        return self.request(
            "POST", f"/admin/review/{item_id}/label", json={"label": label, "status": status}
        )

    def documents(self, limit: int = 50, offset: int = 0) -> Reply:
        return self.request("GET", "/admin/documents", params={"limit": limit, "offset": offset})

    def entities(self, status: str = "pending", limit: int = 50, offset: int = 0) -> Reply:
        return self.request(
            "GET",
            "/admin/entities",
            params={"status": status, "limit": limit, "offset": offset},
        )

    def set_entity(self, entity_id: str, status: str, confirm_single_word: bool = False) -> Reply:
        body: dict[str, Any] = {"status": status}
        if confirm_single_word:
            body["confirm_single_word"] = True
        return self.request("PUT", f"/admin/entities/{entity_id}", json=body)

    def alerts(self) -> Reply:
        return self.request("GET", "/admin/alerts")

    def stage(self) -> Reply:
        return self.request("GET", "/admin/shadow")

    def set_stage(self, stage: str) -> Reply:
        return self.request("PUT", "/admin/shadow", json={"stage": stage})

    # ---- dashboards ---------------------------------------------------------------------
    def dash_deciders(self) -> Reply:
        return self.request("GET", "/admin/dashboards/deciders")

    def dash_keys(self) -> Reply:
        return self.request("GET", "/admin/dashboards/keys")

    def dash_usage(self, window: str = "day", role: str | None = None) -> Reply:
        params: dict[str, Any] = {"window": window}
        if role:
            params["role"] = role
        return self.request("GET", "/admin/dashboards/usage", params=params)

    def dash_cache(self, window: str = "day") -> Reply:
        return self.request("GET", "/admin/dashboards/cache", params={"window": window})

    def dash_ingestion(self) -> Reply:
        return self.request("GET", "/admin/dashboards/ingestion")

    def dash_daily(self, days: int = 14) -> Reply:
        return self.request("GET", "/admin/dashboards/daily", params={"days": days})

    def shadow_answers(self, limit: int = 20, offset: int = 0) -> Reply:
        return self.request(
            "GET", "/admin/shadow/answers", params={"limit": limit, "offset": offset}
        )
