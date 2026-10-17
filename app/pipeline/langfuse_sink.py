"""The Langfuse sink: the per-stage trace of every request leaves through the Langfuse SDK
(spec 14, Change round 1 step 5).

WHAT IS SENT. For every request one trace, and in it one observation per stage, typed the way
Langfuse draws them (retriever, embedding, guardrail, generation, span), each with its INPUT, its
OUTPUT and its METADATA (`app.pipeline.langfuse_content` says exactly what goes in each):
- metadata: stage name, start and end time, how it ended, decider, model and provider names, the key
  FINGERPRINT (never a key), token counts, billed and shadow cost, the branch / exit name, flags,
  the prompt hashes, the prompt-set version, the corpus version, and errors by exception TYPE NAME
  only. Every metadata value passes `clean`: an identifier, a hash, a number or a fixed word; a
  sentence never passes;
- input and output: the question and the answer as the chat store holds them (the pass-2 MASKED
  copies, `ctx.stored_copy`; withheld when they still look like personal data), the retrieved
  passages and the answer prompt's instructions (public documents and our own prompt file), and
  numbers and fixed words. NEVER the raw question, the pass-1 copy the models get, a rewritten
  question, history, a summary, a username, an e-mail address, a user id or a session id. The user
  and the session are identified only by keyed hashes (HMAC-SHA256 with `LANGFUSE_USER_HASH_KEY`,
  a key of its own). `LANGFUSE_SEND_TEXT=false` sends no input or output at all.

HOW. The Langfuse Python SDK (4.x) with NO decorator, NO integration and NO automatic capture:
nothing is instrumented in this process, the SDK is given a tracer provider of its own (the global
one is never touched) and an export filter that lets only its own spans out, and every field is set
by our code from the finished request. It sends OpenTelemetry spans to Langfuse (the legacy
ingestion API it replaces stops on Langfuse Cloud on 2026-11-16). Each span gets the start and end
time the request recorded, so the waterfall in Langfuse is the real one.

OFF BY DEFAULT. `sink_from_env` returns None unless `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
`LANGFUSE_HOST` are all set; a placeholder value, a missing or reused `LANGFUSE_USER_HASH_KEY` or an
insecure host is refused (`SinkConfigError`, naming the variable, never a value). With no sink the
pipeline behaves exactly the same, and the SDK is never imported.

A FAILURE CAN NEVER AFFECT AN ANSWER. The sink only ever sees a request after its result is final
(so it is outside the 30 s budget); `submit_request` puts one small record on a bounded queue
without waiting and drops it if the queue is full; a background thread hands records to the SDK,
which sends them in its own threads with a short timeout; every failure (ours or the SDK's, seen
through its error log) is an alert event with the exception's type name, at most once a minute;
nothing here ever raises into the graph.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.accounts.audit import keyed_hash
from app.api.settings import SettingsError, check_secret, keys_are_related
from app.isolation import is_local_url
from app.pipeline import langfuse_content as content
from app.pipeline.trace import ALIGNED_FIELDS, aligned_list

ENV_PUBLIC = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET = "LANGFUSE_SECRET_KEY"
ENV_HOST = "LANGFUSE_HOST"
ENV_USER_KEY = "LANGFUSE_USER_HASH_KEY"
ENV_SEND_TEXT = "LANGFUSE_SEND_TEXT"  # "false": no input or output text at all (default: true)
ENV_NAMES = (ENV_PUBLIC, ENV_SECRET, ENV_HOST, ENV_USER_KEY)
OTHER_KEYS = (
    "JWT_SECRET_KEY",
    "AUDIT_HASH_KEY",
)  # the user hash key must not equal or build on these

# requests waiting to be handed over; a full queue drops the NEW one (the oldest are not kept)
QUEUE_SIZE = 200
BATCH_REQUESTS = 20  # most requests handed over in one pass
SEND_TIMEOUT_SECONDS = 3  # a short timeout: a slow Langfuse is given up on
FLUSH_AT_SPANS = 64  # the SDK sends a batch when this many spans are waiting ...
FLUSH_INTERVAL_SECONDS = 2.0  # ... or this often
EVENT_INTERVAL_SECONDS = 60.0  # one failure alert at most this often
POLL_SECONDS = 0.5
MAX_STRING = 20_000  # the longest text value handed to the SDK
MAX_DEPTH = 6

# The loggers in which the SDK and the OpenTelemetry exporter report a failed delivery.
EXPORT_LOGGERS = (
    "langfuse",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.sdk.trace.export",
    "opentelemetry.sdk._shared_internal",
)

_PLACEHOLDERS = (
    "your", "change", "replace", "example", "placeholder", "insert", "paste", "todo", "xxx",
    "<", "${", "none", "null", "test", "secret", "password",
)  # fmt: skip
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/()+-]{0,79}$")  # one word: id, hash, name, code
_PHRASE = re.compile(
    r"^[A-Za-z0-9._:/()+ -]{1,60}$"
)  # at most three short words (a clause, a name)
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
STAGES = frozenset(
    "authenticate local_scan triage injection_decision rewrite domain_gate cache_lookup embed "
    "retrieve rerank context_guard confidence generate output_guard cache_write persist "
    "respond summary_fold".split()
)
SPAN_EXTRA = frozenset(
    "candidates filters reranker kept cache stored schema_retry action detector out_of_scope "
    "triage_failed tokens elapsed_ms t_limits t_greeting t_injection t_masking decider".split()
)
TRACE_AUDIT = frozenset(
    "decider in_domain injection_score injection_action injection_detector cache_status "
    "corpus_version "
    "filters_applied context_kept context_dropped reranker best_score low_confidence prompt_answer "
    "prompt_rewrite prompt_set grounded numeric_ok citation_ok coverage cacheable "
    "chunk_ids content_hashes clause_refs".split()
)


class SinkConfigError(ValueError):
    """The sink cannot be enabled. The message names the variable and the rule, never a value."""


# ---- what may be recorded
# -----------------------------------------------------------------------------
def clean(value: Any) -> Any:
    """The value if it is a number, a flag, a short identifier / hash / fixed word, or a short list
    of those; otherwise None. A sentence never passes."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        text = value.strip()
        if _IDENT.match(text) or (len(text.split()) <= 3 and _PHRASE.match(text)):
            return text
        return None
    if isinstance(value, (list, tuple)):
        items = [clean(v) for v in list(value)[:30]]
        return (
            items
            if all(i is not None or v is None for i, v in zip(items, value, strict=False))
            else None
        )
    return None


