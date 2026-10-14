"""The pre-launch gate, the readiness score and the stage checklists (Phase 16).

The rules the owner decided are code: a score line with no cited evidence scores 0, the red-team
gate is judged on the held-out half against the owner's threshold, a claim of "pass" without a
citation is a FAIL, nothing here blocks anything, and nothing is invented while no run has been
made."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.prelaunch import evidence, gate, readiness
from scripts import cost_dry_run_compare, prelaunch_gate

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
RUN = "run:" + "a1b2c3d4e5f6"


def entry(**fields):
    base = {"status": "pass", "evidence": RUN}
    base.update(fields)
    return base


def passing_evidence():
    """An evidence file where every item truly passes (a made-up one, for the rules)."""
    return {
        "gate_items": {
            "load_test": entry(),
            "security_scan": entry(
                scans=["pip-audit", "bandit", "gitleaks"], critical_open=0, high_open=0
            ),
            "red_team": entry(held_out_cases=20, held_out_pass_rate=0.95, threshold=0.9),
            "rollback_drill": entry(seconds=210),
            "cost_projection": entry(ratio=1.1),
            "variance_band": entry(runs=[f"run:{c * 8}" for c in "abc"]),
            "access_control": entry(
                evidence="test:tests/test_access_control_matrix.py::"
                "test_every_route_answers_each_kind_of_caller_as_the_table_says " + RUN
            ),
            "business_owner": entry(name="A. Person", evidence="date:2026-10-20"),
        }
    }


def verdicts(ev, root=ROOT):
    return {v.key: v for v in gate.gate(ev, root)}


# ---- citations -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token",
    [
        "run:0123abcd",
        "file:docs/READINESS.md",
        "doc:docs/TARGETS.md",
        "test:tests/test_prelaunch.py::test_a_run_id_must_be_hex",
        "date:2026-10-13",
    ],
)
def test_a_well_formed_citation_of_something_real_is_valid(token):
    assert evidence.check_citation(token).valid


@pytest.mark.parametrize(
    "token",
    [
        "run:xyz",
        "run:123",
        "file:docs/NO_SUCH_PAGE.md",
        "file:../outside.txt",
        "file:/etc/hosts",
        "test:tests/test_prelaunch.py::test_that_does_not_exist",
        "date:2026-13-45",
        "see the report",
        "",
    ],
)
def test_a_citation_that_is_malformed_or_points_at_nothing_is_not_valid(token):
    assert not evidence.check_citation(token).valid


def test_a_run_id_must_be_hex():
    assert not evidence.check_citation("run:ghijklmn").valid


def test_an_evidence_cell_may_hold_several_citations():
    cell = f"`{RUN}`, file:docs/TARGETS.md; nonsense"
    assert [c.text for c in evidence.valid_citations(cell)] == [RUN, "file:docs/TARGETS.md"]
    assert evidence.has_evidence(cell) and not evidence.has_evidence("nonsense")


# ---- the readiness page ----------------------------------------------------------------------
def page(rows_by_category):
    out = []
    for name, rows in rows_by_category:
        out.append(f"## Category: {name} (max 20)\n")
        out.append("| id | What | Max | Score | Evidence | Where |")
        out.append("|---|---|---|---|---|---|")
        out += rows
        out.append("")
    return "\n".join(out)


def full_page(score="0", evidence_cell=""):
    cats = []
    for prefix, name in (
        ("AQ", "Answer quality"),
        ("SG", "Safety"),
        ("RP", "Reliability"),
        ("SA", "Security"),
        ("OD", "Operations"),
    ):
        rows = [f"| {prefix}{i} | line | 5 | {score} | {evidence_cell} | x |" for i in range(1, 5)]
        cats.append((name, rows))
    return page(cats)


def test_the_shipped_page_is_well_formed_and_scores_zero_until_evidence_exists():
    report = readiness.load()
    assert report.well_formed, report.problems
    assert len(report.categories) == 5 and all(c.maximum == 20 for c in report.categories)
    assert report.total == 0 and not report.passes
    assert sum(len(c.lines) for c in report.categories) == 25


def test_a_line_without_a_citation_scores_zero_whatever_is_written():
    report = readiness.parse(full_page(score="5", evidence_cell=""))
    assert report.total == 0
    assert all(
        line.note == "no valid citation: scores 0" for c in report.categories for line in c.lines
    )


def test_a_line_with_a_citation_counts_and_is_cut_to_its_maximum():
    report = readiness.parse(full_page(score="9", evidence_cell="file:docs/TARGETS.md"))
    assert report.total == 100  # nine is cut to five, four lines of five in five categories
    assert all(c.score == 20 for c in report.categories)
    assert report.passes


def test_the_pass_needs_seventy_in_total_and_twelve_in_every_category():
    rows = lambda prefix, scored: [  # noqa: E731
        f"| {prefix}{i} | line | 5 | {5 if i <= scored else 0} | file:docs/TARGETS.md | x |"
        for i in range(1, 5)
    ]
    cats = [
        ("A", rows("AQ", 4)),
        ("B", rows("SG", 4)),
        ("C", rows("RP", 4)),
        ("D", rows("SA", 4)),
        ("E", rows("OD", 2)),  # 10 of 20: below the category floor
    ]
    report = readiness.parse(page(cats))
    assert report.total == 90 and not report.passes, "one category below 12 fails the whole"
    cats[-1] = ("E", rows("OD", 3))  # 15
    assert readiness.parse(page(cats)).passes
    cats[0] = ("A", rows("AQ", 0))  # 70 total would need the rest: 0+20+20+20+15 = 75, but A = 0
    assert not readiness.parse(page(cats)).passes


def test_every_category_above_twelve_is_still_not_enough_below_seventy_in_total():
    def rows(prefix):  # 5 + 5 + 3 + 0 = 13 of 20
        scored = (5, 5, 3, 0)
        return [
            f"| {prefix}{i + 1} | line | 5 | {pts} | file:docs/TARGETS.md | x |"
            for i, pts in enumerate(scored)
        ]

    cats = [
        (n, rows(p)) for n, p in (("A", "AQ"), ("B", "SG"), ("C", "RP"), ("D", "SA"), ("E", "OD"))
    ]
    report = readiness.parse(page(cats))
    assert report.total == 65 and all(c.score >= 12 for c in report.categories)
    assert not report.passes, "65 of 100 is below 70 although no category is below 12"


def test_a_page_that_lost_a_line_or_a_category_is_malformed_not_passed():
    short = full_page(score="5", evidence_cell="file:docs/TARGETS.md").replace(
        "| AQ4 | line | 5 | 5 | file:docs/TARGETS.md | x |", ""
    )
    report = readiness.parse(short)
    assert not report.well_formed and not report.passes
    assert any("add up to 15, not 20" in p for p in report.problems)
    four = "\n".join(full_page().split("## Category: Operations")[0:1])
    assert any("4 categories" in p for p in readiness.parse(four).problems)


def test_the_second_review_line_is_read_and_a_placeholder_is_not_a_review():
    text = (DOCS / "READINESS.md").read_text("utf-8")
    assert readiness.second_review(text) is None
    done = text.replace(
        "- **Second review:** not done", "- **Second review:** done 2026-11-02 by N."
    )
    assert readiness.second_review(done) == "done 2026-11-02 by N."


def test_the_page_says_it_is_conservative_and_re_scored_and_holds_the_terms_section():
    text = (DOCS / "READINESS.md").read_text("utf-8")
    flat = " ".join(text.lower().split())
    assert "scores 0" in flat and "conservative" in flat and "second review" in flat
    for needle in ("one key per provider", "must not assume pooled free keys",
                   "not** to be used to pool free quota", "gemini key was denied",
                   "phase 12 / run 28"):  # fmt: skip
        assert needle in flat, needle


# ---- the nine items --------------------------------------------------------------------------
def test_with_nothing_recorded_no_item_passes_and_none_is_invented():
    result = gate.gate({})
    assert [v.key for v in result] == [k for k, _ in gate.GATE_ITEMS] and len(result) == 9
    assert all(v.verdict != gate.PASS for v in result)
    assert all(v.verdict == gate.NONE for v in result), "nothing recorded is NO EVIDENCE"


def test_the_shipped_evidence_file_records_nothing():
    data = gate.load_evidence()
    assert set(data["gate_items"]) == {k for k, _ in gate.GATE_ITEMS if k != "readiness"}
    assert all(item["status"] == "none" for item in data["gate_items"].values())
    assert data["gate_items"]["business_owner"]["name"] == "to be named by the owner"
    assert all(
        v["status"] == "none" for stage in data["stage_checks"].values() for v in stage.values()
    )


def test_every_item_passes_when_recorded_with_evidence_and_the_numbers_hold(tmp_path):
    readiness_file = tmp_path / "READINESS.md"
    readiness_file.write_text(full_page("5", "file:docs/TARGETS.md"), "utf-8")
    result = gate.gate(passing_evidence(), ROOT, readiness_file)
    assert [v.verdict for v in result] == [gate.PASS] * 9, [(v.key, v.reason) for v in result]


@pytest.mark.parametrize("key", ["load_test", "security_scan", "red_team", "rollback_drill",
                                 "cost_projection", "variance_band", "access_control",
                                 "business_owner"])  # fmt: skip
def test_a_claimed_pass_without_a_citation_is_a_fail(key):
    ev = passing_evidence()
    ev["gate_items"][key]["evidence"] = ""
    if key == "variance_band":
        ev["gate_items"][key]["runs"] = ["nonsense"] * 3
    result = verdicts(ev)[key]
    assert result.verdict == gate.FAIL, (key, result.reason)


def test_a_recorded_failure_is_a_fail():
    ev = passing_evidence()
    ev["gate_items"]["load_test"]["status"] = "fail"
    assert verdicts(ev)["load_test"].verdict == gate.FAIL


@pytest.mark.parametrize(
    ("key", "change", "why"),
    [
        ("security_scan", {"critical_open": 1}, "1 critical"),
        ("security_scan", {"high_open": 2}, "2 high"),
        ("security_scan", {"scans": ["pip-audit", "bandit"]}, "gitleaks"),
        ("security_scan", {"critical_open": None}, "not numbers"),
        ("red_team", {"held_out_pass_rate": 0.7}, "below the threshold"),
        ("red_team", {"held_out_cases": 19}, "20 cases"),
        ("red_team", {"threshold": None}, "has not set the pass threshold"),
        ("red_team", {"threshold": 1.5}, "has not set the pass threshold"),
        ("red_team", {"held_out_pass_rate": 1.2}, "not a number from 0 to 1"),
        ("red_team", {"evidence": "file:docs/TARGETS.md"}, "run id"),
        ("rollback_drill", {"seconds": 301}, "limit is 300"),
        ("rollback_drill", {"seconds": None}, "not a number"),
        ("cost_projection", {"ratio": 1.4}, "25 %"),
        ("cost_projection", {"ratio": 0.7}, "25 %"),
        ("variance_band", {"runs": [f"run:{'a' * 8}", f"run:{'b' * 8}"]}, "3 unchanged runs"),
        ("access_control", {"evidence": RUN}, "route-matrix test"),
        ("access_control", {"evidence": "test:tests/test_access_control_matrix.py::"
                            "test_every_route_answers_each_kind_of_caller_as_the_table_says"},
         "Run 12b"),
        ("load_test", {"evidence": "date:2026-10-20"}, "load report"),
    ],
)  # fmt: skip
def test_each_item_holds_its_own_numbers(key, change, why):
    ev = passing_evidence()
    ev["gate_items"][key].update(change)
    result = verdicts(ev)[key]
    assert result.verdict == gate.FAIL and why in result.reason, result.reason


def test_the_boundaries_pass():
    ev = passing_evidence()
    ev["gate_items"]["rollback_drill"]["seconds"] = 300
    ev["gate_items"]["cost_projection"]["ratio"] = 1.25
    ev["gate_items"]["red_team"].update(held_out_pass_rate=0.9, threshold=0.9)
    result = verdicts(ev)
    for key in ("rollback_drill", "cost_projection", "red_team"):
        assert result[key].verdict == gate.PASS, key


def test_a_placeholder_business_owner_is_no_evidence_not_a_pass():
    ev = passing_evidence()
    ev["gate_items"]["business_owner"]["name"] = "to be named by the owner"
    assert verdicts(ev)["business_owner"].verdict == gate.NONE
    ev["gate_items"]["business_owner"]["name"] = ""
    assert verdicts(ev)["business_owner"].verdict == gate.NONE


def test_the_readiness_item_follows_the_page(tmp_path):
    file = tmp_path / "r.md"
    file.write_text(full_page("0", ""), "utf-8")
    assert gate.judge_readiness(ROOT, file)[0] == gate.NONE
    file.write_text(full_page("5", "file:docs/TARGETS.md"), "utf-8")
    assert gate.judge_readiness(ROOT, file)[0] == gate.PASS
    file.write_text("# nothing here", "utf-8")
    assert gate.judge_readiness(ROOT, file)[0] == gate.FAIL
    assert gate.judge_readiness(ROOT, tmp_path / "missing.md")[0] == gate.FAIL


# ---- the stage checklists --------------------------------------------------------------------
def stage_evidence():
    stages = {
        "staff_beta": {k: {"status": "none", "evidence": ""} for k, _ in gate.STAGES["staff_beta"]},
        "open": {k: {"status": "none", "evidence": ""} for k, _ in gate.STAGES["open"]},
    }
    return {"stage_checks": stages, "gate_items": passing_evidence()["gate_items"]}


def test_a_stage_checklist_lists_its_items_and_the_red_team_item_follows_the_gate():
    ev = stage_evidence()
    result = {v.key: v for v in gate.stage_checklist("staff_beta", ev)}
    assert set(result) == {k for k, _ in gate.STAGES["staff_beta"]}
    assert result["red_team"].verdict == gate.PASS  # from gate_items.red_team
    assert result["baseline_and_band"].verdict == gate.NONE
    ev["gate_items"]["red_team"]["held_out_pass_rate"] = 0.2
    assert {v.key: v for v in gate.stage_checklist("staff_beta", ev)}[
        "red_team"
    ].verdict == gate.FAIL


def test_open_needs_every_staff_beta_item_again_and_its_own():
    ev = stage_evidence()
    result = {v.key: v for v in gate.stage_checklist("open", ev)}
    assert result["staff_beta_complete"].verdict == gate.NONE
    for key in ev["stage_checks"]["staff_beta"]:
        ev["stage_checks"]["staff_beta"][key] = {"status": "pass", "evidence": "date:2026-10-20"}
    result = {v.key: v for v in gate.stage_checklist("open", ev)}
    assert result["staff_beta_complete"].verdict == gate.PASS
    ev["stage_checks"]["staff_beta"]["retention_task"] = {"status": "fail", "evidence": ""}
    result = {v.key: v for v in gate.stage_checklist("open", ev)}
    assert result["staff_beta_complete"].verdict == gate.FAIL


def test_an_unknown_stage_is_refused():
    with pytest.raises(ValueError):
        gate.stage_checklist("everyone", {})


# ---- the script: alert only, never overwrites ------------------------------------------------
class Lines(list):
    def __call__(self, text):
        self.append(str(text))

    @property
    def text(self):
        return "\n".join(self)


def test_the_script_exits_zero_whatever_the_report_says():
    out = Lines()
    assert prelaunch_gate.main([], out=out, evidence={}) == 0
    assert "Alert only: nothing is blocked" in out.text and "FAIL" in out.text
    out = Lines()
    assert prelaunch_gate.main(["--stage", "open"], out=out, evidence={}) == 0
    assert "Stage checklist: open" in out.text


def test_the_report_is_saved_once_and_never_overwritten(tmp_path):
    when = datetime(2026, 10, 20, tzinfo=UTC)
    out = Lines()
    prelaunch_gate.main(["--write"], out=out, evidence={}, folder=tmp_path, now=when)
    saved = tmp_path / "2026-10-20-prelaunch-gate.md"
    assert saved.is_file() and "| item | verdict | why |" in saved.read_text("utf-8")
    first = saved.read_text("utf-8")
    out = Lines()
    prelaunch_gate.main(
        ["--write"], out=out, evidence=passing_evidence(), folder=tmp_path, now=when
    )
    assert "nothing was overwritten" in out.text and saved.read_text("utf-8") == first


# ---- the dry-run comparison ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("projected", "measured", "word"),
    [(3000, 3000, "within"), (3000, 3750, "within"), (3000, 3751, "over"),
     (3000, 2250, "within"), (3000, 2249, "under")],
)  # fmt: skip
def test_the_dry_run_is_within_a_quarter_of_the_projection_or_not(projected, measured, word):
    assert cost_dry_run_compare.compare(projected, measured)[1] == word


def test_the_comparison_prints_the_ratio_and_asks_for_it_to_be_recorded():
    out = Lines()
    code = cost_dry_run_compare.main(["--measured-tokens-per-query", "9000"], out=out)
    assert (
        code == 0 and "ratio measured/projected" in out.text and "(over the 25% margin)" in out.text
    )
    assert "replaced by the measured ones" in out.text and "prelaunch_evidence.json" in out.text
    bad = Lines()
    assert cost_dry_run_compare.main(["--measured-tokens-per-query", "0"], out=bad) == 1


# ---- the docs --------------------------------------------------------------------------------
def test_the_targets_page_names_every_target_its_rule_and_the_unsigned_floors():
    from app.eval import targets

    text = (DOCS / "TARGETS.md").read_text("utf-8")
    flat = " ".join(text.lower().split())
    for rule in ("regression floor", "ceiling", "absolute floor", "zero rule"):
        assert rule in flat, rule
    assert "baseline mean **minus the variance band**" in text and "plus 20 %" in text
    assert "held-out half only" in flat and "20 development" in flat
    shown = flat.replace("-", " ")
    for target in targets.TARGETS:
        name = target.name.removesuffix("_ms").replace("_", " ")
        assert name in shown, target.name
    assert "to be signed by the owner" in text and "200,000" in text and "2 working days" in text
    assert "two weeks" in flat and "to be named by the owner" in text


def test_the_gate_and_the_audit_pages_exist_and_say_what_the_owner_decided():
    audit = (DOCS / "ACCESS_CONTROL_AUDIT.md").read_text("utf-8")
    flat_audit = " ".join(audit.replace("**", "").split())
    assert "independent second review is required before any real customer data" in flat_audit
    assert "Run 12b" in audit and "test_access_control_matrix" in audit
    scan = (DOCS / "SECURITY_SCAN.md").read_text("utf-8")
    for needle in (
        "requirements-frontend.txt",
        "requirements-ingest.txt",
        "requirements-eval.txt",
        ".bandit-baseline.json",
        "alert only",
        "B608",
    ):
        assert needle in scan, needle


def test_bandit_is_a_pinned_dev_package_and_never_on_a_service():
    dev = (ROOT / "requirements-dev.txt").read_text("utf-8")
    assert re.search(r"^bandit==\d+\.\d+\.\d+$", dev, re.M)
    for name in ("requirements.txt", "requirements-frontend.txt"):
        assert "bandit" not in (ROOT / name).read_text("utf-8"), "never on a service"
