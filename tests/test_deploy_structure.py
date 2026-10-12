"""The deployment files (Phase 15; spec 11.2, 11.3, 13.3, 13.4, 17).

Nothing here deploys anything. These tests read the files that Render and GitHub will read and
keep the rules that must not drift: no secret value in a file, nothing of the admin machine on
Render, the services start from the repository root with the proxy header left to our own code,
a deploy only after the checks pass, a pinger and a check after a deploy that hold no secret and
cannot be triggered by a fork, and docs that name everything an operator has to know.
"""

import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "render.yaml"
WORKFLOWS = ROOT / ".github" / "workflows"
DOCS = ROOT / "docs"


def env_example_names() -> dict[str, str | None]:
    """{variable: the "Where:" tag of its block} from .env.example (names and comments only)."""
    where: str | None = None
    out: dict[str, str | None] = {}
    for line in (ROOT / ".env.example").read_text("utf-8").split("\n"):
        if line.startswith("#"):
            match = re.match(r"#\s*Where:\s*(.*)", line)
            if match:
                where = match.group(1).strip()
            continue
        match = re.match(r"([A-Z][A-Z0-9_]*)=", line)
        if match:
            out[match.group(1)] = where
        elif not line.strip():
            where = None
    return out


ENV = env_example_names()
RENDER_NAMES = [n for n, w in ENV.items() if w == "Render and local"]
ADMIN_ONLY = [n for n, w in ENV.items() if w == "admin machine only"]
# The two EVAL_ names the running service really uses (the masked production sample).
EVAL_ON_RENDER = {"EVAL_DATABASE_URL", "EVAL_SAMPLE_RATE"}
MUST_NOT_BE_ON_RENDER = set(ADMIN_ONLY) | {
    n for n in ENV if n.startswith("EVAL_") and n not in EVAL_ON_RENDER
}


@pytest.fixture(scope="module")
def blueprint():
    return yaml.safe_load(RENDER.read_text("utf-8"))


def service(blueprint, name):
    return next(s for s in blueprint["services"] if s["name"] == name)


def variables(svc):
    return {v["key"]: v for v in svc.get("envVars", [])}


# ============================================================================== render.yaml
def test_there_are_exactly_two_free_python_web_services(blueprint):
    names = [s["name"] for s in blueprint["services"]]
    assert names == ["bfsi-api", "bfsi-frontend"]
    for svc in blueprint["services"]:
        assert svc["type"] == "web" and svc["runtime"] == "python" and svc["plan"] == "free"


def test_the_region_is_a_clear_placeholder_the_owner_must_fill_in(blueprint):
    for svc in blueprint["services"]:
        assert svc["region"] == "REPLACE_WITH_REGION_NEAR_YOUR_NEON_DATABASE"
    guide = (DOCS / "DEPLOY.md").read_text("utf-8")
    assert "REPLACE_WITH_REGION_NEAR_YOUR_NEON_DATABASE" in guide


def test_both_services_deploy_only_after_the_checks_pass_and_have_a_health_check(blueprint):
    api, front = service(blueprint, "bfsi-api"), service(blueprint, "bfsi-frontend")
    assert api["autoDeployTrigger"] == front["autoDeployTrigger"] == "checksPass"
    assert api["healthCheckPath"] == "/live"
    assert front["healthCheckPath"] == "/_stcore/health"


def test_the_api_starts_from_the_root_and_leaves_the_proxy_header_to_our_own_code(blueprint):
    """The EFFECTIVE command: the parsed `startCommand` split like a shell would, not a substring
    of the file (a comment or another service could hold the words)."""
    import shlex

    api = service(blueprint, "bfsi-api")
    tokens = shlex.split(api["startCommand"])
    assert tokens[:4] == ["uvicorn", "app.api.main:create_app_from_env", "--factory", "--host"]
    assert "--no-proxy-headers" in tokens, "uvicorn must not rewrite the client address"
    assert "--proxy-headers" not in tokens
    assert not any(t.startswith("--forwarded-allow-ips") for t in tokens)
    assert tokens[tokens.index("--host") + 1] == "0.0.0.0"
    assert tokens[tokens.index("--port") + 1] == "$PORT"
    assert "cd" not in tokens and "&&" not in tokens
    # the same effect through the environment is not allowed to come back either
    assert "FORWARDED_ALLOW_IPS" not in variables(api)
    # and the frontend service has no uvicorn at all
    assert "uvicorn" not in service(blueprint, "bfsi-frontend")["startCommand"]


