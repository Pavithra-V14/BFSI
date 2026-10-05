"""Test isolation guard (spec section 16 and build rule 3).

Tests, evaluations and load tests must never touch production. The rule is
deliberately strict: a connection string counts as safe only when it is SQLite,
in-memory Qdrant, or points at this machine (localhost or a loopback address).
Everything else, including anything that cannot be parsed, is treated as remote.

Nothing in this module prints or returns a URL value. It reports variable names
only, because a URL can contain a password.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

# Environment variables that hold a database, vector store or cache URL.
# Later phases add their own URL variables here.
URL_VARIABLES = (
    "DATABASE_URL",
    "EVAL_DATABASE_URL",
    "TEST_POSTGRES_URL",
    "QDRANT_URL",
    "UPSTASH_REDIS_URL",
)
# libpq reads these when a connection string names no host.
HOST_VARIABLES = ("PGHOST", "PGHOSTADDR")

_KEY_VALUE = re.compile(r"(\w+)\s*=\s*('(?:[^'\\]|\\.)*'|\S+)")


class ProductionUrlError(RuntimeError):
    """A test run was about to use a remote (possibly production) resource."""


def is_local_host(host: str) -> bool:
    name = host.strip().strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def url_hosts(value: str) -> list[str] | None:
    """Return the hosts a connection string would connect to, or None if unknown.

    A Unix socket directory is returned as a path that starts with "/". Both URLs
    (postgresql://...) and libpq key=value strings are understood.
    """
    text = value.strip()
    hosts: list[str] = []
    try:
        if "://" in text:
            parts = urlsplit(text)
            if parts.hostname:
                hosts.append(parts.hostname)
            query = parse_qs(parts.query)
        else:
            query = {
                match.group(1).lower(): [match.group(2).strip("'")]
                for match in _KEY_VALUE.finditer(text)
            }
    except ValueError:
        return None
    for key in ("host", "hostaddr"):
        for entry in query.get(key, []):
            hosts.extend(host for host in entry.split(",") if host)
    return hosts or None


def _all_local(hosts: list[str]) -> bool:
    return all(host.startswith("/") or is_local_host(host) for host in hosts)


def is_local_url(value: str | None) -> bool:
    """True only for SQLite, in-memory Qdrant, or hosts on this machine."""
    text = (value or "").strip()
    if text in ("", ":memory:") or text.lower().startswith("sqlite:"):
        return True
    hosts = url_hosts(text)
    return bool(hosts) and _all_local(hosts)


def remote_variables(env: Mapping[str, str] | None = None) -> list[str]:
    """Names (never values) of guarded variables that point at a remote host."""
    env = os.environ if env is None else env
    names = [name for name in URL_VARIABLES if env.get(name) and not is_local_url(env[name])]
    for name in HOST_VARIABLES:
        value = env.get(name)
        if value and not _all_local([h for h in value.split(",") if h]):
            names.append(name)
    return names


def require_local_environment(env: Mapping[str, str] | None = None) -> None:
    names = remote_variables(env)
    if names:
        raise ProductionUrlError(
            "refusing to run: " + ", ".join(names) + " point at a remote host. "
            "Tests may only use SQLite, in-memory Qdrant or localhost (spec section 16)."
        )
