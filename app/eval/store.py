"""Reading and writing the evaluation database (spec 12.2). Numbers, case ids and masked or
synthetic text only: never a user id, a session id, a key or an address.

One small set of functions over `app.db.connection.Database`, written once for SQLite (the local
environment, and every unit test) and Postgres (the production evaluation database).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.accounts.tokens import utc_text
from app.db.connection import Database, connect
from app.eval import constants as c
from app.eval.cases import Case, case_from_dict, case_to_dict

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations_eval"
# The local evaluation database and the run folders live under the project root (git-ignored).
DEFAULT_LOCAL_FILE = ROOT / "eval_data" / "eval.db"


def sqlite_url(path: str | Path) -> str:
    return f"sqlite:///{Path(path).as_posix()}"


def local_url(path: str | Path | None = None) -> str:
    return sqlite_url(path or DEFAULT_LOCAL_FILE)


def open_db(url: str) -> Database:
    return connect(url)


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(text: str | None, default: Any = None) -> Any:
    if text in (None, ""):
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


# ================================================================================ the cases ====
def upsert_case(db: Database, case: Case, *, now: datetime | None = None) -> None:
    data = case_to_dict(case)
    row = (
        case.dataset_version,
        case.category,
        bool(case.active),
        case.status,
        case.role,
        _json(data["turns"]),
        _json(data["expect"]),
        _json(data["setup"]) if data["setup"] else None,
        _json(data["source_docs"]),
        case.origin,
        case.review_item_id,
    )
    with db.transaction(write=True):
        found = db.query_one("SELECT id FROM eval_cases WHERE id = ?", (case.id,))
        if found:
            db.execute(
                "UPDATE eval_cases SET dataset_version = ?, category = ?, active = ?, status = ?, "
                "role = ?, turns = ?, expect = ?, setup = ?, source_docs = ?, origin = ?, "
                "review_item_id = ? WHERE id = ?",
                (*row, case.id),
            )
        else:
            db.execute(
                "INSERT INTO eval_cases (id, dataset_version, category, active, status, role, "
                "turns, expect, setup, source_docs, origin, review_item_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (case.id, *row, utc_text(now or _now())),
            )


def sync_cases(db: Database, cases: list[Case]) -> int:
    for case in cases:
        upsert_case(db, case)
    return len(cases)


def _case_from_row(row: tuple) -> Case:
    (cid, version, category, active, status, role, turns, expect, setup, docs, origin, review) = row
    expect_data = _load(expect, {})
    return case_from_dict(
        {
            "id": cid,
            "dataset_version": version,
            "category": category,
            "active": bool(active),
            "status": status,
            "role": role,
            "turns": _load(turns, []),
            "expect": expect_data,
            "setup": _load(setup, []),
            "source_docs": _load(docs, []),
            "origin": origin,
            "review_item_id": review,
        },
        version,
    )


def list_cases(
    db: Database, *, status: str = "set", active_only: bool = False, version: str | None = None
) -> list[Case]:
    sql = (
        "SELECT id, dataset_version, category, active, status, role, turns, expect, setup, "
        "source_docs, origin, review_item_id FROM eval_cases WHERE status = ?"
    )
    params: list[Any] = [status]
    if active_only:
        sql += " AND active = ?"
        params.append(True)
    if version:
        sql += " AND dataset_version = ?"
        params.append(version)
    sql += " ORDER BY id"
    with db.transaction():
        rows = db.query_all(sql, tuple(params))
    return [_case_from_row(row) for row in rows]


def dataset_versions(db: Database) -> list[str]:
    with db.transaction():
        rows = db.query_all(
            "SELECT DISTINCT dataset_version FROM eval_cases WHERE status = 'set' "
            "ORDER BY dataset_version"
        )
    return [r[0] for r in rows]


def candidate_exists(db: Database, review_item_id: str) -> bool:
    with db.transaction():
        row = db.query_one(
            "SELECT 1 FROM eval_cases WHERE review_item_id = ? AND origin = 'review_queue'",
            (review_item_id,),
        )
    return row is not None


# ================================================================================== the runs ====
def create_run(
    db: Database,
    *,
    environment: str,
    kind: str,
    dataset_version: str,
    system_version: dict[str, Any],
    estimate: dict[str, Any],
    estimate_code: str,
    case_ids: list[str],
    batches: int,
    created_by: str,
    now: datetime | None = None,
) -> str:
    if environment not in c.ENVIRONMENTS or kind not in c.RUN_KINDS:
        raise c.EvalConfigError("unknown environment or run kind")
    run_id = uuid.uuid4().hex
    moment = utc_text(now or _now())
    with db.transaction(write=True):
        db.execute(
            "INSERT INTO eval_runs (id, environment, kind, dataset_version, system_version, "
            "status, estimate, estimate_code, planned_items, batches, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                environment,
                kind,
                dataset_version,
                _json(system_version),
                c.RUN_ESTIMATED,
                _json(estimate),
                estimate_code,
                len(case_ids),
                batches,
                created_by,
                moment,
            ),
        )
        for case_id in case_ids:
            db.execute(
                "INSERT INTO eval_items (id, run_id, case_id, status) VALUES (?, ?, ?, 'pending')",
                (uuid.uuid4().hex, run_id, case_id),
            )
    return run_id


_RUN_COLUMNS = (
    "id, environment, kind, dataset_version, system_version, status, estimate, estimate_code, "
    "confirmed_at, planned_items, finished_items, batch_no, batches, tokens_used, requests_used, "
    "created_by, created_at, started_at, finished_at, note"
)
_RUN_FIELDS = _RUN_COLUMNS.split(", ")


def _run_dict(row: tuple) -> dict[str, Any]:
    data = dict(zip(_RUN_FIELDS, row, strict=True))
    data["system_version"] = _load(data["system_version"], {})
    data["estimate"] = _load(data["estimate"], {})
    for name in ("confirmed_at", "created_at", "started_at", "finished_at"):
        if data[name] is not None:
            data[name] = str(data[name])
    return data


def get_run(db: Database, run_id: str) -> dict[str, Any] | None:
    with db.transaction():
        row = db.query_one(f"SELECT {_RUN_COLUMNS} FROM eval_runs WHERE id = ?", (run_id,))
    return _run_dict(row) if row else None


def list_runs(db: Database, *, environment: str | None = None, limit: int = 50) -> list[dict]:
    sql = f"SELECT {_RUN_COLUMNS} FROM eval_runs"
    params: list[Any] = []
    if environment:
        sql += " WHERE environment = ?"
        params.append(environment)
    sql += " ORDER BY created_at DESC, id LIMIT ?"
    params.append(int(limit))
    with db.transaction():
        rows = db.query_all(sql, tuple(params))
    return [_run_dict(r) for r in rows]


def update_run(db: Database, run_id: str, **fields: Any) -> None:
    allowed = {
        "status", "confirmed_at", "finished_items", "batch_no", "tokens_used", "requests_used",
        "started_at", "finished_at", "note",
    }  # fmt: skip
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError("unknown run field")
    if not fields:
        return
    values = []
    for value in fields.values():
        values.append(utc_text(value) if isinstance(value, datetime) else value)
    sets = ", ".join(f"{name} = ?" for name in fields)
    with db.transaction(write=True):
        db.execute(f"UPDATE eval_runs SET {sets} WHERE id = ?", (*values, run_id))


def pending_case_ids(db: Database, run_id: str) -> list[str]:
    with db.transaction():
        rows = db.query_all(
            "SELECT case_id FROM eval_items WHERE run_id = ? AND status = 'pending' "
            "ORDER BY case_id",
            (run_id,),
        )
    return [r[0] for r in rows]


def save_item(
    db: Database,
    run_id: str,
    case_id: str,
    *,
    status: str,
    answer_kind: str | None = None,
    reason: str | None = None,
    answer_text: str | None = None,
    chunk_ids: list[str] | None = None,
    clause_refs: list[str] | None = None,
    doc_ids: list[str] | None = None,
    contexts: list[str] | None = None,
    models: list[str] | None = None,
    cheap_checks: dict[str, Any] | None = None,
    tokens: int = 0,
    latency_ms: float | None = None,
    now: datetime | None = None,
) -> None:
    with db.transaction(write=True):
        db.execute(
            "UPDATE eval_items SET status = ?, answer_kind = ?, reason = ?, answer_text = ?, "
            "chunk_ids = ?, clause_refs = ?, doc_ids = ?, contexts = ?, models = ?, "
            "cheap_checks = ?, tokens = ?, latency_ms = ?, finished_at = ? "
            "WHERE run_id = ? AND case_id = ?",
            (
                status,
                answer_kind,
                reason,
                answer_text,
                _json(chunk_ids or []),
                _json(clause_refs or []),
                _json(doc_ids or []),
                _json(contexts or []),
                _json(models or []),
                _json(cheap_checks or {}),
                int(tokens),
                latency_ms,
                utc_text(now or _now()),
                run_id,
                case_id,
            ),
        )


def get_items(db: Database, run_id: str) -> list[dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT case_id, status, answer_kind, reason, answer_text, chunk_ids, clause_refs, "
            "doc_ids, contexts, models, cheap_checks, tokens, latency_ms FROM eval_items "
            "WHERE run_id = ? ORDER BY case_id",
            (run_id,),
        )
    out = []
    for r in rows:
        out.append(
            {
                "case_id": r[0],
                "status": r[1],
                "answer_kind": r[2],
                "reason": r[3],
                "answer_text": r[4] or "",
                "chunk_ids": _load(r[5], []),
                "clause_refs": _load(r[6], []),
                "doc_ids": _load(r[7], []),
                "contexts": _load(r[8], []),
                "models": _load(r[9], []),
                "cheap_checks": _load(r[10], {}),
                "tokens": int(r[11] or 0),
                "latency_ms": r[12],
            }
        )
    return out


def save_result(
    db: Database,
    run_id: str,
    case_id: str,
    metric: str,
    *,
    score: float | None,
    decider: str,
    model: str | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> None:
    """One score. A None score is "not scored" and is stored as such, never as zero."""
    scored = score is not None
    with db.transaction(write=True):
        db.execute(
            "DELETE FROM eval_results WHERE run_id = ? AND case_id = ? AND metric = ?",
            (run_id, case_id, metric),
        )
        db.execute(
            "INSERT INTO eval_results (id, run_id, case_id, metric, score, scored, decider, "
            "model, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                run_id,
                case_id,
                metric,
                float(score) if scored else None,
                scored,
                decider,
                model,
                detail,
                utc_text(now or _now()),
            ),
        )


def get_results(db: Database, run_id: str) -> list[dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT case_id, metric, score, scored, decider, model, detail FROM eval_results "
            "WHERE run_id = ? ORDER BY case_id, metric",
            (run_id,),
        )
    return [
        {
            "case_id": r[0],
            "metric": r[1],
            "score": r[2],
            "scored": bool(r[3]),
            "decider": r[4],
            "model": r[5],
            "detail": r[6],
        }
        for r in rows
    ]


# ===================================================================== the production sample ====
def insert_sample(db: Database, sample: dict[str, Any], *, now: datetime | None = None) -> str:
    sample_id = uuid.uuid4().hex
    with db.transaction(write=True):
        db.execute(
            "INSERT INTO eval_samples (id, sampled_at, role, result_kind, reason, question, "
            "answer, chunk_ids, clause_refs, contexts, models, prompt_hash, corpus_version, "
            "confidence, degraded, cheap_checks, tokens, latency_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id,
                utc_text(now or _now()),
                sample["role"],
                sample["result_kind"],
                sample.get("reason"),
                sample["question"],
                sample["answer"],
                _json(sample.get("chunk_ids") or []),
                _json(sample.get("clause_refs") or []),
                _json(sample.get("contexts") or []),
                _json(sample.get("models") or []),
                sample.get("prompt_hash"),
                sample.get("corpus_version"),
                sample.get("confidence"),
                _json(sample.get("degraded") or []),
                _json(sample.get("cheap_checks") or {}),
                int(sample.get("tokens") or 0),
                sample.get("latency_ms"),
            ),
        )
    return sample_id


def unjudged_samples(db: Database, limit: int) -> list[dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT id, sampled_at, role, result_kind, reason, question, answer, chunk_ids, "
            "models, cheap_checks, tokens, contexts FROM eval_samples WHERE judged_at IS NULL "
            "ORDER BY sampled_at, id LIMIT ?",
            (int(limit),),
        )
    return [
        {
            "id": r[0],
            "sampled_at": str(r[1]),
            "role": r[2],
            "result_kind": r[3],
            "reason": r[4],
            "question": r[5],
            "answer": r[6],
            "chunk_ids": _load(r[7], []),
            "models": _load(r[8], []),
            "cheap_checks": _load(r[9], {}),
            "tokens": int(r[10] or 0),
            "contexts": _load(r[11], []),
        }
        for r in rows
    ]


def mark_samples_judged(
    db: Database, sample_ids: list[str], run_id: str, *, now: datetime | None = None
) -> None:
    moment = utc_text(now or _now())
    with db.transaction(write=True):
        for sample_id in sample_ids:
            db.execute(
                "UPDATE eval_samples SET judged_at = ?, run_id = ? WHERE id = ?",
                (moment, run_id, sample_id),
            )


def samples_between(db: Database, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT sampled_at, role, result_kind, reason, question, models, prompt_hash, "
            "confidence, degraded, cheap_checks, tokens, latency_ms, corpus_version "
            "FROM eval_samples WHERE sampled_at >= ? AND sampled_at < ? ORDER BY sampled_at",
            (utc_text(start), utc_text(end)),
        )
    return [
        {
            "sampled_at": str(r[0]),
            "role": r[1],
            "result_kind": r[2],
            "reason": r[3],
            "question": r[4],
            "models": _load(r[5], []),
            "prompt_hash": r[6],
            "confidence": r[7],
            "degraded": _load(r[8], []),
            "cheap_checks": _load(r[9], {}),
            "tokens": int(r[10] or 0),
            "latency_ms": r[11],
            "corpus_version": r[12],
        }
        for r in rows
    ]


def purge_samples(
    db: Database, *, now: datetime, days: int = c.SAMPLE_RETENTION_DAYS, dry_run: bool = False
) -> int:
    """Delete masked samples older than `days`. Returns how many (counted only, when `dry_run`).
    Run by the owner's retention command with the evaluation database address in the shell."""
    limit = utc_text(now - timedelta(days=days))
    with db.transaction(write=not dry_run):
        row = db.query_one("SELECT COUNT(*) FROM eval_samples WHERE sampled_at < ?", (limit,))
        count = int(row[0]) if row else 0
        if not dry_run:
            db.execute("DELETE FROM eval_samples WHERE sampled_at < ?", (limit,))
    return count


