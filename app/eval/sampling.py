"""The production sample (spec 15.4: "about 5 % of live answers are queued for scoring off the
request path").

Runs inside the hosted API, so it is light and can never affect an answer:
- the decision and the copy are made in-line in microseconds (a random draw, then the masked text
  the transcript store already made); the write to the evaluation database happens in one
  background thread behind a bounded queue that drops when full;
- only the MASKED copies are stored: the question and the answer as `mask_for_storage` made them
  (the same function that makes the stored transcript), the passages that reached the generator
  (public company documents), names of kinds, and numbers. No user id, no session id;
- it is off unless `EVAL_DATABASE_URL` is set, and every failure is an event (class name only),
  at most once a minute, never an error to a user.

The judging of samples needs the evaluation keys, which are never on Render: it runs on the admin
machine (`python -m app.eval judge-samples`), reading this queue.
"""

from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.db.connection import Database, connect
from app.eval import checks, store
from app.eval import constants as c

log = logging.getLogger("app.eval")

QUEUE_SIZE = 100
FAILURE_EVENT_SECONDS = 60.0
ENV_URL = "EVAL_DATABASE_URL"
ENV_RATE = "EVAL_SAMPLE_RATE"


class SamplingConfigError(ValueError):
    """A bad setting. Names the variable only."""


def rate_from_env(env: Mapping[str, str]) -> float:
    raw = (env.get(ENV_RATE) or "").strip()
    if not raw:
        return c.SAMPLE_RATE
    try:
        value = float(raw)
    except ValueError:
        raise SamplingConfigError(f"{ENV_RATE} must be a number from 0 to 1") from None
    if not 0.0 <= value <= 1.0 or value != value:
        raise SamplingConfigError(f"{ENV_RATE} must be a number from 0 to 1")
    return value


def build_sample(ctx: Any, result: Any) -> dict[str, Any] | None:
    """The row to store for one finished answer, or None when there is no masked copy to store
    (then nothing is sampled: the raw text is never used)."""
    copy = getattr(ctx, "stored_copy", None)
    if not copy:
        return None
    question, answer = copy
    kept = list(getattr(ctx, "kept", []) or [])
    spans = list(getattr(getattr(ctx, "trace", None), "spans", []) or [])
    audit = dict(getattr(getattr(ctx, "trace", None), "audit", {}) or {})
    verdict = getattr(ctx, "verdict", None)
    cited = set(getattr(verdict, "cited_chunk_ids", ()) or ())
    cheap = checks.cheap_checks(result, verdict)
    cheap["cited_idx"] = [i for i, chunk in enumerate(kept) if chunk.chunk_id in cited]
    return {
        "role": str(ctx.request.role),
        "result_kind": str(result.kind),
        "reason": str(result.reason) if result.reason else None,
        "question": question,
        "answer": answer,
        "chunk_ids": [chunk.chunk_id for chunk in kept],
        "clause_refs": [chunk.clause_ref or "" for chunk in kept],
        "contexts": [chunk.text for chunk in kept],
        "models": sorted({str(s.model) for s in spans if getattr(s, "model", None)}),
        "prompt_hash": audit.get("prompt_hash"),
        "corpus_version": str(audit.get("corpus_version")) if audit.get("corpus_version") else None,
        "confidence": result.confidence,
        "degraded": list(result.degraded or []),
        "cheap_checks": cheap,
        "tokens": int(result.tokens or 0),
        "latency_ms": round(sum((result.timings_ms or {}).values()), 1) or None,
    }


class Sampler:
    def __init__(
        self,
        db_factory: Callable[[], Database],
        *,
        rate: float = c.SAMPLE_RATE,
        rng: Callable[[], float] = random.random,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        background: bool = True,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._db_factory = db_factory
        self.rate = rate
        self._rng = rng
        self._on_event = on_event
        self._background = background
        self._clock = clock
        self._now = now
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_failure = -FAILURE_EVENT_SECONDS
        self.dropped = 0
        self.written = 0

    # ---- the request side ------------------------------------------------------------
    def observe(self, ctx: Any, result: Any) -> None:
        """Called once per finished chat request. Never raises."""
        try:
            if result.kind != "answer" or result.cached or self.rate <= 0.0:
                return
            if self._rng() >= self.rate:
                return
            sample = build_sample(ctx, result)
            if sample is None:
                return
            try:
                self._queue.put_nowait(sample)
            except queue.Full:
                self.dropped += 1
                return
            if not self._background:
                self.flush()
        except Exception as error:
            self._failed(error)

    # ---- the writing side ------------------------------------------------------------
    def flush(self) -> int:
        """Write what is queued now. Returns how many rows were written."""
        written = 0
        while True:
            try:
                sample = self._queue.get_nowait()
            except queue.Empty:
                return written
            try:
                db = self._db_factory()
                try:
                    store.insert_sample(db, sample, now=self._now())
                finally:
                    db.close()
                written += 1
                self.written += 1
            except Exception as error:
                self._failed(error)

    def start(self) -> None:
        if not self._background or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="eval-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            self.flush()

    def _failed(self, error: BaseException) -> None:
        moment = self._clock()
        if moment - self._last_failure < FAILURE_EVENT_SECONDS:
            return
        self._last_failure = moment
        log.warning("eval sample failed: %s", type(error).__name__)
        if self._on_event is not None:
            try:
                self._on_event("eval_sample_failed", {"reason": type(error).__name__})
            except Exception:
                pass


def sampler_from_env(
    env: Mapping[str, str],
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Sampler | None:
    """The sampler, or None when no evaluation database is configured."""
    url = (env.get(ENV_URL) or "").strip()
    if not url:
        return None
    rate = rate_from_env(env)
    return Sampler(lambda: connect(url), rate=rate, on_event=on_event)


def chain(*hooks: Callable[[Any, Any], None] | None) -> Callable[[Any, Any], None]:
    """One observer that calls every given one; a failing one never stops the others."""
    active = [h for h in hooks if h is not None]

    def run(ctx: Any, result: Any) -> None:
        for hook in active:
            try:
                hook(ctx, result)
            except Exception:
                pass

    return run