def ident(value: Any, fallback: str | None = None) -> str | None:
    cleaned = clean(value)
    return cleaned if isinstance(cleaned, str) else fallback


def drop_empty(values: Mapping[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        cleaned = clean(value)
        if cleaned is not None:
            out[key] = cleaned
    return out


def plain_json(value: Any, depth: int = 0) -> Any:
    """The value as plain JSON data (text cut to `MAX_STRING`, a number or a flag as it is, a list
    or a mapping made of the same, anything else dropped). The input and output of an observation
    pass through here: their content is decided by `langfuse_content`, this only makes sure that
    nothing but plain data is handed to the SDK."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + " [cut]"
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, (list, tuple)):
        return [plain_json(v, depth + 1) for v in list(value)[:100]]
    if isinstance(value, Mapping):
        return {str(k)[:80]: plain_json(v, depth + 1) for k, v in list(value.items())[:100]}
    return None


@dataclass
class ChildRecord:
    """An observation inside a stage that has no timing of its own (it is shown at the stage's
    start, with no duration)."""

    name: str
    kind: str
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SpanRecord:
    name: str
    start: datetime
    end: datetime
    level: str
    status_message: str | None
    metadata: dict[str, Any]
    kind: str = "span"  # the Langfuse observation type
    input: Any = None
    output: Any = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_billed: float | None = None
    shadow_cost: float = 0.0
    children: list[ChildRecord] = field(default_factory=list)


@dataclass
class TraceRecord:
    trace_id: str
    started_at: datetime
    user_hash: str
    session_hash: str | None
    tags: list[str]
    metadata: dict[str, Any]
    spans: list[SpanRecord] = field(default_factory=list)
    input: Any = None
    output: Any = None
    level: str = "DEFAULT"
    status_message: str | None = None

    @property
    def ended_at(self) -> datetime:
        return max([self.started_at] + [s.end for s in self.spans])


def _level(status: str) -> str:
    return "ERROR" if status in ("error", "timeout") else "DEFAULT"


def build_record(ctx: Any, result: Any, user_key: str, send_text: bool = False) -> TraceRecord:
    """Copy the allow-listed fields of a finished request into plain values, and the content of
    each stage (`langfuse_content`). `ctx` is the pipeline state and `result` its `Result`; nothing
    else about them is read. With `send_text` False no input or output text is made at all."""
    trace = ctx.trace
    started = trace.started_at or ctx.deps.now()
    texts = content.make_texts(ctx, send_text)
    spans: list[SpanRecord] = []
    for span in trace.spans:
        begin = started + timedelta(milliseconds=float(span.started_ms))
        metadata = drop_empty(
            {
                "status": span.status,
                "decider": span.decider,
                "provider": span.provider,
                "key_fingerprint": span.key_fingerprint,
                "cost_billed": span.cost_billed,
                "shadow_cost": span.shadow_cost,
                "input_tokens": span.input_tokens,
                "output_tokens": span.output_tokens,
            }
        )
        metadata.update(drop_empty({k: v for k, v in span.extra.items() if k in SPAN_EXTRA}))
        name = span.stage if span.stage in STAGES else "stage"
        made = content.stage_content(name, ctx, result, texts)
        metadata.update(drop_empty(made.metadata))
        kind = made.kind if (span.model or made.kind not in ("generation", "embedding")) else "span"
        spans.append(
            SpanRecord(
                name=name,
                start=begin,
                end=begin + timedelta(milliseconds=float(span.duration_ms)),
                level=_level(span.status),
                status_message=ident(span.error_code),
                metadata=metadata,
                kind=kind,
                input=plain_json(made.input) if send_text else None,
                output=plain_json(made.output) if send_text else None,
                model=ident(span.model),
                input_tokens=int(span.input_tokens or 0),
                output_tokens=int(span.output_tokens or 0),
                cost_billed=span.cost_billed,
                shadow_cost=float(span.shadow_cost or 0.0),
                children=[
                    ChildRecord(
                        child.name,
                        child.kind,
                        input=plain_json(child.input) if send_text else None,
                        output=plain_json(child.output) if send_text else None,
                        metadata=drop_empty(child.metadata),
                    )
                    for child in made.children
                ],
            )
        )
    audit = {k: v for k, v in trace.audit.items() if k in TRACE_AUDIT and k not in ALIGNED_FIELDS}
    # the aligned lists are checked again here, entry by entry (an empty entry is a placeholder);
    # a list with no real entry at all is left out
    aligned = {
        name: aligned_list(name, trace.audit.get(name))
        for name in ALIGNED_FIELDS
        if isinstance(trace.audit.get(name), (list, tuple))
    }
    aligned = {name: values for name, values in aligned.items() if any(values)}
    billed = sum(float(s.cost_billed or 0.0) for s in trace.spans)
    shadow = sum(float(s.shadow_cost or 0.0) for s in trace.spans)
    flags = {
        "cache_hit": bool(result.cached),
        "degraded": bool(result.degraded),
        "low_confidence": result.confidence == "low",
        "refused": result.kind == "refusal",
        "warning": bool(result.warnings),
    }
    metadata = drop_empty(
        {
            **audit,
            **flags,
            "kind": result.kind,
            "reason": result.reason,
            "branch": ctx.exited or "answer",
            "degraded_modes": list(result.degraded),
            "warning_count": len(result.warnings),
            "citation_count": len(result.citations),
            "total_tokens": result.tokens,
            "cost_billed": billed,
            "cost_shadow": shadow,
            "stages": len(spans),
        }
    )
    metadata.update(aligned)
    root_input, root_output = content.trace_content(ctx, result, texts)
    tags = [ident(result.kind, "unknown") or "unknown"] + [name for name, on in flags.items() if on]
    failed = next((s for s in spans if s.level == "ERROR"), None)
    return TraceRecord(
        trace_id=ident(ctx.request.correlation_id) or uuid.uuid4().hex,
        started_at=started,
        user_hash=keyed_hash(user_key, "langfuse-user", ctx.request.user_id),
        session_hash=(
            keyed_hash(user_key, "langfuse-session", result.session_id)
            if result.session_id
            else None
        ),
        tags=tags,
        metadata=metadata,
        spans=spans,
        input=plain_json(root_input) if send_text else None,
        output=plain_json(root_output) if send_text else None,
        level="ERROR" if failed is not None and result.kind == "unavailable" else "DEFAULT",
        status_message=failed.status_message if failed is not None else None,
    )


# ---- sending
# ------------------------------------------------------------------------------------------
class Exporter(Protocol):
    def send(self, records: list[TraceRecord]) -> None:
        """Hand a batch of requests over. Raises on a failure of its own."""


@dataclass(frozen=True)
class LangfuseConfig:
    host: str
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    user_hash_key: str = field(repr=False)
    send_text: bool = True


def _nanoseconds(moment: datetime) -> int:
    return int(moment.astimezone(UTC).timestamp() * 1_000_000_000)


def _mask(data: Any = None, **_ignored: Any) -> Any:
    """The SDK's mask hook: whatever our code puts on an observation is made plain data first."""
    return plain_json(data)


class ExportWatch(logging.Handler):
    """Hears the SDK and the exporter report a failed delivery (they log it; they do not raise).
    Only the level and the exception's class name are used, never the message: it can hold text of
    the server's answer."""

    def __init__(self, on_failure: Callable[[str], None]) -> None:
        super().__init__(level=logging.ERROR)
        self._on_failure = on_failure

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # a log handler must never raise
            error = record.exc_info[1] if record.exc_info else None
            self._on_failure(type(error).__name__ if error is not None else "ExportFailed")


class SdkExporter:
    """Turns a finished request into Langfuse observations with the SDK, with the times the request
    recorded. Everything is set explicitly; no decorator, no integration, no automatic capture."""

    def __init__(
        self,
        config: LangfuseConfig,
        *,
        span_exporter: Any = None,
    ) -> None:
        # imported here, so a service with the sink off never loads the SDK
        from langfuse import Langfuse, is_langfuse_span
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider

        self._provider = TracerProvider(
            resource=Resource.create({"service.name": "bfsi-assistant"})
        )
        self._client = Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            base_url=config.host,
            timeout=SEND_TIMEOUT_SECONDS,
            tracer_provider=self._provider,  # our own: the global provider is never touched
            span_exporter=span_exporter,
            should_export_span=is_langfuse_span,  # only the SDK's own spans leave this process
            flush_at=FLUSH_AT_SPANS,
            flush_interval=FLUSH_INTERVAL_SECONDS,
            mask=_mask,
            media_upload_thread_count=1,
        )
        self._tracer = self._provider.get_tracer(
            "langfuse-sdk", attributes={"public_key": config.public_key}
        )
        self._watch: ExportWatch | None = None

    def watch(self, on_failure: Callable[[str], None]) -> None:
        """Call `on_failure(reason)` when the SDK or the exporter reports a failed delivery. The
        reason is a class name or a fixed word, never the message of the log line."""
        if self._watch is not None:
            return
        self._watch = ExportWatch(on_failure)
        for name in EXPORT_LOGGERS:
            logging.getLogger(name).addHandler(self._watch)

    # ---- the request side ----
    def send(self, records: list[TraceRecord]) -> None:
        first_error: Exception | None = None
        for record in records:
            try:
                self._send_one(record)
            except Exception as error:  # one request failing must not cost the others
                first_error = first_error or error
        if first_error is not None:
            raise first_error

    def _observation(self, kind: str, otel_span: Any, **fields: Any) -> Any:
        import langfuse

        classes = {
            "span": langfuse.LangfuseSpan,
            "generation": langfuse.LangfuseGeneration,
            "embedding": langfuse.LangfuseEmbedding,
            "retriever": langfuse.LangfuseRetriever,
            "guardrail": langfuse.LangfuseGuardrail,
            "chain": langfuse.LangfuseChain,
        }
        cls = classes.get(kind, langfuse.LangfuseSpan)
        if cls not in (langfuse.LangfuseGeneration, langfuse.LangfuseEmbedding):
            for name in ("model", "usage_details", "cost_details"):
                fields.pop(name, None)
        return cls(otel_span=otel_span, langfuse_client=self._client, **fields)

    def _send_one(self, record: TraceRecord) -> None:
        from langfuse import LangfuseOtelSpanAttributes as A
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

        trace_id = (
            int(record.trace_id, 16)
            if _TRACE_ID.match(record.trace_id)
            else RandomIdGenerator().generate_trace_id()
        )
        remote_parent = otel_trace.NonRecordingSpan(
            otel_trace.SpanContext(
                trace_id=trace_id,
                span_id=RandomIdGenerator().generate_span_id(),
                is_remote=False,
                trace_flags=otel_trace.TraceFlags(0x01),
            )
        )
        shared: dict[str, Any] = {A.TRACE_USER_ID: record.user_hash, A.TRACE_TAGS: record.tags}
        if record.session_hash:
            shared[A.TRACE_SESSION_ID] = record.session_hash

        def start(name: str, parent: Any, moment: datetime) -> Any:
            otel_span = self._tracer.start_span(
                name,
                context=otel_trace.set_span_in_context(parent),
                start_time=_nanoseconds(moment),
            )
            otel_span.set_attributes(shared)
            return otel_span

        root_otel = start("query", remote_parent, record.started_at)
        root_otel.set_attribute(A.AS_ROOT, True)
        root_otel.set_attribute(A.TRACE_NAME, "query")
        for key, value in record.metadata.items():
            root_otel.set_attribute(
                f"{A.TRACE_METADATA}.{key}", value if isinstance(value, str) else json.dumps(value)
            )
        if record.input is not None:
            root_otel.set_attribute(A.TRACE_INPUT, json.dumps(record.input))
        if record.output is not None:
            root_otel.set_attribute(A.TRACE_OUTPUT, json.dumps(record.output))
        root = self._observation(
            "span",
            root_otel,
            input=record.input,
            output=record.output,
            metadata=record.metadata,
            level=record.level,
            status_message=record.status_message,
        )
        for span in record.spans:
            otel_span = start(span.name, root_otel, span.start)
            usage = (
                {
                    "input": span.input_tokens,
                    "output": span.output_tokens,
                    "total": span.input_tokens + span.output_tokens,
                }
                if span.model
                else None
            )
            cost = {"total": float(span.cost_billed)} if span.cost_billed is not None else None
            observation = self._observation(
                span.kind,
                otel_span,
                input=span.input,
                output=span.output,
                metadata={**span.metadata, "shadow_cost_usd": span.shadow_cost}
                if span.shadow_cost
                else span.metadata,
                level=span.level,
                status_message=span.status_message,
                model=span.model,
                usage_details=usage,
                cost_details=cost,
            )
            for child in span.children:
                child_otel = start(child.name, otel_span, span.start)
                self._observation(
                    child.kind,
                    child_otel,
                    input=child.input,
                    output=child.output,
                    metadata=child.metadata,
                ).end(end_time=_nanoseconds(span.start))
            observation.end(end_time=_nanoseconds(span.end))
        root.end(end_time=_nanoseconds(record.ended_at))

    # ---- the end ----
    def flush(self) -> None:
        self._client.flush()

    def close(self) -> None:
        try:
            self._client.flush()
            self._client.shutdown()
        finally:
            if self._watch is not None:
                for name in EXPORT_LOGGERS:
                    logging.getLogger(name).removeHandler(self._watch)
                self._watch = None


def _placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return len(lowered) < 12 or lowered.startswith(_PLACEHOLDERS) or lowered in _PLACEHOLDERS


def config_from_env(
    env: Mapping[str, str], on_event: Callable[[str, dict[str, Any]], None] | None = None
) -> LangfuseConfig | None:
    """None (the sink stays off) unless public key, secret key and host are ALL set. When they are,
    a placeholder, an insecure host, or a missing, weak or reused `LANGFUSE_USER_HASH_KEY` raises
    `SinkConfigError` naming the variable (never a value)."""
    values = {name: (env.get(name) or "").strip() for name in ENV_NAMES}
    base = (ENV_PUBLIC, ENV_SECRET, ENV_HOST)
    missing = [name for name in base if not values[name]]
    if len(missing) == len(base):
        return None
    if missing:
        if on_event:
            on_event("langfuse_disabled", {"missing": missing})
        return None
    for name in (ENV_PUBLIC, ENV_SECRET):
        if _placeholder(values[name]):
            raise SinkConfigError(f"{name} is a placeholder or too short")
    host = values[ENV_HOST]
    if not (host.startswith("https://") or (host.startswith("http://") and is_local_url(host))):
        raise SinkConfigError(
            f"{ENV_HOST} must start with https:// (http:// only for this machine)"
        )
    if _placeholder(host.split("://", 1)[1]):
        raise SinkConfigError(f"{ENV_HOST} is a placeholder")
    user_key = values[ENV_USER_KEY]
    if not user_key:
        raise SinkConfigError(
            f"{ENV_USER_KEY} is not set: the user is only ever sent as a keyed hash"
        )
    try:
        check_secret(user_key, ENV_USER_KEY)
    except SettingsError as error:
        raise SinkConfigError(str(error)) from None
    others = {name: (env.get(name) or "").strip() for name in OTHER_KEYS}
    others[ENV_PUBLIC], others[ENV_SECRET] = values[ENV_PUBLIC], values[ENV_SECRET]
    for name, other in others.items():
        if other and keys_are_related(user_key, other):
            raise SinkConfigError(f"{ENV_USER_KEY} must be a different key from {name}")
    send_text = (env.get(ENV_SEND_TEXT) or "").strip().lower()
    if send_text not in ("", "true", "false"):
        raise SinkConfigError(f"{ENV_SEND_TEXT} must be true or false")
    return LangfuseConfig(
        host, values[ENV_PUBLIC], values[ENV_SECRET], user_key, send_text != "false"
    )


# ---- the sink
# -----------------------------------------------------------------------------------------
class LangfuseSink:
    """A bounded queue and one background thread. `submit_request` never blocks and never raises."""

    def __init__(
        self,
        exporter: Exporter,
        user_hash_key: str,
        *,
        send_text: bool = True,
        queue_size: int = QUEUE_SIZE,
        batch_requests: int = BATCH_REQUESTS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exporter = exporter
        watch = getattr(exporter, "watch", None)
        if callable(watch):  # the SDK reports a failed delivery in its log, not as an error
            watch(self.export_failed)
        self._user_key = user_hash_key
        self._send_text = send_text
        self._queue: queue.Queue[TraceRecord] = queue.Queue(maxsize=queue_size)
        self._batch = max(1, batch_requests)
        self._on_event = on_event
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self._last_event: dict[str, float] = {}
        self.sent_batches = 0
        self.failed_batches = 0
        self.dropped = 0
        self.last_error_type: str | None = None

    # ---- the request side ----
    def submit_request(self, ctx: Any, result: Any) -> bool:
        """Queue a finished request. True if queued. Never raises, never waits."""
        try:
            record = build_record(ctx, result, self._user_key, self._send_text)
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            self._alert("langfuse_dropped", dropped=self.dropped)
        except Exception as error:  # a bug here must never reach the answer
            self._alert("langfuse_failed", reason=type(error).__name__)
        return False

    def export_failed(self, reason: str) -> None:
        """The SDK or the exporter reported a failed delivery (called from their threads)."""
        self.failed_batches += 1
        self.last_error_type = reason
        self._alert("langfuse_failed", reason=reason)

    def _alert(self, name: str, **fields: Any) -> None:
        """An alert event, at most once a minute per kind, with names and counts only."""
        now = self._clock()
        last = self._last_event.get(name)
        if last is not None and now - last < EVENT_INTERVAL_SECONDS:
            return
        self._last_event[name] = now
        if self._on_event:
            try:
                self._on_event(name, fields)
            except Exception:
                pass

    # ---- the background side ----
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="langfuse-sink", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            self._busy = True
            records = [first]
            while len(records) < self._batch:
                try:
                    records.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                self._exporter.send(records)
                self.sent_batches += 1
            except Exception as error:  # slow, down, refused: an alert, nothing else
                self.failed_batches += 1
                self.last_error_type = type(error).__name__
                self._alert("langfuse_failed", reason=type(error).__name__)
            finally:
                for _ in records:
                    self._queue.task_done()
                self._busy = False

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until all queued records are handed over, then ask the SDK to send what it holds
        (shutdown and the smoke test)."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self._queue.unfinished_tasks == 0:
                break
            time.sleep(0.01)
        done = self._queue.unfinished_tasks == 0
        flush = getattr(self._exporter, "flush", None)
        if done and callable(flush):
            try:
                flush()
            except Exception as error:
                self.failed_batches += 1
                self.last_error_type = type(error).__name__
                return False
        return done

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        close = getattr(self._exporter, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                self.last_error_type = type(error).__name__

    @property
    def queued(self) -> int:
        return self._queue.qsize()


def sink_from_env(
    env: Mapping[str, str],
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    exporter: Exporter | None = None,
    span_exporter: Any = None,
) -> LangfuseSink | None:
    """The started sink, or None when it is off (see the module docstring). Phase 10 calls this once
    at start-up and puts the result in `Deps.langfuse`; a `SinkConfigError` means do not enable."""
    config = config_from_env(env, on_event)
    if config is None:
        return None
    if exporter is None:
        try:
            exporter = SdkExporter(config, span_exporter=span_exporter)
        except ImportError:  # the package is missing: say how to fix it, with no value in it
            raise SinkConfigError(
                "the langfuse package is not installed (run: uv pip install -r requirements.txt)"
            ) from None
        except Exception as error:  # the SDK could not start: no tracing, and an alert
            if on_event:
                on_event("langfuse_failed", {"reason": type(error).__name__})
            return None
    sink = LangfuseSink(
        exporter, config.user_hash_key, send_text=config.send_text, on_event=on_event
    )
    sink.start()
    return sink
