"""The secret scan configuration (.gitleaks.toml) stays narrow.

CI runs gitleaks over the whole history. A few paths hold metric and field names that its generic
rule reads as keys; those paths are allowed for that one rule and nothing else. These tests make
sure the file parses, extends the default rules, and cannot be widened by accident (another rule,
another path, a wildcard path, a global allowlist, a rule of its own)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".gitleaks.toml"

ALLOWED_RULES = ["generic-api-key"]
ALLOWED_PATHS = {
    r"^app/eval/drift\.py$",
    r"^tests/test_eval_drift\.py$",
    r"^\.gitleaksignore$",
}


def load():
    return tomllib.loads(CONFIG.read_text("utf-8"))


def test_the_file_parses_and_extends_the_default_rules():
    config = load()
    assert config["extend"] == {"useDefault": True}


def test_it_defines_no_rule_and_no_global_allowlist_of_its_own():
    config = load()
    assert "rules" not in config, "no rule of our own: the defaults are the rules"
    assert "allowlist" not in config, "a global allowlist would apply to every rule"
    assert set(config) <= {"title", "extend", "allowlists"}


def test_every_allowlist_names_only_the_generic_rule_and_only_listed_paths():
    allowlists = load()["allowlists"]
    assert allowlists, "the file is there to allow something"
    seen = set()
    for entry in allowlists:
        assert set(entry) == {"description", "targetRules", "paths"}, sorted(entry)
        assert entry["targetRules"] == ALLOWED_RULES
        assert len(entry["paths"]) == 1
        seen.update(entry["paths"])
    assert seen == ALLOWED_PATHS


def test_no_path_is_a_wildcard_or_a_folder():
    for path in ALLOWED_PATHS:
        assert path.startswith("^") and path.endswith("$")
        assert ".*" not in path and "/*" not in path


def test_the_allowed_files_exist():
    for path in ("app/eval/drift.py", "tests/test_eval_drift.py", ".gitleaksignore"):
        assert (ROOT / path).is_file()


def test_the_ignore_file_quotes_no_code_in_its_comments():
    """A comment that repeats a flagged line is itself reported."""
    for number, line in enumerate((ROOT / ".gitleaksignore").read_text("utf-8").splitlines(), 1):
        if line.startswith("#"):
            assert "`" not in line, number
            assert not re.search(r"for \w+ in|\w+ = [(\[\"']", line), number
