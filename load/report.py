"""Where did it break? Reads a Locust run and prints numbers and names only.

    uv run python load/report.py load_out/run [--mode stub|real] [--steps 10x60,20x60]

Inputs (all written by the run; `--csv load_out/run --csv-full-history`):
- `<prefix>_stats_history.csv`: Locust's one-row-per-second history. With `--csv-full-history`
  its Aggregated row holds the CURRENT percentiles (a window of about the last 10 seconds,
  `CURRENT_RESPONSE_TIME_PERCENTILE_WINDOW`). Without that flag Locust writes the CUMULATIVE
  percentiles of the whole run so far, which hide a late step that got slow; the report says
  which one it read.
- `<prefix>_active.csv`: written by the locustfile once a second: users wanted, users logged in
  (active), and the chat outcomes counted by kind.
- `<prefix>_plan.json`: the plan of the run (otherwise `--steps` or the default plan).

Rows are assigned to a STEP by their time since the start and the plan's seconds (not by the
user count: while users are being added the count passes through other values). The first
`SETTLE_SECONDS` of a step are left out of the latency and the rates, because the 10 s window
still holds the end of the step before.

A step "broke" when ANY of these holds (limits from `load/plan.py`, [T]):
  - more than 1 % of requests failed (HTTP 503 "temporarily unavailable" is a failure; the
    per-user rate limit 429 is not);
  - more than 1 % of chat answers were "temporarily unavailable";
  - the p95 passed the 30 s request budget;
  - fewer than 80 % of the step's users were active (logged in).
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from load import plan  # noqa: E402

SETTLE_SECONDS = 10  # the current-percentile window; the start of a step still holds the last one
BLIND_SPOTS = (
    "Postgres connection ceiling: the API opens one database connection per chat request. The "
    "run uses SQLite, so a cloud database's connection limit (and a pooled versus a direct "
    "address) is NOT exercised here; check it on the deployed service (Phase 15).",
    "Host capacity: CPU, memory and network of the host (Render's free instance) are not "
    "measured by a local run.",
    "Provider limits: a scripted model has none; a real provider's rate limits and latency "
    "are only seen in a confirmed real-provider run.",
)
STUB_STATEMENT = "This report shows how the code behaves under concurrency, not host capacity."
CUMULATIVE_WARNING = (
    "WARNING: this history has no per-endpoint rows, so Locust wrote CUMULATIVE percentiles "
    "(the whole run so far). A late slow step is hidden. Run again with --csv-full-history."
)


@dataclass(frozen=True)
class StepResult:
    users: int
    requests: float
    failures: float
    p50_ms: float
    p95_ms: float
    active_share: float | None = None  # None: no side file, not judged
    unavailable_share: float | None = None
    rate_limited: int = 0
    reasons: tuple[str, ...] = field(default=(), compare=False)

    @property
    def error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    def _why(self) -> list[str]:
        why = []
        if self.error_rate > plan.ERROR_RATE_LIMIT:
            why.append("more than 1 % of requests failed")
        if self.unavailable_share is not None and self.unavailable_share > plan.ERROR_RATE_LIMIT:
            why.append("more than 1 % of answers were 'temporarily unavailable'")
        if self.p95_ms > plan.P95_LIMIT_MS:
            why.append("p95 passed the 30 s request budget")
        if self.active_share is not None and self.active_share < plan.ACTIVE_SHARE_MIN:
            why.append("fewer than 80 % of the users were active (logged in)")
        return why

    @property
    def broken(self) -> bool:
        return bool(self._why())


def _number(row: Mapping[str, str], *names: str) -> float:
    for name in names:
        value = (row.get(name) or "").strip()
        if value and value.upper() != "N/A":
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def _bounds(steps: Sequence[plan.Step]) -> list[tuple[float, float]]:
    out, start = [], 0.0
    for step in steps:
        out.append((start, start + step.seconds))
        start += step.seconds
    return out


def analyse(
    history: Iterable[Mapping[str, str]],
    side: Iterable[Mapping[str, str]],
    steps: Sequence[plan.Step],
) -> tuple[list[StepResult], bool]:
    """(the result of each step, whether the percentiles are the current (windowed) ones)."""
    rows = list(history)
    windowed = any((r.get("Name") or "Aggregated") != "Aggregated" for r in rows)
    aggregated = [r for r in rows if (r.get("Name") or "Aggregated") == "Aggregated"]
    stamps = [_number(r, "Timestamp") for r in aggregated if _number(r, "User Count") > 0]
    sides = [dict(r) for r in side]
    out: list[StepResult] = []
    if not stamps and not sides:
        return out, windowed
    t0 = min(stamps) if stamps else min(_number(r, "time") for r in sides)
    for step, (low, high) in zip(steps, _bounds(steps), strict=True):
        settle = SETTLE_SECONDS if step.seconds > SETTLE_SECONDS * 2 else 0
        inside = [r for r in aggregated if low <= _number(r, "Timestamp") - t0 < high]
        settled = [r for r in inside if _number(r, "Timestamp") - t0 >= low + settle] or inside
        if not inside:
            continue
        requests = sum(_number(r, "Requests/s") for r in settled)
        failures = sum(_number(r, "Failures/s") for r in settled)
        p50 = max((_number(r, "50%") for r in settled), default=0.0)
        p95 = max((_number(r, "95%") for r in settled), default=0.0)
        active = unavailable = None
        limited = 0
        step_side = [r for r in sides if low <= _number(r, "time") - t0 < high]
        if step_side:
            ratios = [
                _number(r, "active_users") / _number(r, "target_users")
                for r in step_side
                if _number(r, "time") - t0 >= low + settle and _number(r, "target_users") > 0
            ] or [
                _number(r, "active_users") / _number(r, "target_users")
                for r in step_side
                if _number(r, "target_users") > 0
            ]
            if ratios:
                active = statistics.median(ratios)
            first, last = step_side[0], step_side[-1]

            def delta(name: str, first=first, last=last) -> int:
                return int(_number(last, name) - _number(first, name))

            kinds = ("chat_ok", "chat_rate_limited", "chat_unavailable", "chat_other")
            chat = sum(delta(kind) for kind in kinds)
            unavailable = delta("chat_unavailable") / chat if chat else 0.0
            limited = delta("chat_rate_limited")
        out.append(StepResult(step.users, requests, failures, p50, p95, active, unavailable,
                              limited))  # fmt: skip
    return out, windowed


def first_break(steps: list[StepResult]) -> StepResult | None:
    return next((s for s in steps if s.broken), None)


def render(steps: list[StepResult], mode: str = "stub", windowed: bool = True) -> list[str]:
    lines = [plan.mode_banner(mode)]
    if mode == "stub":
        lines.append(STUB_STATEMENT)
    if not windowed:
        lines.append(CUMULATIVE_WARNING)
    lines.append(
        "users | requests | failed % | unavail % | active % | limited | p50 ms | p95 ms | verdict"
    )
    for s in steps:
        active = "   n/a" if s.active_share is None else f"{s.active_share * 100:6.0f}"
        unav = "   n/a" if s.unavailable_share is None else f"{s.unavailable_share * 100:6.2f}"
        lines.append(
            f"{s.users:5d} | {s.requests:8.0f} | {s.error_rate * 100:7.2f}  | {unav}   | "
            f"{active}   | {s.rate_limited:7d} | {s.p50_ms:6.0f} | {s.p95_ms:6.0f} | "
            f"{'BROKE' if s.broken else 'ok'}"
        )
    broke = first_break(steps)
    if not steps:
        lines.append("no rows: the run produced no data")
    elif broke is None:
        lines.append(
            f"it did not break up to {steps[-1].users} users "
            f"(failed and unavailable share at most {plan.ERROR_RATE_LIMIT * 100:.0f} %, "
            f"p95 at most {plan.P95_LIMIT_MS / 1000:.0f} s, at least "
            f"{plan.ACTIVE_SHARE_MIN * 100:.0f} % of users active)"
        )
    else:
        lines.append(f"first step that broke: {broke.users} users ({'; '.join(broke._why())})")
    lines.append(
        "limited = chat answers refused by the per-user rate limit (by design, not a failure)"
    )
    lines.append("what this run cannot show:")
    lines.extend(f"  - {spot}" for spot in BLIND_SPOTS)
    return lines


def _read(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="where the load run broke")
    parser.add_argument("prefix", help="the --csv prefix of the Locust run")
    parser.add_argument("--mode", choices=("stub", "real"), default=None)
    parser.add_argument("--steps", help="the plan, if the run did not save it (10x60,20x60)")
    args = parser.parse_args(argv)
    history_path = Path(f"{args.prefix}_stats_history.csv")
    if not history_path.is_file():
        print(f"no file {history_path.name}: run Locust with --csv {args.prefix}")
        return 1
    saved = {}
    plan_path = Path(f"{args.prefix}_plan.json")
    if plan_path.is_file():
        saved = json.loads(plan_path.read_text("utf-8"))
    try:
        steps = plan.parse_steps(args.steps or saved.get("steps"))
    except plan.PlanError as error:
        print(f"error: {error}")
        return 1
    mode = args.mode or saved.get("mode") or "stub"
    result, windowed = analyse(_read(history_path), _read(Path(f"{args.prefix}_active.csv")), steps)
    for line in render(result, mode, windowed):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
