"""Users: creation, roles, the last-admin rule and activation (spec 12.1, 13.1)."""

import inspect
import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from app.accounts import users
from app.accounts.users import (
    DuplicateAccount,
    RoleChangeRefused,
    create_admin,
    create_customer,
    get_by_id,
    get_by_username,
    other_active_admins,
    set_active,
    set_role,
)
from app.db.connection import connect
from tests.api_fixtures import now


def customer(db, name="asha_rao", email=None):
    return create_customer(
        db, username=name, email=email or f"{name}@example.test", password_hash="h", now=now()
    )


def admin(db, name="boss"):
    return create_admin(
        db, username=name, email=f"{name}@example.test", password_hash="h", now=now(), actor="cli"
    )


def audit_rows(db, action):
    rows = db.query_all(
        "SELECT actor, action, target, details FROM audit_log WHERE action = ? ORDER BY created_at",
        (action,),
    )
    return [(a, act, t, json.loads(d)) for a, act, t, d in rows]


# ---- creation -----------------------------------------------------------------------------


def test_create_customer_has_no_role_parameter_so_registration_cannot_set_one():
    assert "role" not in inspect.signature(create_customer).parameters
    with pytest.raises(TypeError):
        create_customer(  # type: ignore[call-arg]
            None, username="x", email="x@e.test", password_hash="h", now=now(), role="admin"
        )


def test_a_new_account_is_a_customer_and_active(db):
    user = customer(db)
    assert (user.role, user.is_active) == ("customer", True)
    assert user.password_hash == "h" and "password_hash" not in repr(user)


def test_the_password_hash_is_hidden_from_repr(db):
    user = create_customer(
        db, username="ravi", email="r@e.test", password_hash="SECRET-HASH", now=now()
    )
    assert "SECRET-HASH" not in repr(user)


def test_username_and_email_are_normalised_when_stored_and_when_looked_up(db):
    user = create_customer(
        db, username="  Asha_RAO ", email=" Asha@Example.TEST ", password_hash="h", now=now()
    )
    assert (user.username, user.email) == ("asha_rao", "asha@example.test")
    assert get_by_username(db, "ASHA_rao  ").id == user.id
    assert get_by_username(db, "nobody") is None


@pytest.mark.parametrize(
    ("name", "email"),
    [("asha_rao", "other@e.test"), ("ASHA_RAO", "other@e.test"), ("other", "ASHA@example.test")],
)
def test_a_taken_username_or_email_is_a_duplicate_whatever_the_case(db, name, email):
    customer(db, email="asha@example.test")
    with pytest.raises(DuplicateAccount) as caught:
        create_customer(db, username=name, email=email, password_hash="h", now=now())
    assert str(caught.value) == ""  # it never says which one


def test_get_by_id(db):
    user = customer(db)
    assert get_by_id(db, user.id).username == "asha_rao"
    assert get_by_id(db, "no-such-id") is None


def test_create_admin_makes_an_admin_and_writes_an_audit_row(db):
    user = admin(db)
    assert user.role == "admin"
    rows = audit_rows(db, "role_change")
    assert rows == [
        ("cli", "role_change", user.id, {"from": "none", "to": "admin", "via": "admin_command"})
    ]


# ---- set_role -------------------------------------------------------------------------------


def test_an_admin_can_make_a_customer_staff_and_it_is_audited(db):
    boss, member = admin(db), customer(db)
    assert set_role(db, actor=boss, target_id=member.id, new_role="staff", now=now()) == (
        "customer",
        "staff",
    )
    assert get_by_id(db, member.id).role == "staff"
    assert audit_rows(db, "role_change")[-1] == (
        boss.id,
        "role_change",
        member.id,
        {"from": "customer", "to": "staff"},
    )


def test_a_change_to_the_same_role_is_a_quiet_no_op_with_no_audit_row(db):
    boss, member = admin(db), customer(db)
    before = len(audit_rows(db, "role_change"))
    assert set_role(db, actor=boss, target_id=member.id, new_role="customer", now=now()) == (
        "customer",
        "customer",
    )
    assert len(audit_rows(db, "role_change")) == before


