"""The Langfuse sink: per-stage span records leave the machine, nothing else (spec 14).

WHAT IS SENT (and only this): for every request one trace and one observation per stage:
the stage name, its start and end time, how it ended (ok, skipped, error, timeout), the decider,
model and provider names, the key FINGERPRINT (never a key), input and output token counts, billed
and shadow cost, the branch / exit name and the result kind and reason code, flags (cache hit,
degraded, low confidence, refused, warnings), the prompt hashes, the prompt-set version, the corpus
version, the number of chunks kept and dropped, and errors by exception TYPE NAME only. The user and
the session are identified only by keyed hashes (HMAC-SHA256 with `LANGFUSE_USER_HASH_KEY`, a key of
its own).

WHAT IS NEVER SENT: no question text (raw, pass-1 or pass-2), no answer text, no chunk text, no
history, no summary, no username, e-mail address, user id or session id. This is structural, not a
filter over everything: `build_record` copies a short ALLOW-LIST of fields into plain values and
every
string passes `clean` (an identifier, a hash or a fixed word; never a sentence). Nothing else of the
request or the state is ever looked at.

HOW: no SDK. The Langfuse Python SDK is built on OpenTelemetry (it can capture the inputs and
outputs
of functions, and anything else instrumented in the process), and it cannot be made to send only the
explicit fields by construction. This module uses Langfuse's public ingestion API (`POST
/api/public/ingestion`) with `httpx`, so the payload is exactly what `build_events` makes. There are
no decorators and no automatic capture.

OFF BY DEFAULT. `sink_from_env` returns None unless `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and
`LANGFUSE_HOST` are all set; a placeholder value, a missing or reused `LANGFUSE_USER_HASH_KEY` or an
insecure host is refused (`SinkConfigError`, naming the variable, never a value). With no sink the
pipeline behaves exactly the same.

A FAILURE CAN NEVER AFFECT AN ANSWER. The sink only ever sees a request after its result is final
(so it is outside the 30 s budget); `submit_request` puts one small record on a bounded queue
without
waiting and drops it if the queue is full; a background thread sends batches with a short timeout;
every failure is an alert event with the exception's type name, at most once a minute; nothing here
ever raises into the graph.
"""

from __future__ import annotations

import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from app.accounts.audit import keyed_hash
from app.api.settings import SettingsError, check_secret, keys_are_related
from app.isolation import is_local_url
from app.pipeline.trace import ALIGNED_FIELDS, aligned_list

ENV_PUBLIC = "LANGFUSE_PUBLIC_KEY"
ENV_SECRET = "LANGFUSE_SECRET_KEY"
ENV_HOST = "LANGFUSE_HOST"
ENV_USER_KEY = "LANGFUSE_USER_HASH_KEY"
ENV_NAMES = (ENV_PUBLIC, ENV_SECRET, ENV_HOST, ENV_USER_KEY)
OTHER_KEYS = (
    "JWT_SECRET_KEY",
    "AUDIT_HASH_KEY",
)  # the user hash key must not equal or build on these

INGESTION_PATH = "/api/public/ingestion"
QUEUE_SIZE = (
    200  # requests waiting to be sent; the oldest are NOT kept: a full queue drops the new one
)
BATCH_REQUESTS = 20  # most requests sent in one HTTP call
SEND_TIMEOUT_SECONDS = 3.0  # a short timeout: a slow Langfuse is given up on
EVENT_INTERVAL_SECONDS = 60.0  # one failure alert at most this often
POLL_SECONDS = 0.5

_PLACEHOLDERS = (
    "your", "change", "replace", "example", "placeholder", "insert", "paste", "todo", "xxx",
    "<", "${", "none", "null", "test", "secret", "password",
)  # fmt: skip
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/()+-]{0,79}$")  # one word: id, hash, name, code
_PHRASE = re.compile(
    r"^[A-Za-z0-9._:/()+ -]{1,60}$"
)  # at most three short words (a clause, a name)
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


class LangfuseRejected(RuntimeError):
    """Langfuse answered but refused some or all events (the message holds no server text)."""


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


@dataclass
class SpanRecord:
    name: str
    start: datetime
    end: datetime
    level: str
    status_message: str | None
    metadata: dict[str, Any]
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class TraceRecord:
    trace_id: str
    started_at: datetime
    user_hash: str
    session_hash: str | None
    tags: list[str]
    metadata: dict[str, Any]
    spans: list[SpanRecord] = field(default_factory=list)


def _level(status: str) -> str:
    return "ERROR" if status in ("error", "timeout") else "DEFAULT"


