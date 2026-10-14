"""The nine items of the pre-launch gate (spec 17.6) and the stage checklists (spec 19).

Every item is judged from `docs/READINESS.md` (the score) and `docs/prelaunch_evidence.json` (what
the owner recorded after each run). The judgement rules are code:

- no status, or the status "none", is `NO EVIDENCE`;
- a status "pass" without a valid citation (evidence.py) is a `FAIL`: a claim is not evidence;
- each item has its own numbers to hold (a held-out pass rate against a threshold the owner set,
  a rollback under 300 seconds, a cost ratio within 25 %, three runs for the band, no open critical
  or high finding in each of three scans ...);
- the gate is judged on the HELD-OUT half of the red-team set only.

Alert only (build rule 5): `render` returns text and the script exits 0 whatever it says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.eval import constants as c
from app.prelaunch import readiness
from app.prelaunch.evidence import ROOT, has_evidence, split_citations, valid_citations

EVIDENCE_PATH = ROOT / "docs" / "prelaunch_evidence.json"
PASS, FAIL, NONE = "PASS", "FAIL", "NO EVIDENCE"
ROLLBACK_SECONDS = 300  # spec 17.3: under 5 minutes
COST_TOLERANCE = c.OVER_ESTIMATE_MARGIN  # the same 25 % the evaluation estimate uses [T]
REQUIRED_SCANS = ("pip-audit", "bandit", "gitleaks")
PLACEHOLDER_OWNER = "to be named by the owner"

# (key in the evidence file, the spec 17.6 words)
GATE_ITEMS: tuple[tuple[str, str], ...] = (
    ("readiness", "production-readiness score of at least 70/100, no category below 12/20"),
    ("load_test", "a load test passed"),
    ("security_scan", "a security scan with no open critical or high findings"),
    ("red_team", "the red-team set passing (held-out half)"),
    ("rollback_drill", "a rollback drill (under 5 minutes)"),
    ("cost_projection", "a cost projection compared with a dry run"),
    ("variance_band", "the evaluation set and variance band established"),
    ("access_control", "the access-control audit"),
    ("business_owner", "a named business owner"),
)

STAGES: dict[str, tuple[tuple[str, str], ...]] = {
    "staff_beta": (
        ("baseline_and_band", "a baseline run and the variance band exist; golden score inside it"),
        ("production_sample", "the production sample is written (masked) and judged (Run 22)"),
        ("recorded_and_real_run", "recorded provider tests and a short real-provider run, clean"),
        ("no_critical_alert", "no critical alert in the last 7 days"),
        ("retention_task", "the retention task has run in the last 7 days"),
        ("red_team", "the red-team set passes (held-out half)"),
        ("release_record", "the release record for this stage exists in docs/releases/"),
        ("data_flow_read", "docs/DATA_FLOW.md read and agreed"),
        ("shadow_period", "the shadow period (planned two weeks) is complete"),
    ),
    "open": (
        ("staff_beta_complete", "every item of staff_beta is true again"),
        ("business_owner", "a named business owner"),
        ("data_terms", "the compliance advisor's answer on the data terms (13.5, 13.7)"),
        ("second_review", "the independent second review of access control and the score is done"),
        (
            "paid_or_zero_retention",
            "paid or zero-retention plans are in place for real customer data",
        ),
    ),
}


@dataclass(frozen=True)
class Verdict:
    key: str
    text: str
    verdict: str
    reason: str


def load_evidence(path: Path = EVIDENCE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _generic(entry: Any, root: Path) -> tuple[str, str] | None:
    """The verdict every item shares, or None when the item has its own rules to apply."""
    if not isinstance(entry, dict) or entry.get("status") in (None, "none"):
        return NONE, "nothing recorded yet"
    status = entry.get("status")
    if status == "fail":
        return FAIL, "recorded as failed"
    if status != "pass":
        return FAIL, f"the status '{status}' is not none, pass or fail"
    if not has_evidence(str(entry.get("evidence", "")), root):
        return FAIL, "recorded as passed without a valid citation (run:, file:, test:, doc:, date:)"
    return None


def _item_rules(key: str, entry: dict[str, Any], root: Path) -> tuple[str, str]:
    cites = valid_citations(str(entry.get("evidence", "")), root)
    kinds = {c_.text.split(":", 1)[0] for c_ in cites}
    if key == "security_scan":
        scans = entry.get("scans") or []
        missing = [s for s in REQUIRED_SCANS if s not in scans]
        critical, high = _number(entry.get("critical_open")), _number(entry.get("high_open"))
        if missing:
            return FAIL, "scans missing: " + ", ".join(missing)
        if critical is None or high is None:
            return FAIL, "the open critical and high counts are not numbers"
        if critical or high:
            return FAIL, f"{int(critical)} critical and {int(high)} high finding(s) are open"
    elif key == "red_team":
        rate, threshold = _number(entry.get("held_out_pass_rate")), _number(entry.get("threshold"))
        if entry.get("held_out_cases") != 20:
            return FAIL, "the held-out half has 20 cases; the result must be for all of them"
        if threshold is None or not 0 < threshold <= 1:
            return FAIL, "the owner has not set the pass threshold (a number above 0 up to 1)"
        if rate is None or not 0 <= rate <= 1:
            return FAIL, "the held-out pass rate is not a number from 0 to 1"
        if rate < threshold:
            return FAIL, f"held-out pass rate {rate:g} is below the threshold {threshold:g}"
        if "run" not in kinds:
            return FAIL, "the run id of the held-out run is not cited"
    elif key == "rollback_drill":
        seconds = _number(entry.get("seconds"))
        if seconds is None or seconds <= 0:
            return FAIL, "the time of the drill is not a number of seconds"
        if seconds > ROLLBACK_SECONDS:
            return FAIL, f"the rollback took {seconds:g} s, the limit is {ROLLBACK_SECONDS} s"
    elif key == "cost_projection":
        ratio = _number(entry.get("ratio"))
        if ratio is None or ratio <= 0:
            return FAIL, "the dry-run to projection ratio is not a number"
        if abs(ratio - 1) > COST_TOLERANCE:
            return FAIL, f"ratio {ratio:g} is more than {COST_TOLERANCE * 100:.0f} % away from 1"
    elif key == "variance_band":
        runs = [r for r in (entry.get("runs") or []) if isinstance(r, str)]
        good = [r for r in runs if has_evidence(r, root)]
        if len(good) < c.VARIANCE_RUNS:
            return FAIL, f"{len(good)} valid run ids, {c.VARIANCE_RUNS} unchanged runs are needed"
    elif key == "access_control":
        if "test" not in kinds or "run" not in kinds:
            return FAIL, "needs the route-matrix test (test:) and the Run 12b run id (run:)"
    elif key == "load_test":
        if "run" not in kinds and "file" not in kinds:
            return FAIL, "cite the load report (run: or file:)"
    elif key == "business_owner":
        name = str(entry.get("name", "")).strip()
        if not name or PLACEHOLDER_OWNER in name.lower():
            return NONE, "the owner has not named a business owner"
    return PASS, "recorded with evidence"


def judge_item(key: str, entry: Any, root: Path = ROOT) -> tuple[str, str]:
    shared = _generic(entry, root)
    if shared is not None:
        return shared
    return _item_rules(key, entry, root)


def judge_readiness(root: Path = ROOT, path: Path | None = None) -> tuple[str, str]:
    path = path or readiness.READINESS_PATH
    try:
        report = readiness.load(path, root)
    except OSError:
        return FAIL, "docs/READINESS.md is missing"
    if not report.well_formed:
        return FAIL, "the page is malformed: " + "; ".join(report.problems[:2])
    detail = ", ".join(f"{cat.name} {cat.score:g}/20" for cat in report.categories)
    if report.total == 0:
        return NONE, f"no scored line has evidence yet ({detail})"
    if not report.passes:
        return FAIL, f"{report.total:g}/100: {detail}"
    return PASS, f"{report.total:g}/100: {detail}"


def gate(evidence: dict[str, Any] | None = None, root: Path = ROOT,
         readiness_path: Path | None = None) -> list[Verdict]:  # fmt: skip
    evidence = load_evidence() if evidence is None else evidence
    items = evidence.get("gate_items") if isinstance(evidence.get("gate_items"), dict) else {}
    out: list[Verdict] = []
    for key, text in GATE_ITEMS:
        if key == "readiness":
            verdict, reason = judge_readiness(root, readiness_path)
        else:
            verdict, reason = judge_item(key, items.get(key), root)
        out.append(Verdict(key, text, verdict, reason))
    return out


def stage_checklist(stage: str, evidence: dict[str, Any] | None = None,
                    root: Path = ROOT) -> list[Verdict]:  # fmt: skip
    if stage not in STAGES:
        raise ValueError("unknown stage")
    evidence = load_evidence() if evidence is None else evidence
    checks = evidence.get("stage_checks") if isinstance(evidence.get("stage_checks"), dict) else {}
    mine = checks.get(stage) if isinstance(checks.get(stage), dict) else {}
    out = []
    for key, text in STAGES[stage]:
        if key == "red_team":
            verdict, reason = judge_item(
                "red_team", (evidence.get("gate_items") or {}).get(key), root
            )
        elif key == "business_owner":
            verdict, reason = judge_item(key, (evidence.get("gate_items") or {}).get(key), root)
        elif key == "staff_beta_complete":
            parts = stage_checklist("staff_beta", evidence, root)
            bad = [p for p in parts if p.verdict != PASS]
            verdict = PASS if not bad else (NONE if all(p.verdict == NONE for p in bad) else FAIL)
            reason = "all pass" if not bad else f"{len(bad)} of {len(parts)} items are not PASS"
        else:
            verdict, reason = judge_item(key, mine.get(key), root)
        out.append(Verdict(key, text, verdict, reason))
    return out


def render(verdicts: list[Verdict], title: str, *, markdown: bool = False) -> str:
    counts = {v: sum(1 for x in verdicts if x.verdict == v) for v in (PASS, FAIL, NONE)}
    lines = [f"# {title}" if markdown else title]
    lines.append(
        f"{counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[NONE]} NO EVIDENCE "
        f"of {len(verdicts)} items. Alert only: nothing is blocked by this report."
    )
    if markdown:
        lines += ["", "| item | verdict | why |", "|---|---|---|"]
        lines += [f"| {v.text} | {v.verdict} | {v.reason} |" for v in verdicts]
    else:
        lines += [f"  [{v.verdict}] {v.text}: {v.reason}" for v in verdicts]
    return "\n".join(lines) + "\n"


def citations_of(entry: Any) -> list[str]:
    return split_citations(str(entry.get("evidence", ""))) if isinstance(entry, dict) else []
