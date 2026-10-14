"""Make the accepted-findings baseline of the static scan (spec 17.6, Phase 16).

    uvx bandit==1.9.4 -r app scripts load frontend -q -f json -o bandit_raw.json
    uv run python scripts/bandit_baseline.py bandit_raw.json   # writes .bandit-baseline.json

bandit's own JSON quotes the lines of code it flagged. The baseline keeps NO quoted code: each
entry holds the file, the line, the test id, the severity, the confidence, the text bandit gave and
the reason the owner accepts that kind of finding. What bandit's loader needs is only that the
`code` key exists (it compares a finding by file, test, severity, confidence and text, never by
the code), so the key stays with an empty value.

A finding of a rule that has no accepted reason below stops the script: it is not accepted until
someone writes down why (and a HIGH finding is never accepted by this script).
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".bandit-baseline.json"

# test id -> why a finding of that kind is accepted (docs/SECURITY_SCAN.md has the same table)
ACCEPTED: dict[str, str] = {
    "B608": "SQL text is built only from table and column names and lists of placeholders "
    "written by our code; every value is a bound parameter",
    "B110": "a best-effort clean-up or a never-break-the-request path; the failure is reported "
    "by an event or an alert elsewhere",
    "B101": "an internal consistency check in an admin-machine tool, not a security check",
    "B105": "a setting name, a label or a field name that looks like a password; never a value",
    "B311": "pseudo-random numbers for a load-test choice or a retry wait; nothing secret "
    "depends on it",
    "B603": "starts a program with fixed arguments, no shell and no user text",
    "B607": "starts git or a detached tool by name with fixed arguments, no shell",
    "B404": "imports subprocess for the fixed-argument calls above",
}
KEEP = (
    "filename",
    "line_number",
    "line_range",
    "test_id",
    "test_name",
    "issue_severity",
    "issue_confidence",
    "issue_text",
    "issue_cwe",
    "col_offset",
    "end_col_offset",
    "more_info",
)


def normal_text(text: str) -> str:
    """bandit quotes the flagged literal in some messages (`Possible hardcoded password: 'x'`).
    The literal is dropped: the baseline quotes nothing from the code."""
    return re.sub(r":\s*['\"].*['\"]\s*$", "", text)


def normal_path(name: str) -> str:
    """Forward slashes, so a baseline made on Windows matches a scan on Linux and back."""
    return name.replace(chr(92), "/").lstrip("./")


def strip(raw: dict) -> tuple[dict, list[str]]:
    """The baseline document and the list of problems (empty: all accepted)."""
    problems: list[str] = []
    results = []
    for item in raw.get("results", []):
        test_id = item.get("test_id", "")
        severity = item.get("issue_severity", "")
        where = f"{item.get('filename')}:{item.get('line_number')} {test_id}"
        if severity == "HIGH":
            problems.append(f"{where}: a HIGH finding is fixed, never accepted")
            continue
        if test_id not in ACCEPTED:
            problems.append(f"{where}: no accepted reason is written for {test_id}")
            continue
        entry = {key: item[key] for key in KEEP if key in item}
        entry["filename"] = normal_path(str(entry.get("filename", "")))
        entry["issue_text"] = normal_text(str(entry.get("issue_text", "")))
        entry["code"] = ""  # the key must exist for bandit's loader; no code is quoted
        entry["accepted_because"] = ACCEPTED[test_id]
        results.append(entry)
    results.sort(key=lambda e: (e["filename"], e["line_number"], e["test_id"]))
    document = {
        "_note": "Accepted findings of the static scan, no code quoted (docs/SECURITY_SCAN.md).",
        "errors": [],
        "results": results,
    }
    return document, problems


def main(
    argv: list[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    target: Path | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        out("usage: bandit_baseline.py RAW_BANDIT_JSON")
        return 1
    try:
        raw = json.loads(Path(args[0]).read_text("utf-8"))
    except (OSError, ValueError):
        out("error: the file is not bandit's JSON output")
        return 1
    document, problems = strip(raw)
    if problems:
        for line in problems:
            out(f"not accepted: {line}")
        return 1
    path = target or BASELINE
    path.write_text(json.dumps(document, indent=1) + "\n", "utf-8")
    out(f"wrote {path.name}: {len(document['results'])} accepted finding(s), no code quoted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
