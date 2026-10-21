"""The evaluation command line (the owner's terminal, admin machine only; never Render).

    uv run python -m app.eval migrate [--production]      create the evaluation database tables
    uv run python -m app.eval check                       check the case set and the corpus files
    uv run python -m app.eval cases-sync                  put the case set into the local database
    uv run python -m app.eval estimate --set active|full|variance    NO call: estimate and code
    uv run python -m app.eval run RUN_ID --confirm CODE   run (one batch), with real keys
    uv run python -m app.eval calibrate [--confirm CODE]  judge and Jev against the 30 labels
    uv run python -m app.eval judge-samples [--limit N] [--confirm CODE]   judge the sample queue
    uv run python -m app.eval drift [--production]        the weekly drift report (no model call)
    uv run python -m app.eval status [RUN_ID]             numbers of a run
    uv run python -m app.eval repair                      undo test-collection changes of a crash

Every command that makes a model call needs `--confirm CODE`, where CODE is the code printed
with that exact estimate. Without it the command prints the estimate and stops. It prints names
and numbers only: never a key, an address, or the text of a question or an answer.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.eval import calibration, drift, redteam, store
from app.eval import constants as c
from app.eval.cases import (
    calibration_problems,
    load_calibration,
    load_cases,
    set_problems,
)
from app.eval.corpus import verify_files
from app.eval.estimate import confirmation_code, describe
from app.eval.guard import CostGuard, NotConfirmed, OverEstimate, check_confirmation
from app.eval.judge import Judge
from app.gateway.config import effective_judge_model

WORKDIR = store.ROOT / "eval_data"


class EvalCommandError(Exception):
    """A fixable problem; the message is safe to print (names and numbers only)."""


@dataclass
class Services:
    """Everything a real run needs. Built by `build_services` from the owner's shell."""

    gateway: Any
    pipeline: Any
    qdrant: Any
    scratch: Any  # the scratch Database (sessions of the run)
    config: Any  # GatewayConfig of the evaluation gateway
    close: Callable[[], None] = lambda: None


def build_services(env: Mapping[str, str], workdir: Path = WORKDIR) -> Services:
    """The real services, from the shell: the evaluation gateway (evaluation keys only), the test
    collection, the masker and a scratch database. Only the owner's own run reaches this."""
    from app.eval.runner import scratch_database
    from app.gateway import Gateway, GatewayConfig
    from app.gateway.tokens import TokenCounter
    from app.guardrails.domains import curated_domains
    from app.pipeline.run import Pipeline
    from app.pipeline.startup import MaskerHolder
    from app.pipeline.state import Deps
    from app.vector.documents import client_from_env

    config = GatewayConfig.for_evaluation(env)
    gateway = Gateway(config)
    qdrant = client_from_env(env)
    holder = MaskerHolder()
    holder.start()
    if not holder.wait(300):
        raise EvalCommandError("the masker did not load")
    deps = Deps(
        gateway=gateway,
        qdrant=qdrant,
        cache=None,
        masker=holder,
        counter=TokenCounter(),
        usage=None,  # no production budget or limiter, ever
        curated_domains=curated_domains(env),
        collection=c.check_eval_collection(
            (env.get("EVAL_COLLECTION") or c.EVAL_COLLECTION).strip()
        ),
    )
    pipeline = Pipeline(deps)
    scratch = scratch_database(workdir / "scratch.db")
    return Services(gateway, pipeline, qdrant, scratch, config, scratch.close)


# ================================================================================ helpers ====
def _eval_url(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, str]:
    """(database url, environment name)."""
    if getattr(args, "production", False):
        url = (env.get("EVAL_DATABASE_URL") or "").strip()
        if not url:
            raise EvalCommandError("EVAL_DATABASE_URL is not set in this terminal")
        return url, "production"
    return store.local_url(getattr(args, "db", None) or WORKDIR / "eval.db"), "local"


