"""The public entry point: `Pipeline(deps).answer(request, db)` (spec 4).

Before the graph runs:
- the masker must be ready (`startup.MaskerHolder`): until then "starting up, please retry", and if
  its
  load failed "temporarily unavailable"; a question is never handled without masking;
- an existing session takes the per-session lock: a second question while one is being answered gets
  "busy". The lock is released in a `finally`, so a timeout, a cancellation or any error frees it.

After the graph: the raw question is dropped from the state whatever happened, and the tokens the
request really used are charged to the user's daily budget (a counter that fails is only an alert).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from app.db.connection import Database
from app.memory.config import MemoryConfig
from app.pipeline import messages
from app.pipeline.graph import build_graph
from app.pipeline.persist import PersistQueue
from app.pipeline.retrieval import ValuesCache
from app.pipeline.safety import disable_tracing, tracing_enabled
from app.pipeline.state import Deps, PipelineState, Request, Result
from app.pipeline.trace import Trace


class Pipeline:
    def __init__(self, deps: Deps) -> None:
        if deps.queue is None:
            deps.queue = PersistQueue(deps.config.persist_queue_size)
        if deps.memory is None:
            deps.memory = MemoryConfig()
        self.deps = deps
        self.values = ValuesCache()
        self.graph = build_graph(self)

    def _early(self, request: Request, kind: str, text: str, reason: str) -> Result:
        return Result(kind, text, reason=reason, correlation_id=request.correlation_id)

    async def answer(self, request: Request, db: Database) -> Result:
        deps = self.deps
        if not deps.masker.ready:
            if deps.masker.failed:
                if deps.on_event:
                    deps.on_event("masker_unavailable", {})
                return self._early(request, "unavailable", messages.UNAVAILABLE, "masker_failed")
            return self._early(request, "starting_up", messages.STARTING_UP, "starting_up")
        if tracing_enabled():  # something switched tracing on since the graph was built
            disable_tracing()
            if deps.on_event:
                deps.on_event("tracing_switched_off", {})
        locked = False
        if request.session_id is not None:
            locked = deps.locks.try_acquire(request.user_id, request.session_id)
            if not locked:
                return self._early(request, "busy", messages.BUSY, "busy")
        started = deps.clock()
        budget = deps.config.budget_seconds
        ctx = PipelineState(
            request=replace(request, query=""),  # the raw question is held only in `raw_query`
            db=db,
            deps=deps,
            trace=Trace(request.correlation_id, on_span=deps.on_span, started_at=deps.now()),
            deadline=started + budget,
            started=started,
            raw_query=request.query,
            values=self.values,
        )
        try:
            try:
                await asyncio.wait_for(
                    self.graph.ainvoke({"ctx": ctx}, {"recursion_limit": 60}), timeout=budget
                )
            except TimeoutError:
                ctx.result = Result("unavailable", messages.UNAVAILABLE, reason="budget_exhausted")
                ctx.emit("request_budget_exhausted")
            except asyncio.CancelledError:
                raise
            except Exception as error:  # a bug in the graph: a fixed answer, never the error text
                ctx.result = Result(
                    "unavailable", messages.UNAVAILABLE, reason=type(error).__name__
                )
                ctx.emit("graph_failed", reason=type(error).__name__)
        finally:
            ctx.drop_raw()
            if locked and request.session_id is not None:
                deps.locks.release(request.user_id, request.session_id)
        result = ctx.result or Result("unavailable", messages.UNAVAILABLE, reason="no_result")
        result.correlation_id = request.correlation_id
        if not result.timings_ms:
            result.timings_ms = ctx.trace.timings_ms()
        if deps.langfuse is not None:  # after the result is final: outside the budget, never raises
            deps.langfuse.submit_request(ctx, result)
        if deps.usage is not None and result.tokens > 0:
            try:
                await asyncio.to_thread(deps.usage.charge, request.user_id, result.tokens)
            except Exception:  # a counter that fails is not a failed answer
                ctx.emit("budget_charge_failed")
        return result
