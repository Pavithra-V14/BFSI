"""LiteLLM must never read the owner's `.env` file (CLAUDE.md: settings come from the shell).

In its default mode LiteLLM calls `dotenv.load_dotenv()` the moment it is imported. That search
starts in LiteLLM's own folder and climbs to the project root, so it finds the owner's real `.env`
and puts its contents (real keys, store addresses) into the process. It happened to the test
process once the API tests imported the app early, and the isolation guard caught it.
app/gateway sets LITELLM_MODE=PRODUCTION to prevent it.

These tests replace `load_dotenv` with a counter inside a child process, so no `.env` file is ever
opened, here or there.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COUNT_LOADS = (
    "import dotenv\ncalls = []\ndotenv.load_dotenv = lambda *a, **k: calls.append(1) or False\n"
)


def run(code: str, mode: str | None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "LITELLM_MODE"}
    env["PYTHONPATH"] = str(ROOT)
    if mode is not None:
        env["LITELLM_MODE"] = mode
    result = subprocess.run(
        [sys.executable, "-c", COUNT_LOADS + code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.split()


def test_importing_the_gateway_package_never_asks_litellm_to_load_a_dotenv_file():
    loads, mode, litellm_mode = run(
        "import os, app.gateway, litellm\n"
        "print(len(calls)); print(os.environ['LITELLM_MODE']); print(litellm.litellm_mode)",
        mode=None,
    )
    assert (loads, mode, litellm_mode) == ("0", "PRODUCTION", "PRODUCTION")


def test_the_control_in_its_default_mode_litellm_does_load_a_dotenv_file():
    # Proves the test above can fail: LiteLLM on its own, with no mode set, asks for the file.
    (loads,) = run("import litellm\nprint(len(calls))", mode=None)
    assert int(loads) >= 1


def test_an_explicit_choice_of_mode_is_left_alone():
    (mode,) = run("import os, app.gateway\nprint(os.environ['LITELLM_MODE'])", mode="DEV")
    assert mode == "DEV"
