"""Password hashing must not block the event loop (owner addition b).

The auth routes are plain `def`, which FastAPI runs in a worker thread. These tests prove it in
two ways: by looking at the routes, and by running a slow login next to quick requests.
"""

import asyncio
import inspect
import time

import httpx
import pytest

from app.accounts import passwords
from app.api.routes import admin, auth, health

SLOW = 0.6


@pytest.mark.parametrize(
    "route",
    [
        auth.register,
        auth.login,
        auth.logout,
        auth.me,
        admin.change_role,
        health.health,
        health.live,
    ],
)
def test_every_route_that_touches_the_database_or_hashes_is_a_plain_def(route):
    assert not inspect.iscoroutinefunction(route), route.__name__


def test_no_route_in_the_app_is_an_async_function_that_could_block_the_loop(env):
    for route in env.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None and route.path in {"/auth/register", "/auth/login", "/health"}:
            assert not inspect.iscoroutinefunction(endpoint), route.path


def test_a_slow_login_does_not_stop_other_requests_being_served(env, monkeypatch):
    env.register()
    real = passwords.verify_password

    def slow(stored, typed):
        time.sleep(SLOW)  # stands in for a heavy argon2 check
        return real(stored, typed)

    monkeypatch.setattr(passwords, "verify_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            slow_login = asyncio.create_task(
                client.post("/auth/login", json={"username": "asha_rao", "password": "x" * 12})
            )
            await asyncio.sleep(0.1)  # let the login reach the slow check
            quick_times = []
            for _ in range(5):
                before = time.perf_counter()
                response = await client.get("/live")
                quick_times.append(time.perf_counter() - before)
                assert response.status_code == 200
            logged_in = await slow_login
            return quick_times, time.perf_counter() - started, logged_in.status_code

    quick_times, total, status = asyncio.run(scenario())
    assert status == 401 and total >= SLOW  # the slow login really was slow
    assert max(quick_times) < SLOW / 2  # and the quick requests were not held up behind it


def test_two_slow_logins_overlap_instead_of_queueing(env, monkeypatch):
    env.register("asha_rao")
    env.register("ravi_k")
    real = passwords.verify_password

    def slow(stored, typed):
        time.sleep(SLOW)
        return real(stored, typed)

    monkeypatch.setattr(passwords, "verify_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            await asyncio.gather(
                client.post("/auth/login", json={"username": "asha_rao", "password": "x" * 12}),
                client.post("/auth/login", json={"username": "ravi_k", "password": "x" * 12}),
            )
            return time.perf_counter() - started

    assert asyncio.run(scenario()) < SLOW * 1.8  # about one delay, not two in a row


def test_registration_hashing_is_off_the_loop_too(env, monkeypatch):
    real = passwords.hash_password

    def slow(typed):
        time.sleep(SLOW)
        return real(typed)

    monkeypatch.setattr(passwords, "hash_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task = asyncio.create_task(
                client.post(
                    "/auth/register", json=env.body("asha_rao", password="correct horse battery")
                )
            )
            await asyncio.sleep(0.1)
            before = time.perf_counter()
            await client.get("/live")
            quick = time.perf_counter() - before
            return quick, (await task).status_code

    quick, status = asyncio.run(scenario())
    assert status == 201 and quick < SLOW / 2
