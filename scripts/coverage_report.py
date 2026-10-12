"""Per-package coverage from a `coverage json` file (spec 16: "coverage target 80 % on gateway,
guardrails, cache and memory").

    uv run pytest --cov=app --cov-branch --cov-report=json:coverage.json -q
    uv run python scripts/coverage_report.py coverage.json

The four spec packages are held to 80 % of their lines; the exit code is 1 when one is below,
which the CI job treats as an ALERT (it is `continue-on-error`: build rule 5, no gate blocks a
merge or a release). Accounts, api, pipeline, ingest and the rest are reported for information
only. Names and numbers only; no source text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path

THRESHOLD = 80.0  # the spec's target, in percent of lines
SPEC_PACKAGES = ("gateway", "guardrails", "cache", "memory")  # gated (alert only)
INFO_PACKAGES = ("accounts", "api", "pipeline", "ingest")  # reported, never alerting


def package_of(path: str) -> str | None:
    """`app/gateway/gateway.py` -> `gateway` (either slash); anything outside app/ is None."""
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 3 and parts[0] == "app":
        return parts[1]
    if len(parts) == 2 and parts[0] == "app":
        return "(app root)"
    return None


def summarise(files: Mapping[str, Mapping]) -> dict[str, dict[str, float]]:
    """Per package: statements, covered lines, branches, covered branches and the two percents."""
    totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"statements": 0, "covered": 0, "branches": 0, "covered_branches": 0}
    )
    for path, data in files.items():
        package = package_of(path)
        if package is None:
            continue
        summary = data.get("summary", {})
        row = totals[package]
        row["statements"] += summary.get("num_statements", 0)
        row["covered"] += summary.get("covered_lines", 0)
        row["branches"] += summary.get("num_branches", 0)
        row["covered_branches"] += summary.get("covered_branches", 0)
    for row in totals.values():
        row["line_percent"] = (
            100.0 * row["covered"] / row["statements"] if row["statements"] else 100.0
        )
        row["branch_percent"] = (
            100.0 * row["covered_branches"] / row["branches"] if row["branches"] else 100.0
        )
    return dict(totals)


def below_threshold(summary: Mapping[str, Mapping[str, float]]) -> list[str]:
    """The spec packages under the target (a package with no data counts as under)."""
    return [
        name
        for name in SPEC_PACKAGES
        if name not in summary or summary[name]["line_percent"] < THRESHOLD
    ]


def render(summary: Mapping[str, Mapping[str, float]]) -> list[str]:
    lines = ["package         | lines % | branches % | statements | role"]
    ordered = [*SPEC_PACKAGES, *INFO_PACKAGES]
    ordered += sorted(set(summary) - set(ordered))
    for name in ordered:
        if name not in summary:
            lines.append(f"{name:15} | no data")
            continue
        row = summary[name]
        role = f"target {THRESHOLD:.0f} %" if name in SPEC_PACKAGES else "information only"
        flag = ""
        if name in SPEC_PACKAGES and row["line_percent"] < THRESHOLD:
            flag = "  <-- BELOW THE TARGET (alert)"
        lines.append(
            f"{name:15} | {row['line_percent']:7.1f} | {row['branch_percent']:10.1f} | "
            f"{int(row['statements']):10d} | {role}{flag}"
        )
    low = below_threshold(summary)
    lines.append(
        "alert: below the target: " + ", ".join(low)
        if low
        else f"all four spec packages are at or above {THRESHOLD:.0f} %"
    )
    return lines


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(description="per-package coverage")
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args(argv)
    if not args.json_file.is_file():
        out(f"no file {args.json_file.name}: run pytest with --cov-report=json:{args.json_file}")
        return 2
    data = json.loads(args.json_file.read_text("utf-8"))
    summary = summarise(data.get("files", {}))
    for line in render(summary):
        out(line)
    return 1 if below_threshold(summary) else 0


if __name__ == "__main__":
    sys.exit(main())
