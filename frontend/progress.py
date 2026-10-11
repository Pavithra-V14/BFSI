"""Waiting for an answer, and waiting for a service that is waking up.

The service does not stream, and it does not report which step it is on. While a request runs the
page shows GENERIC stage names that change with the time that has passed (see
`messages.PROGRESS_STAGES`) and an elapsed timer. They are a courtesy, not a progress report, and
the wording says nothing about which step is really running.

A free Render service that has gone to sleep takes up to about a minute to answer. That is shown as
a calm "waking up" state with a retry, not as an error.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from frontend import messages, render
from frontend.api import ApiClient, Reply


def stage_for(elapsed: float) -> str:
    """The stage text for the time that has passed."""
    text = messages.PROGRESS_STAGES[0][1]
    for start, label in messages.PROGRESS_STAGES:
        if elapsed >= start:
            text = label
    return text


def wait_with_progress(
    work: Callable[[], Reply],
    placeholder: Any,
    *,
    clock: Callable[[], float] | None = None,
    tick: float = 0.25,
) -> Reply:
    """Run `work` (a blocking API call) in a thread and keep `placeholder` updated meanwhile."""
    clock = clock or time.monotonic
    box: list[Reply] = []
    failed: list[BaseException] = []

    def target() -> None:
        try:
            box.append(work())
        except BaseException as error:  # handed to the caller, which owns the page
            failed.append(error)

    thread = threading.Thread(target=target, daemon=True)
    started = clock()
    thread.start()
    while True:
        thread.join(tick)
        if not thread.is_alive():
            break
        elapsed = clock() - started
        placeholder.html(render.waiting(stage_for(elapsed), f"{int(elapsed)} s"))
    placeholder.empty()
    if failed:
        raise failed[0]
    return box[0] if box else Reply("timeout")


def wait_until_awake(
    client: ApiClient,
    placeholder: Any,
    *,
    max_seconds: float,
    poll_seconds: float,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Ask the service's liveness address until it answers (True) or the time is up (False)."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    started = clock()
    while True:
        elapsed = clock() - started
        placeholder.html(
            render.waiting(messages.WAKING_TITLE, f"{messages.WAKING_TEXT} ({int(elapsed)} s)")
        )
        if client.live().ok:
            placeholder.empty()
            return True
        if elapsed >= max_seconds:
            placeholder.empty()
            return False
        sleep(poll_seconds)
