"""Proposed numeric targets (Phase 16): the owner's four rules are code, a missing baseline is
"baseline" and never a made-up number, and a number without a sign-off is not a signed floor."""

import json

import pytest

from app.eval import cli, store, targets
from app.eval import constants as c
from app.eval.cases import load_cases
from tests.test_eval_cli import db_args, run_cli, workdir  # noqa: F401

VERSION, CASES = load_cases()
CATEGORY = {case.id: case.category for case in CASES}


def combined(**values):
    """combined-stats rows with a band: name -> (mean, width)."""
    out = {}
    for metric, (mean, width) in values.items():
        out[metric] = {
            "mean": mean,
            "band": {"width": width, "mean": mean, "n": 3},
            "runs": 3,
        }
    return out


def one(name, stats, signed=None):
    return targets.propose(targets.BY_NAME[name], stats, signed or {})


# ---- the four rules --------------------------------------------------------------------------
def test_the_regression_floor_is_the_baseline_mean_minus_the_band():
    row = one("golden_score", combined(case_pass=(0.85, 0.06)))
    assert row["target"] == pytest.approx(0.79) and row["rule"] == targets.REGRESSION
    assert row["alert_when"] == "below 0.79"


def test_latency_and_tokens_are_the_baseline_plus_twenty_percent():
    stats = combined(latency_p95_ms=(5000.0, None), tokens_per_query=(3000.0, None))
    assert one("latency_p95_ms", stats)["target"] == pytest.approx(6000.0)
    assert one("tokens_per_query", stats)["target"] == pytest.approx(3600.0)
    assert one("latency_p95_ms", stats)["rule"] == targets.CEILING


def test_a_zero_rule_has_target_zero_with_or_without_a_baseline():
    for name in ("pii_leak_rate", "cross_role_leak_rate", "prompt_leak_rate", "stale_answer_rate"):
        row = one(name, {})
        assert row["target"] == 0.0 and row["rule"] == targets.ZERO
        assert targets.judge(row, 0.0) == "ok"
        assert targets.judge(row, 0.05) == "alert"


def test_an_absolute_floor_is_independent_of_a_weak_baseline():
    """A baseline of 0.60 must not lower a signed floor of 0.95; and a strong baseline raises it."""
    signed = {"citation_validity": {"floor": 0.95, "signed_by": "the owner", "date": "2026-10-20"}}
    weak = one("citation_validity", combined(citation_accuracy_code=(0.60, 0.05)), signed)
    assert weak["target"] == pytest.approx(0.95) and weak["signed_floor"] == 0.95
    strong = one("citation_validity", combined(citation_accuracy_code=(0.99, 0.02)), signed)
    assert strong["target"] == pytest.approx(0.97)  # the regression floor is the higher one
    assert targets.judge(weak, 0.90) == "alert" and targets.judge(weak, 0.96) == "ok"


def test_an_unsigned_absolute_floor_is_not_invented():
    row = one("faithfulness", combined(faithfulness=(0.9, 0.04)))
    assert row["signed_floor"] is None and "no signed absolute floor" in row["note"]
    assert row["target"] == pytest.approx(0.86), "only the regression floor applies until signed"


@pytest.mark.parametrize(
    "entry",
    [
        {"floor": 0.9},  # no name, no date
        {"floor": 0.9, "signed_by": "the owner"},
        {"floor": True, "signed_by": "the owner", "date": "2026-10-20"},
        {"floor": "high", "signed_by": "the owner", "date": "2026-10-20"},
        "0.9",
    ],
)
def test_a_floor_without_a_signature_or_a_number_is_ignored(entry):
    row = one("faithfulness", combined(faithfulness=(0.9, 0.04)), {"faithfulness": entry})
    assert row["signed_floor"] is None


def test_without_a_baseline_every_target_stays_baseline_never_a_number():
    for target in targets.TARGETS:
        row = targets.propose(target, {}, {})
        if target.rule == targets.ZERO:
            assert row["target"] == 0.0
        else:
            assert row["target"] == "baseline", target.name
        assert targets.judge(row, 0.5) in ("no_target", "alert", "ok")


def test_a_missing_band_leaves_a_regression_floor_unset():
    stats = {"case_pass": {"mean": 0.85, "band": {"width": None}, "runs": 3}}
    row = one("golden_score", stats)
    assert row["target"] == "baseline" and "no variance band" in row["note"]


def test_the_judgement_directions():
    floor = {"rule": targets.REGRESSION, "target": 0.8}
    assert targets.judge(floor, 0.79) == "alert" and targets.judge(floor, 0.8) == "ok"
    ceiling = {"rule": targets.CEILING, "target": 100.0}
    assert targets.judge(ceiling, 101.0) == "alert" and targets.judge(ceiling, 100.0) == "ok"
    assert targets.judge(floor, None) == "not_measured"
    assert targets.judge({"rule": targets.REGRESSION, "target": "baseline"}, 0.5) == "no_target"


