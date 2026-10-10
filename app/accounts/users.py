"""Users (spec 12.1, 13.1): create, find, change a role, activate or deactivate.

A username or email is normalised with one rule (app/db/identity.py) before it is stored or looked
up. An account made through `create_customer` is always a customer: the function has no role
parameter, so registration can never set one. Roles change only through `set_role` (an admin, in
one transaction, with an audit row).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.accounts.audit import write_audit
from app.accounts.tokens import utc_text
from app.db.connection import Database
from app.db.identity import normalise_email, normalise_username
from app.memory.roles import reset_sessions_for_role_change

ROLES = ("customer", "staff", "admin")
ASSIGNABLE_ROLES = ("customer", "staff")  # admins are made only by the owner's command

_COLUMNS = "id, username, email, password_hash, role, created_at, is_active"


class DuplicateAccount(Exception):
    """The username or the email is already in use (never says which)."""


class RoleChangeRefused(Exception):
    """A role change that is not allowed. `code` is a short machine-readable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class User:
    id: str
    username: str
    email: str
    role: str
    created_at: str
    is_active: bool
    password_hash: str = field(repr=False)


def _user(row: tuple) -> User:
    return User(
        id=row[0],
        username=row[1],
        email=row[2],
        password_hash=row[3],
        role=row[4],
        created_at=str(row[5]),
        is_active=bool(row[6]),
    )


def _is_duplicate(error: BaseException) -> bool:
    if isinstance(error, sqlite3.IntegrityError):
        return "UNIQUE" in str(error).upper()
    try:
        import psycopg

        return isinstance(error, psycopg.errors.UniqueViolation)
    except ImportError:  # pragma: no cover
        return False


def create_customer(
    db: Database, *, username: str, email: str, password_hash: str, now: datetime
) -> User:
    """Create a customer. The role is not a parameter: it is always 'customer'."""
    user_id = str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO users (id, username, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                user_id,
                normalise_username(username),
                normalise_email(email),
                password_hash,
                utc_text(now),
            ),
        )
    except Exception as error:
        if _is_duplicate(error):
            raise DuplicateAccount from None
        raise
    return get_by_id(db, user_id)  # type: ignore[return-value]


def create_admin(
    db: Database, *, username: str, email: str, password_hash: str, now: datetime, actor: str
) -> User:
    """Create an admin. Only the owner's command uses this; it writes an audit row."""
    user = create_customer(db, username=username, email=email, password_hash=password_hash, now=now)
    db.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user.id,))
    write_audit(
        db,
        actor=actor,
        action="role_change",
        target=user.id,
        details={"from": "none", "to": "admin", "via": "admin_command"},
        now=now,
    )
    return get_by_id(db, user.id)  # type: ignore[return-value]


def get_by_id(db: Database, user_id: str) -> User | None:
    row = db.query_one(f"SELECT {_COLUMNS} FROM users WHERE id = ?", (user_id,))
    return _user(row) if row else None


def get_by_username(db: Database, username: str) -> User | None:
    row = db.query_one(
        f"SELECT {_COLUMNS} FROM users WHERE username = ?", (normalise_username(username),)
    )
    return _user(row) if row else None


def other_active_admins(db: Database, exclude_id: str) -> int:
    """How many active admins there are besides `exclude_id`."""
    row = db.query_one(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = ? AND id != ?",
        (True, exclude_id),
    )
    return int(row[0])  # type: ignore[index]


def set_role(
    db: Database, *, actor: User, target_id: str, new_role: str, now: datetime
) -> tuple[str, str]:
    """An admin makes someone a customer or staff. Returns (old role, new role).

    The API can assign only the roles in ASSIGNABLE_ROLES and never touches an account that is
    already an admin: admins are created by the owner's command (create_admin) and an admin is
    removed or locked out only there (set_active). Refused (RoleChangeRefused): an unknown or
    unassignable role, an actor who is not an active admin, an admin changing their own role,
    an unknown target, or a target that is an admin. The check and the update run in ONE
    transaction that holds the write lock (BEGIN IMMEDIATE on SQLite, LOCK TABLE on Postgres), and
    the audit row is part of the same transaction.
    """
    if new_role not in ASSIGNABLE_ROLES:
        raise RoleChangeRefused("unknown_role")
    with db.transaction(write=True):
        if db.kind == "postgres":
            db.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        current = get_by_id(db, actor.id)  # read again under the lock
        if current is None or not current.is_active or current.role != "admin":
            raise RoleChangeRefused("not_an_admin")
        if target_id == actor.id:
            raise RoleChangeRefused("own_role")
        target = get_by_id(db, target_id)
        if target is None:
            raise RoleChangeRefused("not_found")
        if target.role == "admin":
            raise RoleChangeRefused("target_is_admin")
        if target.role == new_role:
            return target.role, new_role
        db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, target.id))
        # Sessions, summaries and facts can rest on documents only the old role could see: end them
        # in the same transaction (the user starts a fresh session).
        sessions_reset = reset_sessions_for_role_change(db, target.id)
        write_audit(
            db,
            actor=actor.id,
            action="role_change",
            target=target.id,
            details={"from": target.role, "to": new_role, "sessions_reset": sessions_reset},
            now=now,
        )
    return target.role, new_role


def set_active(db: Database, *, username: str, active: bool, now: datetime, actor: str) -> bool:
    """Activate or deactivate an account (the owner's command). Returns False if no such user.
    Deactivating the last active admin is refused. Writes an audit row."""
    with db.transaction(write=True):
        if db.kind == "postgres":
            db.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        user = get_by_username(db, username)
        if user is None:
            return False
        if not active and user.role == "admin" and other_active_admins(db, user.id) == 0:
            raise RoleChangeRefused("last_admin")
        db.execute("UPDATE users SET is_active = ? WHERE id = ?", (active, user.id))
        write_audit(
            db,
            actor=actor,
            action="account_activated" if active else "account_deactivated",
            target=user.id,
            details={},
            now=now,
        )
    return True


def list_users(db: Database, *, limit: int, offset: int) -> tuple[list[User], int]:
    """One page of accounts (oldest first) and the total. The caller shows no password hash."""
    total = db.query_one("SELECT COUNT(*) FROM users")
    rows = db.query_all(
        f"SELECT {_COLUMNS} FROM users ORDER BY created_at, id LIMIT ? OFFSET ?",
        (int(limit), int(offset)),
    )
    return [_user(row) for row in rows], int(total[0]) if total else 0


def set_active_by_id(
    db: Database, *, actor: User, target_id: str, active: bool, now: datetime
) -> bool:
    """An admin turns a customer or staff account on or off (Phase 10). Never an admin account
    (those are changed only by the owner's command), never one's own, and the last active admin
    is protected by that rule. Returns the new state. Refused with RoleChangeRefused."""
    with db.transaction(write=True):
        if db.kind == "postgres":
            db.execute("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE")
        current = get_by_id(db, actor.id)
        if current is None or not current.is_active or current.role != "admin":
            raise RoleChangeRefused("not_an_admin")
        target = get_by_id(db, target_id)
        if target is None:
            raise RoleChangeRefused("not_found")
        if target.role == "admin":
            raise RoleChangeRefused("target_is_admin")
        if target.is_active != active:
            db.execute("UPDATE users SET is_active = ? WHERE id = ?", (active, target.id))
            write_audit(
                db,
                actor=actor.id,
                action="account_activated" if active else "account_deactivated",
                target=target.id,
                details={"via": "api"},
                now=now,
            )
    return active


def update_password_hash(db: Database, user_id: str, password_hash: str) -> None:
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))
