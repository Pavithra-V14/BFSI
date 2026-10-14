"""Compare a bandit scan with the accepted baseline (spec 17.6, Phase 16). ALERT ONLY in CI.

    uvx bandit==1.9.4 -r app scripts load frontend -q -f json -o bandit_now.json
    uv run python scripts/bandit_check.py bandit_now.json

Prints every finding that is NOT in `.bandit-baseline.json` and exits 1 when there is one (the CI
job is `continue-on-error`, so a red cross never blocks anything: build rule 5).

Why not bandit's own `-b`: its comparison uses the file name as the scan wrote it, so a baseline
made on Windows (backslashes) never matches a scan on Linux, and the owner runs this on Windows
while CI runs on Linux. Here the name is normalised to forward slashes and findings are matched by
(file, rule, severity, confidence, text), counted, so a line that moved is not a new finding but a
second finding of the same kind in the same file is. The baseline holds no quoted code.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".bandit-baseline.json"


def _name(path: str) -> str:
    return path.replace(chr(92), "/").lstrip("./")


def _text(text: str) -> str:
    """The message without a quoted literal (the baseline keeps none), as in bandit_baseline."""
    return re.sub(r":\s*['\"].*['\"]\s*$", "", text)


def key(item: dict) -> tuple[str, str, str, str, str]:
    return (
        _name(str(item.get("filename", ""))),
        str(item.get("test_id", "")),
        str(item.get("issue_severity", "")),
        str(item.get("issue_confidence", "")),
        _text(str(item.get("issue_text", ""))),
    )


def new_findings(current: list[dict], baseline: list[dict]) -> list[dict]:
    """The findings of `current` that the baseline does not account for (by count per key)."""
    allowed = Counter(key(item) for item in baseline)
    out: list[dict] = []
    for item in sorted(current, key=lambda i: (key(i), i.get("line_number", 0))):
        k = key(item)
        if allowed[k] > 0:
            allowed[k] -= 1
        else:
            out.append(item)
    return out


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    baseline_path: Path | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        out("usage: bandit_check.py BANDIT_JSON")
        return 2
    try:
        current = json.loads(Path(args[0]).read_text("utf-8")).get("results", [])
        accepted = json.loads((baseline_path or BASELINE).read_text("utf-8")).get("results", [])
    except (OSError, ValueError):
        out("error: the scan output or the baseline is not readable JSON")
        return 2
    fresh = new_findings(current, accepted)
    out(f"{len(current)} finding(s) in the scan, {len(accepted)} accepted, {len(fresh)} new")
    for item in fresh:
        out(
            f"NEW {item.get('issue_severity')} {item.get('test_id')} "
            f"{_name(str(item.get('filename')))}:{item.get('line_number')}: "
            f"{item.get('issue_text')}"
        )
    return 1 if fresh else 0


if __name__ == "__main__":
    sys.exit(main())
