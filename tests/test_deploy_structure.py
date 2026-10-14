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


def test_the_owner_runs_list_runs_28_to_39_as_pending():
    text = (DOCS / "OWNER_RUNS.md").read_text("utf-8")
    for number in range(28, 40):
        assert re.search(rf"^\| {number} \|.*pending", text, re.M | re.I), number
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


# ===================================================== round 3: what the audit found in the docs
def runbook(name: str) -> str:
    return (DOCS / "runbooks" / f"{name}.md").read_text("utf-8")


def test_the_guide_never_uses_the_bare_curl_alias_of_windows_powershell():
    for number, line in enumerate(guide().split("\n"), start=1):
        assert not re.match(r"\s*curl\s", line), f"DEPLOY.md line {number}: use curl.exe"


def test_the_guide_gets_a_token_without_typing_a_password_or_the_token():
    text = guide()
    section = text[text.index("### 2.5a") : text.index("### 2.5b")]
    assert "Read-Secret" in section and "Invoke-RestMethod" in section
    assert "/auth/login" in section and "/auth/logout" in section
    assert "Remove-Variable" in section
    assert "8 hours" in " ".join(section.split())
    # no password or token is ever a literal in a command of the guide
    assert not re.search(r"Bearer [A-Za-z0-9_-]{20,}", text)
    assert "TOKEN" not in re.sub(r"`[^`]*`", "", section), "no placeholder token to paste"


def test_the_guide_says_what_runs_where_for_admin_jobs():
    text = guide()
    section = text[text.index("### 2.5b") : text.index("### 2.6")]
    flat = _flat(section)
    assert "hosted frontend" in flat and "`admin_machine`" in flat
    assert "does not work against render" in flat or "do not work against render" in flat
    assert "review entities" in section


def test_the_guide_tells_the_reader_to_expect_the_first_frontend_start_to_fail():
    flat = _flat(guide())
    assert "expect `bfsi-frontend` to show failed" in flat
    assert "no path and no trailing slash" in flat


def test_the_guide_gives_the_region_words_and_a_mapping_from_neon():
    text = guide()
    for word in ("`oregon`", "`ohio`", "`virginia`", "`frankfurt`", "`singapore`"):
        assert word in text, word
    assert "Mumbai" in text and "[T:" in text[text.index("Mumbai") - 1500 : text.index("Mumbai")]


def test_the_guide_checks_the_stage_of_a_database_that_is_not_new():
    text = guide()
    section = text[text.index("### 2.8") : text.index("### 2.9")]
    assert "/admin/shadow" in section and '{"stage":"shadow"}' in section
    assert "may not be new" in section


def test_the_variable_table_agrees_with_render_yaml_on_the_proxy_setting_and_the_api_address():
    text = guide()
    row = next(line for line in text.split("\n") if line.startswith("| `TRUSTED_PROXY_HOPS` |"))
    assert "ships as `0`" in row and "set to 1 in render.yaml" not in row
    spec = yaml.safe_load(RENDER.read_text("utf-8"))
    api = next(s for s in spec["services"] if s["name"] == "bfsi-api")
    shipped = next(v["value"] for v in api["envVars"] if v["key"] == "TRUSTED_PROXY_HOPS")
    assert str(shipped) == "0"
    api_row = next(line for line in text.split("\n") if line.startswith("| `API_BASE_URL` |"))
    assert "required" in api_row and "| optional |" not in api_row


def test_the_guide_warns_that_a_rollback_may_switch_auto_deploy_off():
    for text in (guide(), runbook("bad-deploy-rollback")):
        assert "Auto-Deploy" in text
    assert "3a." in runbook("bad-deploy-rollback")


def test_the_scheduled_snapshot_task_downloads_before_it_prunes():
    text = guide()
    create = text.index("snapshot.ps1")
    section = text[create : create + 1500]
    assert section.index("create --run") < section.index("download --newest --run")
    assert section.index("download --newest --run") < section.index("prune --keep 5 --run")
    row = next(line for line in text.split("\n") if line.startswith("| BFSI snapshot |"))
    assert row.index("create --run") < row.index("download --newest --run")
    assert row.index("download --newest --run") < row.index("prune --keep 5 --run")