# ---- the measures of one run -----------------------------------------------------------------
def results(rows):
    return [
        {"case_id": cid, "metric": m, "score": s, "scored": s is not None} for cid, m, s in rows
    ]


def test_measures_of_a_run_are_means_a_p95_and_the_leak_shares():
    role = [cid for cid, cat in CATEGORY.items() if cat == "role_isolation"]
    rows = [(cid, "forbidden_absent", 1.0) for cid in role[:-1]] + [
        (role[-1], "forbidden_absent", 0.0)
    ]
    rows += [("f01-penal-charge", "case_pass", 1.0), ("f01-penal-charge", "fact_recall", 1.0)]
    rows += [("t01-x", "case_pass", 0.0)]
    items = [
        {"case_id": f"x{i}", "status": "done", "tokens": 1000 + i, "latency_ms": float(i * 100)}
        for i in range(1, 21)
    ]
    got = targets.measures_of_run(results(rows), items, CATEGORY)
    assert got["case_pass"] == pytest.approx(0.5) and got["fact_recall"] == 1.0
    assert got["latency_p95_ms"] == pytest.approx(1900.0)
    assert got["tokens_per_query"] == pytest.approx(1010.5)
    assert got["cross_role_leak"] == pytest.approx(1 / len(role))
    assert got["access_isolation"] == pytest.approx(1 - 1 / len(role))


def test_a_not_scored_result_is_not_a_zero():
    got = targets.measures_of_run(results([("f01-penal-charge", "case_pass", None)]), [], CATEGORY)
    assert got["case_pass"] is None and got["latency_p95_ms"] is None


# ---- from a database -------------------------------------------------------------------------
def finished_variance_run(db, scores, latency=1000.0):
    case_ids = [case.id for case in CASES if case.active]
    run_id = store.create_run(
        db, environment="local", kind="variance", dataset_version=VERSION,
        system_version={}, estimate={}, estimate_code="x", case_ids=case_ids, batches=1,
        created_by="test",
    )  # fmt: skip
    for position, case_id in enumerate(case_ids):
        store.save_item(
            db, run_id, case_id, status="done", answer_kind="answer", tokens=3000,
            latency_ms=latency,
        )  # fmt: skip
        store.save_result(
            db, run_id, case_id, "case_pass", score=scores[position % len(scores)], decider="code"
        )
    store.update_run(db, run_id, status=c.RUN_FINISHED)
    return run_id


