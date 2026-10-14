"""The accepted-findings baseline of the static scan (Phase 16).

It holds no quoted code (so the secret scan has nothing to misread and nothing is leaked by
accident), each entry says why it is accepted, no HIGH finding is in it, and the comparison with a
scan is by file, rule, severity, confidence and text: the same on Windows and on Linux."""

import json
import re
from pathlib import Path

import pytest

from scripts import bandit_baseline, bandit_check

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".bandit-baseline.json"


@pytest.fixture(scope="module")
def baseline():
    return json.loads(BASELINE.read_text("utf-8"))


def finding(**fields):
    base = {
        "filename": "app/db/example.py",
        "line_number": 10,
        "line_range": [10],
        "test_id": "B608",
        "test_name": "hardcoded_sql_expressions",
        "issue_severity": "MEDIUM",
        "issue_confidence": "LOW",
        "issue_text": "Possible SQL injection vector through string-based query construction.",
        "issue_cwe": {"id": 89, "link": "x"},
        "code": "10     sql = f\"SELECT {name} FROM t WHERE token = 'abc123secret'\"",
    }
    base.update(fields)
    return base


# ---- the shipped file ------------------------------------------------------------------------
def test_the_baseline_holds_no_code_field_value(baseline):
    assert baseline["results"], "a baseline with no entries is not the accepted list"
    for entry in baseline["results"]:
        assert entry["code"] == "", entry["filename"]  # the key exists for bandit's loader only


def test_nothing_in_the_file_looks_like_quoted_code_or_a_secret():
    text = BASELINE.read_text("utf-8")
    assert not re.search(r"\bf\"|\bf'|SELECT |INSERT |UPDATE |DELETE FROM|\.execute\(", text)
    assert not re.search(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"']{6,}", text)
    assert "\\\\" not in text, "file names use forward slashes only"


def test_every_entry_says_why_it_is_accepted_and_none_is_high(baseline):
    for entry in baseline["results"]:
        assert entry["issue_severity"] in ("LOW", "MEDIUM")
        assert entry["test_id"] in bandit_baseline.ACCEPTED
        assert entry["accepted_because"] == bandit_baseline.ACCEPTED[entry["test_id"]]
        assert entry["accepted_because"].strip()
        assert "/" in entry["filename"] and not entry["filename"].startswith(("./", "/"))


def test_every_accepted_rule_is_explained_on_the_security_page():
    page = (ROOT / "docs" / "SECURITY_SCAN.md").read_text("utf-8")
    for rule in bandit_baseline.ACCEPTED:
        assert rule in page, f"{rule} is accepted in the script but not explained in the page"


def test_the_accepted_files_still_exist(baseline):
    for entry in baseline["results"]:
        assert (ROOT / entry["filename"]).is_file(), entry["filename"]


# ---- making the baseline ---------------------------------------------------------------------
def test_stripping_removes_the_code_and_normalises_the_path():
    document, problems = bandit_baseline.strip(
        {"results": [finding(filename="app\\db\\example.py")]}
    )
    assert problems == []
    (entry,) = document["results"]
    assert entry["code"] == "" and entry["filename"] == "app/db/example.py"
    assert "abc123secret" not in json.dumps(document)
    assert entry["accepted_because"].startswith("SQL text is built only from")


def test_a_high_finding_and_an_unexplained_rule_are_never_accepted():
    document, problems = bandit_baseline.strip(
        {
            "results": [
                finding(issue_severity="HIGH", test_id="B613"),
                finding(test_id="B999", issue_severity="LOW"),
            ]
        }
    )
    assert document["results"] == []
    assert any("HIGH finding is fixed" in p for p in problems)
    assert any("no accepted reason is written for B999" in p for p in problems)


def test_the_script_writes_nothing_when_a_finding_is_not_accepted(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"results": [finding(issue_severity="HIGH")]}), "utf-8")
    target = tmp_path / "baseline.json"
    lines: list[str] = []
    code = bandit_baseline.main([str(raw)], out=lines.append, target=target)
    assert code == 1 and not target.exists() and any("not accepted" in x for x in lines)


def test_a_quoted_literal_in_bandits_message_is_not_kept_and_still_matches():
    quoted = finding(test_id="B105", issue_text="Possible hardcoded password: 'bearer'")
    document, problems = bandit_baseline.strip({"results": [quoted]})
    assert problems == []
    assert document["results"][0]["issue_text"] == "Possible hardcoded password"
    assert "bearer" not in json.dumps(document)
    # the scan still says it with the literal; the comparison drops it on both sides
    assert bandit_check.new_findings([quoted], document["results"]) == []


# ---- comparing a scan with it ----------------------------------------------------------------
def test_a_scan_that_matches_the_baseline_has_no_new_finding_whatever_the_slashes():
    accepted = [dict(finding(filename="app/db/example.py"), code="")]
    now = [finding(filename="app\\db\\example.py", line_number=99)]  # moved, other platform
    assert bandit_check.new_findings(now, accepted) == []


def test_a_second_finding_of_the_same_kind_in_the_same_file_is_new():
    accepted = [dict(finding(), code="")]
    now = [finding(line_number=10), finding(line_number=40)]
    fresh = bandit_check.new_findings(now, accepted)
    assert [f["line_number"] for f in fresh] == [40]


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "app/db/other.py"},
        {"test_id": "B101"},
        {"issue_severity": "HIGH"},
        {"issue_confidence": "HIGH"},
        {"issue_text": "Another thing entirely."},
    ],
)
def test_a_finding_that_differs_in_any_matched_field_is_new(change):
    accepted = [dict(finding(), code="")]
    assert len(bandit_check.new_findings([finding(**change)], accepted)) == 1


def test_the_command_prints_new_findings_and_exits_one(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(json.dumps({"results": [dict(finding(), code="")]}), "utf-8")
    scan = tmp_path / "scan.json"
    scan.write_text(json.dumps({"results": [finding(), finding(line_number=55)]}), "utf-8")
    lines: list[str] = []
    code = bandit_check.main([str(scan)], out=lines.append, baseline_path=base)
    assert code == 1
    assert "2 finding(s) in the scan, 1 accepted, 1 new" in lines[0]
    assert any(x.startswith("NEW MEDIUM B608 app/db/example.py:55") for x in lines)
    scan.write_text(json.dumps({"results": [finding()]}), "utf-8")
    assert bandit_check.main([str(scan)], out=lambda t: None, baseline_path=base) == 0


def test_an_unreadable_scan_or_baseline_is_exit_two_not_a_pass(tmp_path):
    lines: list[str] = []
    assert bandit_check.main([str(tmp_path / "missing.json")], out=lines.append) == 2
    assert bandit_check.main([], out=lines.append) == 2


def test_the_ci_job_compares_with_the_script_and_stays_alert_only():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    job = ci[ci.index("\n  bandit:") :]
    job = job[: job.index("\n\n")]
    assert "continue-on-error: true" in job
    assert "scripts/bandit_check.py" in job and "-f json" in job
    assert re.search(r"bandit==\d+\.\d+\.\d+", job)
