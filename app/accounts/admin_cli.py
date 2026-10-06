"""The owner's account commands, run in the owner's own terminal (never by Claude):

    uv run python -m app.accounts.admin_cli create-admin --username NAME --email ADDRESS
    uv run python -m app.accounts.admin_cli set-active --username NAME --active no

The password is asked for with hidden input (twice), so it is never on the command line, in the
shell history or on the screen. The database address comes from DATABASE_URL in the shell. Output
is names and status only. Each command writes an audit row.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from app.accounts import passwords, users
from app.db.connection import Database, connect
from app.db.identity import normalise_email, normalise_username
from app.db.migrate import MigrationError
from app.store_errors import StoreRejected, StoreUnavailable

ACTOR = "admin_command"


def _ask_password(ask: Callable[[str], str]) -> str | None:
    first = ask("Password: ")
    if ask("Repeat the password: ") != first:
        print("error: the two passwords are not the same", file=sys.stderr)
        return None
    return first


def create_admin(db: Database, username: str, email: str, password: str) -> int:
    name, address = normalise_username(username), normalise_email(email)
    problems = passwords.check_password_rule(password, username=name, email=address)
    if problems:
        print("error: that password is not allowed: " + ", ".join(problems), file=sys.stderr)
        return 1
    password_hash = passwords.hash_password(password)
    try:
        with db.transaction(write=True):
            user = users.create_admin(
                db,
                username=name,
                email=address,
                password_hash=password_hash,
                now=datetime.now(UTC),
                actor=ACTOR,
            )
    except users.DuplicateAccount:
        print("error: that username or email is already in use", file=sys.stderr)
        return 1
    print(f"created admin '{user.username}' (id {user.id})")
    return 0


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    ask: Callable[[str], str] = getpass.getpass,
    open_database: Callable[[str], Database] = connect,
) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.accounts.admin_cli", description="Create or deactivate accounts."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-admin", help="create an admin account")
    create.add_argument("--username", required=True)
    create.add_argument("--email", required=True)
    active = commands.add_parser("set-active", help="activate or deactivate an account")
    active.add_argument("--username", required=True)
    active.add_argument("--active", required=True, choices=["yes", "no"])
    args = parser.parse_args(argv)

    env = os.environ if env is None else env
    url = (env.get("DATABASE_URL") or "").strip()
    if not url:
        print("error: the environment variable DATABASE_URL is not set", file=sys.stderr)
        return 2
    password = None
    if args.command == "create-admin":
        password = _ask_password(ask)
        if password is None:
            return 1
    try:
        db = open_database(url)
    except (StoreUnavailable, StoreRejected, MigrationError) as error:
        print(f"error: the database could not be used ({type(error).__name__})", file=sys.stderr)
        return 1
    try:
        if args.command == "create-admin":
            return create_admin(db, args.username, args.email, password or "")
        try:
            found = users.set_active(
                db,
                username=args.username,
                active=args.active == "yes",
                now=datetime.now(UTC),
                actor=ACTOR,
            )
        except users.RoleChangeRefused:
            print("error: that would leave no active admin", file=sys.stderr)
            return 1
        if not found:
            print("error: no such user", file=sys.stderr)
            return 1
        print(
            f"account '{normalise_username(args.username)}' is now "
            f"{'active' if args.active == 'yes' else 'inactive'}"
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