def add_usage(db: Database, run_id: str, day: str, who: str, *, requests: int, tokens: int) -> None:
    """Add to what a run used of a provider group on a UTC day (YYYY-MM-DD)."""
    with db.transaction(write=True):
        row = db.query_one(
            "SELECT requests, tokens FROM eval_usage_days WHERE day = ? AND run_id = ? AND who = ?",
            (day, run_id, who),
        )
        if row:
            db.execute(
                "UPDATE eval_usage_days SET requests = ?, tokens = ? "
                "WHERE day = ? AND run_id = ? AND who = ?",
                (int(row[0]) + requests, int(row[1]) + tokens, day, run_id, who),
            )
        else:
            db.execute(
                "INSERT INTO eval_usage_days (day, run_id, who, requests, tokens) "
                "VALUES (?, ?, ?, ?, ?)",
                (day, run_id, who, requests, tokens),
            )


def usage_for_day(db: Database, day: str) -> dict[str, tuple[int, int]]:
    """Requests and tokens all runs together used of each provider group on that UTC day."""
    with db.transaction():
        rows = db.query_all(
            "SELECT who, SUM(requests), SUM(tokens) FROM eval_usage_days WHERE day = ? "
            "GROUP BY who",
            (day,),
        )
    return {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}


def count_samples(db: Database) -> int:
    with db.transaction():
        row = db.query_one("SELECT COUNT(*) FROM eval_samples")
    return int(row[0]) if row else 0


