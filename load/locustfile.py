"""The Locust scenario (spec 16). Run by the owner, in their own terminal, after one of the two
servers of `load/` is listening (see docs/OWNER_RUNS.md, Runs 26 and 27):

    uv run python load/stub_server.py                       # window A (or load/real_server.py)
    uv run locust -f load/locustfile.py --headless --host http://127.0.0.1:8099 \
        --csv load_out/run --csv-full-history               # window B: the users

`--csv-full-history` matters: only then are the percentiles of the history file the CURRENT ones
(about the last 10 seconds) and not the whole run so far. `LOAD_MODE` is `stub` (default) or
`real`; a real run needs `LOAD_CONFIRM` (the ticket's code) and the real server of `load/`.
`LOAD_STEPS` is the plan (default `10x60,20x60,40x60,80x60,160x60`: users x seconds).

Every virtual user is one customer account that the server made before it started to listen
(`load/seed.py`; nothing registers through the API, the registration limit stays on). The user
logs in, asks a mix of the active evaluation questions (some as a follow-up in the same session),
now and then lists and deletes its chats. A user that could not log in is counted as NOT active,
and a step with fewer than 80 % of its users active is a failed step in the report.

What is a failure: any chat answer that is not 200, except HTTP 429 `too_many_requests` (the
per-user rate limit working as designed), and a 503 `busy` of the password hasher at login.
HTTP 503 `service_unavailable` ("temporarily unavailable") IS a failure.
"""

from __future__ import annotations

import csv
import itertools
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import gevent  # noqa: E402
from locust import HttpUser, LoadTestShape, between, events, task  # noqa: E402

from app.eval.cases import load_cases  # noqa: E402
from load import plan, seed  # noqa: E402

MODE, STEPS = plan.check_start()  # a bad plan or a missing code stops the run here
print(plan.mode_banner(MODE), flush=True)

_CASES = load_cases()[1]
QUESTIONS = [
    (c.turns[0], c.turns[-1] if len(c.turns) > 1 else None)
    for c in _CASES
    if c.active and c.role == "customer" and c.category not in ("pii", "injection")
]

# counted here, written once a second to `<prefix>_active.csv` for the report
COUNTS = {"active": 0, "chat_ok": 0, "chat_rate_limited": 0, "chat_unavailable": 0, "chat_other": 0}
SIDE_COLUMNS = ["time", "target_users", "active_users", "chat_ok", "chat_rate_limited",
                "chat_unavailable", "chat_other"]  # fmt: skip
_accounts: tuple[list[str], str] | None = None
_next_account = itertools.count()


def accounts() -> tuple[list[str], str]:
    global _accounts
    if _accounts is None:
        _accounts = seed.read_users_file(plan.users_path())
    return _accounts


def error_code(reply) -> str:
    """The `error.code` of an API error body, or an empty string."""
    try:
        return str(reply.json().get("error", {}).get("code", ""))
    except (ValueError, AttributeError):
        return ""


def _prefix(environment) -> str:
    options = getattr(environment, "parsed_options", None)
    return getattr(options, "csv_prefix", None) or str(plan.OUT_DIR / "run")


@events.test_start.add_listener
def _start(environment, **kwargs):  # pragma: no cover - needs locust running
    host = environment.host or ""
    if not plan.is_loopback(host):
        raise SystemExit("refused: a load run targets this machine only (127.0.0.1)")
    if MODE == "real":
        try:
            plan.check_marker(os.environ.get("LOAD_CONFIRM", ""), STEPS, host)
        except plan.PlanError as error:
            raise SystemExit(f"refused: {error}") from None
    prefix = Path(_prefix(environment))
    prefix.parent.mkdir(parents=True, exist_ok=True)
    plan_file = Path(f"{prefix}_plan.json")
    plan_file.write_text(
        json.dumps({"mode": MODE, "steps": plan.steps_text(STEPS), "started": time.time()}),
        "utf-8",
    )
    side = Path(f"{prefix}_active.csv")

    def sampler() -> None:
        with side.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(SIDE_COLUMNS)
            while True:
                runner = environment.runner
                writer.writerow([
                    int(time.time()), runner.user_count if runner else 0, COUNTS["active"],
                    COUNTS["chat_ok"], COUNTS["chat_rate_limited"], COUNTS["chat_unavailable"],
                    COUNTS["chat_other"],
                ])  # fmt: skip
                handle.flush()
                gevent.sleep(1)

    environment._load_sampler = gevent.spawn(sampler)


@events.test_stop.add_listener
def _stop(environment, **kwargs):  # pragma: no cover - needs locust running
    greenlet = getattr(environment, "_load_sampler", None)
    if greenlet is not None:
        greenlet.kill()


class Customer(HttpUser):
    wait_time = between(3, 8)  # a person reads the answer before asking again

    def on_start(self) -> None:
        names, self.password = accounts()
        self.username = names[next(_next_account) % len(names)]
        self.headers: dict[str, str] = {}
        self.session_id: str | None = None
        self.counted = False
        self.login()

    def on_stop(self) -> None:
        if self.counted:
            COUNTS["active"] -= 1
            self.counted = False

    def login(self) -> None:
        with self.client.post(
            "/auth/login",
            json={"username": self.username, "password": self.password},
            name="login",
            catch_response=True,
        ) as reply:
            if reply.status_code == 200:
                self.headers = {"Authorization": "Bearer " + reply.json()["access_token"]}
                if not self.counted:
                    COUNTS["active"] += 1
                    self.counted = True
            elif reply.status_code == 503 and error_code(reply) == "busy":
                reply.success()  # the password hasher says busy: the user retries on its next task
            else:
                reply.failure(f"login {reply.status_code}")

    @task(10)
    def ask(self) -> None:
        if not self.headers:
            self.login()
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
            code = error_code(reply)
            if reply.status_code == 200:
                COUNTS["chat_ok"] += 1
                self.session_id = reply.json().get("session_id") or self.session_id
            elif reply.status_code == 429 and code == "too_many_requests":
                COUNTS["chat_rate_limited"] += 1  # the per-user limit working as designed
                reply.success()
            elif reply.status_code == 503 and code == "service_unavailable":
                COUNTS["chat_unavailable"] += 1  # "temporarily unavailable" IS a failure
                reply.failure("service_unavailable")
            else:
                COUNTS["chat_other"] += 1
                reply.failure(f"chat {reply.status_code} {code}".strip())

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
