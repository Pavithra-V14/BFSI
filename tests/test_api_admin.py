"""The admin role endpoint (spec 13.1: staff roles are assigned only by an admin)."""

import json

import pytest


def role_of(env, user_id):
    return env.rows("SELECT role FROM users WHERE id = ?", (user_id,))[0][0]


def change(env, token, user_id, role):
    return env.client.put(
        f"/admin/users/{user_id}/role", json={"role": role}, headers=env.bearer(token)
    )


def audit_rows(env):
    rows = env.rows("SELECT actor, target, details FROM audit_log WHERE action = 'role_change'")
    return [(actor, target, json.loads(details)) for actor, target, details in rows]


def test_an_admin_can_make_a_customer_staff_and_it_takes_effect_at_once(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    asha_token = env.token("asha_rao")
    assert env.client.get("/auth/me", headers=env.bearer(asha_token)).json()["role"] == "customer"
    response = change(env, token, user_id, "staff")
    assert response.status_code == 200
    assert response.json() == {"id": user_id, "role": "staff", "previous_role": "customer"}
    assert role_of(env, user_id) == "staff"
    # it shows in the user's existing token at once: the role is read from the database
    assert env.client.get("/auth/me", headers=env.bearer(asha_token)).json()["role"] == "staff"


def test_the_role_change_writes_an_audit_row_naming_the_admin_and_the_target(env):
    token = env.admin_token()
    admin_id = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    user_id = env.make_user("asha_rao")
    change(env, token, user_id, "staff")
    assert (admin_id, user_id, {"from": "customer", "to": "staff"}) in audit_rows(env)


@pytest.mark.parametrize("role", ["customer", "staff"])
def test_a_non_admin_cannot_change_roles(env, role):
    env.admin_token()
    env.make_user("ravi_k", role=role)
    target = env.make_user("asha_rao")
    token = env.token("ravi_k")
    response = change(env, token, target, "staff")
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert role_of(env, target) == "customer"


def test_a_customer_cannot_make_themselves_an_admin(env):
    env.admin_token()
    me = env.make_user("asha_rao")
    token = env.token("asha_rao")
    assert change(env, token, me, "admin").status_code == 403
    assert role_of(env, me) == "customer"


def test_without_a_token_the_endpoint_is_a_401(env):
    user_id = env.make_user("asha_rao")
    response = env.client.put(f"/admin/users/{user_id}/role", json={"role": "staff"})
    assert response.status_code == 401


def test_a_revoked_admin_token_cannot_change_roles(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    env.client.post("/auth/logout", headers=env.bearer(token))
    assert change(env, token, user_id, "staff").status_code == 401
    assert role_of(env, user_id) == "customer"


def test_an_admin_who_was_just_demoted_loses_the_power_at_once(env):
    first = env.admin_token("boss1")
    other_id = env.make_user("boss2", role="admin")
    second = env.token("boss2")
    victim = env.make_user("asha_rao")
    assert change(env, first, other_id, "staff").status_code == 200  # boss1 demotes boss2
    assert change(env, second, victim, "staff").status_code == 403  # boss2's token still valid
    assert role_of(env, victim) == "customer"


def test_nobody_changes_their_own_role(env):
    token = env.admin_token()
    admin_id = env.rows("SELECT id FROM users WHERE username = 'boss'")[0][0]
    response = change(env, token, admin_id, "staff")
    assert response.status_code == 409 and response.json()["error"]["code"] == "own_role"
    assert role_of(env, admin_id) == "admin"


def test_an_admin_can_promote_a_second_admin_who_can_then_manage_the_first(env):
    first = env.admin_token("boss1")
    first_id = env.rows("SELECT id FROM users WHERE username = 'boss1'")[0][0]
    second_id = env.make_user("boss2")
    assert change(env, first, second_id, "admin").status_code == 200
    second = env.token("boss2")
    assert change(env, second, first_id, "staff").status_code == 200  # fine: boss2 stays admin
    assert role_of(env, first_id) == "staff" and role_of(env, second_id) == "admin"


def test_an_active_admin_may_demote_an_inactive_one_and_stays_admin_themselves(env):
    token = env.admin_token("boss1")
    ghost = env.make_user("boss2", role="admin", active=False)
    boss1 = env.rows("SELECT id FROM users WHERE username = 'boss1'")[0][0]
    # boss2 is inactive and cannot act; boss1 demotes it and is untouched
    assert change(env, token, ghost, "staff").status_code == 200  # an active admin may demote it
    assert role_of(env, boss1) == "admin"


def test_an_unknown_user_is_a_404(env):
    token = env.admin_token()
    response = change(env, token, "00000000-0000-0000-0000-000000000000", "staff")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("user_id", ["a b", "x" * 65, "../x", "id;drop", "%00"])
def test_a_malformed_user_id_is_a_404_or_422_never_a_server_error(env, user_id):
    token = env.admin_token()
    response = change(env, token, user_id, "staff")
    assert response.status_code in (404, 422)


@pytest.mark.parametrize("role", ["superuser", "Admin", "", "root", None, 1])
def test_an_unknown_role_is_a_422(env, role):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    response = env.client.put(
        f"/admin/users/{user_id}/role", json={"role": role}, headers=env.bearer(token)
    )
    assert response.status_code == 422 and role_of(env, user_id) == "customer"


def test_extra_fields_in_the_role_body_are_refused(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    response = env.client.put(
        f"/admin/users/{user_id}/role",
        json={"role": "staff", "is_active": False},
        headers=env.bearer(token),
    )
    assert response.status_code == 422 and role_of(env, user_id) == "customer"


def test_changing_to_the_same_role_is_a_quiet_success_with_no_audit_row(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    before = len(audit_rows(env))
    response = change(env, token, user_id, "customer")
    assert response.status_code == 200 and len(audit_rows(env)) == before


def test_there_is_no_get_or_post_on_the_role_endpoint(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    url = f"/admin/users/{user_id}/role"
    assert env.client.get(url, headers=env.bearer(token)).status_code == 405
    assert (
        env.client.post(url, json={"role": "staff"}, headers=env.bearer(token)).status_code == 405
    )


def test_registration_still_cannot_make_an_admin_even_when_an_admin_exists(env):
    env.admin_token()
    response = env.client.post("/auth/register", json=env.body("sneaky", role="admin"))
    assert response.status_code == 422
    assert env.rows("SELECT COUNT(*) FROM users WHERE username = 'sneaky'") == [(0,)]


def test_the_admin_endpoint_counts_against_the_per_user_rate_limit(env):
    token = env.admin_token()
    user_id = env.make_user("asha_rao")
    codes = [change(env, token, user_id, "customer").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429
