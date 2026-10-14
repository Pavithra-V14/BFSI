"""The nine items of the pre-launch gate (spec 17.6) and the stage checklists (spec 19).

Every item is judged from `docs/READINESS.md` (the score) and `docs/prelaunch_evidence.json` (what
the owner recorded after each run). The judgement rules are code:

- no status, or the status "none", is `NO EVIDENCE`; an unreadable evidence file is also
  `NO EVIDENCE`, and says "evidence file unreadable";
- a status "pass" without a valid citation (evidence.py) is a `FAIL`: a claim is not evidence;
- a pass needs a date (`YYYY-MM-DD`, not in the future). A gate item is `STALE` after 30 days; a
  stage-checklist item with a seven-day window ("no critical alert in the last 7 days", "the
  retention task ran in the last 7 days") after 7 days, the other stage items after 30. A stale
  item is not a pass: it has to be re-checked and re-recorded;
- each item has its own numbers to hold (a held-out pass rate against a threshold the owner set,
  a rollback under 300 seconds, a cost ratio within 25 %, three DISTINCT runs for the band, no open
  critical or high finding in each of three scans ...);
- the gate is judged on the HELD-OUT half of the red-team set only.

What this report cannot do (and prints, every time): the evidence file is typed by a person, so it
is self-attested; a `file:` or `doc:` citation proves the file exists, not what it says; a `run:`
id is checked by its shape only. The gate is a checklist that refuses an empty or inconsistent
claim, not a proof. It is alert only (build rule 5): `render` returns text and the script exits 0
whatever it says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.eval import constants as c
from app.prelaunch import readiness
from app.prelaunch.evidence import ROOT, has_evidence, parse_date, split_citations, valid_citations

EVIDENCE_PATH = ROOT / "docs" / "prelaunch_evidence.json"
PASS, FAIL, NONE, STALE = "PASS", "FAIL", "NO EVIDENCE", "STALE"
ROLLBACK_SECONDS = 300  # spec 17.3: under 5 minutes
COST_TOLERANCE = c.OVER_ESTIMATE_MARGIN  # the same 25 % the evaluation estimate uses [T]
REQUIRED_SCANS = ("pip-audit", "bandit", "gitleaks")
PLACEHOLDER_OWNER = "to be named by the owner"
GATE_MAX_AGE_DAYS = 30
# stage items with a seven-day window go stale after 7 days; every other item after 30
STAGE_MAX_AGE_DAYS = {"no_critical_alert": 7, "retention_task": 7}
UNREADABLE = "evidence file unreadable"

LIMITS_NOTE = (
    "Limits of this report: the evidence file is typed by a person (self-attested); a file: or "
    "doc: citation proves the file exists, not what it says; a run: id is checked by its shape "
    "only. It is a checklist that refuses an empty or inconsistent claim, not a proof."
)

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


def _today(today: date | None) -> date:
    return today or datetime.now(UTC).date()


def load_evidence_checked(path: Path = EVIDENCE_PATH) -> tuple[dict[str, Any], str | None]:
    """(the evidence, None) or ({}, why it could not be read)."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except OSError:
        return {}, UNREADABLE + ": the file is missing"
    except ValueError:
        return {}, UNREADABLE + ": it is not valid JSON"
    if not isinstance(data, dict):
        return {}, UNREADABLE + ": it is not a JSON object"
    return data, None


def load_evidence(path: Path = EVIDENCE_PATH) -> dict[str, Any]:
    return load_evidence_checked(path)[0]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _fresh(entry: dict[str, Any], max_age: int, today: date) -> tuple[str, str] | None:
    """FAIL for a missing, invalid or future date, STALE past `max_age` days, None when fresh."""
    stamp = parse_date(str(entry.get("date", "")))
    if stamp is None:
        return FAIL, "recorded as passed without a date (YYYY-MM-DD)"
    if stamp > today:
        return FAIL, f"the date {stamp.isoformat()} is in the future"
    age = (today - stamp).days
    if age > max_age:
        return STALE, f"recorded {age} days ago, older than {max_age}: re-check and re-record"
    return None


def _generic(
    entry: Any, root: Path, today: date, max_age: int, missing: str
) -> tuple[str, str] | None:
    """The verdict every item shares, or None when the item has its own rules to apply."""
    if not isinstance(entry, dict) or entry.get("status") in (None, "none"):
        return NONE, missing
    status = entry.get("status")
    if status == "fail":
        return FAIL, "recorded as failed"
    if status != "pass":
        return FAIL, f"the status '{status}' is not none, pass or fail"
    if not has_evidence(str(entry.get("evidence", "")), root):
        return FAIL, "recorded as passed without a valid citation (run:, file:, test:, doc:, date:)"
    return _fresh(entry, max_age, today)


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
        distinct = {r.strip().lower() for r in good}
        if len(distinct) < c.VARIANCE_RUNS:
            return FAIL, (
                f"{len(distinct)} distinct valid run ids, {c.VARIANCE_RUNS} different unchanged "
                "runs are needed"
            )
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


def judge_item(
    key: str,
    entry: Any,
    root: Path = ROOT,
    *,
    today: date | None = None,
    max_age: int = GATE_MAX_AGE_DAYS,
    missing: str = "nothing recorded yet",
) -> tuple[str, str]:
    shared = _generic(entry, root, _today(today), max_age, missing)
    if shared is not None:
        return shared
    return _item_rules(key, entry, root)


