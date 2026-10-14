"""Compare the cost projection with a dry run (spec 17.6, 18). No call is made.

    uv run python scripts/cost_dry_run_compare.py --queries-per-day 100 \
        --measured-tokens-per-query 3400 [--measured-requests-per-query 5]

The projection (`scripts/cost_projection.py`) is built from placeholder per-call sizes. The dry
run is what a real run MEASURED: the tokens and requests per question from the evaluation run
(`python -m app.eval status RUN_ID`) or the short real-provider load run (Run 27). The ratio is
dry run over projection; within 25 % (the margin the evaluation estimate uses [T]) the projection
stands, outside it the placeholder sizes are replaced by the measured ones. Alert only.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOLERANCE = 0.25


def compare(projected: float, measured: float) -> tuple[float, str]:
    """(ratio measured/projected, 'within' | 'under' | 'over')."""
    ratio = measured / projected if projected else float("inf")
    if abs(ratio - 1) <= TOLERANCE:
        return ratio, "within"
    return ratio, "under" if ratio < 1 else "over"


def main(argv: list[str] | None = None, *, out: Callable[[str], None] = print) -> int:
    from scripts import cost_projection

    parser = argparse.ArgumentParser(description="projection against a measured dry run")
    parser.add_argument("--queries-per-day", type=int, default=100)
    parser.add_argument("--share-follow-up", type=float, default=0.2)
    parser.add_argument("--measured-tokens-per-query", type=float, required=True)
    parser.add_argument("--measured-requests-per-query", type=float)
    args = parser.parse_args(argv)
    if not 1 <= args.queries_per_day <= 1_000_000 or args.measured_tokens_per_query <= 0:
        out("error: queries per day from 1 to 1,000,000 and tokens per query above 0")
        return 1
    day = cost_projection.project(args.queries_per_day, args.share_follow_up)
    tokens = sum(row["tokens"] for row in day.values()) / args.queries_per_day
    requests = sum(row["requests"] for row in day.values()) / args.queries_per_day
    ratio, verdict = compare(tokens, args.measured_tokens_per_query)
    out(f"projected tokens per query: {tokens:.0f} [T placeholder sizes]")
    out(f"measured tokens per query: {args.measured_tokens_per_query:.0f}")
    out(f"ratio measured/projected: {ratio:.2f} ({verdict} the {TOLERANCE:.0%} margin)")
    if args.measured_requests_per_query:
        r_ratio, r_verdict = compare(requests, args.measured_requests_per_query)
        measured = args.measured_requests_per_query
        out(
            f"requests per query: projected {requests:.1f}, measured {measured:.1f}, "
            f"ratio {r_ratio:.2f} ({r_verdict})"
        )
    if verdict != "within":
        out("the placeholder sizes are replaced by the measured ones (spec 17.6, 18)")
    out("record the ratio in docs/prelaunch_evidence.json (cost_projection.ratio)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