# ================================================================ drift and calibration ====
def save_drift(
    db: Database,
    *,
    environment: str,
    window: tuple[datetime, datetime],
    baseline: tuple[datetime, datetime] | None,
    measures: dict[str, Any],
    now: datetime | None = None,
) -> str:
    snapshot_id = uuid.uuid4().hex
    with db.transaction(write=True):
        db.execute(
            "INSERT INTO eval_drift_snapshots (id, environment, window_start, window_end, "
            "baseline_start, baseline_end, measures, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                environment,
                utc_text(window[0]),
                utc_text(window[1]),
                utc_text(baseline[0]) if baseline else None,
                utc_text(baseline[1]) if baseline else None,
                _json(measures),
                utc_text(now or _now()),
            ),
        )
    return snapshot_id


def list_drift(db: Database, environment: str, limit: int = 12) -> list[dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT window_start, window_end, measures, created_at FROM eval_drift_snapshots "
            "WHERE environment = ? ORDER BY window_end DESC, id LIMIT ?",
            (environment, int(limit)),
        )
    return [
        {
            "window_start": str(r[0]),
            "window_end": str(r[1]),
            "measures": _load(r[2], {}),
            "created_at": str(r[3]),
        }
        for r in rows
    ]


def save_calibration(
    db: Database,
    metric: str,
    *,
    items: int,
    judge_agreement: float | None,
    jev_agreement: float | None,
    threshold: float,
    trusted: bool,
    now: datetime | None = None,
) -> None:
    with db.transaction(write=True):
        db.execute("DELETE FROM eval_calibration WHERE metric = ?", (metric,))
        db.execute(
            "INSERT INTO eval_calibration (metric, items, judge_agreement, jev_agreement, "
            "threshold, trusted, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                metric,
                int(items),
                judge_agreement,
                jev_agreement,
                float(threshold),
                bool(trusted),
                utc_text(now or _now()),
            ),
        )


def get_calibration(db: Database) -> dict[str, dict[str, Any]]:
    with db.transaction():
        rows = db.query_all(
            "SELECT metric, items, judge_agreement, jev_agreement, threshold, trusted, "
            "computed_at FROM eval_calibration ORDER BY metric"
        )
    return {
        r[0]: {
            "items": int(r[1]),
            "judge_agreement": r[2],
            "jev_agreement": r[3],
            "threshold": r[4],
            "trusted": bool(r[5]),
            "computed_at": str(r[6]),
        }
        for r in rows
    }
