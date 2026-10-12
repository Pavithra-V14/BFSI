"""The load run's users, made straight in the local database with the application's own account
functions (the same `create_customer` and password hasher that registration uses).

Why not register through the API: the registration limit per address (5 an hour) is on in the
real application and is never switched off; a load run that registered 160 users through it
would measure the limit, not the service. So the server process makes the accounts before it
starts to listen and writes the made-up credentials to a file in `load_out/` (git-ignored). The
load users then only log in, which is the part that is load.

The accounts exist in a throw-away SQLite file that is deleted when the server stops.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def seed_users(
    db: Any, count: int, *, prefix: str = "load", now: datetime | None = None
) -> tuple[list[str], str]:
    """Create `count` customer accounts; returns (usernames, the one password they share).

    One password hash is made and reused: this is a throw-away file and hashing 160 times would
    only slow the start. The password is random per run and never printed."""
    from app.accounts import passwords, users

    password = secrets.token_urlsafe(18) + "Aa1"
    password_hash = passwords.hash_password(password)
    moment = now or datetime.now(UTC)
    tag = secrets.token_hex(3)
    names: list[str] = []
    for index in range(count):
        name = f"{prefix}{tag}{index:04d}"
        with db.transaction(write=True):
            users.create_customer(
                db,
                username=name,
                email=f"{name}@load.invalid",
                password_hash=password_hash,
                now=moment,
            )
        names.append(name)
    return names, password


def write_users_file(path: Path, usernames: list[str], password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"usernames": usernames, "password": password}) + "\n", "utf-8")


def read_users_file(path: Path) -> tuple[list[str], str]:
    if not path.is_file():
        raise FileNotFoundError("no users file: start the server of this mode first")
    data = json.loads(path.read_text("utf-8"))
    return list(data["usernames"]), str(data["password"])