def test_the_table_reads_three_finished_variance_runs(tmp_path):
    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    from app.db.migrate import run as migrate

    migrate(store.sqlite_url(tmp_path / "e.db"), store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    store.sync_cases(db, CASES)
    for latency in (900.0, 1000.0, 1100.0):
        finished_variance_run(db, [1.0, 1.0, 1.0, 0.0], latency)
    view = targets.table(db, "local", VERSION, CATEGORY, signed={})
    db.close()
    assert view["complete"] and len(view["runs"]) == 3
    rows = {r["name"]: r for r in view["rows"]}
    assert rows["golden_score"]["baseline"] == pytest.approx(0.75)
    assert rows["latency_p95_ms"]["target"] == pytest.approx(1000.0 * 1.2)


def test_fewer_than_three_runs_is_not_a_baseline(tmp_path):
    from app.db.migrate import run as migrate

    migrate(store.sqlite_url(tmp_path / "e.db"), store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    store.sync_cases(db, CASES)
    finished_variance_run(db, [1.0])
    finished_variance_run(db, [1.0])
    view = targets.table(db, "local", VERSION, CATEGORY, signed={})
    db.close()
    assert not view["complete"]
    assert all(r["target"] in ("baseline", 0.0) for r in view["rows"])


def test_red_team_runs_are_never_part_of_the_baseline(tmp_path):
    from app.db.migrate import run as migrate

    migrate(store.sqlite_url(tmp_path / "e.db"), store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(store.sqlite_url(tmp_path / "e.db"))
    store.sync_cases(db, CASES)
    run_id = finished_variance_run(db, [1.0])
    with db.transaction(write=True):
        db.execute("UPDATE eval_runs SET dataset_version = 'redteam-1' WHERE id = ?", (run_id,))
    assert targets.baseline_runs(db, "local", VERSION) == []
    assert targets.baseline_runs(db, "local", "redteam-1") == []
    db.close()


def test_the_command_says_there_is_no_baseline_and_writes_nothing(workdir, monkeypatch):  # noqa: F811
    run_cli(["migrate", *db_args(workdir)])
    code, out, calls = run_cli(["targets", *db_args(workdir)])
    assert code == 0 and calls == []
    assert "no baseline yet" in out.text and 'stays "baseline"' in out.text
    assert "PROPOSALS" in out.text
    assert "pii_leak_rate | zero_rule |" in out.text and "| 0 |" in out.text


def test_the_shipped_signed_file_holds_no_floor_yet():
    data = json.loads(targets.SIGNED_PATH.read_text("utf-8"))
    assert data["absolute_floors"] == {}, "a floor is added only when the owner signs it"
    assert cli.redteam is not None


# ---- round 2: edge cases ---------------------------------------------------------------------
def test_a_band_larger_than_the_mean_is_no_usable_target_and_is_not_clamped():
    row = one("golden_score", combined(case_pass=(0.05, 0.10)))
    assert row["target"] == "baseline"
    assert row["note"] == "no usable target (band larger than the mean)"
    assert row["alert_when"] == ""
    assert targets.judge(row, 0.0) == "no_target"
    equal = one("golden_score", combined(case_pass=(0.10, 0.10)))
    assert equal["target"] == 0.0, "a band equal to the mean gives a floor of exactly 0"


def test_a_big_band_still_leaves_a_signed_absolute_floor_in_force():
    signed = {"faithfulness": {"floor": 0.9, "signed_by": "the owner", "date": "2026-10-20"}}
    row = one("faithfulness", combined(faithfulness=(0.05, 0.2)), signed)
    assert row["target"] == pytest.approx(0.9) and "band larger than the mean" in row["note"]


def test_a_metric_measured_in_fewer_than_three_runs_gets_no_proposal_and_says_how_many():
    stats = {"case_pass": {"mean": 0.85, "band": {"width": 0.04}, "runs": 2}}
    row = one("golden_score", stats)
    assert row["target"] == "baseline" and row["note"] == "measured in 2 of 3 runs: no proposal"
    stats["case_pass"]["runs"] = 1
    assert "measured in 1 of 3 runs" in one("golden_score", stats)["note"]
    stats["case_pass"]["runs"] = 3
    assert one("golden_score", stats)["target"] == pytest.approx(0.81)
    # a ceiling and an absolute floor follow the same rule
    ceiling = {"latency_p95_ms": {"mean": 5000.0, "band": {"width": None}, "runs": 2}}
    assert one("latency_p95_ms", ceiling)["target"] == "baseline"


def test_a_signed_floor_survives_a_baseline_measured_in_too_few_runs():
    signed = {"citation_validity": {"floor": 0.95, "signed_by": "the owner", "date": "2026-10-20"}}
    stats = {"citation_accuracy_code": {"mean": 0.99, "band": {"width": 0.01}, "runs": 1}}
    row = one("citation_validity", stats, signed)
    assert row["target"] == 0.95 and "measured in 1 of 3 runs" in row["note"]


def test_a_ceiling_with_a_zero_baseline_is_no_usable_target():
    stats = {"latency_p95_ms": {"mean": 0.0, "band": {"width": 0.0}, "runs": 3}}
    row = one("latency_p95_ms", stats)
    assert row["target"] == "baseline" and "the baseline is 0" in row["note"]


@pytest.mark.parametrize(
    "entry",
    [
        {"floor": 1.5, "signed_by": "the owner", "date": "2026-10-20"},
        {"floor": -0.1, "signed_by": "the owner", "date": "2026-10-20"},
        {"floor": 5, "signed_by": "the owner", "date": "2026-10-20"},
        {"floor": 0.9, "signed_by": "the owner", "date": "today"},
        {"floor": 0.9, "signed_by": "the owner", "date": "20/10/2026"},
        {"floor": 0.9, "signed_by": "the owner", "date": "2026-13-45"},
        {"floor": 0.9, "signed_by": "  ", "date": "2026-10-20"},
        {"floor": 0.9, "signed_by": 7, "date": "2026-10-20"},
    ],
)
def test_a_signed_floor_outside_zero_to_one_or_without_an_iso_date_is_ignored(entry):
    row = one("faithfulness", combined(faithfulness=(0.9, 0.04)), {"faithfulness": entry})
    assert row["signed_floor"] is None


@pytest.mark.parametrize("floor", [0, 0.0, 0.5, 1, 1.0])
def test_a_signed_floor_at_the_ends_of_the_range_is_accepted(floor):
    entry = {"floor": floor, "signed_by": "the owner", "date": "2026-10-20"}
    row = one("faithfulness", combined(faithfulness=(0.9, 0.04)), {"faithfulness": entry})
    assert row["signed_floor"] == float(floor)


def test_the_proposals_are_never_applied_the_command_writes_nothing(workdir, tmp_path):  # noqa: F811
    before = targets.SIGNED_PATH.read_bytes()
    run_cli(["migrate", *db_args(workdir)])
    run_cli(["targets", *db_args(workdir)])
    assert targets.SIGNED_PATH.read_bytes() == before
    assert not list(workdir.rglob("*.md")) and not list(workdir.rglob("targets*.json"))
