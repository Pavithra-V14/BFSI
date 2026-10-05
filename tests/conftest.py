"""Shared test setup. Runs the isolation guard before any test (spec section 16)."""

import pytest

from app.isolation import remote_variables


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
