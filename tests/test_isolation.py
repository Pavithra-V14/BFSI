"""Isolation guard: local resources pass, every remote host is refused."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.isolation import (
    HOST_VARIABLES,
    URL_VARIABLES,
    ProductionUrlError,
    is_local_host,
    is_local_url,
    remote_variables,
    require_local_environment,
    url_hosts,
)

ROOT = Path(__file__).resolve().parents[1]

# Made-up hosts in the style of cloud providers. None of them is real.
NEON_HOST = "ep-quiet-sky-123456.us-east-2.aws.neon.tech"
NEON_POOLED = "ep-quiet-sky-123456-pooler.us-east-2.aws.neon.tech"
SUPABASE_DIRECT = "db.abcdefghijklmnop.supabase.co"
SUPABASE_POOLER = "aws-0-us-east-1.pooler.supabase.com"
QDRANT_CLOUD = "abc12345.us-east-1-0.aws.cloud.qdrant.io"
UPSTASH_HOST = "eu1-sample-12345.upstash.io"

LOCAL = [
    pytest.param("sqlite:///:memory:", id="sqlite-memory"),
    pytest.param("sqlite:///C:/temp/test.db", id="sqlite-file"),
    pytest.param(":memory:", id="qdrant-memory"),
    pytest.param("", id="empty"),
    pytest.param("postgresql://user:pw@localhost:5432/db", id="localhost"),
    pytest.param("postgresql://user:pw@127.0.0.1/db", id="loopback-v4"),
    pytest.param("postgresql://user:pw@[::1]:5432/db", id="loopback-v6"),
    pytest.param("redis://localhost:6379/0", id="redis-local"),
    pytest.param("http://localhost:6333", id="qdrant-local"),
    pytest.param("postgresql:///db?host=/var/run/postgresql", id="unix-socket"),
    pytest.param("host=localhost dbname=test", id="key-value-local"),
    pytest.param("postgresql://u:pw@localhost:5432,127.0.0.1:5433/db", id="multi-host-all-local"),
    pytest.param("postgresql://u:pw@[::1]:5432,localhost/db", id="multi-host-v6-and-name-local"),
]

REMOTE = [
    pytest.param(f"postgresql://user:pw@{NEON_HOST}/db?sslmode=require", id="neon-direct"),
    pytest.param(f"postgresql://user:pw@{NEON_POOLED}/db?sslmode=require", id="neon-pooled"),
    pytest.param(f"postgresql://user:pw@{SUPABASE_DIRECT}:5432/postgres", id="supabase-direct"),
    pytest.param(f"postgresql://user:pw@{SUPABASE_POOLER}:6543/postgres", id="supabase-pooler"),
    pytest.param(f"https://{QDRANT_CLOUD}:6333", id="qdrant-cloud"),
    pytest.param(f"rediss://default:pw@{UPSTASH_HOST}:6379", id="upstash"),
    pytest.param("postgresql://user:pw@db.internal.test:5432/db", id="other-cloud-host"),
    pytest.param("postgresql://user:pw@10.0.0.5/db", id="private-address"),
    pytest.param("http://localhost@evil.test/", id="user-info-trick"),
    pytest.param("postgresql://user:pw@localhost.evil.test/db", id="lookalike-host"),
    pytest.param("postgresql://localhost,db.remote.test/db", id="multi-host"),
    pytest.param("postgresql:///db", id="no-host"),
    pytest.param("host=db.remote.test dbname=test", id="key-value-remote"),
    pytest.param("postgresql://localhost/db?host=db.remote.test", id="host-in-query"),
    pytest.param("postgresql://localhost/db?hostaddr=10.1.2.3", id="hostaddr-in-query"),
    pytest.param("http://[::1", id="unparsable"),
    pytest.param("postgresql://u:pw@localhost:5432,remote.test:5432/db", id="ports-local-first"),
    pytest.param("postgresql://u:pw@remote.test:5432,localhost:5432/db", id="ports-remote-first"),
    pytest.param("postgresql://u:pw@localhost,remote.test/db", id="no-ports-local-first"),
    pytest.param("postgresql://u:pw@remote.test,localhost/db", id="no-ports-remote-first"),
    pytest.param(
        "postgresql://u:pw@localhost:5432,remote.test:5432,127.0.0.1:5432/db",
        id="remote-in-second-position",
    ),
    pytest.param("postgresql://u:pw@[::1]:5432,remote.test:5432/db", id="v6-then-remote"),
]


@pytest.mark.parametrize("url", LOCAL)
def test_local_urls_are_allowed(url):
    assert is_local_url(url)


@pytest.mark.parametrize("url", REMOTE)
def test_remote_urls_are_refused(url):
    assert not is_local_url(url)


def test_is_local_host():
    assert is_local_host("localhost")
    assert is_local_host("[::1]")
    assert not is_local_host(NEON_HOST)


def test_url_hosts_lists_every_host_of_a_multi_host_url():
    assert url_hosts("postgresql://u:pw@localhost:5432,remote.test:5433/db") == [
        "localhost",
        "remote.test",
    ]
    assert url_hosts("postgresql://u:pw@[::1]:5432,127.0.0.1/db") == ["::1", "127.0.0.1"]


def test_remote_variables_names_a_multi_host_url_without_printing_it():
    env = {"DATABASE_URL": "postgresql://u:pw@localhost:5432,remote.test:5432/db"}
    assert remote_variables(env) == ["DATABASE_URL"]
    with pytest.raises(ProductionUrlError) as caught:
        require_local_environment(env)
    assert "remote.test" not in str(caught.value)
    assert "postgresql://" not in str(caught.value)


def test_remote_variables_reports_names_only():
    env = {
        "DATABASE_URL": f"postgresql://user:pw@{NEON_HOST}/db",
        "QDRANT_URL": f"https://{QDRANT_CLOUD}:6333",
        "UPSTASH_REDIS_URL": "redis://localhost:6379/0",
        "EVAL_DATABASE_URL": "sqlite:///:memory:",
    }
    assert remote_variables(env) == ["DATABASE_URL", "QDRANT_URL"]


def test_unset_and_empty_variables_are_ignored():
    assert remote_variables({"DATABASE_URL": "", "QDRANT_URL": "  "}) == []
    assert remote_variables({}) == []


def test_remote_pghost_is_refused():
    assert remote_variables({"PGHOST": NEON_HOST}) == ["PGHOST"]
    assert remote_variables({"PGHOST": "localhost"}) == []
    assert remote_variables({"PGHOST": "/var/run/postgresql"}) == []


def test_error_message_has_names_but_no_url():
    env = {"DATABASE_URL": f"postgresql://user:pw@{SUPABASE_POOLER}:6543/postgres"}
    with pytest.raises(ProductionUrlError) as caught:
        require_local_environment(env)
    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert SUPABASE_POOLER not in message
    assert "postgresql://" not in message


def _run_pytest_with(extra_env):
    env = {k: v for k, v in os.environ.items() if k not in URL_VARIABLES + HOST_VARIABLES}
    env.update(extra_env)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "tests/test_isolation.py",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_session_refuses_to_start_with_a_cloud_url():
    url = f"postgresql://user:pw@{NEON_HOST}/db?sslmode=require"
    result = _run_pytest_with({"DATABASE_URL": url})
    output = result.stdout + result.stderr
    assert result.returncode == 3
    assert "DATABASE_URL" in output
    assert NEON_HOST not in output
    assert "postgresql://" not in output


def test_session_starts_with_local_resources():
    result = _run_pytest_with({"DATABASE_URL": "sqlite:///:memory:", "QDRANT_URL": ":memory:"})
    assert result.returncode == 0
