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
    """Relative, not absolute: every quick request finishes BEFORE the slow login finishes. If the
    hash ran on the event loop, the loop would be held until the login was done and the quick
    requests could only finish after it, whatever the speed of the machine."""
    env.register()
    real = passwords.verify_password

    def slow(stored, typed):
        time.sleep(SLOW)  # stands in for a heavy argon2 check
        return real(stored, typed)

    monkeypatch.setattr(passwords, "verify_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def finished(call):
                response = await call
                return response.status_code, time.perf_counter()

            started = time.perf_counter()
            slow_login = asyncio.create_task(
                finished(
                    client.post("/auth/login", json={"username": "asha_rao", "password": "x" * 12})
                )
            )
            await asyncio.sleep(0.1)  # let the login reach the slow check
            quick = [await finished(client.get("/live")) for _ in range(5)]
            slow_status, slow_done = await slow_login
            return quick, slow_status, slow_done - started, slow_done

    quick, slow_status, total, slow_done = asyncio.run(scenario())
    assert slow_status == 401 and total >= SLOW  # the slow login really was slow
    assert all(status == 200 for status, _ in quick)
    # the order of completion, not a number of seconds: every quick request was served first
    assert all(done < slow_done for _, done in quick)


def test_two_slow_logins_overlap_instead_of_queueing(env, monkeypatch):
    """Relative: the two slow checks ran at the same time (their intervals overlap). One after
    the other (a held loop, or one worker) would give two intervals that do not meet."""
    env.register("asha_rao")
    env.register("ravi_k")
    real = passwords.verify_password
    intervals: list[tuple[float, float]] = []

    def slow(stored, typed):
        begun = time.perf_counter()
        time.sleep(SLOW)
        intervals.append((begun, time.perf_counter()))
        return real(stored, typed)

    monkeypatch.setattr(passwords, "verify_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await asyncio.gather(
                client.post("/auth/login", json={"username": "asha_rao", "password": "x" * 12}),
                client.post("/auth/login", json={"username": "ravi_k", "password": "x" * 12}),
            )

    asyncio.run(scenario())
    assert len(intervals) == 2
    first, second = sorted(intervals)
    assert second[0] < first[1]  # the second began before the first ended: they overlapped


def test_registration_hashing_is_off_the_loop_too(env, monkeypatch):
    """Relative: the quick request finishes before the slow registration does."""
    real = passwords.hash_password

    def slow(typed):
        time.sleep(SLOW)
        return real(typed)

    monkeypatch.setattr(passwords, "hash_password", slow)

    async def scenario():
        transport = httpx.ASGITransport(app=env.app, client=("203.0.113.5", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def finished(call):
                response = await call
                return response.status_code, time.perf_counter()

            slow_call = asyncio.create_task(
                finished(
                    client.post(
                        "/auth/register",
                        json=env.body("asha_rao", password="correct horse battery"),
                    )
                )
            )
            await asyncio.sleep(0.1)
            quick_status, quick_done = await finished(client.get("/live"))
            slow_status, slow_done = await slow_call
            return quick_status, quick_done, slow_status, slow_done

    quick_status, quick_done, slow_status, slow_done = asyncio.run(scenario())
    assert slow_status == 201 and quick_status == 200
    assert quick_done < slow_done  # served first, while the hash was still running
