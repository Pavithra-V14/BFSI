"""The variance band and the weekly drift report (spec 15.4, 15.5a). Alerts only."""

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.db.migrate import run as migrate
from app.eval import drift, store, variance

END = datetime(2026, 10, 12, tzinfo=UTC)


# ================================================================================= the band ====
def test_the_band_is_two_standard_deviations_of_the_runs():
    band = variance.band([0.80, 0.85, 0.90])
    assert band["mean"] == pytest.approx(0.85) and band["sd"] == pytest.approx(0.05)
    assert band["width"] == pytest.approx(0.10)
    assert band["low"] == pytest.approx(0.75) and band["high"] == pytest.approx(0.95)


def test_the_standard_deviation_is_the_sample_one():
    assert variance.band([1.0, 3.0, 5.0])["sd"] == pytest.approx(2.0)
    assert variance.band([1.0, 2.0, 4.0, 5.0])["sd"] == pytest.approx(math.sqrt(10 / 3))


def test_a_band_needs_three_runs():
    for scores in ([], [0.9], [0.9, 0.8]):
        band = variance.band(scores)
        assert band["width"] is None and band["low"] is None
    assert variance.band([0.9])["mean"] == 0.9
    assert variance.band([0.9, 0.8])["n"] == 2 and variance.band([0.9, 0.8])["sd"] is None
    assert variance.verdict(0.5, variance.band([0.9, 0.8])) == "no_band"


def test_identical_runs_give_a_zero_width_band_so_any_move_is_a_change():
    band = variance.band([0.9, 0.9, 0.9])
    assert band["width"] == 0.0
    assert variance.verdict(0.9, band) == "noise"
    assert variance.verdict(0.95, band) == "better"
    assert variance.verdict(0.85, band) == "worse"


def test_inside_the_band_is_noise_and_outside_is_a_change():
    band = variance.band([0.80, 0.85, 0.90])
    assert variance.verdict(0.76, band) == "noise" and variance.verdict(0.94, band) == "noise"
    assert variance.verdict(0.96, band) == "better" and variance.verdict(0.70, band) == "worse"
    assert variance.verdict(None, band) == "not_scored"
    assert variance.verdict(0.9, variance.band([0.9])) == "no_band"


def test_not_a_number_scores_are_left_out_of_the_band():
    assert variance.band([0.8, float("nan"), 0.9, 0.85])["n"] == 3
    assert variance.band([0.8, float("nan"), 0.9])["width"] is None  # only two real scores


def test_the_set_and_the_system_changing_together_is_flagged():
    base = {
        "dataset_version": "v1",
        "system_version": {"p": 1},
        "created_at": "2026-10-10T09:00:00+00:00",
    }
    both = {
        "dataset_version": "v2",
        "system_version": {"p": 2},
        "created_at": "2026-10-12T09:00:00+00:00",
    }
    only_set = {**both, "system_version": {"p": 1}}
    only_system = {**both, "dataset_version": "v1"}
    assert variance.change_in_same_week(base, both) is True
    assert variance.change_in_same_week(base, only_set) is False
    assert variance.change_in_same_week(base, only_system) is False
    assert variance.change_in_same_week(None, both) is False
    later = {**both, "created_at": "2026-11-12T09:00:00+00:00"}
    assert variance.change_in_same_week(base, later) is False  # not the same week


# ========================================================================= divergence ====
def test_identical_distributions_do_not_drift():
    counts = {"a": 50, "b": 30, "c": 20}
    assert drift.psi(counts, counts) == pytest.approx(0.0, abs=1e-9)
    assert drift.jensen_shannon(counts, counts) == pytest.approx(0.0, abs=1e-9)


def test_a_large_shift_has_a_large_psi_and_the_known_value():
    expected = {"a": 50, "b": 50}
    actual = {"a": 10, "b": 90}
    # (0.1-0.5)ln(0.1/0.5) + (0.9-0.5)ln(0.9/0.5) = 0.6438 + 0.2351
    assert drift.psi(expected, actual) == pytest.approx(0.8789, abs=1e-3)
    assert drift.psi(expected, actual) > drift.PSI_ALERT


def test_disjoint_distributions_have_a_js_divergence_of_one_bit():
    assert drift.jensen_shannon({"a": 5}, {"b": 5}) == pytest.approx(1.0)


def test_a_bucket_that_appears_from_nowhere_does_not_divide_by_zero():
    value = drift.psi({"a": 100}, {"a": 50, "new": 50})
    assert value is not None and math.isfinite(value) and value > 0


def test_an_empty_side_has_no_divergence():
    assert drift.psi({}, {"a": 1}) is None and drift.jensen_shannon({"a": 1}, {}) is None


# ================================================================================= report ====
def sample(model="m1", prompt="p1", kind="answer", reason=None, question="x" * 50, **extra):
    row = {
        "models": [model],
        "prompt_hash": prompt,
        "corpus_version": "1",
        "result_kind": kind,
        "reason": reason,
        "question": question,
        "confidence": None,
        "degraded": [],
        "tokens": 3000,
        "latency_ms": 4000.0,
    }
    row.update(extra)
    return row


def test_two_equal_weeks_raise_no_alert():
    week = [sample() for _ in range(30)]
    report = drift.drift_report(
        week,
        list(week),
        window=(END - timedelta(days=7), END),
        baseline_window=(END - timedelta(days=35), END - timedelta(days=7)),
    )
    assert report["alerts"] == [] and report["samples"] == 30
    assert all(m["level"] == "ok" for m in report["measures"].values())


