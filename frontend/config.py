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
from urllib.parse import urlsplit

DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
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
    request_timeout: float = REQUEST_TIMEOUT_SECONDS
    wake_max_seconds: int = WAKE_MAX_SECONDS
    wake_poll_seconds: float = WAKE_POLL_SECONDS


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    base = (env.get("API_BASE_URL") or "").strip().rstrip("/") or DEFAULT_API_BASE_URL
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("API_BASE_URL must be a web address starting with http:// or https://")
    on_host = _truthy(env.get("RENDER"))
    return Settings(
        api_base_url=base,
        admin_machine=_truthy(env.get("ADMIN_MACHINE")) and not on_host,
        on_host=on_host,
    )
