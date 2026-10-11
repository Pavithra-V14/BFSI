"""Settings of the frontend service, read from the environment (names only in the repository).

- `API_BASE_URL`: where the API is (for example the Render service address). Without it the
  frontend uses the local address of the API on this machine.
- `ADMIN_MACHINE`: `true` on the owner's own machine. It only decides which controls are SHOWN
  (the ingest and delete commands, entity review, "Run eval"). The API enforces the real rules,
  so a wrong value here never grants anything. It is ignored on the host (`RENDER` is set).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
REQUEST_TIMEOUT_SECONDS = 45.0  # the API's own budget for one answer is 30 seconds
WAKE_MAX_SECONDS = 75  # a free Render service can take about a minute to wake up
WAKE_POLL_SECONDS = 3.0


class ConfigError(Exception):
    """A setting that cannot be used. The message names the variable, never its value."""


@dataclass(frozen=True)
class Settings:
    api_base_url: str = DEFAULT_API_BASE_URL
    admin_machine: bool = False
    on_host: bool = False
    insecure_api: bool = False  # plain http to a host that is not this machine
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    wake_max_seconds: int = WAKE_MAX_SECONDS
    wake_poll_seconds: float = WAKE_POLL_SECONDS


def config_file_found(cwd: Path | None = None) -> bool:
    """Streamlit reads `.streamlit/config.toml` from the folder it is started in (and the home
    folder), not from the folder of the script. Started from another folder, the file's safety
    settings (XSRF protection, no error details) are NOT active."""
    return ((cwd or Path.cwd()) / ".streamlit" / "config.toml").is_file()


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    base = (env.get("API_BASE_URL") or "").strip().rstrip("/") or DEFAULT_API_BASE_URL
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("API_BASE_URL must be a web address starting with http:// or https://")
    on_host = _truthy(env.get("RENDER"))
    host = (parts.hostname or "").lower()
    return Settings(
        api_base_url=base,
        insecure_api=parts.scheme == "http" and host not in LOOPBACK_HOSTS,
        admin_machine=_truthy(env.get("ADMIN_MACHINE")) and not on_host,
        on_host=on_host,
    )