def _open(args: argparse.Namespace, env: Mapping[str, str]):
    url, environment = _eval_url(args, env)
    if environment == "local":
        Path(url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
    return store.open_db(url), environment


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # pragma: no cover
        return "cli:unknown"


def _trusted(db: Any, gateway: Any) -> dict[str, bool]:
    """The metrics Jev may decide: calibrated trusted AND calibrated with the Jev model and the
    judge model that are configured now."""
    config = getattr(gateway, "config", None)
    return calibration.trusted_metrics(
        db,
        jev_model=(getattr(config, "models", None) or {}).get("JEV_MODEL"),
        judge_model=effective_judge_model(config),
    )


def _need_from(estimate: Mapping[str, Any]) -> Any:
    from app.eval.estimate import Need

    need = Need()
    for who, row in estimate["providers"].items():
        need.add(who, int(row["requests"]), int(row["tokens"]))
    return need


def _count_to_usage(db: Any, guard: CostGuard, run_id: str) -> None:
    """Every call the guard counts is written at once as today's usage of this run, so a crash
    or a stop loses none of it (spec 15.1: the day's limit check counts every run)."""

    def on_charge(who: str, requests: int, tokens: int) -> None:
        now = datetime.now(UTC)
        store.add_usage(db, run_id, now.strftime("%Y-%m-%d"), who, requests=requests, tokens=tokens)

    guard.on_charge = on_charge


def _day_room(db: Any, estimate: Mapping[str, Any], config: Any) -> str | None:
    from app.eval.runner import room_problem

    return room_problem(db, _need_from(estimate), config, now=datetime.now(UTC))


def _need_admin_machine(env: Mapping[str, str]) -> None:
    from app.gateway.embeddings import running_on_render

    if running_on_render(env):
        raise EvalCommandError("evaluation runs only on the admin machine, never on Render")


# ================================================================================ commands ====
def cmd_migrate(args, env, out) -> int:
    from app.db.migrate import run as migrate

    url, environment = _eval_url(args, env)
    if environment == "local":
        Path(url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
    done = migrate(url, store.MIGRATIONS, dry_run=args.dry_run, log=out)
    word = "pending" if args.dry_run else "applied"
    out(f"{environment} evaluation database: {len(done)} file(s) {word}")
    return 0


def cmd_check(args, env, out) -> int:
    from app.eval.redteam import load_redteam, redteam_problems

    version, cases = load_cases()
    _rt_version, redteam = load_redteam()
    problems = (
        set_problems(cases)
        + calibration_problems(load_calibration())
        + redteam_problems(redteam, cases)
    )
    bad_files = verify_files()
    for line in problems:
        out(f"problem: {line}")
    for doc in bad_files:
        out(f"problem: the sample file of {doc} is missing or is not the listed file")
    review = [case.id for case in cases if case.review]
    out(f"dataset {version}: {len(cases)} cases, {sum(1 for x in cases if x.active)} active")
    out(f"red-team set: {len(redteam)} cases (development and held-out halves of 20)")
    out(f"for the owner to read: all {len(review)} active cases and the calibration review items")
    return 1 if problems or bad_files else 0


def cmd_cases_sync(args, env, out) -> int:
    version, cases = load_cases()
    problems = set_problems(cases)
    if problems:
        for line in problems:
            out(f"problem: {line}")
        return 1
    db, environment = _open(args, env)
    try:
        n = store.sync_cases(db, cases)
    finally:
        db.close()
    out(f"{environment}: {n} cases of dataset {version} written")
    return 0


def cmd_estimate(args, env, out, services_factory) -> int:
    """Make a run in the 'estimated' state. No model call, no key use beyond reading the pool
    sizes from the settings."""
    from app.eval.runner import prepare_run
    from app.gateway import GatewayConfig

    _need_admin_machine(env)
    config = GatewayConfig.for_evaluation(env)
    db, environment = _open(args, env)
    try:
        version, all_cases = load_cases()
        store.sync_cases(db, all_cases)
        kind = args.set
        if args.set in redteam.SETS:
            # its own cases, its own dataset version (so its own confirmation code), run as a
            # "full" run: the red-team set is judged on the held-out half only (the gate)
            version, red = redteam.load_redteam()
            problems = redteam.redteam_problems(red, all_cases)
            if problems:
                raise EvalCommandError("the red-team set is broken: " + "; ".join(problems[:3]))
            store.sync_cases(db, red)
            cases = redteam.select(store.list_cases(db, status="set", version=version), args.set)
            kind = "full"
        else:
            # only the main set: red-team cases live in the same table under their own version
            cases = [
                x
                for x in store.list_cases(
                    db, status="set", active_only=args.set in ("active", "variance")
                )
                if x.dataset_version == version
            ]
        judged = not args.no_judge
        run_id, estimate, code = prepare_run(
            db,
            cases=cases,
            kind=kind,
            environment=environment,
            dataset_version=version,
            config=config,
            created_by=_actor(),
            judged=judged,
        )
    finally:
        db.close()
    out(f"run {run_id} ({args.set}, {environment}, {len(cases)} cases)")
    for line in describe(estimate):
        out(line)
    out(f"To start it, type the confirmation code: run {run_id} --confirm {code}")
    return 0


def cmd_run(args, env, out, services_factory) -> int:
    from app.eval.runner import (
        RunExecutor,
        cases_for_today,
        current_batch,
        guard_for,
        prompt_lines,
        start_run,
    )

    _need_admin_machine(env)
    db, environment = _open(args, env)
    services = None
    try:
        run = store.get_run(db, args.run_id)
        if run is None:
            raise EvalCommandError("no such run")
        cases = {case.id: case for case in store.list_cases(db, status="set")}
        # The code and the day's room are checked BEFORE anything is built or called: a wrong
        # code never loads the masker or touches a key.
        from app.eval.runner import ConfirmationExpired, RunBusy
        from app.gateway import GatewayConfig

        try:
            start_run(
                db,
                run["id"],
                args.confirm,
                cases=cases,
                config=GatewayConfig.for_evaluation(env),
                take_over_starting=bool(getattr(args, "from_page", False)),
            )
        except NotConfirmed:
            out("not started: the confirmation code does not match this estimate")
            return 2
        except ConfirmationExpired:
            out(
                f"not started: the code is older than {c.CONFIRMATION_MINUTES} minutes; "
                "make a new estimate"
            )
            return 2
        except RunBusy:
            out("not started: this run is already running (or its last heartbeat is recent)")
            return 2
        services = services_factory(env)
        run = store.get_run(db, run["id"]) or run
        guard = guard_for(run)
        judge = (
            None
            if args.no_judge
            else Judge(services.gateway, guard=guard, trusted=_trusted(db, services.gateway))
        )
        from app.eval.runner import Harness

        async def embed(texts: list[str]) -> list[list[float]]:
            # a changed clause is re-embedded with the same call ingestion uses; one request
            made = await services.gateway.embed(texts, task="document", max_wait_s=60.0)
            return [list(v) for v in made.vectors]

        harness = Harness(
            services.pipeline,
            services.scratch,
            services.qdrant,
            services.pipeline.deps.collection,
            WORKDIR,
            pace_seconds=args.pace,
            embed=embed,
        )
        executor = RunExecutor(db, harness, guard, judge, cases, run["id"], lines=prompt_lines())
        today = cases_for_today(
            db, run, cases, GatewayConfig.for_evaluation(env), now=datetime.now(UTC)
        )
        if len(today) < len([x for x in current_batch(run) if x in cases]):
            out(
                f"today's room fits {len(today)} of the batch's waiting cases; "
                "the rest waits for the next UTC day"
            )
        outcome = asyncio.run(executor.run_batch(today))
        out(
            f"run {run['id']}: {outcome.status}; finished {outcome.finished_items} case(s); "
            f"{outcome.requests_used} requests, {outcome.tokens_used} tokens used"
        )
        if outcome.status == c.RUN_ABORTED:
            out(f"stopped by the cost guard: {outcome.detail}")
            return 3
        if outcome.status == c.RUN_PAUSED:
            out("more batches wait: resume with the same command tomorrow (UTC)")
        return 0
    finally:
        if services is not None:
            services.close()
        db.close()


def cmd_calibrate(args, env, out, services_factory) -> int:
    from app.gateway import GatewayConfig

    _need_admin_machine(env)
    items = load_calibration()
    problems = calibration_problems(items)
    if problems:
        for line in problems:
            out(f"problem: {line}")
        return 1
    config = GatewayConfig.for_evaluation(env)
    estimate = calibration.calibration_estimate(items, config)
    code = confirmation_code(estimate, kind="calibration", dataset_version="calibration-v1")
    if not args.confirm:
        for line in describe(estimate):
            out(line)
        out(f"To run it: calibrate --confirm {code}")
        return 0
    try:
        check_confirmation(
            args.confirm, estimate, kind="calibration", dataset_version="calibration-v1"
        )
    except NotConfirmed:
        out("not started: the confirmation code does not match this estimate")
        return 2
    db, environment = _open(args, env)
    services = None
    try:
        problem = _day_room(db, estimate, config)
        if problem:
            out(f"not started: {problem}")
            return 2
        services = services_factory(env)
        # a calibration is a run like any other: its calls are counted against today's limits
        run_id = store.create_run(
            db,
            environment=environment,
            kind="calibration",
            dataset_version="calibration-v1",
            system_version={},
            estimate=estimate,
            estimate_code=code,
            case_ids=[item.id for item in items],
            batches=1,
            created_by=_actor(),
        )
        store.update_run(db, run_id, status=c.RUN_RUNNING, started_at=datetime.now(UTC))
        guard = CostGuard(estimate["total_requests"], estimate["total_tokens"])
        _count_to_usage(db, guard, run_id)
        try:
            figures = asyncio.run(calibration.run_calibration(services.gateway, items, guard=guard))
        except OverEstimate as stop:
            store.update_run(
                db, run_id, status=c.RUN_ABORTED, note=str(stop)[:200],
                tokens_used=guard.tokens, requests_used=guard.requests,
            )  # fmt: skip
            out(f"stopped by the cost guard: {stop}")
            return 3
        calibration.save(db, figures)
        store.update_run(
            db, run_id, status=c.RUN_FINISHED, finished_items=len(items),
            tokens_used=guard.tokens, requests_used=guard.requests,
            finished_at=datetime.now(UTC),
        )  # fmt: skip
    finally:
        if services is not None:
            services.close()
        db.close()
    for metric, row in sorted(figures.items()):
        out(
            f"{metric}: judge {row['judge_agreement']}, Jev {row['jev_agreement']} "
            f"({row['jev_counted']} of {row['items']} counted), "
            f"Jev vs judge {row['jev_judge_agreement']}, "
            f"Jev trusted: {'yes' if row['trusted'] else 'no'}"
        )
    return 0


def cmd_judge_samples(args, env, out, services_factory) -> int:
    from app.eval import estimate as est
    from app.eval.judging import judge_item
    from app.eval.metrics import Score
    from app.gateway import GatewayConfig

    _need_admin_machine(env)
    GatewayConfig.for_evaluation(env)  # refuses early when no evaluation key is set
    db, environment = _open(args, env)
    try:
        samples = store.unjudged_samples(db, args.limit)
        need = est.Need()
        need.add(
            "judge",
            len(samples) * est.JUDGE_TEXT_CALLS,
            len(samples) * est.JUDGE_TEXT_CALLS * est.JUDGE_TEXT_TOKENS,
        )
        need.add(
            "jev",
            len(samples) * est.JUDGE_VERDICT_CALLS,
            len(samples) * est.JUDGE_VERDICT_CALLS * est.JUDGE_VERDICT_TOKENS,
        )
        need.add(
            "embedding",
            len(samples) * est.JUDGE_EMBED_CALLS,
            len(samples) * est.JUDGE_EMBED_CALLS * 120,
        )
        estimate = {
            "cases": len(samples),
            "turns": len(samples),
            "judged": True,
            "providers": {
                who: {
                    "label": est.PROVIDER_LABELS[who],
                    "requests": need.requests[who],
                    "tokens": need.tokens[who],
                    "daily_request_limit": None,
                    "daily_token_limit": None,
                    "keys": 0,
                    "days_needed": 1,
                }
                for who in ("judge", "jev", "embedding")
            },
            "total_requests": need.total_requests,
            "total_tokens": need.total_tokens,
            "minutes": round(need.total_requests * 0.05, 1),
            "batches": [[s["id"] for s in samples]],
            "too_big_for_one_day": [],
            "margin": c.OVER_ESTIMATE_MARGIN,
        }
        code = confirmation_code(estimate, kind="samples", dataset_version="samples")
        if not samples:
            out("no unjudged samples")
            return 0
        if not args.confirm:
            for line in describe(estimate):
                out(line)
            out(f"To run it: judge-samples --limit {args.limit} --confirm {code}")
            return 0
        try:
            check_confirmation(args.confirm, estimate, kind="samples", dataset_version="samples")
        except NotConfirmed:
            out("not started: the confirmation code does not match this estimate")
            return 2
        problem = _day_room(db, estimate, GatewayConfig.for_evaluation(env))
        if problem:
            out(f"not started: {problem}")
            return 2
        services = services_factory(env)
        guard = CostGuard(estimate["total_requests"], estimate["total_tokens"])
        judge = Judge(services.gateway, guard=guard, trusted=_trusted(db, services.gateway))
        run_id = store.create_run(
            db,
            environment=environment,
            kind="samples",
            dataset_version="samples",
            system_version={},
            estimate=estimate,
            estimate_code=code,
            case_ids=[s["id"] for s in samples],
            batches=1,
            created_by=_actor(),
        )
        store.update_run(db, run_id, status=c.RUN_RUNNING, started_at=datetime.now(UTC))
        _count_to_usage(db, guard, run_id)

        async def work() -> int:
            done = 0
            for sample in samples:
                item = {
                    "answer_kind": sample["result_kind"],
                    "answer_text": sample["answer"],
                    "contexts": sample["contexts"],
                    "models": sample["models"],
                    "cheap_checks": sample["cheap_checks"],
                }
                scores = await judge_item(
                    item, question=sample["question"], judge=judge, case=None, use_deepeval=False
                )
                scores += [
                    Score(k, 1.0 if v is True else 0.0 if v is False else None, "code")
                    for k, v in sample["cheap_checks"].items()
                    if k in ("schema_ok", "citation_ok", "numeric_ok", "grounded")
                ]
                for score in scores:
                    store.save_result(
                        db,
                        run_id,
                        sample["id"],
                        score.metric,
                        score=score.value,
                        decider=score.decider,
                        detail=score.detail,
                    )
                done += 1
            return done

        try:
            judged = asyncio.run(work())
        except OverEstimate as stop:
            store.update_run(
                db,
                run_id,
                status=c.RUN_ABORTED,
                tokens_used=guard.tokens,
                requests_used=guard.requests,
            )
            out(f"stopped by the cost guard: {stop}")
            return 3
        finally:
            services.close()
        store.mark_samples_judged(db, [s["id"] for s in samples], run_id)
        store.update_run(
            db,
            run_id,
            status=c.RUN_FINISHED,
            finished_items=judged,
            tokens_used=guard.tokens,
            requests_used=guard.requests,
            finished_at=datetime.now(UTC),
        )
        out(f"judged {judged} sample(s)")
        return 0
    finally:
        db.close()


def cmd_targets(args, env, out) -> int:
    """Propose numeric targets from the stored baseline (three unchanged runs of the active set).
    Reads only: no call, no write; the owner decides each number (docs/TARGETS.md)."""
    from app.eval import targets

    version, cases = load_cases()
    categories = {case.id: case.category for case in cases}
    db, environment = _open(args, env)
    try:
        view = targets.table(db, environment, version, categories)
    finally:
        db.close()
    if not view["complete"]:
        out(
            f"{environment}: no baseline yet ({len(view['runs'])} of {c.VARIANCE_RUNS} finished "
            'runs of the active set): every target stays "baseline"'
        )
    else:
        out(f"{environment}: baseline of {len(view['runs'])} runs of dataset {version}")
    out("target | rule | baseline | band | signed floor | proposed target | alert when")
    for row in view["rows"]:

        def shown(value):
            return "-" if value is None else (f"{value:g}" if isinstance(value, float) else value)

        out(
            f"{row['name']} | {row['rule']} | {shown(row['baseline'])} | {shown(row['width'])} | "
            f"{shown(row['signed_floor'])} | {shown(row['target'])} | {row['alert_when'] or '-'}"
            + (f" | {row['note']}" if row.get("note") else "")
        )
    out("these are PROPOSALS: nothing was written; the owner decides each number")
    return 0


def cmd_drift(args, env, out) -> int:
    db, environment = _open(args, env)
    try:
        end = datetime.now(UTC)
        window, baseline = drift.week_windows(end)
        recent = store.samples_between(db, *window)
        before = store.samples_between(db, *baseline)
        report = drift.drift_report(recent, before, window=window, baseline_window=baseline)
        store.save_drift(
            db, environment=environment, window=window, baseline=baseline, measures=report
        )
    finally:
        db.close()
    out(
        f"{environment}: {report['samples']} sample(s) this week, "
        f"{report['baseline_samples']} in the baseline window"
    )
    for name, row in report["measures"].items():
        out(f"{name}: PSI {row['psi']}, JS {row['js']} ({row['level']})")
    for note in report["notes"]:
        out(note)
    out("alerts: " + (", ".join(report["alerts"]) or "none"))
    return 0


def cmd_status(args, env, out) -> int:
    db, environment = _open(args, env)
    try:
        if args.run_id:
            run = store.get_run(db, args.run_id)
            if run is None:
                raise EvalCommandError("no such run")
            runs = [run]
        else:
            runs = store.list_runs(db, environment=environment, limit=10)
        for run in runs:
            out(
                f"{run['id']} {run['kind']} {run['status']} "
                f"{run['finished_items']}/{run['planned_items']} cases, "
                f"batch {run['batch_no']}/{run['batches']}, "
                f"{run['requests_used']} requests, {run['tokens_used']} tokens"
            )
    finally:
        db.close()
    return 0


def cmd_repair(args, env, out, services_factory) -> int:
    from app.eval.scenarios import Scenario

    _need_admin_machine(env)
    from app.vector.documents import client_from_env

    scenario = Scenario(
        client_from_env(env),
        (env.get("EVAL_COLLECTION") or c.EVAL_COLLECTION).strip(),
        WORKDIR / "scenario_journal.json",
    )
    out(f"undone {scenario.restore_all()} change(s) in the test collection")
    return 0


# =================================================================================== main ====
def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    services_factory: Callable[[Mapping[str, str]], Services] = build_services,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def with_db(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--production",
            action="store_true",
            help="use the production evaluation database (EVAL_DATABASE_URL)",
        )
        p.add_argument("--db", help="the local SQLite file (default eval_data/eval.db)")
        return p

    migrate = with_db(commands.add_parser("migrate", help="create the evaluation tables"))
    migrate.add_argument("--dry-run", action="store_true")
    commands.add_parser("check", help="check the case set and the corpus files")
    with_db(commands.add_parser("cases-sync", help="write the case set to the database"))
    estimate = with_db(commands.add_parser("estimate", help="estimate a run (no call)"))
    estimate.add_argument(
        "--set",
        choices=["active", "full", "variance", "redteam_dev", "redteam_heldout"],
        default="active",
    )
    estimate.add_argument("--no-judge", action="store_true", help="score by rules only")
    run = with_db(commands.add_parser("run", help="run a confirmed run (one batch)"))
    run.add_argument("run_id")
    run.add_argument("--confirm", help="the confirmation code printed with the estimate")
    run.add_argument("--no-judge", action="store_true")
    run.add_argument("--pace", type=float, default=3.0, help="seconds between questions")
    run.add_argument("--from-page", action="store_true", help=argparse.SUPPRESS)
    cal = with_db(commands.add_parser("calibrate", help="judge and Jev against the labels"))
    cal.add_argument("--confirm")
    samples = with_db(commands.add_parser("judge-samples", help="judge the production sample"))
    samples.add_argument("--limit", type=int, default=20)
    samples.add_argument("--confirm")
    with_db(commands.add_parser("drift", help="the weekly drift report (no call)"))
    with_db(commands.add_parser("targets", help="proposed numeric targets from the baseline"))
    status = with_db(commands.add_parser("status", help="numbers of a run"))
    status.add_argument("run_id", nargs="?")
    commands.add_parser("repair", help="undo test-collection changes left by a crash")

    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    try:
        if args.command == "migrate":
            return cmd_migrate(args, env, out)
        if args.command == "check":
            return cmd_check(args, env, out)
        if args.command == "cases-sync":
            return cmd_cases_sync(args, env, out)
        if args.command == "estimate":
            return cmd_estimate(args, env, out, services_factory)
        if args.command == "run":
            return cmd_run(args, env, out, services_factory)
        if args.command == "calibrate":
            return cmd_calibrate(args, env, out, services_factory)
        if args.command == "judge-samples":
            return cmd_judge_samples(args, env, out, services_factory)
        if args.command == "drift":
            return cmd_drift(args, env, out)
        if args.command == "targets":
            return cmd_targets(args, env, out)
        if args.command == "status":
            return cmd_status(args, env, out)
        return cmd_repair(args, env, out, services_factory)
    except EvalCommandError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except c.EvalConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(
            f"error: the command stopped ({type(error).__name__}); no key, address or text "
            "is shown",
            file=sys.stderr,
        )
        return 1