def test_the_guide_names_the_encrypted_file_update_when_a_key_is_rotated():
    assert "replace its file too" in guide().lower() or "replace its file too" in _flat(guide())
    leaked = _flat(runbook("key-leaked"))
    assert "encrypted files" in leaked and "9.1" in leaked and "fail without a sound" in leaked
    assert "bfsi-secrets" in _flat((DOCS / "KEY_ROTATION.md").read_text("utf-8"))


def test_the_restore_runbook_says_snapshot_restore_is_unproven_and_ends_with_the_withdrawn_check():
    text = runbook("restore-documents")
    assert "UNPROVEN" in text and "documents_restore_test" in text
    assert "app.ingest withdrawn" in text and "STILL STORED" in text
    assert "originals/_withdrawn/" in text
    flat = _flat(text)
    assert "discards" in flat and "corpus version" in flat
    # both routes reach the check
    assert text.count("Go to step C") == 2


def test_the_withdrawn_runbook_records_moves_and_says_what_stays_behind():
    text = runbook("document-withdrawn")
    for needle in ("--reason", "records the id as withdrawn", "originals/_withdrawn/",
                   "Qdrant snapshots still hold", "backups/qdrant/", "new id",
                   "before this record existed"):  # fmt: skip
        assert needle in text, needle


def test_the_pii_runbook_calls_the_retention_command_irreversible_and_asks_for_a_dry_run():
    flat = _flat(runbook("suspected-pii-leak"))
    assert "irreversible" in flat and "--dry-run" in flat
    assert "no admin command that removes one named chat" in flat


def test_the_key_leaked_runbook_does_not_promise_no_downtime_and_explains_the_inventory():
    flat = _flat(runbook("key-leaked"))
    assert "no downtime" not in flat.replace("(no downtime)", "") or "restarts the service" in flat
    assert "every save restarts the service" in flat
    assert "only the variables of the window" in flat and "not render's" in flat
    assert "langfuse_user_hash_key" in flat and "can no longer be found" in flat


