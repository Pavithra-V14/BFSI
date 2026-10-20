"""Stage 16: persist (spec 4.1 step 16, 8.3, 11.1 "Postgres: answer if possible and queue the
transcript write", 11.4 review-queue writes).

What is stored, and only ever as MASKED text (pass 2, `mask_for_storage`, made from the raw question
with only the surviving documents' approved terms):
- the user message (flagged `history_excluded` for a blocked, reworded or refused question);
- the assistant message (a generated answer with its citations and `grounded` flag, or a fixed
  reply,
  which is always ungrounded);
- the session facts (product line, topic, approved names) and the unclear-turn count (a second
  unclear
  turn in a row replaces the reply with the fixed ending and writes the review row, Phase 7);
- a review-queue row for out-of-domain, no-reliable-context and low-confidence answers, with a
  reason
  code and the masked question, de-duplicated per user, day, reason and question.

If Postgres fails, the answer is still returned and the pass-2 masked turn goes into a bounded
in-process queue (an alert event says so); the next request tries the queue first. The queue is lost
if the host restarts; that is the accepted cost of the [P] decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.accounts.tokens import utc_text
from app.guardrails.normalise import normalise_entity
from app.guardrails.scan import StoredAnswer, StoredQuery, mask_for_storage
from app.memory import store
from app.memory.facts import Entity, match_entities, update_facts
from app.memory.messages import UNCLEAR_END
from app.memory.unclear import record_turn
from app.pipeline.state import PipelineState
from app.pipeline.trace import Span


@dataclass
class PendingTurn:
    """One turn to store: masked text only. `done` lists the steps already written, so a retry
    after a failure never writes anything twice."""

    user_id: str
    session_id: str | None
    question: StoredQuery
    answer: StoredAnswer
    ended_answer: StoredAnswer | None  # the fixed ending, ready if this unclear turn ends the chat
    fixed: bool
    grounded: bool
    citations: list[dict[str, Any]]
    history_excluded: bool
    unclear: bool | None
    review_reason: str | None
    review_kind: str
    question_hash: str
    product_line: str | None = None
    topic: str | None = None
    entities: list[Entity] = field(default_factory=list)
    done: set[str] = field(default_factory=set)
    ended: bool = False
    assistant_message_id: str | None = None
    attempts: int = 0  # failed tries while the database itself was reachable (see `flush`)
    shadowed: bool = False  # the answer must stay hidden from its user (rollout stage)
    new_session: bool = False  # `session_id` was handed out by `build_job`: the write creates it
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def question_hash(text: str) -> str:
    return hashlib.sha256(normalise_entity(text).encode()).hexdigest()[:16]


def build_job(ctx: PipelineState) -> PendingTurn:
    """The pass-2 copies. Needs the raw question (still in memory) and the masker; raises if either
    is missing, and the caller then stores nothing."""
    plan = ctx.plan
    if plan is None or ctx.raw_query is None:
        raise RuntimeError("nothing to store")
    masker = ctx.deps.masker.masker
    docs = set(plan.surviving_doc_ids)
    question = mask_for_storage(ctx.raw_query, masker=masker, allow=ctx.allow, doc_ids=docs)
    answer = mask_for_storage(
        plan.text, masker=masker, allow=ctx.allow, doc_ids=docs, kind="answer"
    )
    ended = (
        mask_for_storage(UNCLEAR_END, masker=masker, allow=ctx.allow, doc_ids=docs, kind="answer")
        if plan.unclear
        else None
    )
    cfg = ctx.deps.config
    triage = ctx.triage
    product = (
        triage.product_line
        if plan.update_facts
        and (triage.product_line_probability or 0.0) >= cfg.soft_filter_probability
        else None
    )
    topic = (
        triage.clause_type
        if plan.update_facts
        and (triage.clause_type_probability or 0.0) >= cfg.soft_filter_probability
        else None
    )
    entities = match_entities(ctx.pass1.text, ctx.allow) if plan.update_facts and ctx.pass1 else []
    # A first question gets its conversation id NOW, not when the write ends: the answer must carry
    # it even when the write fails or is late, or the next question would start another chat.
    session_id = ctx.request.session_id
    new_session = session_id is None
    if new_session:
        session_id = str(uuid.uuid4())
    return PendingTurn(
        user_id=ctx.request.user_id,
        session_id=session_id,
        new_session=new_session,
        question=question,
        answer=answer,
        ended_answer=ended,
        fixed=plan.kind == "fixed_reply",
        grounded=plan.grounded,
        citations=list(plan.citations),
        history_excluded=plan.history_excluded,
        unclear=plan.unclear,
        review_reason=plan.review_reason,
        review_kind=plan.review_kind,
        question_hash=question_hash(question.text),
        product_line=product,
        topic=topic,
        entities=entities,
        shadowed=ctx.request.shadowed,
    )


def _review_exists(db: Any, job: PendingTurn, day_start: str) -> bool:
    row = db.query_one(
        "SELECT 1 FROM review_queue r JOIN sessions s ON s.id = r.session_id "
        "WHERE s.user_id = ? AND r.kind = ? AND r.created_at >= ? "
        "AND r.details LIKE ? AND r.details LIKE ?",
        (
            job.user_id,
            job.review_kind,
            day_start,
            f'%"question_hash": "{job.question_hash}"%',
            f'%"reason": "{job.review_reason}"%',
        ),
    )
    return row is not None


def _write_review(db: Any, job: PendingTurn, now: datetime) -> None:
    day_start = utc_text(now.replace(hour=0, minute=0, second=0, microsecond=0))
    with db.transaction(write=True):
        if _review_exists(db, job, day_start):
            return
        details = {
            "reason": job.review_reason,
            "question_hash": job.question_hash,
            "question": job.question.text,
        }
        db.execute(
            "INSERT INTO review_queue "
            "(id, kind, status, session_id, message_id, details, created_at) "
            "VALUES (?, ?, 'open', ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                job.review_kind,
                job.session_id,
                job.assistant_message_id,
                json.dumps(details, sort_keys=True),
                utc_text(now),
            ),
        )


STEPS = ("session", "unclear", "user", "assistant", "facts", "review")
WRITE_ATTEMPTS = store.MAX_SEQ_RETRIES


def _write_in_one_transaction(db: Any, job: PendingTurn, now: datetime) -> tuple[bool, str, str]:
    """The whole turn in ONE transaction: the session (when it is new), the unclear-turn count, the
    user message, the answer, the session facts and the review row. One commit instead of one per
    step, no second look at a session that was just checked, and the answer's number is the one
    after the question's. Returns (the unclear turn ended the chat, the answer's message id, the
    session id). Nothing is changed on the job here: a failure rolls everything back."""
    with db.transaction(write=True):
        sid = job.session_id
        checked = False  # has this transaction already seen that the session is the user's?
        if job.new_session or sid is None:
            sid = store.create_session(db, job.user_id, now=now, session_id=job.session_id)
            checked = True
        ended = False
        if job.unclear is not None:
            outcome = record_turn(db, job.user_id, sid, unclear=job.unclear, now=now)
            ended = bool(outcome.ended and job.ended_answer is not None)
            checked = True
        question = store.add_user_message(
            db,
            job.user_id,
            sid,
            job.question,
            now=now,
            history_excluded=job.history_excluded,
            owned=checked,
        )
        follow = {"seq": question.seq + 1, "owned": True}
        if ended and job.ended_answer is not None:
            message = store.add_fixed_reply(
                db, job.user_id, sid, job.ended_answer, now=now, shadowed=job.shadowed, **follow
            )
        elif job.fixed:
            message = store.add_fixed_reply(
                db, job.user_id, sid, job.answer, now=now, shadowed=job.shadowed, **follow
            )
        else:
            message = store.add_assistant_message(
                db,
                job.user_id,
                sid,
                job.answer,
                citations=job.citations,
                grounded=job.grounded,
                now=now,
                shadowed=job.shadowed,
                **follow,
            )
        if job.product_line or job.topic or job.entities:
            update_facts(
                db,
                job.user_id,
                sid,
                product_line=job.product_line,
                topic=job.topic,
                entities=job.entities,
            )
        if job.review_reason:
            # the review row is linked to the stored reply (both are undone if the write fails)
            job.session_id, job.assistant_message_id = sid, message.id
            _write_review(db, job, now)
    return ended, message.id, sid


def write_turn(db: Any, job: PendingTurn, now: datetime) -> bool:
    """Write the turn in one transaction. Returns True when this unclear turn ended the exchange
    (the stored reply is then the fixed ending). All or nothing: a failure leaves no half-written
    turn, so a retry starts clean and nothing is ever written twice. Another writer taking the
    next message number (Postgres) makes the whole write retry after a short random wait."""
    with job.lock:
        if "committed" in job.done:
            return job.ended
        before = (job.session_id, job.assistant_message_id)
        for attempt in range(WRITE_ATTEMPTS):
            try:
                ended, message_id, session_id = _write_in_one_transaction(db, job, now)
            except Exception as error:
                job.session_id, job.assistant_message_id = before  # undone with the rollback
                if store.is_unique_violation(error) and attempt + 1 < WRITE_ATTEMPTS:
                    time.sleep(random.uniform(0, store.SEQ_BACKOFF_SECONDS * (attempt + 1)))
                    continue
                raise
            job.ended, job.assistant_message_id, job.session_id = ended, message_id, session_id
            job.done.update(STEPS)
            job.done.add("committed")
            return job.ended
        return job.ended  # not reached: the last attempt raises


class PersistQueue:
    """A bounded in-process queue of turns that could not be written (Postgres down)."""

    def __init__(self, size: int = 200) -> None:
        self._jobs: deque[PendingTurn] = deque()
        self._size = size
        self._mutex = threading.Lock()

    def add(self, job: PendingTurn) -> bool:
        """Queue a job. True if the oldest one had to be dropped to make room."""
        with self._mutex:
            dropped = len(self._jobs) >= self._size
            if dropped:
                self._jobs.popleft()
            self._jobs.append(job)
            return dropped

    def has_session(self, user_id: str, session_id: str) -> bool:
        """True when a turn of this user's conversation is waiting to be written (so the
        conversation exists for the user even though its row is not in the database yet)."""
        with self._mutex:
            return any(j.user_id == user_id and j.session_id == session_id for j in self._jobs)

    def _remove(self, job: PendingTurn) -> None:
        with self._mutex:
            try:
                self._jobs.remove(job)
            except ValueError:  # already written or dropped by someone else
                pass

    def flush(
        self,
        db: Any,
        now: datetime,
        *,
        max_attempts: int = 5,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> int:
        """Try the queued turns, oldest first. Returns how many were written.

        - The database does not answer (a probe query fails): stop, nothing is counted, the jobs
          stay for the next request. An outage never costs a job an attempt.
        - The database answers but this job still fails (its session is gone, a bad row): count
          the attempt, carry on with the next job, and set a job aside ("dead letter", an alert
          event with the exception type name only) after `max_attempts`, so that it cannot block
          the queue.
        - Within one session the order is kept: once a job of a session failed in this pass, the
          later jobs of that session wait for the next pass.
        """
        written = 0
        blocked: set[tuple[str, str | None]] = set()
        with self._mutex:
            pending = list(self._jobs)
        for job in pending:
            key = (job.user_id, job.session_id)
            if key in blocked:
                continue
            try:
                write_turn(db, job, now)
            except Exception as error:
                try:
                    db.query_one("SELECT 1")
                except Exception:  # an outage: stop here and keep every job
                    return written
                job.attempts += 1
                if job.session_id is not None:
                    blocked.add(key)
                if job.attempts >= max_attempts:
                    self._remove(job)
                    if on_event:
                        on_event("persist_job_dead_lettered", {"reason": type(error).__name__})
                continue
            self._remove(job)
            written += 1
        return written

    def __len__(self) -> int:
        with self._mutex:
            return len(self._jobs)


_WRITER = ThreadPoolExecutor(max_workers=8, thread_name_prefix="persist")


def _queue(ctx: PipelineState, job: PendingTurn, reason: str, span: Span) -> None:
    """Put a turn that could not be written into the bounded queue, with the alert events."""
    dropped = ctx.deps.queue.add(job)
    span.status = "error"
    span.error_code = reason
    ctx.emit("persist_queued", reason=reason, queued=len(ctx.deps.queue))
    if dropped:
        ctx.emit("persist_queue_overflow")


def queue_unwritten(ctx: PipelineState, reason: str) -> None:
    """A turn whose persist stage never ran or never started (no time left, the 30 s budget ended
    while the answer was being finished): make its masked copy and queue it, do not lose it. Does
    nothing when the stage already made the copy (the raw question is gone) or there is no plan."""
    if ctx.plan is None or ctx.raw_query is None:
        return
    try:
        job = build_job(ctx)
    except Exception as error:  # cannot make the masked copy: store nothing (fail closed)
        ctx.drop_raw()
        ctx.emit("persist_masking_failed", reason=type(error).__name__)
        return
    ctx.stored_copy = (job.question.text, job.answer.text)
    ctx.drop_raw()
    if ctx.result is not None:
        ctx.result.session_id = job.session_id  # the user keeps one conversation id
    dropped = ctx.deps.queue.add(job)
    ctx.emit("persist_queued", reason=reason, queued=len(ctx.deps.queue))
    if dropped:
        ctx.emit("persist_queue_overflow")


async def persist_stage(ctx: PipelineState, span: Span, timeout_s: float) -> None:
    """Stage 16. Never fails the request: a failed write is queued and an alert event is emitted."""
    if ctx.plan is None or ctx.raw_query is None:
        span.status = "skipped"
        return
    try:
        job = build_job(ctx)
    except Exception as error:  # cannot make the masked copy: store nothing (fail closed)
        ctx.drop_raw()
        span.status = "error"
        span.error_code = type(error).__name__
        ctx.emit("persist_masking_failed", reason=type(error).__name__)
        return
    ctx.stored_copy = (job.question.text, job.answer.text)
    ctx.drop_raw()  # the raw question is not needed any more
    now = ctx.deps.now()
    if ctx.result is not None:
        # before the write: a write that fails, is late or is cancelled still leaves the answer
        # with its conversation id, so the next question joins this chat
        ctx.result.session_id = job.session_id
    # The write runs in a worker thread that a timeout cannot stop. It is tracked so that the
    # session stays locked until it has ended (the answer is still returned at once).
    future = _WRITER.submit(write_turn, ctx.db, job, now)
    ctx.writes.track(future)
    try:
        ended = await asyncio.wait_for(  # a little under the node's own limit, so this one fires
            asyncio.wrap_future(future), timeout=max(timeout_s * 0.9, 0.05)
        )
    except asyncio.CancelledError:  # the 30 s budget ended or the caller went away: do not lose it
        _queue(ctx, job, "cancelled", span)
        raise
    except Exception as error:  # Postgres down, a timeout, a bad row: queue it
        _queue(ctx, job, type(error).__name__, span)
        return
    if ctx.result is not None:
        ctx.result.session_id = job.session_id
        ctx.result.message_id = job.assistant_message_id
        if ended:
            ctx.result.text = UNCLEAR_END
            ctx.result.reason = "two_unclear_turns"
