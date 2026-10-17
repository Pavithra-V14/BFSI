"""Running the whole system on the evaluation cases and scoring what comes back (spec 15.4).

`Harness` asks the real pipeline one case at a time (the question, or the turns of a short
conversation), applies and always undoes the case's changes to the TEST collection, and returns the
stored shape of what happened. `RunExecutor` works through a confirmed run: checkpoint after every
case, the cost guard counted after every call, one batch per UTC day, and a stop (not a crash)
when the run goes more than 25 % over its estimate.

What a run never does, by construction:
- start without the confirmation code of its estimate (`start_run`);
- read the production collection (`check_eval_collection`), a production budget or limit (the
  gateway of a run is built from the EVALUATION keys only, `GatewayConfig.for_evaluation`, and the
  pipeline has no usage limiter) or the production cache (there is none);
- use a database other than its own scratch SQLite file for sessions and answers.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.db.connection import Database, connect
from app.db.migrate import run as run_migrations
from app.eval import checks, store
from app.eval import constants as c
from app.eval.cases import Case
from app.eval.estimate import estimate_run
from app.eval.guard import CostGuard, OverEstimate, check_confirmation, confirmation_expired
from app.eval.judge import Judge
from app.eval.judging import judge_item
from app.eval.metrics import score_item
from app.eval.notscored import NotScored
from app.eval.scenarios import Scenario
from app.pipeline.state import Request

MAIN_MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
SETTLE_SECONDS = 30.0


def scratch_database(path: Path) -> Database:
    """The throw-away database the pipeline writes its sessions to during a run: a SQLite file
    with the main schema, never the main database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path.as_posix()}"
    run_migrations(url, MAIN_MIGRATIONS, log=lambda line: None)
    return connect(url)


def prompt_lines(directory: Path | None = None) -> list[str]:
    """The lines of the system prompts, for the prompt-leak rule."""
    root = directory or Path(__file__).resolve().parents[2] / "prompts"
    lines: list[str] = []
    if root.is_dir():
        for path in sorted(root.glob("*")):
            if path.is_file():
                lines.extend(path.read_text("utf-8", errors="replace").splitlines())
    return lines