@pytest.mark.parametrize("role", ["customer", "staff"])
def test_only_an_admin_may_change_roles(db, role):
    boss, other = admin(db), customer(db, "ravi")
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, other.id))
    actor = get_by_id(db, other.id)
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=actor, target_id=boss.id, new_role="customer", now=now())
    assert caught.value.code == "not_an_admin"


def test_the_actors_role_is_read_again_from_the_database_not_trusted(db):
    admin(db)
    other_admin, member = admin(db, "boss2"), customer(db)
    stale = get_by_id(db, other_admin.id)  # an admin object that goes out of date
    db.execute("UPDATE users SET role = 'staff' WHERE id = ?", (other_admin.id,))  # demoted
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=stale, target_id=member.id, new_role="staff", now=now())
    assert caught.value.code == "not_an_admin"
    assert get_by_id(db, member.id).role == "customer"


def test_nobody_changes_their_own_role(db):
    boss, _ = admin(db), admin(db, "boss2")
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=boss, target_id=boss.id, new_role="staff", now=now())
    assert caught.value.code == "own_role"
    assert get_by_id(db, boss.id).role == "admin"


def test_an_unknown_role_or_target_is_refused(db):
    boss = admin(db)
    with pytest.raises(RoleChangeRefused) as unknown:
        set_role(db, actor=boss, target_id=boss.id, new_role="superuser", now=now())
    assert unknown.value.code == "unknown_role"
    with pytest.raises(RoleChangeRefused) as missing:
        set_role(db, actor=boss, target_id="no-such-user", new_role="staff", now=now())
    assert missing.value.code == "not_found"


def test_a_refused_change_writes_nothing(db):
    boss = admin(db)
    before = db.query_one("SELECT COUNT(*) FROM audit_log")[0]
    with pytest.raises(RoleChangeRefused):
        set_role(db, actor=boss, target_id=boss.id, new_role="staff", now=now())
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == before


def test_the_role_change_and_its_audit_row_are_one_transaction(db, monkeypatch):
    boss, member = admin(db), customer(db)

    def broken(*args, **kwargs):
        raise RuntimeError("the audit write failed")

    monkeypatch.setattr(users, "write_audit", broken)
    with pytest.raises(RuntimeError):
        set_role(db, actor=boss, target_id=member.id, new_role="staff", now=now())
    assert get_by_id(db, member.id).role == "customer"  # rolled back with the failed audit row


