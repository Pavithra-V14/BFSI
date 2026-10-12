"""The Locust scenario (spec 16). Run by the owner, in their own terminal:

    uv run python load/stub_server.py                       # window A: the stubbed API
    uv run locust -f load/locustfile.py --headless --host http://127.0.0.1:8099 \
        --csv load_out/run                                   # window B: the users

`LOAD_MODE` is `stub` (default) or `real`. A real-provider run needs `LOAD_CONFIRM`: the code that
`scripts/load_estimate.py` printed for this exact plan, which needs the owner's explicit yes.
`LOAD_STEPS` is the plan (default `10x60,20x60,40x60,80x60,160x60`: users x seconds).

Every virtual user is one customer: registers, logs in, asks a mix of the active evaluation
questions (some as a follow-up in the same session), now and then lists and deletes its chats.
The usernames and passwords are made up per run; nothing here is a real person's data.
"""

from __future__ import annotations

import random
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from locust import HttpUser, LoadTestShape, between, events, task  # noqa: E402

from app.eval.cases import load_cases  # noqa: E402
from load import plan  # noqa: E402

MODE, STEPS = plan.check_start()  # a bad plan or a missing code stops the run here
print(plan.mode_banner(MODE), flush=True)

_CASES = load_cases()[1]
QUESTIONS = [
    (c.turns[0], c.turns[-1] if len(c.turns) > 1 else None)
    for c in _CASES
    if c.active and c.role == "customer" and c.category not in ("pii", "injection")
]
# the statuses that are the system working as designed under load, not a failure of the code:
# the per-user rate limit, the busy signal of the password hasher
EXPECTED_BUSY = {429, 503}


@events.test_start.add_listener
def _check_host(environment, **kwargs):  # pragma: no cover - needs locust running
    host = (environment.host or "").lower()
    if MODE == "stub" and not any(h in host for h in ("127.0.0.1", "localhost")):
        raise SystemExit("the stub mode runs against this machine only")


class Customer(HttpUser):
    wait_time = between(3, 8)  # a person reads the answer before asking again

    def on_start(self) -> None:
        tag = secrets.token_hex(5)
        self.username = f"load{tag}"
        self.password = secrets.token_urlsafe(18) + "Aa1"
        self.headers: dict[str, str] = {}
        self.session_id: str | None = None
        body = {
            "username": self.username,
            "email": f"{self.username}@load.invalid",
            "password": self.password,
        }
        with self.client.post("/auth/register", json=body, name="register",
                              catch_response=True) as reply:  # fmt: skip
            if reply.status_code in EXPECTED_BUSY:
                reply.success()
                return
            if reply.status_code != 201:
                reply.failure(f"register {reply.status_code}")
                return
        with self.client.post(
            "/auth/login",
            json={"username": self.username, "password": self.password},
            name="login",
            catch_response=True,
        ) as reply:
            if reply.status_code == 200:
                self.headers = {"Authorization": "Bearer " + reply.json()["access_token"]}
            elif reply.status_code in EXPECTED_BUSY:
                reply.success()
            else:
                reply.failure(f"login {reply.status_code}")

    @task(10)
    def ask(self) -> None:
        if not self.headers:
            return
        first, follow_up = random.choice(QUESTIONS)
        use_follow_up = bool(follow_up) and self.session_id is not None and random.random() < 0.3
        message = follow_up if use_follow_up and follow_up else first
        payload: dict[str, str] = {"message": message}
        if self.session_id and (use_follow_up or random.random() < 0.2):
            payload["session_id"] = self.session_id
        with self.client.post("/chat", json=payload, headers=self.headers, name="chat",
                              catch_response=True) as reply:  # fmt: skip
            if reply.status_code == 200:
                self.session_id = reply.json().get("session_id") or self.session_id
            elif reply.status_code in EXPECTED_BUSY:
                reply.success()  # limited or busy by design; counted in its own column below
                events.request.fire(
                    request_type="POST",
                    name="chat (limited or busy: by design)",
                    response_time=reply.elapsed.total_seconds() * 1000,
                    response_length=0,
                    exception=None,
                    context={},
                )
            else:
                reply.failure(f"chat {reply.status_code}")

    @task(1)
    def list_chats(self) -> None:
        if self.headers:
            self.client.get("/sessions", headers=self.headers, name="sessions")

    @task(1)
    def forget(self) -> None:
        if self.headers and self.session_id:
            self.client.delete("/sessions", headers=self.headers, name="delete chats")
            self.session_id = None


class StepShape(LoadTestShape):
    """The plan of `LOAD_STEPS`, one step after the other."""

    def tick(self):
        elapsed = self.get_run_time()
        end = 0
        for step in STEPS:
            end += step.seconds
            if elapsed < end:
                return step.users, max(step.users, 10)
        return None