def test_the_frontend_starts_from_the_root_so_the_safety_settings_are_read(blueprint):
    command = service(blueprint, "bfsi-frontend")["startCommand"]
    assert command.startswith("streamlit run frontend/app.py")
    assert "cd " not in command and "&&" not in command
    assert (ROOT / ".streamlit" / "config.toml").is_file()


def test_each_service_installs_only_its_own_requirements(blueprint):
    api = service(blueprint, "bfsi-api")["buildCommand"]
    front = service(blueprint, "bfsi-frontend")["buildCommand"]
    assert api == "pip install -r requirements.txt"
    assert front == "pip install -r requirements-frontend.txt"
    for command in (api, front):
        for banned in ("ingest", "eval", "dev"):
            assert f"requirements-{banned}" not in command


def test_the_python_version_matches_the_project(blueprint):
    pinned = (ROOT / ".python-version").read_text("utf-8").strip()
    for svc in blueprint["services"]:
        assert variables(svc)["PYTHON_VERSION"]["value"].startswith(pinned)


def test_no_secret_has_a_value_in_the_file(blueprint):
    plain = {"PYTHON_VERSION", "TRUSTED_PROXY_HOPS", "PASSWORD_HASH_CONCURRENCY"}
    for svc in blueprint["services"]:
        for key, entry in variables(svc).items():
            if key in plain:
                assert "value" in entry and entry.get("sync", True) is not False
            else:
                assert entry.get("sync") is False and "value" not in entry, key


def test_nothing_of_the_admin_machine_is_on_render(blueprint):
    for svc in blueprint["services"]:
        names = set(variables(svc))
        assert not names & MUST_NOT_BE_ON_RENDER, sorted(names & MUST_NOT_BE_ON_RENDER)
    assert {"ADMIN_MACHINE", "ENTITY_REVIEW_ENABLED", "DEV_STAGE_TRACE", "INGEST_COLLECTION"} <= (
        MUST_NOT_BE_ON_RENDER
    )
    assert {"EVAL_GROQ_API_KEYS", "EVAL_GEMINI_API_KEYS", "EVAL_OPENROUTER_API_KEYS"} <= (
        MUST_NOT_BE_ON_RENDER
    )


def test_every_variable_in_the_file_is_one_the_template_knows(blueprint):
    known = set(ENV) | {"PYTHON_VERSION"}
    for svc in blueprint["services"]:
        assert set(variables(svc)) <= known, set(variables(svc)) - known


def test_the_api_has_what_it_cannot_start_without(blueprint):
    names = set(variables(service(blueprint, "bfsi-api")))
    assert {
        "JWT_SECRET_KEY", "AUDIT_HASH_KEY", "DATABASE_URL", "UPSTASH_REDIS_URL", "QDRANT_URL",
        "QDRANT_API_KEY", "TRUSTED_PROXY_HOPS", "CORS_ALLOWED_ORIGIN",
    } <= names  # fmt: skip


def test_the_proxy_setting_and_the_hash_concurrency_are_the_documented_values(blueprint):
    api = variables(service(blueprint, "bfsi-api"))
    # ships at 0 until the first deploy's check (docs/DEPLOY.md 2.6, Run 28): a wrong 1 would let
    # a client choose the address that limits are counted for
    assert api["TRUSTED_PROXY_HOPS"]["value"] == "0"
    assert api["PASSWORD_HASH_CONCURRENCY"]["value"] == "2"