def test_two_admins_deactivating_each_other_at_once_cannot_leave_no_active_admin(env):
    """The last-admin check and the update share one transaction that holds the write lock, so
    of two simultaneous deactivations exactly one succeeds (the owner's command can do this)."""
    seed = env.db()
    admin(seed, "boss1")
    admin(seed, "boss2")
    seed.close()
    barrier = threading.Barrier(2)
    outcomes = []

    def deactivate(username):
        db = connect(env.url)
        try:
            barrier.wait()
            try:
                set_active(db, username=username, active=False, now=now(), actor="cli")
                outcomes.append("done")
            except RoleChangeRefused as refusal:
                outcomes.append(refusal.code)
        finally:
            db.close()

    threads = [
        threading.Thread(target=deactivate, args=("boss1",)),
        threading.Thread(target=deactivate, args=("boss2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = env.db()
    active = check.query_one("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1")[0]
    check.close()
    assert active == 1, outcomes  # never zero
    assert sorted(outcomes) == ["done", "last_admin"]


# ---- activation ---------------------------------------------------------------------------


def test_deactivate_and_reactivate_write_audit_rows(db):
    admin(db)
    member = customer(db)
    assert set_active(db, username="asha_rao", active=False, now=now(), actor="cli") is True
    assert get_by_id(db, member.id).is_active is False
    assert set_active(db, username="ASHA_RAO", active=True, now=now(), actor="cli") is True
    assert get_by_id(db, member.id).is_active is True
    assert [row[1] for row in audit_rows(db, "account_deactivated")] == ["account_deactivated"]
    assert len(audit_rows(db, "account_activated")) == 1


def test_activating_an_unknown_user_reports_false_and_writes_nothing(db):
    assert set_active(db, username="nobody", active=False, now=now(), actor="cli") is False
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == 0


def test_the_last_active_admin_cannot_be_deactivated(db):
    admin(db)
    with pytest.raises(RoleChangeRefused) as caught:
        set_active(db, username="boss", active=False, now=now(), actor="cli")
    assert caught.value.code == "last_admin"
    admin(db, "boss2")
    assert set_active(db, username="boss", active=False, now=now(), actor="cli") is True


def test_sqlite_rejects_a_bad_role_at_the_database_level_too(db):
    member = customer(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE users SET role = 'superuser' WHERE id = ?", (member.id,))


def test_a_deactivated_admin_cannot_change_roles(db):
    admin(db)
    other_admin, member = admin(db, "boss2"), customer(db)
    set_active(db, username="boss2", active=False, now=now(), actor="cli")
    actor = get_by_id(db, other_admin.id)
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=actor, target_id=member.id, new_role="staff", now=now())
    assert caught.value.code == "not_an_admin"
    assert get_by_id(db, member.id).role == "customer"


def test_the_api_can_assign_only_customer_and_staff():
    assert users.ASSIGNABLE_ROLES == ("customer", "staff")


@pytest.mark.parametrize("role", ["admin", "superuser", "", "Staff"])
def test_nobody_can_be_made_an_admin_through_set_role(db, role):
    boss, member = admin(db), customer(db)
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=boss, target_id=member.id, new_role=role, now=now())
    assert caught.value.code == "unknown_role"
    assert get_by_id(db, member.id).role == "customer"
    assert db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'role_change'") == (1,)


@pytest.mark.parametrize("new_role", ["customer", "staff"])
def test_an_account_that_is_already_an_admin_is_never_changed_through_set_role(db, new_role):
    first, second = admin(db), admin(db, "boss2")
    before = db.query_one("SELECT COUNT(*) FROM audit_log")[0]
    with pytest.raises(RoleChangeRefused) as caught:
        set_role(db, actor=first, target_id=second.id, new_role=new_role, now=now())
    assert caught.value.code == "target_is_admin"
    assert get_by_id(db, second.id).role == "admin"
    assert db.query_one("SELECT COUNT(*) FROM audit_log")[0] == before  # nothing written


def test_an_admin_cannot_change_themselves_and_both_admins_stay(db):
    first = admin(db)
    admin(db, "boss2")
    with pytest.raises(RoleChangeRefused) as own:
        set_role(db, actor=first, target_id=first.id, new_role="staff", now=now())
    assert own.value.code == "own_role"  # checked before the target-is-admin rule
    assert db.query_one("SELECT COUNT(*) FROM users WHERE role = 'admin'") == (2,)


def test_the_last_admin_check_counts_only_active_admins(db):
    first, second = admin(db), admin(db, "boss2")
    assert other_active_admins(db, first.id) == 1
    set_active(db, username="boss2", active=False, now=now(), actor="cli")
    assert other_active_admins(db, first.id) == 0  # the other admin is inactive: not counted
    assert other_active_admins(db, second.id) == 1  # first is active and is not second
    assert other_active_admins(db, "nobody") == 2 - 1  # one active admin exists in all


def test_audit_rows_from_role_changes_hold_no_password_or_hash_text(db):
    boss, member = admin(db), customer(db)
    set_role(db, actor=boss, target_id=member.id, new_role="staff", now=datetime.now(UTC))
    text = json.dumps(db.query_all("SELECT actor, action, target, details FROM audit_log")).lower()
    assert "password" not in text and "hash" not in text
