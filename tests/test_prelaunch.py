"""The pre-launch gate, the readiness score and the stage checklists (Phase 16).

The rules the owner decided are code: a score line with no cited evidence scores 0, the red-team
gate is judged on the held-out half against the owner's threshold, a claim of "pass" without a
citation is a FAIL, nothing here blocks anything, and nothing is invented while no run has been
made."""

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.prelaunch import evidence, gate, readiness
from scripts import cost_dry_run_compare, prelaunch_gate

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
RUN = "run:" + "a1b2c3d4e5f6"
TODAY = date(2026, 10, 20)
FRESH = "2026-10-15"


def entry(**fields):
    base = {"status": "pass", "evidence": RUN, "date": FRESH}
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
    return {v.key: v for v in gate.gate(ev, root, today=TODAY)}


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
    return page(cats) + "\n- **Scored on:** " + FRESH + "\n"


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
    result = gate.gate({}, today=TODAY)
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
    result = gate.gate(passing_evidence(), ROOT, readiness_file, today=TODAY)
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
        (
            "variance_band",
            {"runs": [f"run:{'a' * 8}", f"run:{'b' * 8}"]},
            "2 distinct valid run ids",
        ),
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
    assert gate.judge_readiness(ROOT, file, today=TODAY)[0] == gate.NONE
    file.write_text(full_page("5", "file:docs/TARGETS.md"), "utf-8")
    assert gate.judge_readiness(ROOT, file, today=TODAY)[0] == gate.PASS
    file.write_text("# nothing here", "utf-8")
    assert gate.judge_readiness(ROOT, file, today=TODAY)[0] == gate.FAIL
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
    result = {v.key: v for v in gate.stage_checklist("staff_beta", ev, today=TODAY)}
    assert set(result) == {k for k, _ in gate.STAGES["staff_beta"]}
    assert result["red_team"].verdict == gate.PASS  # from gate_items.red_team
    assert result["baseline_and_band"].verdict == gate.NONE
    ev["gate_items"]["red_team"]["held_out_pass_rate"] = 0.2
    assert {v.key: v for v in gate.stage_checklist("staff_beta", ev, today=TODAY)}[
        "red_team"
    ].verdict == gate.FAIL


def test_open_needs_every_staff_beta_item_again_and_its_own():
    ev = stage_evidence()
    result = {v.key: v for v in gate.stage_checklist("open", ev, today=TODAY)}
    assert result["staff_beta_complete"].verdict == gate.NONE
    for key in ev["stage_checks"]["staff_beta"]:
        ev["stage_checks"]["staff_beta"][key] = {
            "status": "pass",
            "evidence": "date:2026-10-20",
            "date": FRESH,
        }
    result = {v.key: v for v in gate.stage_checklist("open", ev, today=TODAY)}
    assert result["staff_beta_complete"].verdict == gate.PASS
    ev["stage_checks"]["staff_beta"]["retention_task"] = {"status": "fail", "evidence": ""}
    result = {v.key: v for v in gate.stage_checklist("open", ev, today=TODAY)}
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


# ---- round 2: dates, staleness, distinct runs, unreadable file, exclusive write ---------------
def days_ago(n):
    from datetime import timedelta

    return (TODAY - timedelta(days=n)).isoformat()


def test_a_pass_without_a_date_or_with_a_bad_or_future_date_is_a_fail():
    for bad in (None, "", "yesterday", "2026-13-40", "20/10/2026", "2026-10-21"):
        ev = passing_evidence()
        if bad is None:
            del ev["gate_items"]["load_test"]["date"]
        else:
            ev["gate_items"]["load_test"]["date"] = bad
        result = verdicts(ev)["load_test"]
        assert result.verdict == gate.FAIL, (bad, result.reason)
        assert "date" in result.reason


def test_a_gate_item_goes_stale_after_thirty_days_not_before():
    ev = passing_evidence()
    ev["gate_items"]["load_test"]["date"] = days_ago(30)
    assert verdicts(ev)["load_test"].verdict == gate.PASS
    ev["gate_items"]["load_test"]["date"] = days_ago(31)
    stale = verdicts(ev)["load_test"]
    assert stale.verdict == gate.STALE and "31 days ago" in stale.reason
    assert stale.verdict != gate.PASS


def test_the_seven_day_stage_items_go_stale_after_seven_days_the_others_after_thirty():
    ev = stage_evidence()
    for key in ("no_critical_alert", "retention_task", "data_flow_read"):
        ev["stage_checks"]["staff_beta"][key] = {
            "status": "pass",
            "evidence": "date:2026-10-10",
            "date": days_ago(8),
        }
    result = {v.key: v for v in gate.stage_checklist("staff_beta", ev, today=TODAY)}
    assert result["no_critical_alert"].verdict == gate.STALE
    assert result["retention_task"].verdict == gate.STALE
    assert result["data_flow_read"].verdict == gate.PASS, "a one-off check lasts 30 days"
    ev["stage_checks"]["staff_beta"]["retention_task"]["date"] = days_ago(7)
    result = {v.key: v for v in gate.stage_checklist("staff_beta", ev, today=TODAY)}
    assert result["retention_task"].verdict == gate.PASS