class Harness:
    def __init__(
        self,
        pipeline: Any,
        db: Database,
        qdrant: Any,
        collection: str,
        workdir: Path,
        *,
        pace_seconds: float = 0.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        embed: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.db = db
        self.collection = c.check_eval_collection(collection)
        self.pace = pace_seconds
        self._sleep = sleep
        # Where each model call of the system under test is counted, at the moment it is made
        # (`bind_guard`): a span that ends is one call. None until a guard is bound.
        self._sink: Callable[[str, int, int], None] | None = None
        self._after_turn: Callable[[], None] | None = None
        self._embed_raw = embed
        self.scenario = Scenario(
            qdrant,
            self.collection,
            workdir / "scenario_journal.json",
            embed=self._counted_embed if embed is not None else None,
        )
        self._users: dict[str, str] = {}
        self._captured: dict[str, tuple[Any, Any]] = {}
        previous = pipeline.deps.observer

        def observer(ctx: Any, result: Any) -> None:
            self._captured[ctx.request.correlation_id] = (ctx, result)
            if previous is not None:
                previous(ctx, result)

        pipeline.deps.observer = observer
        previous_span = pipeline.deps.on_span

        def on_span(span: Any) -> None:
            if previous_span is not None:
                previous_span(span)
            self._count_span(span)

        pipeline.deps.on_span = on_span

    def bind_guard(self, guard: Any) -> None:
        """Count every call of the system under test on `guard` as it is made (never raising in
        the middle of a turn) and let the guard stop the run between turns."""
        self._sink = lambda who, n, t: guard.charge(requests=n, tokens=t, who=who, defer=True)
        self._after_turn = guard.check

    def _count_span(self, span: Any) -> None:
        if self._sink is None:
            return
        tokens = int(getattr(span, "input_tokens", 0) or 0) + int(
            getattr(span, "output_tokens", 0) or 0
        )
        if not (getattr(span, "model", None) or getattr(span, "provider", None) or tokens):
            return
        self._sink(_group_of(span), 1, tokens)

    async def _counted_embed(self, texts: list[str]) -> list[list[float]]:
        assert self._embed_raw is not None
        vectors = await self._embed_raw(texts)
        if self._sink is not None:
            self._sink("embedding", 1, sum(len(t) // 4 + 1 for t in texts))
        return vectors

    # ---- set-up -----------------------------------------------------------------------
    def recover(self) -> int:
        """Undo the changes a crashed earlier run left in the test collection."""
        return self.scenario.restore_all()

    def user_for(self, role: str) -> str:
        if role not in self._users:
            # the scratch file lasts across days: a resumed run finds the user of an earlier day
            with self.db.transaction(write=True):
                found = self.db.query_one(
                    "SELECT id FROM users WHERE username = ?", (f"eval_{role}",)
                )
                if found:
                    user_id = str(found[0])
                else:
                    user_id = str(uuid.uuid4())
                    self.db.execute(
                        "INSERT INTO users (id, username, email, password_hash, role) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (user_id, f"eval_{role}", f"eval_{role}@eval.invalid", "!", role),
                    )
            self._users[role] = user_id
        return self._users[role]

    # ---- one case --------------------------------------------------------------------
    async def run_case(self, case: Case) -> dict[str, Any]:
        user = self.user_for(case.role)
        session_id: str | None = None
        tokens = 0
        usage: dict[str, list[int]] = {}
        latency_ms = 0.0
        last: tuple[Any, Any] | None = None
        try:
            for index, text in enumerate(case.turns):
                for op in case.setup:
                    if op.before_turn == index:
                        await self.scenario.apply(op)
                request = Request(user_id=user, role=case.role, query=text, session_id=session_id)
                started = time.perf_counter()
                result = await self.pipeline.answer(request, self.db)
                latency_ms += (time.perf_counter() - started) * 1000.0
                await self._settle(result)
                session_id = result.session_id or session_id
                tokens += int(result.tokens or 0)
                captured = self._captured.pop(request.correlation_id, None)
                if captured is not None:
                    for who, (n, t) in _model_usage(captured[0]).items():
                        row = usage.setdefault(who, [0, 0])
                        row[0] += n
                        row[1] += t
                    last = captured
                else:
                    last = (None, result)
                if self._after_turn is not None:
                    self._after_turn()  # the cost guard may stop the run between turns
                if self.pace:
                    await self._sleep(self.pace)
        finally:
            self.scenario.restore_all()
        ctx, result = last if last else (None, None)
        item = _item_from(ctx, result)
        item["question_masked"] = self.masked_question(case, ctx)
        item["tokens"] = tokens
        item["latency_ms"] = round(latency_ms, 1)
        item["usage"] = usage
        return item

    def masked_question(self, case: Case, ctx: Any) -> str:
        """The question as the SAME masking function that makes a stored transcript makes it, for
        every judge, Jev and RAGAS call (CLAUDE.md: masking happens before any model call). The
        made-up identifiers of the PII cases never leave this machine unmasked."""
        from app.guardrails.allowlist import AllowList
        from app.guardrails.scan import mask_for_storage

        masker = getattr(self.pipeline.deps.masker, "masker", None)
        if masker is None:
            raise NotScored("masker_unavailable")
        allow = getattr(ctx, "allow", None) or AllowList()
        return mask_for_storage(
            case.question, masker=masker, allow=allow, doc_ids=set(case.source_docs)
        ).text

    async def _settle(self, result: Any) -> None:
        """Wait until the transcript write of this turn has ended on the scratch database."""
        callback = getattr(result, "on_db_idle", None)
        if not callable(callback):
            return
        done = threading.Event()
        callback(done.set)
        await asyncio.to_thread(done.wait, SETTLE_SECONDS)


def _group_of(span: Any) -> str:
    """The provider group a span's call counted against (the daily limits are per group)."""
    stage = str(getattr(span, "stage", ""))
    if getattr(span, "decider", None) == "jev":
        return "jev"
    if "embed" in stage:
        return "embedding"
    if "rerank" in stage:
        return "rerank"
    return "answer"


def _model_usage(ctx: Any) -> dict[str, list[int]]:
    """{group: [requests, tokens]} of the model calls a finished request made."""
    usage: dict[str, list[int]] = {}
    spans = list(getattr(getattr(ctx, "trace", None), "spans", []) or [])
    for span in spans:
        tokens = int(getattr(span, "input_tokens", 0) or 0) + int(
            getattr(span, "output_tokens", 0) or 0
        )
        if not (getattr(span, "model", None) or getattr(span, "provider", None) or tokens):
            continue
        row = usage.setdefault(_group_of(span), [0, 0])
        row[0] += 1
        row[1] += tokens
    return usage


def _item_from(ctx: Any, result: Any) -> dict[str, Any]:
    if result is None:
        return {"status": "failed", "answer_kind": "none", "reason": "no_result"}
    kept = list(getattr(ctx, "kept", []) or []) if ctx is not None else []
    verdict = getattr(ctx, "verdict", None) if ctx is not None else None
    cited_ids = set(getattr(verdict, "cited_chunk_ids", ()) or ())
    spans = list(getattr(getattr(ctx, "trace", None), "spans", []) or []) if ctx is not None else []
    models = sorted({str(s.model) for s in spans if getattr(s, "model", None)})
    cheap = checks.cheap_checks(result, verdict)
    cheap["cited_idx"] = [i for i, chunk in enumerate(kept) if chunk.chunk_id in cited_ids]
    cheap["stages_ms"] = {
        str(s.stage): round(float(s.duration_ms or 0.0), 1)
        for s in spans
        if getattr(s, "duration_ms", None) is not None
    }
    return {
        "status": "done",
        "answer_kind": str(result.kind),
        "reason": str(result.reason) if result.reason else None,
        "answer_text": result.text or "",
        "chunk_ids": [chunk.chunk_id for chunk in kept],
        "clause_refs": [chunk.clause_ref or "" for chunk in kept],
        "doc_ids": sorted({chunk.doc_id for chunk in kept}),
        "contexts": [chunk.text for chunk in kept],
        "models": models,
        "cheap_checks": cheap,
    }


# ====================================================================== a confirmed run ====
def system_version(config: Any, corpus_version: str | None = None) -> dict[str, Any]:
    """What was evaluated: the models in use and the prompt hashes (no key, no text)."""
    models = dict(getattr(config, "models", {}) or {})
    versions: dict[str, Any] = {
        "models": {
            k: models[k]
            for k in ("GROQ_MODEL", "GEMINI_MODEL", "OPENROUTER_MODEL", "JEV_MODEL")
            if k in models
        },
        "judge_model": getattr(config, "eval_judge_model", None),
    }
    try:
        import hashlib

        digest = hashlib.sha256()
        for line in prompt_lines():
            digest.update(line.encode("utf-8"))
        versions["prompt_hash"] = digest.hexdigest()[:16]
    except OSError:
        versions["prompt_hash"] = None
    versions["corpus_version"] = corpus_version
    return versions


def prepare_run(
    eval_db: Database,
    *,
    cases: list[Case],
    kind: str,
    environment: str,
    dataset_version: str,
    config: Any,
    created_by: str,
    corpus_version: str | None = None,
    judged: bool = True,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Create a run in the 'estimated' state: nothing is called. Returns (run id, the estimate,
    the confirmation code the owner must give to start it)."""
    from app.eval.estimate import confirmation_code

    estimate = estimate_run(cases, config, judged=judged)
    code = confirmation_code(estimate, kind=kind, dataset_version=dataset_version)
    batches = estimate["batches"]
    run_id = store.create_run(
        eval_db,
        environment=environment,
        kind=kind,
        dataset_version=dataset_version,
        system_version=system_version(config, corpus_version),
        estimate=estimate,
        estimate_code=code,
        case_ids=[cid for batch in batches for cid in batch],
        batches=max(len(batches), 1),
        created_by=created_by,
        now=now,
    )
    return run_id, estimate, code


def day_room_problem(
    eval_db: Database,
    run: dict[str, Any],
    cases: dict[str, Case],
    config: Any,
    *,
    now: datetime,
) -> str | None:
    """None when the current batch fits what is left of today's free-tier limits (what every run
    already used today counted), else a plain sentence saying which group would be passed."""
    from app.eval.estimate import Need, case_need

    need = Need()
    judged = bool(run["estimate"].get("judged", True))
    for case_id in current_batch(run):
        if case_id in cases:
            need.merge(case_need(cases[case_id], judged=judged))
    return room_problem(eval_db, need, config, now=now)


def room_problem(eval_db: Database, need: Any, config: Any, *, now: datetime) -> str | None:
    """None when `need` fits what is left of today's free-tier limits: what EVERY run, calibration
    and sample judging already used today counted (also the groups shared with production)."""
    from app.eval.estimate import daily_limits

    limits = daily_limits(config)
    used = store.usage_for_day(eval_db, now.strftime("%Y-%m-%d"))
    for who in set(need.requests) | set(need.tokens):
        taken_r, taken_t = used.get(who, (0, 0))
        cap_r, cap_t = limits.requests.get(who), limits.tokens.get(who)
        if cap_r is not None and taken_r + need.requests.get(who, 0) > cap_r * c.DAILY_LIMIT_SAFETY:
            return f"today's request limit of the {who} keys would be passed; try tomorrow (UTC)"
        if cap_t is not None and taken_t + need.tokens.get(who, 0) > cap_t * c.DAILY_LIMIT_SAFETY:
            return f"today's token limit of the {who} keys would be passed; try tomorrow (UTC)"
    return None


def start_run(
    eval_db: Database,
    run_id: str,
    given_code: str | None,
    *,
    cases: dict[str, Case] | None = None,
    config: Any = None,
    now: datetime | None = None,
    claim_status: str = c.RUN_RUNNING,
    take_over_starting: bool = False,
) -> dict[str, Any]:
    """Check the confirmation code, its age and the day's room, then take the run for this one
    process, atomically (`RunBusy` when another process holds it). The code belongs to its own
    estimate and starts the run only within `CONFIRMATION_MINUTES` of that estimate; a run that
    was already confirmed continues its own later batches under it. Raises `NotConfirmed` (nothing
    is called), `ConfirmationExpired`, `RunBusy` or `EvalConfigError`.

    `claim_status` is `starting` when the page starts the run (the process it spawns then takes
    the run over with `take_over_starting`)."""
    run = store.get_run(eval_db, run_id)
    if run is None:
        raise c.EvalConfigError("no such run")
    if run["status"] in (c.RUN_FINISHED, c.RUN_ABORTED):
        raise c.EvalConfigError("this run has ended; make a new estimate")
    check_confirmation(
        given_code, run["estimate"], kind=run["kind"], dataset_version=run["dataset_version"]
    )
    moment = now or datetime.now(UTC)
    if confirmation_expired(run["created_at"], run.get("confirmed_at"), moment):
        raise ConfirmationExpired(
            f"the confirmation code is valid for {c.CONFIRMATION_MINUTES} minutes from its "
            "estimate: make a new estimate"
        )
    if cases is not None and config is not None:
        problem = day_room_problem(eval_db, run, cases, config, now=moment)
        if problem:
            raise c.EvalConfigError(problem)
    claimed = store.claim_run(
        eval_db,
        run_id,
        now=moment,
        stale_minutes=c.HEARTBEAT_STALE_MINUTES,
        status=claim_status,
        take_over_starting=take_over_starting,
    )
    if not claimed:
        raise RunBusy("this run is already running; wait for it or for its heartbeat to go stale")
    if run.get("confirmed_at") is None:
        store.update_run(eval_db, run_id, confirmed_at=moment)
    return run


class ConfirmationExpired(c.EvalConfigError):
    """The code is older than its window."""


class RunBusy(c.EvalConfigError):
    """Another process holds the run."""


@dataclass
class RunOutcome:
    status: str
    finished_items: int
    tokens_used: int
    requests_used: int
    detail: str = ""


class RunExecutor:
    def __init__(
        self,
        eval_db: Database,
        harness: Harness,
        guard: CostGuard,
        judge: Judge | None,
        cases: dict[str, Case],
        run_id: str,
        *,
        lines: list[str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.db = eval_db
        self.harness = harness
        self.guard = guard
        self.judge = judge
        self.cases = cases
        self.run_id = run_id
        self.lines = lines or []
        self._now = now
        # every call, at the moment it is counted, becomes today's usage row and the run's totals
        guard.on_charge = self._persist_charge
        harness.bind_guard(guard)

    def _persist_charge(self, who: str, requests: int, tokens: int) -> None:
        moment = self._now()
        store.add_usage(
            self.db, self.run_id, moment.strftime("%Y-%m-%d"), who,
            requests=requests, tokens=tokens,
        )  # fmt: skip
        store.update_run(
            self.db, self.run_id,
            tokens_used=self.guard.tokens, requests_used=self.guard.requests,
            heartbeat_at=moment,
        )  # fmt: skip

    async def run_batch(self, case_ids: list[str]) -> RunOutcome:
        """Work through the given cases (the current batch) that are still pending."""
        run = store.get_run(self.db, self.run_id)
        assert run is not None
        self.harness.recover()
        pending = set(store.pending_case_ids(self.db, self.run_id))
        todo = [cid for cid in case_ids if cid in pending]
        finished = run["finished_items"]
        try:
            for case_id in todo:
                case = self.cases[case_id]
                item = await self.harness.run_case(case)
                store.save_item(
                    self.db,
                    self.run_id,
                    case_id,
                    status=item.get("status", "done"),
                    answer_kind=item.get("answer_kind"),
                    reason=item.get("reason"),
                    answer_text=item.get("answer_text"),
                    chunk_ids=item.get("chunk_ids"),
                    clause_refs=item.get("clause_refs"),
                    doc_ids=item.get("doc_ids"),
                    contexts=item.get("contexts"),
                    models=item.get("models"),
                    cheap_checks=item.get("cheap_checks"),
                    tokens=item.get("tokens", 0),
                    latency_ms=item.get("latency_ms"),
                    now=self._now(),
                )
                for score in score_item(case, item, self.lines):
                    store.save_result(
                        self.db, self.run_id, case_id, score.metric,
                        score=score.value, decider=score.decider, detail=score.detail,
                        now=self._now(),
                    )  # fmt: skip
                if self.judge is not None:
                    await self._judge(case, item)
                finished += 1
                store.update_run(
                    self.db, self.run_id,
                    finished_items=finished,
                    tokens_used=self.guard.tokens,
                    requests_used=self.guard.requests,
                )  # fmt: skip
        except OverEstimate as stop:
            store.update_run(
                self.db, self.run_id,
                status=c.RUN_ABORTED, finished_at=self._now(),
                tokens_used=self.guard.tokens, requests_used=self.guard.requests,
                note=f"stopped: {stop.kind} over the estimate",
            )  # fmt: skip
            return RunOutcome(
                c.RUN_ABORTED, finished, self.guard.tokens, self.guard.requests, str(stop)
            )
        return self._close_batch(finished)

    async def _judge(self, case: Case, item: dict[str, Any]) -> None:
        assert self.judge is not None
        question = item.get("question_masked") or ""
        for score in await judge_item(item, question=question, judge=self.judge, case=case):
            store.save_result(
                self.db, self.run_id, case.id, score.metric,
                score=score.value, decider=score.decider, detail=score.detail,
                now=self._now(),
            )  # fmt: skip

    def _close_batch(self, finished: int) -> RunOutcome:
        """The batch is over: the next batch (the next day) is announced, or the run is finished.
        A batch that still has pending cases (a subset was run) keeps its number."""
        run = store.get_run(self.db, self.run_id)
        assert run is not None
        remaining = store.pending_case_ids(self.db, self.run_id)
        left_in_batch = [cid for cid in current_batch(run) if cid in set(remaining)]
        day = self._now().strftime("%Y-%m-%d")
        if remaining:
            number = run["batch_no"] if left_in_batch else run["batch_no"] + 1
            store.update_run(
                self.db, self.run_id,
                status=c.RUN_PAUSED, batch_no=number, note=f"last_batch_day={day}",
                tokens_used=self.guard.tokens, requests_used=self.guard.requests,
            )  # fmt: skip
            status = c.RUN_PAUSED
        else:
            store.update_run(
                self.db, self.run_id,
                status=c.RUN_FINISHED, finished_at=self._now(),
                tokens_used=self.guard.tokens, requests_used=self.guard.requests,
            )  # fmt: skip
            status = c.RUN_FINISHED
        return RunOutcome(status, finished, self.guard.tokens, self.guard.requests)


def current_batch(run: dict[str, Any]) -> list[str]:
    batches = run["estimate"].get("batches") or []
    index = max(int(run.get("batch_no") or 1), 1) - 1
    return list(batches[index]) if index < len(batches) else []


def guard_for(run: dict[str, Any]) -> CostGuard:
    estimate = run["estimate"]
    return CostGuard(
        estimate["total_requests"],
        estimate["total_tokens"],
        margin=float(estimate.get("margin", c.OVER_ESTIMATE_MARGIN)),
        used_requests=int(run.get("requests_used") or 0),
        used_tokens=int(run.get("tokens_used") or 0),
    )


def _json_default(value: Any) -> str:
    return str(value)


def summary_json(outcome: RunOutcome) -> str:
    return json.dumps(outcome.__dict__, default=_json_default, sort_keys=True)
