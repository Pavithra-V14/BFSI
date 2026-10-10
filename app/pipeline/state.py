"""The request, the result and the one state object that travels through the graph (spec 4).

`PipelineState` holds everything a stage reads or writes: the request, the masked copies, the
decisions, the candidates, the answer and the flags. The RAW question is only ever in
`request.query`
(hidden from `repr`) and is dropped from the state as soon as the pass-2 copy has been made.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.guardrails.context import ContextChunk
from app.guardrails.scan import ModelCopy, ScanResult
from app.pipeline.config import PipelineConfig
from app.pipeline.locks import SessionLocks, WriteGuard
from app.pipeline.startup import MaskerHolder
from app.pipeline.trace import Trace

Event = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class Request:
    user_id: str
    role: str
    query: str = field(repr=False)  # the raw question: never logged, stored or put in a span
    session_id: str | None = None  # None: a new session
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # True when this user may not see answers yet (rollout stage): the turn is generated and
    # stored for evaluation, and its assistant message is flagged `shadowed`.
    shadowed: bool = False
    # True when the caller (the chat route) already counted this request against the per-user
    # rate limit at its entry: stage 1 then skips its own count, so a chat is counted once.
    rate_counted: bool = False


@dataclass
class Result:
    """What the caller (the chat API, Phase 10) gets back. `text` is a fixed text or an answer."""

    kind: str  # answer | reply | refusal | clarify | reword | unavailable | starting_up | busy |
    # rate_limited | over_budget | session_not_found
    text: str = ""
    reason: str | None = None  # a fixed code: too_long, blocked, out_of_domain, no_context, ...
    citations: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    confidence: str | None = None  # "low" when the low-confidence branch was taken
    degraded: list[str] = field(default_factory=list)  # lexical_rerank, mock_answer ...
    disclaimer: bool = False
    cached: bool = False
    session_id: str | None = None
    correlation_id: str = ""  # recorded internally, not shown to users
    retry_after: int | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    tokens: int = 0
    audit: dict[str, Any] = field(default_factory=dict)  # audit-grade ids and hashes, no text
    # True when a transcript write that timed out is still running in its thread on the request's
    # database connection: the caller (Phase 10) must not close that connection yet.
    db_busy: bool = False
    # Call it with a function: that function runs once no transcript write of this request is
    # still running on the connection (at once when none is). The chat API closes the
    # request's connection through it.
    on_db_idle: Any = field(default=None, repr=False)


@dataclass
class Deps:
    """Everything the pipeline talks to. Built once by the application (Phase 10)."""

    gateway: Any  # app.gateway.gateway.Gateway: EVERY model call goes through it
    qdrant: Any  # a QdrantClient
    cache: Any | None  # a CacheStore, or None (no cache)
    masker: MaskerHolder
    counter: Any  # a token counter (count_text)
    usage: Any | None = None  # accounts.budget.UsageLimits: rate limit and daily budget
    config: PipelineConfig = field(default_factory=PipelineConfig)
    curated_domains: tuple[str, ...] = ()
    collection: str = "documents"
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    on_event: Event | None = None
    on_span: Callable[[Any], None] | None = None
    locks: SessionLocks = field(default_factory=SessionLocks)
    queue: Any = None  # persist.PersistQueue (made by Pipeline when not given)
    langfuse: Any | None = None  # langfuse_sink.LangfuseSink, or None: the sink is off
    memory: Any = None  # memory.config.MemoryConfig (made by Pipeline when not given)


@dataclass
class Triage:
    """The decisions of stage 3. `None` means "not known" (a failed or invalid answer)."""

    decider: str = "none"  # jev | groq | gemini | openrouter | rules | none
    in_domain_probability: float | None = None
    needs_history: float | None = None
    vague: float | None = None
    injection_score: float | None = None
    product_line: str | None = None
    product_line_probability: float | None = None
    clause_type: str | None = None
    clause_type_probability: float | None = None
    failed: bool = False  # nothing answered: in-domain, soft filters off, regex-only injection


@dataclass
class PersistPlan:
    """What stage 16 stores for an early exit or a finished answer (masked copies only)."""

    kind: str  # answer | fixed_reply
    text: str  # the assistant text to store (masked again by the storage masking function)
    history_excluded: bool = False  # the QUESTION must never feed history (blocked, refused ...)
    grounded: bool = False
    citations: list[dict[str, Any]] = field(default_factory=list)
    unclear: bool | None = None  # True: an unclear turn, False: a clear one, None: not counted
    review_reason: str | None = None  # out_of_domain | no_context | low_confidence
    review_kind: str = "refused"
    surviving_doc_ids: frozenset[str] = frozenset()
    update_facts: bool = False
    skip_unclear_message: bool = False


@dataclass
class PipelineState:
    request: Request
    db: Any
    deps: Deps
    trace: Trace
    deadline: float  # monotonic time at which the request budget ends
    started: float = 0.0  # monotonic time at which the request began
    raw_query: str | None = field(default=None, repr=False)
    values: Any = None  # retrieval.ValuesCache shared by the pipeline

    # stage 1-2
    visible_roles: tuple[str, ...] = ()
    allow: Any = None  # guardrails.allowlist.AllowList for the visible roles
    session_state: Any = None  # memory.store.SessionState, or None for a new session
    first_message: bool = True
    scan: ScanResult | None = field(default=None, repr=False)
    pass1: ModelCopy | None = field(default=None, repr=False)  # for models only

    # stage 3-6
    triage: Triage = field(default_factory=Triage)
    question: ModelCopy | None = field(
        default=None, repr=False
    )  # the question the rest of the pipeline uses (maybe rewritten)
    rewritten: bool = False
    history_used: bool = False  # a follow-up that needed its history (the domain gate is skipped)
    rewrite_failed: bool = False  # that follow-up's rewrite failed: low confidence, no soft filters
    domain_checked: bool = False

    # stage 7
    corpus_start: int | None = None
    cache_key: str | None = None
    cache_status: str = "none"  # none | hit | miss | discarded | unavailable

    # stage 8-12
    query_vector: list[float] | None = None
    candidates: list[ContextChunk] = field(default_factory=list, repr=False)  # chunk text
    filters_applied: list[str] = field(default_factory=list)
    soft_applied: dict[str, str] = field(default_factory=dict)  # the soft filters of the search
    soft_retried: bool = False  # the search was repeated once without the soft filters
    reranker: str | None = None
    degraded: list[str] = field(default_factory=list)
    kept: list[ContextChunk] = field(default_factory=list, repr=False)  # chunk text
    labels: dict[str, str] = field(default_factory=dict, repr=False)
    block: str = field(default="", repr=False)  # the chunk text of the prompt
    best_score: float | None = None
    low_confidence: bool = False

    # stage 13-15
    generation: Any = field(default=None, repr=False)  # the model's answer
    verdict: Any = field(default=None, repr=False)  # the checked answer
    answer_text: str = field(default="", repr=False)
    disclaimer_shown: bool = False
    cacheable: bool = False

    # stage 16-17
    plan: PersistPlan | None = field(default=None, repr=False)  # the text to store
    result: Result | None = field(default=None, repr=False)  # the answer text
    writes: WriteGuard = field(default_factory=WriteGuard, repr=False)
    tokens: int = 0
    exited: str | None = None  # the stage that ended the request early, for the trace

    def time_left(self) -> float:
        return self.deadline - self.deps.clock()

    def emit(self, name: str, **fields: Any) -> None:
        if self.deps.on_event:
            try:
                self.deps.on_event(name, fields)
            except Exception:  # an alert hook must never break a request
                pass

    def drop_raw(self) -> None:
        self.raw_query = None