def build_record(ctx: Any, result: Any, user_key: str) -> TraceRecord:
    """Copy the allow-listed fields of a finished request into plain values. `ctx` is the pipeline
    state and `result` its `Result`; nothing else about them is read."""
    trace = ctx.trace
    started = trace.started_at or ctx.deps.now()
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
        spans.append(
            SpanRecord(
                name=span.stage if span.stage in STAGES else "stage",
                start=begin,
                end=begin + timedelta(milliseconds=float(span.duration_ms)),
                level=_level(span.status),
                status_message=ident(span.error_code),
                metadata=metadata,
                model=ident(span.model),
                input_tokens=int(span.input_tokens or 0),
                output_tokens=int(span.output_tokens or 0),
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
    tags = [ident(result.kind, "unknown") or "unknown"] + [name for name, on in flags.items() if on]
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
    )


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_events(record: TraceRecord) -> list[dict[str, Any]]:
    """The ingestion events of one request: the trace, then one observation per stage."""
    trace_body: dict[str, Any] = {
        "id": record.trace_id,
        "timestamp": _stamp(record.started_at),
        "name": "query",
        "userId": record.user_hash,
        "tags": record.tags,
        "metadata": record.metadata,
    }
    if record.session_hash:
        trace_body["sessionId"] = record.session_hash
    events = [
        {
            "id": uuid.uuid4().hex,
            "type": "trace-create",
            "timestamp": _stamp(record.started_at),
            "body": trace_body,
        }
    ]
    for span in record.spans:
        body: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "traceId": record.trace_id,
            "name": span.name,
            "startTime": _stamp(span.start),
            "endTime": _stamp(span.end),
            "level": span.level,
            "metadata": span.metadata,
        }
        if span.status_message:
            body["statusMessage"] = span.status_message
        kind = "span-create"
        if span.model:
            kind = "generation-create"
            body["model"] = span.model
            body["usage"] = {
                "input": span.input_tokens,
                "output": span.output_tokens,
                "total": span.input_tokens + span.output_tokens,
                "unit": "TOKENS",
            }
        events.append(
            {"id": uuid.uuid4().hex, "type": kind, "timestamp": _stamp(span.start), "body": body}
        )
    return events


# ---- sending
# ------------------------------------------------------------------------------------------
class Exporter(Protocol):
    def send(self, events: list[dict[str, Any]]) -> None:
        """Send one batch. Raises on any failure."""


@dataclass(frozen=True)
class LangfuseConfig:
    host: str
    public_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    user_hash_key: str = field(repr=False)


class HttpExporter:
    """POST to `<host>/api/public/ingestion` with basic auth. A short timeout; never retries."""

    def __init__(
        self,
        config: LangfuseConfig,
        *,
        timeout: float = SEND_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = config.host.rstrip("/") + INGESTION_PATH
        self._auth = (config.public_key, config.secret_key)
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def send(self, events: list[dict[str, Any]]) -> None:
        response = self._client.post(self._url, json={"batch": events}, auth=self._auth)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            return
        if isinstance(body, dict) and body.get("errors"):
            raise LangfuseRejected("some events were refused")

    def close(self) -> None:
        self._client.close()


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
    return LangfuseConfig(host, values[ENV_PUBLIC], values[ENV_SECRET], user_key)


# ---- the sink
# -----------------------------------------------------------------------------------------
class LangfuseSink:
    """A bounded queue and one background thread. `submit_request` never blocks and never raises."""

    def __init__(
        self,
        exporter: Exporter,
        user_hash_key: str,
        *,
        queue_size: int = QUEUE_SIZE,
        batch_requests: int = BATCH_REQUESTS,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exporter = exporter
        self._user_key = user_hash_key
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
            record = build_record(ctx, result, self._user_key)
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self.dropped += 1
            self._alert("langfuse_dropped", dropped=self.dropped)
        except Exception as error:  # a bug here must never reach the answer
            self._alert("langfuse_failed", reason=type(error).__name__)
        return False

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
                events = [e for record in records for e in build_events(record)]
                self._exporter.send(events)
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
        """Wait until all queued records are sent or failed (shutdown and the smoke test)."""
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def queued(self) -> int:
        return self._queue.qsize()


def sink_from_env(
    env: Mapping[str, str],
    *,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    exporter: Exporter | None = None,
) -> LangfuseSink | None:
    """The started sink, or None when it is off (see the module docstring). Phase 10 calls this once
    at start-up and puts the result in `Deps.langfuse`; a `SinkConfigError` means do not enable."""
    config = config_from_env(env, on_event)
    if config is None:
        return None
    sink = LangfuseSink(exporter or HttpExporter(config), config.user_hash_key, on_event=on_event)
    sink.start()
    return sink
