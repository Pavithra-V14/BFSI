"""The per-package coverage report (spec 16: 80 % on gateway, guardrails, cache and memory).

The job that runs it is alert only (build rule 5); these tests make sure the report is right and
that the CI job keeps its two promises: it never blocks, and it runs with the Postgres container
so the Postgres tests count."""

import json
from pathlib import Path

import pytest

from scripts import coverage_report as cov

ROOT = Path(__file__).resolve().parents[1]


def files(**packages):
    """{package: (statements, covered, branches, covered_branches)} -> coverage-json files."""
    out = {}
    for name, (statements, covered, branches, hit) in packages.items():
        out[f"app/{name}/module.py"] = {
            "summary": {
                "num_statements": statements,
                "covered_lines": covered,
                "num_branches": branches,
                "covered_branches": hit,
            }
        }
    return out


GOOD = files(
    gateway=(100, 98, 20, 18),
    guardrails=(100, 90, 0, 0),
    cache=(50, 45, 4, 4),
    memory=(80, 70, 10, 7),
    accounts=(40, 20, 0, 0),
    api=(60, 55, 6, 6),
)


def test_each_package_is_summed_from_its_files():
    data = files(gateway=(100, 98, 20, 18))
    data["app/gateway/other.py"] = {
        "summary": {
            "num_statements": 100,
            "covered_lines": 50,
            "num_branches": 0,
            "covered_branches": 0,
        }
    }
    summary = cov.summarise(data)
    assert summary["gateway"]["statements"] == 200 and summary["gateway"]["covered"] == 148
    assert summary["gateway"]["line_percent"] == pytest.approx(74.0)
    assert summary["gateway"]["branch_percent"] == pytest.approx(90.0)


def test_windows_paths_and_files_outside_app_are_handled():
    assert cov.package_of("app\\gateway\\gateway.py") == "gateway"
    assert cov.package_of("tests/test_x.py") is None
    assert cov.package_of("scripts/x.py") is None


def test_only_the_four_spec_packages_alert_and_at_exactly_eighty_they_do_not():
    summary = cov.summarise(
        files(
            gateway=(100, 80, 0, 0),
            guardrails=(100, 79, 0, 0),
            cache=(10, 10, 0, 0),
            memory=(10, 10, 0, 0),
            accounts=(100, 1, 0, 0),
        )
    )
    assert cov.below_threshold(summary) == ["guardrails"]  # accounts is far lower: information


def test_a_spec_package_with_no_data_counts_as_below():
    assert cov.below_threshold(cov.summarise(files(gateway=(10, 10, 0, 0)))) == [
        "guardrails",
        "cache",
        "memory",
    ]


def test_the_report_shows_the_information_packages_without_a_target():
    lines = cov.render(cov.summarise(GOOD))
    accounts = next(line for line in lines if line.startswith("accounts"))
    assert "information only" in accounts and "BELOW" not in accounts
    assert any(line.startswith("pipeline") and "no data" in line for line in lines)


def test_the_command_exits_one_when_a_spec_package_is_below_and_zero_otherwise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"files": files(gateway=(100, 10, 0, 0))}), "utf-8")
    lines = []
    assert cov.main([str(bad)], out=lines.append) == 1
    assert any("BELOW THE TARGET" in line for line in lines)
    full = {n: (10, 10, 0, 0) for n in cov.SPEC_PACKAGES}
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"files": files(**full)}), "utf-8")
    assert cov.main([str(ok)], out=lambda line: None) == 0
    assert cov.main([str(tmp_path / "missing.json")], out=lambda line: None) == 2


def ci():
    import yaml

    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))


def test_the_coverage_job_is_alert_only_and_runs_with_the_postgres_service():
    job = ci()["jobs"]["coverage"]
    assert job["continue-on-error"] is True  # build rule 5: never a gate
    assert "postgres" in job["services"]
    assert job["env"]["TEST_POSTGRES_URL"].startswith("postgresql://")
    text = json.dumps(job["steps"])
    assert "--cov=app" in text and "--cov-branch" in text and "coverage_report.py" in text


def test_the_coverage_job_holds_no_credential_and_no_threshold_flag_turns_it_into_a_gate():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    block = text[text.index("  coverage:") : text.index("  evaluation-core:")]
    assert "secrets." not in block
    assert "--cov-fail-under" not in text  # a threshold flag would turn the alert into a gate