def judge_readiness(
    root: Path = ROOT, path: Path | None = None, *, today: date | None = None
) -> tuple[str, str]:
    path = path or readiness.READINESS_PATH
    try:
        text = path.read_text("utf-8")
    except OSError:
        return FAIL, "docs/READINESS.md is missing"
    report = readiness.parse(text, root)
    if not report.well_formed:
        return FAIL, "the page is malformed: " + "; ".join(report.problems[:2])
    detail = ", ".join(f"{cat.name} {cat.score:g}/20" for cat in report.categories)
    unproven = [
        line.id
        for cat in report.categories
        for line in cat.lines
        if line.note.startswith("no valid")
    ]
    if unproven:
        shown = ", ".join(unproven[:5]) + (" ..." if len(unproven) > 5 else "")
        return FAIL, (
            f"{len(unproven)} line(s) claim a score without a valid citation and score 0: "
            f"{shown} ({detail})"
        )
    if report.total == 0:
        return NONE, f"no scored line has evidence yet ({detail})"
    if not report.passes:
        return FAIL, f"{report.total:g}/100: {detail}"
    stamp = readiness.scored_on(text)
    if stamp is None:
        return FAIL, "the page has no 'Scored on' date (YYYY-MM-DD)"
    now = _today(today)
    if stamp > now:
        return FAIL, f"the 'Scored on' date {stamp.isoformat()} is in the future"
    age = (now - stamp).days
    if age > GATE_MAX_AGE_DAYS:
        return STALE, f"scored {age} days ago, older than {GATE_MAX_AGE_DAYS}: re-score"
    return PASS, f"{report.total:g}/100: {detail}"


def gate(
    evidence: dict[str, Any] | None = None,
    root: Path = ROOT,
    readiness_path: Path | None = None,
    *,
    today: date | None = None,
    unreadable: str | None = None,
) -> list[Verdict]:
    if evidence is None:
        evidence, unreadable = load_evidence_checked()
    items = evidence.get("gate_items") if isinstance(evidence.get("gate_items"), dict) else {}
    missing = unreadable or "nothing recorded yet"
    out: list[Verdict] = []
    for key, text in GATE_ITEMS:
        if key == "readiness":
            verdict, reason = judge_readiness(root, readiness_path, today=today)
        else:
            verdict, reason = judge_item(key, items.get(key), root, today=today, missing=missing)
        out.append(Verdict(key, text, verdict, reason))
    return out


def stage_checklist(
    stage: str,
    evidence: dict[str, Any] | None = None,
    root: Path = ROOT,
    *,
    today: date | None = None,
    unreadable: str | None = None,
) -> list[Verdict]:
    if stage not in STAGES:
        raise ValueError("unknown stage")
    if evidence is None:
        evidence, unreadable = load_evidence_checked()
    missing = unreadable or "nothing recorded yet"
    checks = evidence.get("stage_checks") if isinstance(evidence.get("stage_checks"), dict) else {}
    mine = checks.get(stage) if isinstance(checks.get(stage), dict) else {}
    gate_items = evidence.get("gate_items") if isinstance(evidence.get("gate_items"), dict) else {}
    out = []
    for key, text in STAGES[stage]:
        if key in ("red_team", "business_owner"):
            verdict, reason = judge_item(
                key, gate_items.get(key), root, today=today, missing=missing
            )
        elif key == "staff_beta_complete":
            parts = stage_checklist(
                "staff_beta", evidence, root, today=today, unreadable=unreadable
            )
            bad = [p for p in parts if p.verdict != PASS]
            if not bad:
                verdict, reason = PASS, "all pass"
            elif all(p.verdict == NONE for p in bad):
                verdict, reason = NONE, f"{len(bad)} of {len(parts)} items are not PASS"
            else:
                verdict, reason = FAIL, f"{len(bad)} of {len(parts)} items are not PASS"
        else:
            verdict, reason = judge_item(
                key,
                mine.get(key),
                root,
                today=today,
                max_age=STAGE_MAX_AGE_DAYS.get(key, GATE_MAX_AGE_DAYS),
                missing=missing,
            )
        out.append(Verdict(key, text, verdict, reason))
    return out


def render(
    verdicts: list[Verdict], title: str, *, markdown: bool = False, note: str | None = None
) -> str:
    counts = {v: sum(1 for x in verdicts if x.verdict == v) for v in (PASS, FAIL, STALE, NONE)}
    lines = [f"# {title}" if markdown else title]
    lines.append(
        f"{counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[STALE]} STALE, {counts[NONE]} NO "
        f"EVIDENCE of {len(verdicts)} items. Alert only: nothing is blocked by this report."
    )
    if note:
        lines.append(("**" + note + "**") if markdown else "WARNING: " + note)
    if markdown:
        lines += ["", "| item | verdict | why |", "|---|---|---|"]
        lines += [f"| {v.text} | {v.verdict} | {v.reason} |" for v in verdicts]
        lines += ["", LIMITS_NOTE]
    else:
        lines += [f"  [{v.verdict}] {v.text}: {v.reason}" for v in verdicts]
        lines += [LIMITS_NOTE]
    return "\n".join(lines) + "\n"


def citations_of(entry: Any) -> list[str]:
    return split_citations(str(entry.get("evidence", ""))) if isinstance(entry, dict) else []