def test_a_stale_staff_beta_item_stops_open_from_passing():
    ev = stage_evidence()
    for key in ev["stage_checks"]["staff_beta"]:
        ev["stage_checks"]["staff_beta"][key] = {
            "status": "pass",
            "evidence": "date:2026-10-10",
            "date": FRESH,
        }
    ev["stage_checks"]["staff_beta"]["retention_task"]["date"] = days_ago(9)
    result = {v.key: v for v in gate.stage_checklist("open", ev, today=TODAY)}
    assert result["staff_beta_complete"].verdict == gate.FAIL


def test_the_readiness_page_needs_a_date_and_goes_stale_too(tmp_path):
    good = full_page("5", "file:docs/TARGETS.md")
    file = tmp_path / "r.md"
    file.write_text(good, "utf-8")
    assert gate.judge_readiness(ROOT, file, today=TODAY)[0] == gate.PASS
    file.write_text(good.replace(FRESH, days_ago(31)), "utf-8")
    assert gate.judge_readiness(ROOT, file, today=TODAY)[0] == gate.STALE
    file.write_text(good.split("- **Scored on:**")[0], "utf-8")
    verdict, why = gate.judge_readiness(ROOT, file, today=TODAY)
    assert verdict == gate.FAIL and "Scored on" in why
    file.write_text(good.replace(FRESH, "2026-10-21"), "utf-8")
    assert "future" in gate.judge_readiness(ROOT, file, today=TODAY)[1]


def test_a_line_that_claims_points_without_a_valid_citation_makes_the_item_fail_naming_it(tmp_path):
    rows = [
        "| AQ1 | line | 5 | 5 | see the report | x |",
        "| AQ2 | line | 5 | 5 | file:docs/TARGETS.md | x |",
        "| AQ3 | line | 5 | 0 | | x |",
        "| AQ4 | line | 5 | 0 | | x |",
    ]
    cats = [("Answer quality", rows)] + [
        (n, [f"| {p}{i} | line | 5 | 0 | | x |" for i in range(1, 5)])
        for n, p in (("B", "SG"), ("C", "RP"), ("D", "SA"), ("E", "OD"))
    ]
    file = tmp_path / "r.md"
    file.write_text(page(cats), "utf-8")
    verdict, why = gate.judge_readiness(ROOT, file, today=TODAY)
    assert verdict == gate.FAIL
    assert "1 line(s) claim a score without a valid citation" in why and "AQ1" in why


def test_the_same_run_id_three_times_is_not_three_runs():
    ev = passing_evidence()
    same = "run:" + "a" * 8
    ev["gate_items"]["variance_band"]["runs"] = [same, same, same.upper()]
    result = verdicts(ev)["variance_band"]
    assert result.verdict == gate.FAIL and "1 distinct valid run ids" in result.reason
    ev["gate_items"]["variance_band"]["runs"] = [same, "run:" + "b" * 8, "run:" + "c" * 8]
    assert verdicts(ev)["variance_band"].verdict == gate.PASS


def test_a_missing_or_corrupt_evidence_file_says_unreadable_not_just_no_evidence(tmp_path):
    missing, why = gate.load_evidence_checked(tmp_path / "nope.json")
    assert missing == {} and why == "evidence file unreadable: the file is missing"
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", "utf-8")
    assert "not valid JSON" in gate.load_evidence_checked(broken)[1]
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2]", "utf-8")
    assert "not a JSON object" in gate.load_evidence_checked(listy)[1]
    result = gate.gate({}, today=TODAY, unreadable=why)
    assert all(v.verdict == gate.NONE for v in result)
    assert all(why in v.reason for v in result if v.key != "readiness")
    out = Lines()
    prelaunch_gate.main([], out=out, evidence={})
    assert "WARNING" not in out.text


def test_the_output_says_what_the_report_cannot_prove_every_time():
    out = Lines()
    prelaunch_gate.main([], out=out, evidence={})
    assert "self-attested" in out.text
    assert "file: or doc: citation proves the file exists" in out.text
    assert "run: id is checked by its shape only" in out.text
    stage = Lines()
    prelaunch_gate.main(["--stage", "staff_beta"], out=stage, evidence={})
    assert "self-attested" in stage.text


def test_the_summary_line_counts_stale_apart_from_fail_and_no_evidence():
    ev = passing_evidence()
    ev["gate_items"]["load_test"]["date"] = days_ago(40)
    ev["gate_items"]["rollback_drill"]["seconds"] = 999
    text = gate.render(gate.gate(ev, today=TODAY), "t")
    assert "1 STALE" in text and "FAIL" in text and "NO EVIDENCE" in text


