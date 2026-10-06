"""The security layer: headers, the size limit, CORS, docs off and the error shape (spec 13.3)."""

import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.security import MAX_BODY_BYTES, SECURITY_HEADERS
from tests.api_fixtures import make_settings

ORIGIN = "https://app.example.test"
OTHER_ORIGIN = "https://evil.example.test"


def assert_headers(response):
    for name, value in SECURITY_HEADERS:
        assert response.headers.get(name) == value, name


# ---- security headers on every kind of response -----------------------------------------------


def test_the_planned_headers_are_the_expected_ones():
    names = {name for name, _ in SECURITY_HEADERS}
    assert names == {
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Cache-Control",
        "Content-Security-Policy",
        "Permissions-Policy",
        "Strict-Transport-Security",
        "Cross-Origin-Resource-Policy",
    }
    values = dict(SECURITY_HEADERS)
    assert values["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert values["X-Frame-Options"] == "DENY" and values["Cache-Control"] == "no-store"


def test_headers_on_a_normal_response(env):
    response = env.client.get("/live")
    assert response.status_code == 200
    assert_headers(response)


@pytest.mark.parametrize(
    ("method", "url", "expected"),
    [
        ("GET", "/auth/me", 401),
        ("GET", "/no/such/page", 404),
        ("GET", "/auth/login", 405),
        ("POST", "/auth/login", 422),
    ],
)
def test_headers_on_error_responses_too(env, method, url, expected):
    response = env.client.request(method, url)
    assert response.status_code == expected
    assert_headers(response)


def test_headers_on_a_429_and_a_409(env):
    env.register()
    assert_headers(env.register())  # 409
    for _ in range(10):
        env.login("asha_rao", "wrong password here")
    throttled = env.login("asha_rao", "wrong password here")
    assert throttled.status_code == 429
    assert_headers(throttled)


def test_headers_on_a_413(env):
    response = env.client.post("/auth/login", content=b"x" * (MAX_BODY_BYTES + 1))
    assert response.status_code == 413
    assert_headers(response)


def test_headers_on_a_500(env, monkeypatch):
    from app.accounts import users

    def boom(*args, **kwargs):
        raise RuntimeError("something broke inside")

    monkeypatch.setattr(users, "get_by_username", boom)
    response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 500
    assert_headers(response)


def test_a_header_set_by_a_route_is_not_overwritten(env):
    # Retry-After and WWW-Authenticate pass through; the security headers only fill gaps.
    response = env.client.get("/auth/me")
    assert response.headers["www-authenticate"] == "Bearer"
    assert_headers(response)


# ---- the size limit -------------------------------------------------------------------------


def test_a_body_at_the_limit_is_read_and_one_byte_over_is_refused(env):
    at_limit = json.dumps({"username": "u", "password": "p" * 10}).encode()
    padded = at_limit + b" " * (MAX_BODY_BYTES - len(at_limit))
    assert len(padded) == MAX_BODY_BYTES
    ok = env.client.post(
        "/auth/login", content=padded, headers={"Content-Type": "application/json"}
    )
    assert ok.status_code == 401  # read, parsed, and the login simply failed
    over = env.client.post(
        "/auth/login", content=padded + b" ", headers={"Content-Type": "application/json"}
    )
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "request_too_large"


def test_the_limit_is_16_kib():
    assert MAX_BODY_BYTES == 16 * 1024


def test_a_declared_oversize_body_is_refused_before_it_is_read(env):
    response = env.client.post(
        "/auth/register",
        content=b"{}",
        headers={"Content-Length": str(MAX_BODY_BYTES * 100), "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_a_chunked_body_with_no_content_length_cannot_get_past_the_limit(env):
    def chunks():
        for _ in range(8):
            yield b"x" * 4096  # 32 KiB in total, never declared up front

    response = env.client.post(
        "/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert env.rows("SELECT COUNT(*) FROM audit_log") == [(0,)]  # nothing was processed


def run_asgi(inner, chunks, headers=None):
    """Drive SecurityMiddleware directly as raw ASGI. Returns everything it sent."""
    import asyncio

    from app.api.security import SecurityMiddleware

    sent = []
    queue = list(chunks)

    async def receive():
        if queue:
            body = queue.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        await asyncio.sleep(0.01)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/x",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    asyncio.run(SecurityMiddleware(inner)(scope, receive, send))
    return sent


async def reading_then_answering_app(scope, receive, send):
    """An app that reads its whole body and then answers 200, as a route would."""
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"the app's own answer"})


def test_after_the_413_the_apps_own_answer_is_dropped_so_only_one_response_is_sent():
    chunks = [b"x" * (MAX_BODY_BYTES // 2 + 1)] * 3  # 24 KiB streamed with no Content-Length
    sent = run_asgi(reading_then_answering_app, chunks)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert [m["status"] for m in starts] == [413]  # one response, and it is the 413
    bodies = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"request_too_large" in bodies and b"the app's own answer" not in bodies


def test_the_413_still_carries_the_security_headers_when_streamed():
    sent = run_asgi(reading_then_answering_app, [b"x" * (MAX_BODY_BYTES + 1)])
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    names = {name.decode().lower() for name, _ in start["headers"]}
    assert {name.lower() for name, _ in SECURITY_HEADERS} <= names


def test_a_body_within_the_limit_gets_the_apps_own_answer_untouched():
    sent = run_asgi(reading_then_answering_app, [b"x" * 100, b"y" * 100])
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 200
    assert b"the app's own answer" in b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def test_a_declared_oversize_body_is_refused_without_calling_the_app():
    called = []

    async def app(scope, receive, send):
        called.append(1)

    sent = run_asgi(app, [], headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
    assert called == [] and [m["status"] for m in sent if m["type"] == "http.response.start"] == [
        413
    ]


def test_an_oversize_body_never_reaches_the_route(env, monkeypatch):
    from app.api.routes import auth

    called = []
    monkeypatch.setattr(auth.users, "get_by_username", lambda *a, **k: called.append(1))
    env.client.post("/auth/login", content=b"x" * (MAX_BODY_BYTES + 1))
    assert called == []


def test_the_limit_applies_to_every_route(env):
    big = b"x" * (MAX_BODY_BYTES + 1)
    for method, url in (
        ("POST", "/auth/register"),
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("PUT", "/admin/users/abc/role"),
    ):
        assert env.client.request(method, url, content=big).status_code == 413


def test_a_small_request_is_untouched_by_the_counting_wrapper(env):
    env.register()
    assert env.login().status_code == 200


# ---- CORS: denied unless the one origin is configured -----------------------------------------


def test_with_no_origin_configured_no_cors_header_is_ever_sent(env):
    assert env.settings.cors_origin is None
    response = env.client.get("/live", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_with_no_origin_configured_a_preflight_gets_no_permission(env):
    response = env.client.options(
        "/auth/login",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-methods" not in response.headers


@pytest.fixture
def cors_client(env):
    app = create_app(
        make_settings(cors_origin=ORIGIN),
        db_factory=env.app.state.services.db_factory,
        cache=env.cache,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_the_configured_origin_is_allowed_and_only_it(cors_client):
    ok = cors_client.get("/live", headers={"Origin": ORIGIN})
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    other = cors_client.get("/live", headers={"Origin": OTHER_ORIGIN})
    assert "access-control-allow-origin" not in other.headers


def test_cors_never_allows_cookies_or_a_wildcard(cors_client):
    response = cors_client.get("/live", headers={"Origin": ORIGIN})
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers["access-control-allow-origin"] != "*"


def test_a_preflight_from_the_configured_origin_lists_only_the_needed_methods_and_headers(
    cors_client,
):
    response = cors_client.options(
        "/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    methods = {m.strip() for m in response.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PUT", "DELETE"}
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "content-type" in allowed and "*" not in allowed


def test_a_preflight_from_another_origin_is_refused(cors_client):
    response = cors_client.options(
        "/auth/login",
        headers={"Origin": OTHER_ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_a_preflight_asking_for_a_header_we_do_not_allow_is_refused(cors_client):
    response = cors_client.options(
        "/auth/login",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-evil-header",
        },
    )
    assert response.status_code == 400


def test_the_security_headers_are_on_cors_answers_too(cors_client):
    assert_headers(cors_client.get("/live", headers={"Origin": ORIGIN}))
    preflight = cors_client.options(
        "/auth/login",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert_headers(preflight)


# ---- docs are off ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_the_interactive_docs_and_the_api_description_are_not_published(env, url):
    response = env.client.get(url)
    assert response.status_code == 404
    assert "swagger" not in response.text.lower() and "openapi" not in response.text.lower()


def test_the_app_has_no_docs_urls(env):
    assert env.app.docs_url is None and env.app.redoc_url is None and env.app.openapi_url is None


# ---- the error shape ------------------------------------------------------------------------


def test_every_error_has_the_same_shape(env):
    for response in (
        env.client.get("/no/such/page"),
        env.client.get("/auth/login"),
        env.client.get("/auth/me"),
        env.client.post("/auth/login", json={}),
        env.client.post("/auth/login", content=b"x" * (MAX_BODY_BYTES + 1)),
    ):
        body = response.json()
        assert set(body) == {"error"}
        assert {"code", "message"} <= set(body["error"])
        assert response.headers["content-type"].startswith("application/json")


def test_a_422_names_the_fields_and_never_what_was_sent(env):
    response = env.client.post("/auth/login", json={"username": 5, "password": ["leaky-value"]})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["fields"] == ["body.password", "body.username"]
    assert "leaky-value" not in response.text and "input" not in body and "ctx" not in body


def test_a_500_says_nothing_about_the_cause(env, monkeypatch):
    from app.accounts import users

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail with a path C:/code/app.py")

    monkeypatch.setattr(users, "get_by_username", boom)
    response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Something went wrong on our side."}
    }
    assert "secret internal detail" not in response.text and "Traceback" not in response.text


def test_a_database_that_cannot_be_reached_is_a_503_with_no_address(env):
    from app.store_errors import DatabaseUnavailable

    def broken():
        raise DatabaseUnavailable("OperationalError")

    env.app.state.services.db_factory = broken
    response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert "database" not in response.text.lower() and "OperationalError" not in response.text


def test_a_rejected_database_setting_is_also_just_a_503_for_the_user(env, caplog):
    from app.store_errors import StoreRejected

    def broken():
        raise StoreRejected("database", "credentials")

    env.app.state.services.db_factory = broken
    with caplog.at_level("ERROR"):
        response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 503 and "credentials" not in response.text
    assert "reason=credentials" in caplog.text  # the operator is told, the user is not


def test_a_database_error_raised_inside_a_route_is_a_503_not_a_500(env, monkeypatch):
    import sqlite3

    from app.accounts import users

    def gone(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(users, "get_by_username", gone)
    response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 503


def test_a_wrong_sql_error_is_a_500_not_hidden_as_an_outage(env, monkeypatch):
    import sqlite3

    from app.accounts import users

    def bug(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: users")

    monkeypatch.setattr(users, "get_by_username", bug)
    response = env.login("asha_rao", "wrong password here")
    assert response.status_code == 500
