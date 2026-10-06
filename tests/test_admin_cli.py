"""The owner's account commands (create-admin, set-active). Run here with a typed-in password
supplied by the test, never from a real prompt."""

import json

import pytest

from app.accounts import admin_cli
from app.db.connection import connect
from tests.api_fixtures import PASSWORD

GOOD = PASSWORD


def run_cli(env, argv, *typed, url=None):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return typed[len(asked) - 1]

    code = admin_cli.main(argv, env={"DATABASE_URL": url or env.url}, ask=ask)
    return code, asked


def test_create_admin_makes_an_admin_who_can_sign_in(env, capsys):
    code, asked = run_cli(
        env, ["create-admin", "--username", "Boss", "--email", "Boss@Example.test"], GOOD, GOOD
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "created admin 'boss'" in out and GOOD not in out
    assert asked == ["Password: ", "Repeat the password: "]
    assert env.login("boss", GOOD).status_code == 200
    me = env.client.get("/auth/me", headers=env.bearer(env.token("boss"))).json()
    assert me["role"] == "admin"


def test_the_password_is_never_printed_or_stored_in_clear(env, capsys):
    run_cli(env, ["create-admin", "--username", "boss", "--email", "b@example.test"], GOOD, GOOD)
    captured = capsys.readouterr()
    assert GOOD not in captured.out + captured.err
    (stored,) = env.rows("SELECT password_hash FROM users")[0]
    assert stored.startswith("$argon2") and GOOD not in stored


def test_the_password_is_not_a_command_line_option(env):
    with pytest.raises(SystemExit):
        admin_cli.main(
            ["create-admin", "--username", "boss", "--email", "b@e.test", "--password", GOOD],
            env={"DATABASE_URL": env.url},
        )


def test_create_admin_writes_an_audit_row(env):
    run_cli(env, ["create-admin", "--username", "boss", "--email", "b@example.test"], GOOD, GOOD)
    ((actor, action, target, details),) = env.rows(
        "SELECT actor, action, target, details FROM audit_log"
    )
    assert (actor, action) == ("admin_command", "role_change")
    assert json.loads(details) == {"from": "none", "to": "admin", "via": "admin_command"}
    assert target == env.rows("SELECT id FROM users")[0][0]


def test_two_different_passwords_create_nothing(env, capsys):
    code, _ = run_cli(
        env, ["create-admin", "--username", "boss", "--email", "b@example.test"], GOOD, GOOD + "x"
    )
    assert code == 1 and "not the same" in capsys.readouterr().err
    assert env.rows("SELECT COUNT(*) FROM users") == [(0,)]


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("short1", "too_short"),
        ("password1234", "too_common"),
        ("a-boss-password-1", "contains_username"),
    ],
)
def test_a_password_that_breaks_the_rule_is_refused_with_its_reason(env, capsys, typed, reason):
    code, _ = run_cli(
        env, ["create-admin", "--username", "boss", "--email", "b@example.test"], typed, typed
    )
    err = capsys.readouterr().err
    assert code == 1 and reason in err and typed not in err
    assert env.rows("SELECT COUNT(*) FROM users") == [(0,)]


def test_a_taken_name_or_email_is_refused(env, capsys):
    env.register("boss", email="b@example.test")
    code, _ = run_cli(
        env, ["create-admin", "--username", "BOSS", "--email", "other@example.test"], GOOD, GOOD
    )
    assert code == 1 and "already in use" in capsys.readouterr().err
    assert env.rows("SELECT role FROM users") == [("customer",)]  # the existing user is untouched


def test_a_missing_database_address_is_exit_code_2(env, capsys):
    code = admin_cli.main(["set-active", "--username", "x", "--active", "no"], env={})
    assert code == 2 and "DATABASE_URL" in capsys.readouterr().err


def test_a_database_that_cannot_be_opened_prints_a_class_name_only(env, capsys):
    code = admin_cli.main(
        ["set-active", "--username", "x", "--active", "no"],
        env={"DATABASE_URL": "postgresql://someone:pw@127.0.0.1:1/db"},
    )
    err = capsys.readouterr().err
    assert code == 1 and "could not be used" in err and "127.0.0.1" not in err and "pw" not in err


# ---- set-active ----------------------------------------------------------------------------


def test_set_active_no_locks_an_account_out_and_yes_lets_it_back(env, capsys):
    env.make_user("boss", role="admin")
    env.make_user("asha_rao")
    token = env.token("asha_rao")
    assert run_cli(env, ["set-active", "--username", "ASHA_RAO", "--active", "no"])[0] == 0
    assert "now inactive" in capsys.readouterr().out
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401
    assert env.login("asha_rao").status_code == 401
    assert run_cli(env, ["set-active", "--username", "asha_rao", "--active", "yes"])[0] == 0
    assert env.login("asha_rao").status_code == 200


def test_set_active_writes_audit_rows(env):
    env.make_user("asha_rao")
    run_cli(env, ["set-active", "--username", "asha_rao", "--active", "no"])
    run_cli(env, ["set-active", "--username", "asha_rao", "--active", "yes"])
    actions = [row[0] for row in env.rows("SELECT action FROM audit_log ORDER BY created_at")]
    assert actions == ["account_deactivated", "account_activated"]


def test_set_active_on_an_unknown_user_says_so(env, capsys):
    code, _ = run_cli(env, ["set-active", "--username", "nobody", "--active", "no"])
    assert code == 1 and "no such user" in capsys.readouterr().err


def test_the_last_active_admin_cannot_be_locked_out(env, capsys):
    env.make_user("boss", role="admin")
    code, _ = run_cli(env, ["set-active", "--username", "boss", "--active", "no"])
    assert code == 1 and "no active admin" in capsys.readouterr().err
    assert env.rows("SELECT is_active FROM users") == [(1,)]


def test_an_unknown_command_or_choice_is_refused(env):
    with pytest.raises(SystemExit):
        admin_cli.main(["delete-everything"], env={"DATABASE_URL": env.url})
    with pytest.raises(SystemExit):
        admin_cli.main(
            ["set-active", "--username", "x", "--active", "maybe"], env={"DATABASE_URL": env.url}
        )


def test_the_command_closes_its_database_connection(env, monkeypatch):
    closed = []
    real = connect

    def tracking(url):
        db = real(url)
        original = db.close
        db.close = lambda: (closed.append(1), original())[1]
        return db

    code = admin_cli.main(
        ["set-active", "--username", "nobody", "--active", "no"],
        env={"DATABASE_URL": env.url},
        open_database=tracking,
    )
    assert code == 1 and closed == [1]