def test_the_frontend_gets_only_the_api_address(blueprint):
    assert set(variables(service(blueprint, "bfsi-frontend"))) == {"PYTHON_VERSION", "API_BASE_URL"}


# ================================================================================ the wheel
# The SHA-256 of en_core_web_sm-3.8.0-py3-none-any.whl as computed from the release asset
# (Phase 15). Changing the model version means a new value here, on purpose, in the same commit.
MODEL_WHEEL_SHA256 = "1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85"
MODEL_WHEEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/"
    "en_core_web_sm-3.8.0-py3-none-any.whl"
)


def test_the_spacy_model_wheel_is_pinned_by_the_recorded_sha256():
    text = (ROOT / "requirements.txt").read_text("utf-8")
    lines = [x for x in text.split("\n") if x.startswith("en_core_web_sm @ ")]
    assert lines == [f"en_core_web_sm @ {MODEL_WHEEL_URL}#sha256={MODEL_WHEEL_SHA256}"]


def test_no_other_requirements_file_installs_the_small_model_without_the_pin():
    for name in ("requirements-frontend.txt", "requirements-dev.txt", "requirements-eval.txt"):
        assert "en_core_web_sm" not in (ROOT / name).read_text("utf-8"), name


# ============================================================================== the workflows
def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text("utf-8"))


def triggers(wf):
    return wf.get("on", wf.get(True))  # YAML reads a bare `on` key as the boolean True


def test_no_workflow_runs_on_pull_request_target_or_exposes_a_secret_to_a_fork():
    for path in WORKFLOWS.glob("*.yml"):
        wf = yaml.safe_load(path.read_text("utf-8"))
        assert "pull_request_target" not in triggers(wf), path.name
        assert wf.get("permissions") == {"contents": "read"}, path.name


def test_the_deploy_is_renders_own_and_ci_has_no_deploy_or_smoke_job():
    """Render waits for every check of a commit: a smoke job in CI could hold the fix."""
    jobs = workflow("ci.yml")["jobs"]
    assert "post-deploy-smoke" not in jobs and "deploy" not in jobs
    assert "autoDeployTrigger: checksPass" in RENDER.read_text("utf-8")


def test_the_after_a_deploy_workflow_starts_only_from_a_successful_push_to_main_of_this_repo():
    wf = workflow("post-deploy.yml")
    trigger = triggers(wf)
    assert set(trigger) == {"workflow_run"}
    run = trigger["workflow_run"]
    assert run["workflows"] == ["CI"] and run["types"] == ["completed"]
    assert run["branches"] == ["main"]
    condition = " ".join(wf["jobs"]["smoke"]["if"].split())
    for part in (
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
    ):
        assert part in condition, part


def test_the_pinger_and_the_check_after_a_deploy_fail_visibly_and_hold_no_secret():
    for name in ("keepalive.yml", "post-deploy.yml"):
        wf = workflow(name)
        for job_name, job in wf["jobs"].items():
            assert "continue-on-error" not in job, (name, job_name)
            for step in job["steps"]:
                assert "continue-on-error" not in step, (name, job_name)
        text = (WORKFLOWS / name).read_text("utf-8")
        assert "secrets." not in text.replace("Secrets and variables", ""), name
        assert "vars.RENDER_API_URL" in text
    assert "vars.RENDER_FRONTEND_URL" in (WORKFLOWS / "keepalive.yml").read_text("utf-8")


def _window_script() -> str:
    """The shell script of the pinger's window step, from the parsed step (not from a comment)."""
    steps = workflow("keepalive.yml")["jobs"]["ping"]["steps"]
    return next(s for s in steps if s.get("id") == "window")["run"]


WINDOW_CONDITION = '[ "$day" -le 5 ] && [ "$now" -ge 0230 ] && [ "$now" -le 1430 ]'


