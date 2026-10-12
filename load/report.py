"""Where did it break? Reads Locust's CSV and prints numbers and names only.

    uv run python load/report.py load_out/run [--mode stub|real]

It looks at `<prefix>_stats_history.csv` (one row per second with the user count, requests and
failures per second, and percentiles) and groups the rows by the number of users, i.e. by step.
A step "breaks" when more than 1 % of its requests failed or its p95 passed the 30 s request
budget (both [T], from `load/plan.py`). The first such step is where the run broke.

The report always says what the run cannot show (see `BLIND_SPOTS`).
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from load import plan  # noqa: E402

BLIND_SPOTS = (
    "Postgres connection ceiling: the API opens one database connection per chat request. The "
    "stub uses SQLite, so a cloud database's connection limit (and a pooled versus a direct "
    "address) is NOT exercised here; check it on the deployed service (Phase 15).",
    "Host capacity: CPU, memory and network of the host (Render's free instance) are not "
    "measured by a stubbed run.",
    "Provider limits: a scripted model has none; a real provider's rate limits and latency "
    "are only seen in a confirmed real-provider run.",
)
STUB_STATEMENT = "This report shows how the code behaves under concurrency, not host capacity."


@dataclass(frozen=True)
class StepResult:
    users: int
    requests: float
    failures: float
    p50_ms: float
    p95_ms: float

    @property
    def error_rate(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    @property
    def broken(self) -> bool:
        return self.error_rate > plan.ERROR_RATE_LIMIT or self.p95_ms > plan.P95_LIMIT_MS


def _number(row: Mapping[str, str], *names: str) -> float:
    for name in names:
        value = (row.get(name) or "").strip()
        if value and value.upper() != "N/A":
            try:
                return float(value)
            except ValueError:
                continue
    return 0.0


def steps_from_rows(rows: Iterable[Mapping[str, str]]) -> list[StepResult]:
    """Group the per-second rows by user count (the aggregated rows only)."""
    by_users: dict[int, list[Mapping[str, str]]] = defaultdict(list)
    for row in rows:
        if (row.get("Name") or "Aggregated") != "Aggregated":
            continue
        users = int(_number(row, "User Count"))
        if users > 0:
            by_users[users].append(row)
    out: list[StepResult] = []
    for users in sorted(by_users):
        group = by_users[users]
        requests = sum(_number(r, "Requests/s") for r in group)
        failures = sum(_number(r, "Failures/s") for r in group)
        p50 = max((_number(r, "50%") for r in group), default=0.0)
        p95 = max((_number(r, "95%") for r in group), default=0.0)
        out.append(StepResult(users, requests, failures, p50, p95))
    return out


def first_break(steps: list[StepResult]) -> StepResult | None:
    return next((s for s in steps if s.broken), None)


def render(steps: list[StepResult], mode: str = "stub") -> list[str]:
    lines = [plan.mode_banner(mode), STUB_STATEMENT if mode == "stub" else ""]
    lines = [line for line in lines if line]
    lines.append("users | requests | failed % | p50 ms | p95 ms | verdict")
    for s in steps:
        verdict = "BROKE" if s.broken else "ok"
        lines.append(
            f"{s.users:5d} | {s.requests:8.0f} | {s.error_rate * 100:7.2f}  | "
            f"{s.p50_ms:6.0f} | {s.p95_ms:6.0f} | {verdict}"
        )
    broke = first_break(steps)
    if not steps:
        lines.append("no rows: the run produced no data")
    elif broke is None:
        lines.append(
            f"it did not break up to {steps[-1].users} users "
            f"(failed share at most {plan.ERROR_RATE_LIMIT * 100:.0f} %, "
            f"p95 at most {plan.P95_LIMIT_MS / 1000:.0f} s)"
        )
    else:
        reason = (
            "more than 1 % of requests failed"
            if broke.error_rate > plan.ERROR_RATE_LIMIT
            else "p95 passed the 30 s request budget"
        )
        lines.append(f"first step that broke: {broke.users} users ({reason})")
    lines.append("what this run cannot show:")
    lines.extend(f"  - {spot}" for spot in BLIND_SPOTS)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="where the load run broke")
    parser.add_argument("prefix", help="the --csv prefix of the Locust run")
    parser.add_argument("--mode", choices=("stub", "real"), default="stub")
    args = parser.parse_args(argv)
    path = Path(f"{args.prefix}_stats_history.csv")
    if not path.is_file():
        print(f"no file {path.name}: run Locust with --csv {args.prefix}")
        return 1
    with path.open(newline="", encoding="utf-8") as handle:
        steps = steps_from_rows(csv.DictReader(handle))
    for line in render(steps, args.mode):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
