"""One event loop for a whole ingestion run.

The gateway is async, ingestion is plain synchronous code. A single long-lived loop (instead of
`asyncio.run` per call) keeps the gateway's limiter and breaker state on one loop for the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


class LoopRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()

    def run(self, coroutine: Coroutine[Any, Any, Any]) -> Any:
        return self._loop.run_until_complete(coroutine)

    def close(self) -> None:
        self._loop.close()
