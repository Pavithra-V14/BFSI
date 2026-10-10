"""LangChain / LangSmith tracing off, in code (the graph state holds the raw question).

LangGraph can send run data to LangSmith when a tracing variable is set (by the shell, a `.env` file
that something loads, or a hosting dashboard). The state of this graph holds the raw question until
it is masked for storage, so tracing must never be on. `disable_tracing` sets every known switch to
"false" in this process; `tracing_enabled` reports whether any is on (the tests, and the build of
the graph, refuse to continue if it is). The graph is also compiled with no checkpointer, because a
checkpointer would persist the state (`graph.compile`, checked by a test).
"""

from __future__ import annotations

import os

TRACING_VARIABLES = (
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
)
_ON = {"1", "true", "yes", "on"}


class TracingEnabled(RuntimeError):
    """A tracing switch is on. The message names the variable, never a value."""


def disable_tracing() -> None:
    for name in TRACING_VARIABLES:
        os.environ[name] = "false"
    try:  # the LangSmith client caches the setting after the first read
        from langsmith import utils

        cache_clear = getattr(utils.tracing_is_enabled, "cache_clear", None)
        if callable(cache_clear):
            cache_clear()
    except Exception:  # pragma: no cover - a different langsmith layout
        pass


def tracing_enabled() -> list[str]:
    """The names of the tracing variables that are on (empty when all are off)."""
    return [n for n in TRACING_VARIABLES if os.environ.get(n, "").strip().lower() in _ON]


def require_tracing_off() -> None:
    names = tracing_enabled()
    if names:
        raise TracingEnabled("tracing is on: " + ", ".join(names))