def test_the_pinger_window_is_the_one_in_its_shell_condition_and_its_cron():
    script = _window_script()
    # the condition itself, with the exact bounds: weekday 1 to 5 (`%u`), 02:30 to 14:30 UTC
    assert "now=$(date -u +%H%M)" in script and "day=$(date -u +%u)" in script
    assert WINDOW_CONDITION in script
    cron = triggers(workflow("keepalive.yml"))["schedule"][0]["cron"]
    assert cron == "*/10 2-14 * * 1-5", "every ten minutes, 02:00 to 14:59 UTC, Monday to Friday"
    ping = next(s for s in workflow("keepalive.yml")["jobs"]["ping"]["steps"] if "if" in s)
    assert ping["if"] == "steps.window.outputs.inside == 'yes'"


def test_the_window_shell_condition_behaves_as_stated():
    """Run the real condition against fake clocks (when a shell is available)."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on this machine")
    assert WINDOW_CONDITION in _window_script()
    cases = {
        ("1", "0230"): "yes", ("5", "1430"): "yes", ("3", "0900"): "yes",
        ("1", "0229"): "no", ("1", "1431"): "no", ("6", "0900"): "no", ("7", "0900"): "no",
    }  # fmt: skip
    for (day, now), expected in cases.items():
        script = f"day={day}; now={now}; if {WINDOW_CONDITION}; then echo yes; else echo no; fi"
        done = subprocess.run([bash, "-c", script], capture_output=True, text=True, check=False)
        assert done.stdout.strip() == expected, (day, now, done.stdout, done.stderr)


def test_the_workflows_use_no_render_api_key_and_no_deploy_hook_address():
    """Render deploys by itself after the checks pass: nothing in GitHub holds a Render secret."""
    for path in WORKFLOWS.glob("*.yml"):
        text = path.read_text("utf-8").lower()
        assert "render_api_key" not in text and "deploy_hook" not in text, path.name
        assert "api.render.com" not in text, path.name


# ================================================================================ the runbooks
RUNBOOKS = [
    "all-keys-down",
    "wrong-answer-reported",
    "document-withdrawn",
    "suspected-pii-leak",
    "key-leaked",
    "bad-deploy-rollback",
]


@pytest.mark.parametrize("name", [*RUNBOOKS, "restore-documents"])
def test_each_runbook_has_its_sections_and_exact_steps(name):
    text = (DOCS / "runbooks" / f"{name}.md").read_text("utf-8")
    for heading in ("**When:**", "**Who does it:**", "## Steps", "## Check", "## Last rehearsal"):
        assert heading in text, (name, heading)
    assert re.search(r"^1\. ", text, re.M), f"{name} has numbered steps"


def test_the_runbook_index_lists_every_runbook_with_a_rehearsal_column():
    text = (DOCS / "runbooks" / "README.md").read_text("utf-8")
    assert "Last rehearsal" in text
    for name in [*RUNBOOKS, "restore-documents"]:
        assert f"({name}.md)" in text, name
    assert "under 5 minutes" in text


def test_the_restore_runbook_says_where_originals_is_backed_up_and_has_a_restore_test():
    text = (DOCS / "runbooks" / "restore-documents.md").read_text("utf-8")
    assert "Where `originals/` is backed up" in text and "Copy 2" in text
    assert "The restore test" in text and "documents_eval" in text


def _flat(text: str) -> str:
    return " ".join(text.lower().split())


def test_no_runbook_tells_the_reader_to_turn_masking_off_or_to_use_the_mock_in_production():
    for name in RUNBOOKS:
        text = _flat((DOCS / "runbooks" / f"{name}.md").read_text("utf-8"))
        assert "turn masking off" not in text.replace("do not turn masking off", ""), name
        assert "set `gateway_enable_mock`" not in text, name
    pii = _flat((DOCS / "runbooks" / "suspected-pii-leak.md").read_text("utf-8"))
    assert "do not turn masking off" in pii
    keys = _flat((DOCS / "runbooks" / "all-keys-down.md").read_text("utf-8"))
    assert "do not switch on the offline mock" in keys


# ================================================================================ DEPLOY.md
def guide():
    return (DOCS / "DEPLOY.md").read_text("utf-8")


def test_the_guide_names_every_variable_that_belongs_on_render():
    text = guide()
    missing = [name for name in RENDER_NAMES if f"`{name}`" not in text]
    assert not missing, missing


def test_the_guide_lists_the_variables_that_must_not_be_on_render():
    text = guide()
    section = text[text.index("### Variables that must NOT be set on Render") :]
    for name in (
        "ADMIN_MACHINE",
        "ENTITY_REVIEW_ENABLED",
        "DEV_STAGE_TRACE",
        "INGEST_COLLECTION",
        "EVAL_GROQ_API_KEYS",
        "EVAL_GEMINI_API_KEYS",
        "EVAL_OPENROUTER_API_KEYS",
    ):
        assert f"`{name}`" in section, name
    assert "EVAL_DATABASE_URL" in section and "EVAL_SAMPLE_RATE" in section


def test_the_guide_states_the_free_plan_limits_as_unverified_and_the_pinger_limits():
    text = guide()
    assert "[T: verify" in text and "750 instance hours" in text and "512 MB" in text
    assert "best-effort" in text and "60 days" in text
    assert "ephemeral" in text and "in-process state" in text.lower()


def test_the_guide_says_the_first_deploy_is_shadow_and_moving_on_is_a_checklist():
    text = guide()
    assert "SHADOW mode" in text and "`staff_beta`" in text
    section = text[text.index("## 6. Moving from shadow") :]
    assert "- [ ]" in section and "baseline" in section.lower()


def test_the_guide_documents_the_gate_exception_and_the_manual_deploy_override():
    text = _flat(guide())
    assert "documented exception" in text and "holds only the automatic deploy" in text
    assert "manual deploy" in text and "deploy a specific commit" in text
    spec = _flat((ROOT / "spec" / "build.md").read_text("utf-8"))
    assert "documented exception to build rule 5" in spec and "manual deploy" in spec


def test_the_guide_defines_read_secret_before_it_uses_it():
    text = guide()
    assert text.index("function Read-Secret($name)") < text.index('Read-Secret "DATABASE_URL"')


def test_the_guide_has_the_step_that_moves_the_proxy_setting_from_0_to_1():
    text = _flat(guide())
    assert "ships with `trusted_proxy_hops` at 0" in text
    assert "`address_at_one_hop`" in text and "to `1`" in text


def test_the_guide_says_seven_days_for_the_retention_alert():
    text = _flat(guide())
    assert "more than seven days old" in text and "retention_overdue" in text


def test_the_guide_gives_the_memory_fallback_order_and_never_turns_masking_off():
    text = guide()
    assert (
        "lower\n`PASSWORD_HASH_CONCURRENCY`" in text or "lower `PASSWORD_HASH_CONCURRENCY`" in text
    )
    assert "larger Render instance" in text and "Masking is **never** turned off" in text


def test_the_guide_keeps_the_direct_database_address_for_now():
    text = guide()
    section = text[text.index("## 4. The database address") : text.index("## 5. Every Render")]
    flat = _flat(section)
    assert "direct" in flat and "pooled address is not recommended" in flat and "run 32" in flat
    assert "deliberate test" in flat


def test_the_guide_documents_each_scheduled_task_and_its_shell_secrets():
    text = guide()
    section = text[text.index("## 9. Scheduled jobs") : text.index("## 10. Cost")]
    for needle in ("BFSI retention", "BFSI drift", "BFSI snapshot", "Task Scheduler",
                   "DATABASE_URL", "EVAL_DATABASE_URL", "QDRANT_API_KEY", "retention_overdue",
                   "judge-samples"):  # fmt: skip
        assert needle in section, needle


def test_the_guide_has_the_client_address_check_with_the_diagnostics_route():
    text = guide()
    assert "/admin/diagnostics" in text and "client_address_used" in text
    assert "real public address" in text


# ================================================================================ the rest
def test_the_data_flow_page_names_every_vendor_of_the_spec():
    text = (DOCS / "DATA_FLOW.md").read_text("utf-8")
    for vendor in ("Groq", "Gemini", "OpenRouter", "Jev", "Cohere", "Jina", "Langfuse", "Upstash",
                   "Neon", "Qdrant", "Render"):  # fmt: skip
        assert vendor in text, vendor
    assert "no real customer data" in text.lower()


def test_the_key_rotation_page_covers_every_secret_of_the_production_service():
    text = (DOCS / "KEY_ROTATION.md").read_text("utf-8")
    for name in (
        "GROQ_API_KEYS",
        "JEV_OPENROUTER_API_KEY",
        "GEMINI_EMBEDDING_KEY_A",
        "COHERE_API_KEY",
        "JINA_API_KEY",
        "QDRANT_API_KEY",
        "UPSTASH_REDIS_URL",
        "DATABASE_URL",
        "JWT_SECRET_KEY",
        "AUDIT_HASH_KEY",
        "ALERT_WEBHOOK_URL",
    ):
        assert name in text, name
    assert "scripts/key_inventory.py" in text and "Run 31" in text


def test_the_release_record_template_has_the_three_things_the_spec_names():
    text = (DOCS / "releases" / "TEMPLATE.md").read_text("utf-8")
    assert "What changed" in text and "Evaluation result against the baseline" in text
    assert "Approved by" in text


def test_the_owner_runs_list_runs_28_to_33_as_pending():
    text = (DOCS / "OWNER_RUNS.md").read_text("utf-8")
    for number in range(28, 34):
        assert re.search(rf"^\| {number} \|.*PENDING", text, re.M), number
        assert re.search(rf"^## Run {number}\. .*PENDING", text, re.M), number


# ===================================================== the API writes nothing it needs locally
# Admin-machine tools and local-only helpers. The hosted API never calls them (a test of Phase 11
# scans the imports); everything else under app/ must not write a file.
ADMIN_ONLY_PATHS = (
    "app/ingest/",
    "app/eval/",  # runs on the admin machine; its API-side parts only read databases
    "app/accounts/admin_cli.py",
    "app/db/migrate.py",
    "app/vector/documents.py",
)
# One route makes the folder of the local evaluation database. Its estimate route refuses on
# Render before it gets there (`_refuse_on_render`), so it only ever runs on the admin machine.
GUARDED_ON_RENDER = {"app/api/routes/admin_eval.py": "mkdir"}
# Methods that create or change a file. `str.replace` is not one of them, so only the file-system
# `replace` and `rename` of `os` are looked for (below).
WRITERS = {"write_text", "write_bytes", "mkdir", "makedirs", "touch", "unlink"}
OS_WRITERS = {"replace", "rename", "remove", "unlink", "makedirs", "mkdir"}
WRITE_MODES = ("w", "a", "x", "+")


def _writes(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in WRITERS and isinstance(func, ast.Attribute):
            found.append((node.lineno, name))
        elif (
            name in OS_WRITERS
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            found.append((node.lineno, f"os.{name}"))
        if name == "open":
            mode = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if isinstance(mode, str) and any(ch in mode for ch in WRITE_MODES):
                found.append((node.lineno, "open(write)"))
    return found


def test_the_hosted_code_writes_no_file():
    offenders = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(ADMIN_ONLY_PATHS):
            continue
        for line, what in _writes(ast.parse(path.read_text("utf-8"))):
            if GUARDED_ON_RENDER.get(relative) == what:
                assert "_refuse_on_render(request)" in path.read_text("utf-8")
                continue
            offenders.append(f"{relative}:{line} {what}")
    assert not offenders, "the API's filesystem is ephemeral: " + ", ".join(offenders)


def test_the_scan_really_finds_a_write():
    tree = ast.parse("from pathlib import Path\nPath('x').write_text('y')\nopen('f', 'w')\n")
    assert [what for _line, what in _writes(tree)] == ["write_text", "open(write)"]
    assert _writes(ast.parse("open('f')\nopen('f', 'rb')\n")) == []
