"""Structure rules of the evaluation package (Phase 13).

The hosted API may use the light parts (the sample, the cheap checks, the stored numbers). The
evaluation libraries live in exactly two adapter modules and are never loaded by the API process.
No provider is reached from here except through the gateway."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
EVAL = APP / "eval"

LIBRARIES = ("ragas", "deepeval", "langchain", "langchain_core", "langchain_openai", "datasets")
ADAPTERS = {"ragas_adapter.py", "deepeval_adapter.py"}
PROVIDERS = (
    "litellm",
    "openai",
    "groq",
    "google.generativeai",
    "google.genai",
    "cohere",
    "anthropic",
)
# What only the command line and the runner may import: it makes calls or uses the libraries.
HEAVY_EVAL = (
    "app.eval.ragas_adapter",
    "app.eval.deepeval_adapter",
    "app.eval.judge",
    "app.eval.judging",
    "app.eval.calibration",
    "app.eval.cli",
)
HEAVY_ALLOWED_IN = {
    "ragas_adapter.py": set(),
    "deepeval_adapter.py": set(),
    "judging.py": {"app.eval.ragas_adapter", "app.eval.deepeval_adapter", "app.eval.judge"},
    "calibration.py": {"app.eval.judge"},
    "runner.py": {"app.eval.judge", "app.eval.judging"},
    "cli.py": {"app.eval.calibration", "app.eval.judge", "app.eval.judging"},
    "__main__.py": {"app.eval.cli"},
}


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def matches(name: str, banned: tuple[str, ...]) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in banned)


def test_the_evaluation_libraries_are_imported_in_the_two_adapters_only():
    found = []
    for path in APP.rglob("*.py"):
        for name in imported_names(path):
            if matches(name, LIBRARIES):
                found.append(path.relative_to(ROOT).as_posix())
    assert sorted(set(found)) == [f"app/eval/{name}" for name in sorted(ADAPTERS)]


def test_no_module_of_the_evaluation_package_calls_a_provider_directly():
    for path in EVAL.glob("*.py"):
        for name in imported_names(path):
            assert not matches(name, PROVIDERS), (path.name, name)
    assert any(EVAL.glob("*.py"))


def test_the_heavy_parts_are_imported_only_by_the_modules_that_use_them():
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP)
        inside = relative.parts[0] == "eval"
        for name in imported_names(path):
            if not matches(name, HEAVY_EVAL):
                continue
            assert inside, (relative.as_posix(), name)
            allowed = HEAVY_ALLOWED_IN.get(path.name, set())
            assert name in allowed or name.startswith(tuple(allowed)), (
                relative.as_posix(),
                name,
            )


def test_the_api_routes_import_only_the_light_parts_of_the_package():
    light_or_lazy = {
        "app.eval", "app.eval.access", "app.eval.candidates", "app.eval.cases",
        "app.eval.constants", "app.eval.dashboard", "app.eval.estimate", "app.eval.guard",
        "app.eval.runner", "app.eval.sampling", "app.eval.store",
    }  # fmt: skip
    for path in (APP / "api").rglob("*.py"):
        for name in imported_names(path):
            if name == "app.eval" or name.startswith("app.eval."):
                module = ".".join(name.split(".")[:3])
                assert module in light_or_lazy, (path.name, name)


def test_a_fresh_api_process_loads_none_of_the_evaluation_libraries():
    code = (
        "import sys\n"
        "import app.api.main, app.api.routes.admin_eval, app.eval.sampling, app.eval.dashboard\n"
        f"libs = {LIBRARIES!r}\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in libs)\n"
        "heavy = sorted(n for n in sys.modules if n in ('app.eval.ragas_adapter',"
        " 'app.eval.deepeval_adapter', 'app.eval.judge', 'app.eval.judging', 'app.eval.cli'))\n"
        "print('RESULT', ','.join(bad), '|', ','.join(heavy))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=240
    )
    assert done.returncode == 0, done.stderr[-800:]
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT")][-1]
    libs, _, heavy = line.removeprefix("RESULT").partition("|")
    # langchain_core may already be there for another package (LiteLLM); ragas and deepeval never
    loaded = {x.strip() for x in libs.split(",") if x.strip()}
    assert not {n for n in loaded if n.split(".")[0] in ("ragas", "deepeval")}, loaded
    assert heavy.strip() == "", heavy


def test_the_frontend_never_imports_the_evaluation_package():
    for path in (ROOT / "frontend").rglob("*.py"):
        for name in imported_names(path):
            assert not (name == "app" or name.startswith("app.")), (path.name, name)


def test_an_evaluation_run_has_no_path_to_a_production_budget_or_cache():
    text = (EVAL / "cli.py").read_text("utf-8")
    assert "usage=None" in text and "cache=None" in text
    assert "UsageLimits" not in text and "store_from_env" not in text
    assert "for_evaluation" in text  # the gateway of a run is the evaluation profile
    runner = (EVAL / "runner.py").read_text("utf-8")
    assert "check_eval_collection" in runner


def test_the_test_collection_check_is_in_every_place_that_writes_to_a_collection():
    scenarios = (EVAL / "scenarios.py").read_text("utf-8")
    assert "check_eval_collection" in scenarios
    ingest = (APP / "ingest" / "cli.py").read_text("utf-8")
    assert "check_eval_collection" in ingest
    documents = (APP / "vector" / "documents.py").read_text("utf-8")
    assert "check_eval_collection" in documents


def test_requirements_eval_is_never_installed_by_a_build_or_the_ci_audit():
    """The two accepted advisories (spec 15.7) are acceptable only because the evaluation packages
    exist on the admin machine alone: no Render build file, no workflow job (the pip-audit job
    included) and no other requirements file may name `requirements-eval.txt` (Phase 15 adds the
    Render files: this test then covers them too)."""
    candidates = [
        *sorted((ROOT / ".github").rglob("*.yml")),
        *sorted((ROOT / ".github").rglob("*.yaml")),
        *sorted(ROOT.glob("render.y*ml")),
        *sorted(ROOT.glob("Dockerfile*")),
        *sorted(ROOT.glob("Procfile")),
        *sorted(ROOT.glob("requirements*.txt")),
        *sorted(ROOT.glob("*.toml")),
    ]
    assert any(path.name == "ci.yml" for path in candidates)
    for path in candidates:
        if path.name == "requirements-eval.txt":
            continue
        text = path.read_text("utf-8")
        assert "requirements-eval" not in text, path.name
    audit_job = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    audit_line = next(ln for ln in audit_job.splitlines() if "pip-audit -r" in ln)
    assert "requirements-eval" not in audit_line and "requirements.txt" in audit_line


def test_the_ci_evaluation_job_is_alert_only_and_has_no_key():
    import yaml

    path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(path.read_text("utf-8"))
    job = workflow["jobs"]["evaluation-core"]
    assert job["continue-on-error"] is True  # an alert, never a gate (build rule 5)
    text = path.read_text("utf-8")
    block = text[text.index("evaluation-core:") :]
    assert "secrets." not in block and "API_KEY" not in block and "EVAL_" not in block
    assert "stubbed_model" in block


def test_the_ci_job_selects_at_least_one_test_so_it_cannot_pass_by_running_none():
    """A renamed test would otherwise leave the alert-only job selecting nothing."""
    import re

    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    block = text[text.index("evaluation-core:") :]
    match = re.search(r"pytest (tests/\S+\.py) -k (\S+)", block)
    assert match, "the job runs one file with a -k selection"
    source = (ROOT / match.group(1)).read_text("utf-8")
    pattern = r"^(?:async )?def (test_\w*" + re.escape(match.group(2)) + r"\w*)\("
    assert re.findall(pattern, source, re.M), "the CI selection matches no test"
    assert "--co" not in block and "|| true" not in block  # a zero-test run must fail the step