def test_the_script_opens_the_report_for_exclusive_creation(tmp_path, monkeypatch):
    """Even if the file appears between the check and the write, it is not overwritten."""
    when = datetime(2026, 10, 20, tzinfo=UTC)
    target = tmp_path / "2026-10-20-prelaunch-gate.md"
    target.write_text("an earlier report", "utf-8")
    real = Path.exists
    monkeypatch.setattr(Path, "exists", lambda self: False if self == target else real(self))
    out = Lines()
    prelaunch_gate.main(["--write"], out=out, evidence={}, folder=tmp_path, now=when)
    assert "nothing was overwritten" in out.text
    assert target.read_text("utf-8") == "an earlier report"


# ---- round 2: the docs are checked against the code, not only for phrases -----------------------
def _table_rows(text, heading):
    """The rows (cells) of the first markdown table after a heading."""
    section = text[text.index(heading) :]
    rows = []
    for line in section.split("\n")[1:]:
        if line.startswith("|---"):
            continue
        if line.startswith("|"):
            rows.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif rows:
            break
    return rows


def test_every_target_of_the_page_has_the_rule_the_code_gives_it():
    from app.eval import targets

    page = (DOCS / "TARGETS.md").read_text("utf-8")
    rows = _table_rows(page, "## A. Targets the baseline sets")
    shown = {r[0].lower().replace("-", " "): r[1].lower() for r in rows[1:]}
    word = {
        targets.REGRESSION: "regression floor",
        targets.CEILING: "ceiling",
        targets.ABSOLUTE: "absolute floor",
        targets.ZERO: "zero rule",
    }
    assert len(shown) == len(targets.TARGETS)
    for target in targets.TARGETS:
        name = target.name.removesuffix("_ms").replace("_", " ")
        found = [rule for label, rule in shown.items() if label.startswith(name.split(" rate")[0])]
        assert found, target.name
        assert word[target.rule] in found[0], (target.name, found[0], target.rule)


def test_the_page_names_the_two_halves_the_threshold_and_the_ceiling_of_the_code():
    from app.eval import redteam, targets

    page = (DOCS / "TARGETS.md").read_text("utf-8")
    assert f"**{redteam.HALF} development**" in page and f"**{redteam.HALF} held-out**" in page
    assert f"baseline plus {int(targets.CEILING_MARGIN * 100)} %" in page
    assert f"plus {int(targets.CEILING_MARGIN * 100)} %" in page
    assert redteam.DEV_PREFIX in page and redteam.HELD_PREFIX in page


def test_the_readiness_page_has_the_categories_and_points_the_code_expects():
    text = (DOCS / "READINESS.md").read_text("utf-8")
    report = readiness.parse(text)
    assert report.well_formed and len(report.categories) == readiness.CATEGORY_COUNT
    assert [c.name.split(" (")[0] for c in report.categories] == [
        "Answer quality",
        "Safety and guardrails",
        "Reliability and performance",
        "Security and access control",
        "Operations and delivery",
    ]
    assert all(c.maximum == readiness.CATEGORY_MAX for c in report.categories)
    # every line names where its evidence comes from, and none is already scored
    for category in report.categories:
        for line in category.lines:
            assert line.claimed == 0 and line.evidence == "", line.id
    assert str(readiness.PASS_TOTAL) in text and str(readiness.PASS_CATEGORY) in text


def test_the_terms_of_service_section_matches_what_the_estimate_and_the_inventory_do():
    from app.eval import estimate
    from scripts import key_inventory

    assert estimate.ONE_KEY_PER_PROVIDER is True
    limits = estimate.daily_limits(
        __import__("tests.test_eval_estimate", fromlist=["config"]).config(
            groq_keys=["a" * 12, "b" * 12, "c" * 12]
        )
    )
    one_key = estimate.daily_limits(
        __import__("tests.test_eval_estimate", fromlist=["config"]).config(groq_keys=["a" * 12])
    )
    assert limits.requests["answer"] == one_key.requests["answer"], "three keys, one key's limit"
    out = Lines()
    key_inventory.main([], {"GROQ_API_KEYS": "a" * 20 + "," + "b" * 20}, out=out)
    assert "NOTE: Groq holds 2 different keys" in out.text
    text = (DOCS / "READINESS.md").read_text("utf-8")
    assert "ONE_KEY_PER_PROVIDER" in text


def test_the_key_b_wording_is_a_rotation_overlap_everywhere_and_nowhere_a_second_account():
    for rel in ("docs/DEPLOY.md", "docs/KEY_ROTATION.md", "docs/runbooks/all-keys-down.md",
                "docs/OWNER_RUNS.md", ".env.example", "scripts/key_inventory.py"):  # fmt: skip
        text = (ROOT / rel).read_text("utf-8")
        for line in text.split("\n"):
            if (
                "EMBEDDING_KEY_B" in line
                or "embedding" in line.lower()
                and "account" in line.lower()
            ):
                low = line.lower()
                assert "second account" not in low and "different account" not in low, (rel, line)
                assert "failover" not in low or "overlap" in low or "tried" in low, (rel, line)