def test_every_command_and_flag_the_docs_show_exists():
    """The words of the runbooks, DEPLOY.md and OWNER_RUNS.md are checked against the real
    argument parsers: an unknown subcommand or flag in a document is a failing test."""
    import contextlib
    import io

    from app.accounts import admin_cli
    from app.db import migrate
    from app.eval import cli as eval_cli
    from app.ingest import cli as ingest_cli
    from app.memory import retention
    from app.observability import scan
    from app.vector import documents
    from scripts import (
        cost_dry_run_compare,
        cost_projection,
        key_inventory,
        prelaunch_gate,
        qdrant_snapshot,
        release_record,
    )

    def helped(call, *argv) -> str | None:
        """The --help text of a parser, or None when the parser refused the words."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                call([*argv, "--help"])
            except SystemExit as stop:
                if stop.code not in (0, None):
                    return None
        return out.getvalue()

    # tool pattern -> (does it take a subcommand, how to ask for its help)
    tools = {
        r"python -m app\.ingest": (True, lambda sub: helped(ingest_cli.main, sub)),
        r"python -m app\.eval": (True, lambda sub: helped(eval_cli.main, sub)),
        r"python -m app\.memory\.retention": (False, lambda sub: helped(retention.main)),
        r"python -m app\.db\.migrate": (False, lambda sub: helped(migrate.main)),
        r"python -m app\.vector\.documents": (False, lambda sub: helped(documents.main)),
        r"python -m app\.observability\.scan": (False, lambda sub: helped(scan.main)),
        r"python -m app\.accounts\.admin_cli": (
            True,
            lambda sub: helped(admin_cli.main, sub),
        ),
        r"scripts/qdrant_snapshot\.py": (True, lambda sub: helped(qdrant_snapshot.main, sub)),
        r"scripts/cost_projection\.py": (False, lambda sub: helped(cost_projection.main)),
        r"scripts/key_inventory\.py": (False, lambda sub: helped(key_inventory.main)),
        r"scripts/release_record\.py": (False, lambda sub: helped(release_record.main)),
        r"scripts/prelaunch_gate\.py": (False, lambda sub: helped(prelaunch_gate.main)),
        r"scripts/cost_dry_run_compare\.py": (False, lambda sub: helped(cost_dry_run_compare.main)),
    }
    files = [
        DOCS / "DEPLOY.md",
        DOCS / "OWNER_RUNS.md",
        DOCS / "KEY_ROTATION.md",
        DOCS / "releases" / "README.md",
        *sorted((DOCS / "runbooks").glob("*.md")),
    ]
    checked = 0
    problems: list[str] = []
    for path in files:
        for number, line in enumerate(path.read_text("utf-8").split("\n"), start=1):
            for pattern, (has_sub, ask) in tools.items():
                found = re.search(pattern + r"\s*(.*)", line)
                if not found:
                    continue
                rest = found.group(1).split("`")[0]
                sub = ""
                if has_sub:
                    word = re.match(r"([a-z][a-z-]*)\b", rest)
                    if not word:
                        continue  # a sentence about the tool, not a command with a subcommand
                    sub = word.group(1)
                text = ask(sub)
                where = f"{path.name}:{number}"
                if text is None:
                    problems.append(f"{where}: `{pattern} {sub}` is not accepted by the tool")
                    continue
                for flag in re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", rest):
                    if flag != "--help" and flag not in text:
                        problems.append(f"{where}: {flag} is not an option of `{sub or pattern}`")
                checked += 1
    assert not problems, "\n".join(problems)
    assert checked > 20, "the check found too few commands: its patterns are wrong"


def test_routine_rehearsals_use_throw_away_ids_and_never_reinstate():
    """Round 3: a delete records the id as withdrawn for good. The drills (Run 10 f and h, Run 12b)
    use throw-away ids, so only Run 34 (the safeguards drill) mentions `--reinstate`."""
    runs = (DOCS / "OWNER_RUNS.md").read_text("utf-8")
    readme = (ROOT / "README.md").read_text("utf-8")
    f_test = runs[runs.index("### (f) The delete test") : runs.index("### (g) The other two")]
    h_test = runs[runs.index("### (h) A failed page") : runs.index("### (i) The staff-only copy")]
    run_12b = runs[runs.index("### Run 12b.") : runs.index("## Run 13.")]
    readme_f = readme[
        readme.index("**(f) The delete test.**") : readme.index("**(g) The other two")
    ]
    readme_h = readme[readme.index("**(h) A failed page") : readme.index("### Known limitations")]
    for name, text, needle in (
        ("Run 10 (f)", f_test, "rehearsal-insurance-1"),
        ("Run 10 (h)", h_test, "rehearsal-banking-1"),
        ("Run 12b", run_12b, "rehearsal-ecas-1"),
        ("README (f)", readme_f, "rehearsal-insurance-1"),
        ("README (h)", readme_h, "rehearsal-banking-1"),
    ):
        assert needle in text, name
        assert "--reinstate" not in text, f"{name}: a routine drill must never need --reinstate"
        assert "delete --doc-id insurance-flexi-property" not in text
        assert "delete --doc-id banking-rtl-facility" not in text
        assert "delete --doc-id finserv-mf-ecas" not in text
    run_34 = runs[runs.index("## Run 34.") : runs.index("# C. Added by later phases")]
    assert "--reinstate withdraw-test --confirm-id withdraw-test" in run_34
    assert "explicit yes" in run_34
    withdrawn_runbook = runbook("document-withdrawn")
    assert "--confirm-id" in withdrawn_runbook and "--even-if-not-stored" in withdrawn_runbook
    assert "--even-if-not-stored" in runbook("restore-documents")


def test_the_guide_names_the_diagnostics_fields_the_route_really_returns():
    route = (ROOT / "app" / "api" / "routes" / "admin.py").read_text("utf-8")
    text = guide()
    section = text[text.index("### 2.6") : text.index("### 2.8")]
    for field in re.findall(r"`([a-z_]+)`", section):
        if field in {"forwarded_entries", "trusted_proxy_hops", "address_at_one_hop",
                     "client_address_used", "address_source", "socket_address", "memory_mb",
                     "peak_memory_mb"}:  # fmt: skip
            assert f'"{field}"' in route, f"{field} is not a field of /admin/diagnostics"
    for needed in ("address_at_one_hop", "client_address_used", "peak_memory_mb"):
        assert needed in section
