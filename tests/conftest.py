"""Shared test setup. Runs the isolation guard before any test (spec section 16)."""

import os

# LiteLLM must use the model map that ships inside the package and stay quiet. These are set
# before any test module can import litellm, so no test depends on import order and no test run
# asks the network for a model map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")
# LiteLLM's default mode reads the owner's `.env` file when it is imported and puts its contents
# (real keys, database and cache addresses) into this process. Never during a test run.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

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


# ---- fixtures for the API and account tests -----------------------------------------------------
from tests.api_fixtures import Env  # noqa: E402


@pytest.fixture
def fast_hashing(monkeypatch):
    """Cheap argon2 settings for tests that go through the API (the real settings are tested in
    test_accounts_passwords). Not automatic: tests that need the real hasher do not ask for it."""
    from argon2 import PasswordHasher

    from app.accounts import passwords

    monkeypatch.setattr(
        passwords, "_HASHER", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    passwords.dummy_hash.cache_clear()
    yield
    passwords.dummy_hash.cache_clear()


@pytest.fixture
def env(tmp_path, fast_hashing):
    return Env(tmp_path)


@pytest.fixture
def db(env):
    connection = env.db()
    yield connection
    connection.close()
