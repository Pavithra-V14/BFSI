"""Shared test setup. Runs the isolation guard before any test (spec section 16)."""

import os

# LiteLLM must use the model map that ships inside the package and stay quiet. These are set
# before any test module can import litellm, so no test depends on import order and no test run
# asks the network for a model map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")

import pytest  # noqa: E402

from app.isolation import remote_variables  # noqa: E402


def pytest_sessionstart(session):
    names = remote_variables()
    if names:
        pytest.exit(
            "Refusing to run tests: "
            + ", ".join(names)
            + " point at a remote host. Tests may only use SQLite, in-memory Qdrant or "
            "localhost (spec section 16). Unset or change the variable(s) and run again.",
            returncode=3,
        )