def test_a_model_that_replaced_another_is_model_drift():
    baseline = [sample(model="gpt-oss") for _ in range(30)]
    recent = [sample(model="gemma") for _ in range(30)]
    report = drift.drift_report(
        recent, baseline, window=(END - timedelta(days=7), END), baseline_window=None
    )
    assert "model_shift" in report["alerts"]
    assert report["distribution"]["model"] == {"gemma": 30}


def test_a_changed_prompt_and_a_refusal_wave_are_named():
    baseline = [sample() for _ in range(30)]
    recent = [sample(prompt="p2") for _ in range(15)] + [
        sample(kind="refusal", reason="out_of_domain", prompt="p2") for _ in range(15)
    ]
    report = drift.drift_report(
        recent, baseline, window=(END - timedelta(days=7), END), baseline_window=None
    )
    assert {"prompt_shift", "result_shift"} <= set(report["alerts"])


def test_cost_and_latency_creep_over_thirty_percent_is_flagged():
    baseline = [sample() for _ in range(30)]
    recent = [sample(tokens=4500, latency_ms=6000.0) for _ in range(30)]
    report = drift.drift_report(
        recent, baseline, window=(END - timedelta(days=7), END), baseline_window=None
    )
    assert {"tokens_mean_creep", "latency_p95_ms_creep"} <= set(report["alerts"])


def test_too_few_samples_is_a_note_not_a_comparison():
    report = drift.drift_report(
        [sample()] * 5, [sample()] * 30, window=(END, END), baseline_window=None
    )
    assert report["alerts"] == [] and report["measures"] == {}
    assert any("too few" in note for note in report["notes"])


def test_the_week_windows_are_seven_days_and_the_baseline_before_it():
    window, baseline = drift.week_windows(END)
    assert window == (END - timedelta(days=7), END)
    assert baseline[1] == window[0] and baseline[1] - baseline[0] == timedelta(days=28)


def test_every_category_dipping_with_nothing_changed_is_model_drift():
    deltas = {"factual": -0.2, "table_lookup": -0.15, "greeting": -0.3}
    assert drift.classify_category_dip(deltas, width=0.1, system_changed=False) == "model_drift"
    assert drift.classify_category_dip(deltas, width=0.1, system_changed=True) == (
        "prompt_or_data_change"
    )
    one = {"factual": -0.2, "table_lookup": 0.0, "greeting": 0.01}
    assert drift.classify_category_dip(one, width=0.1, system_changed=False) is None
    assert (
        drift.classify_category_dip(one, width=0.1, system_changed=True) == "prompt_or_data_change"
    )
    assert drift.classify_category_dip({}, width=0.1, system_changed=False) is None


# ======================================================================== the database ====
def test_samples_are_read_in_a_window_and_the_retention_purge_removes_old_ones(tmp_path):
    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    row = {"role": "customer", "result_kind": "answer", "question": "q", "answer": "a"}
    store.insert_sample(db, row, now=END - timedelta(days=40))
    store.insert_sample(db, row, now=END - timedelta(days=3))
    store.insert_sample(db, row, now=END - timedelta(days=1))
    found = store.samples_between(db, END - timedelta(days=7), END)
    assert len(found) == 2
    assert store.purge_samples(db, now=END, dry_run=True) == 1  # counted, not deleted
    assert store.count_samples(db) == 3
    assert store.purge_samples(db, now=END) == 1
    assert store.count_samples(db) == 2
    assert store.purge_samples(db, now=END) == 0
    db.close()


def test_a_drift_report_is_saved_and_listed_newest_first(tmp_path):
    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    for days in (21, 14, 7):
        window = (END - timedelta(days=days + 7), END - timedelta(days=days))
        store.save_drift(
            db,
            environment="production",
            window=window,
            baseline=None,
            measures={"alerts": [], "n": days},
        )
    listed = store.list_drift(db, "production")
    assert [d["measures"]["n"] for d in listed] == [7, 14, 21]
    assert store.list_drift(db, "local") == []
    db.close()


def test_a_sample_exactly_thirty_days_old_is_kept_and_one_second_older_is_purged(tmp_path):
    url = store.sqlite_url(tmp_path / "e.db")
    migrate(url, store.MIGRATIONS, log=lambda line: None)
    db = store.open_db(url)
    row = {"role": "customer", "result_kind": "answer", "question": "q", "answer": "a"}
    store.insert_sample(db, row, now=END - timedelta(days=30))
    store.insert_sample(db, row, now=END - timedelta(days=30, seconds=1))
    assert store.purge_samples(db, now=END) == 1
    assert store.count_samples(db) == 1
    db.close()


def _drift_week(**extra):
    return [sample(**extra) for _ in range(30)]


@pytest.mark.parametrize(
    ("measure", "changed"),
    [
        ("corpus", {"corpus_version": "2"}),
        ("refusal_reason", {"reason": "unclear"}),
        ("length", {"question": "word " * 80}),
        ("confidence", {"confidence": "low"}),
        ("reduced_mode", {"degraded": ["rerank"]}),
    ],
)
def test_each_distribution_measure_alerts_when_its_input_shifts(measure, changed):
    report = drift.drift_report(
        _drift_week(**changed),
        _drift_week(),
        window=(END - timedelta(days=7), END),
        baseline_window=(END - timedelta(days=35), END - timedelta(days=7)),
    )
    assert f"{measure}_shift" in report["alerts"], report["measures"][measure]
