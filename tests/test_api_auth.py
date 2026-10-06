"""Registration, login, logout and "who am I" through the API (spec 13.1, 16 auth tests)."""

import json
import sqlite3
from datetime import timedelta

import pytest

from app.accounts import tokens
from app.accounts.audit import keyed_hash
from tests.api_fixtures import (  # noqa: F401
    AUDIT_KEY,
    JWT_SECRET,
    OTHER_SECRET,
    PASSWORD,
    SOCKET_ADDRESS,
    Env,
    now,
)

# Throw-away phrases, built from words and not typed out as one string.
PROBE = "-".join(["never", "echo", "this", "back", "7Qx"])
NAMED_PASSWORD = "-".join(["my", "ravikumar", "pw", "1"])


def audit(env, action="login"):
    rows = env.rows(
        "SELECT actor, action, target, details FROM audit_log WHERE action = ? ORDER BY created_at",
        (action,),
    )
    return [(a, act, t, json.loads(d)) for a, act, t, d in rows]


def everything_in_database(env):
    """All text in all tables, to look for things that must never be stored."""
    connection = sqlite3.connect(env.path)
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    return dump


# ---- registration ------------------------------------------------------------------------------


def test_registration_makes_a_customer_and_returns_no_secret(env):
    response = env.register()
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "username", "role"} and body["role"] == "customer"
    assert body["username"] == "asha_rao"
    assert PASSWORD not in response.text and "hash" not in response.text.lower()


def test_the_password_is_stored_only_as_an_argon2_hash(env):
    env.register()
    (stored,) = env.rows("SELECT password_hash FROM users")[0]
    assert stored.startswith("$argon2") and PASSWORD not in stored
    assert PASSWORD not in everything_in_database(env)


@pytest.mark.parametrize("role", ["admin", "staff", "customer", "superuser", ""])
def test_a_role_in_the_registration_body_is_refused_with_422(env, role):
    response = env.client.post("/auth/register", json=env.body(role=role))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert "body.role" in response.json()["error"]["fields"]
    assert env.rows("SELECT COUNT(*) FROM users") == [(0,)]  # nothing was created


@pytest.mark.parametrize("extra", ["is_active", "id", "created_at", "password_hash", "is_admin"])
def test_any_other_extra_field_is_refused_too(env, extra):
    response = env.client.post("/auth/register", json=env.body(**{extra: 1}))
    assert response.status_code == 422 and env.rows("SELECT COUNT(*) FROM users") == [(0,)]


def test_whatever_the_request_the_new_account_is_always_a_customer(env):
    for name in ("asha_rao", "ravi_k", "meena_s"):
        assert env.register(name).json()["role"] == "customer"
    assert env.rows("SELECT DISTINCT role FROM users") == [("customer",)]


@pytest.mark.parametrize("missing", ["username", "email", "password"])
def test_a_missing_field_is_a_422(env, missing):
    body = env.body()
    del body[missing]
    response = env.client.post("/auth/register", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize(
    "username", ["ab", "a" * 33, "-asha", "asha rao", "asha@rao", "asha/rao", "Ünicode", ""]
)
def test_a_bad_username_is_refused(env, username):
    response = env.register(username, email="x@example.test")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_username"


@pytest.mark.parametrize(
    "email",
    ["nobody", "a@b", "a@@example.test", "a b@example.test", "@example.test", "a@.test", ""],
)
def test_a_bad_email_is_refused(env, email):
    response = env.register("asha_rao", email=email)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_email"


@pytest.mark.parametrize(
    ("typed", "reason"),
    [
        ("short1", "too_short"),
        ("password123", "too_common"),
        ("my-asha_rao-pass-1", "contains_username"),
        ("x" * 129, "too_long"),
    ],
)
def test_a_password_that_breaks_the_rule_is_refused_with_its_reason(env, typed, reason):
    response = env.register(password=typed)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_password" and reason in error["reasons"]
    assert typed not in response.text  # the password is never echoed back


def test_the_email_name_in_the_password_is_refused(env):
    response = env.register("asha_rao", email="ravikumar@example.test", password=NAMED_PASSWORD)
    assert response.json()["error"]["reasons"] == ["contains_email_name"]


def test_a_validation_error_never_echoes_what_was_sent(env):
    response = env.client.post("/auth/register", json=env.body(password=PROBE, unexpected=PROBE))
    assert response.status_code == 422
    assert PROBE not in response.text and "asha_rao" not in response.text


def test_a_username_taken_gives_the_same_409_as_an_email_taken(env):
    env.register("asha_rao", email="asha@example.test")
    same_name = env.register("asha_rao", email="other@example.test")
    same_email = env.register("ravi_k", email="asha@example.test")
    assert same_name.status_code == same_email.status_code == 409
    assert same_name.json() == same_email.json()  # one message: it never says which
    assert same_name.json()["error"]["code"] == "account_exists"


def test_names_and_emails_are_taken_whatever_their_case_or_spacing(env):
    env.register("Asha_Rao", email="Asha@Example.TEST")
    assert env.register("asha_rao", email="other@example.test").status_code == 409
    assert env.register("ravi_k", email=" ASHA@example.test ").status_code == 409
    assert env.rows("SELECT username, email FROM users") == [("asha_rao", "asha@example.test")]


def test_a_failed_registration_leaves_no_half_account(env):
    env.register("asha_rao", email="asha@example.test")
    env.register("ravi_k", email="asha@example.test")
    assert env.rows("SELECT COUNT(*) FROM users") == [(1,)]


def test_registration_is_throttled_to_five_an_hour_per_address(env):
    codes = [env.register(f"user_{i}").status_code for i in range(6)]
    assert codes == [201] * 5 + [429]
    blocked = env.register("user_7")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    assert blocked.json()["error"]["code"] == "too_many_attempts"


def test_registration_writes_no_audit_row(env):
    env.register()
    assert env.rows("SELECT COUNT(*) FROM audit_log") == [(0,)]


# ---- login ---------------------------------------------------------------------------------


def test_a_correct_login_returns_a_token_that_works(env):
    env.register()
    response = env.login()
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert set(body) == {"access_token", "token_type", "expires_at"}
    me = env.client.get("/auth/me", headers=env.bearer(body["access_token"]))
    assert me.status_code == 200 and me.json()["username"] == "asha_rao"


def test_the_username_is_matched_whatever_its_case_or_spacing(env):
    env.register("asha_rao")
    assert env.login("  ASHA_Rao ").status_code == 200


def test_a_token_lasts_for_the_configured_time(env):
    env.register()
    token = env.login().json()["access_token"]
    claims = tokens.decode_token(env.settings, token)
    assert claims.expires_at - claims.issued_at == timedelta(minutes=480)


def test_wrong_password_and_unknown_user_look_exactly_the_same(env):
    env.register()
    wrong = env.login("asha_rao", "not the right password")
    unknown = env.login("nobody_here", "not the right password")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.json()["error"]["code"] == "bad_credentials"
    assert "set-cookie" not in wrong.headers


def test_an_unknown_user_costs_a_password_check_like_a_known_one(env, monkeypatch):
    from app.accounts import passwords

    checked = []
    real = passwords.verify_password

    def spy(stored, typed):
        checked.append(stored)
        return real(stored, typed)

    monkeypatch.setattr(passwords, "verify_password", spy)
    env.register()
    env.login("asha_rao", "wrong password here")
    env.login("nobody_here", "wrong password here")
    assert len(checked) == 2 and checked[1] == passwords.dummy_hash()  # the dummy hash was checked


def test_an_inactive_account_cannot_log_in_and_looks_like_a_wrong_password(env):
    env.make_user("asha_rao", active=False)
    inactive = env.login("asha_rao", PASSWORD)
    wrong = env.login("nobody_here", PASSWORD)
    assert inactive.status_code == 401 and inactive.json() == wrong.json()


def test_login_with_extra_fields_or_missing_fields_is_a_422(env):
    assert env.client.post("/auth/login", json={"username": "a"}).status_code == 422
    extra = {"username": "a", "role": "admin", "pw": "b"}
    assert env.client.post("/auth/login", json=extra).status_code == 422
    assert env.client.post("/auth/login", content=b"not json").status_code == 422


def test_an_enormous_password_is_refused_without_a_crash(env):
    env.register()
    response = env.login("asha_rao", "x" * 5_000)
    assert response.status_code == 422  # over the field's length limit


# ---- login throttling --------------------------------------------------------------------------


def test_the_eleventh_login_attempt_for_a_username_is_throttled(env):
    env.register()
    codes = [env.login("asha_rao", "wrong password here").status_code for _ in range(11)]
    assert codes == [401] * 10 + [429]
    blocked = env.login("asha_rao", PASSWORD)  # even the right password is blocked now
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "too_many_attempts"
    assert int(blocked.headers["retry-after"]) > 0


def test_a_throttled_attempt_does_not_check_the_password_or_write_an_audit_row(env):
    env.register()
    for _ in range(10):
        env.login("asha_rao", "wrong password here")
    rows_before = env.rows("SELECT COUNT(*) FROM audit_log")
    env.login("asha_rao", PASSWORD)
    assert env.rows("SELECT COUNT(*) FROM audit_log") == rows_before


def test_one_address_is_throttled_after_30_attempts_across_usernames(env):
    codes = [env.login(f"user_{i:02d}", "wrong password here").status_code for i in range(31)]
    assert codes == [401] * 30 + [429]


def test_a_spoofed_forwarded_header_does_not_change_the_address_the_throttle_sees(env):
    # Every attempt claims a different address, as an attacker would. The throttle uses the
    # socket address, so the claims change nothing and the limit still applies.
    codes = []
    for index in range(31):
        headers = {"X-Forwarded-For": f"198.51.100.{index}", "X-Real-IP": f"192.0.2.{index}"}
        response = env.login(f"user_{index:02d}", "wrong password here", headers=headers)
        codes.append(response.status_code)
    assert codes == [401] * 30 + [429]
    assert len(env.redis.keys("ctr:rl:login_ip:*")) == 1  # one counter: one socket address


def test_a_spoofed_header_cannot_get_a_blocked_address_through(env):
    for index in range(30):
        env.login(f"user_{index:02d}", "wrong password here")
    headers = {"X-Forwarded-For": "198.51.100.1"}
    assert env.login("brand_new", "wrong password here", headers=headers).status_code == 429


def test_the_counter_key_is_made_from_the_socket_address_not_the_header(env):
    env.login("asha_rao", "x", headers={"X-Forwarded-For": "198.51.100.77"})
    (key,) = env.redis.keys("ctr:rl:login_ip:*")
    assert key.endswith(keyed_hash(AUDIT_KEY, "throttle-ip", SOCKET_ADDRESS))
    assert not key.endswith(keyed_hash(AUDIT_KEY, "throttle-ip", "198.51.100.77"))


def test_a_different_socket_address_has_its_own_ip_counter(env):
    from fastapi.testclient import TestClient

    other = TestClient(env.app, raise_server_exceptions=False, client=("198.51.100.9", 1234))
    for index in range(30):
        env.login(f"user_{index:02d}", "wrong password here")
    assert env.login("brand_new", "wrong password here").status_code == 429
    assert env.login("brand_new", "wrong password here", client=other).status_code == 401


def test_login_throttling_goes_on_when_upstash_is_down(env):
    from app.accounts.throttle import Throttle
    from app.cache.store import CacheStore
    from tests.test_cache_store import FakeClock, FlakyRedis

    dead = CacheStore(FlakyRedis(), clock=FakeClock())
    env.app.state.services.throttle = Throttle(dead, env.settings.audit_hash_key)
    env.register()
    codes = [env.login("asha_rao", "wrong password here").status_code for _ in range(11)]
    assert codes == [401] * 10 + [429]  # the in-process stand-in kept counting


# ---- tokens, logout, revocation ----------------------------------------------------------------


def test_a_request_without_a_token_is_a_401_with_a_challenge(env):
    response = env.client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "not_signed_in"


@pytest.mark.parametrize(
    "header", ["", "Bearer", "Bearer ", "Basic abc", "abc", "Bearer a b", "bearer"]
)
def test_a_malformed_authorization_header_is_a_401(env, header):
    assert env.client.get("/auth/me", headers={"Authorization": header}).status_code == 401


def test_the_scheme_word_is_case_insensitive(env):
    env.register()
    token = env.token()
    headers = {"Authorization": f"bearer {token}"}
    assert env.client.get("/auth/me", headers=headers).status_code == 200


def test_a_tampered_token_is_a_401(env):
    env.register()
    token = env.token()
    broken = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert env.client.get("/auth/me", headers=env.bearer(broken)).status_code == 401


def test_an_expired_token_is_a_401(env):
    env.register()
    user_id = env.rows("SELECT id FROM users")[0][0]
    token, _ = tokens.issue_token(env.settings, user_id, now() - timedelta(days=2))
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_a_token_signed_with_another_secret_is_a_401(env):
    from tests.api_fixtures import make_settings

    env.register()
    user_id = env.rows("SELECT id FROM users")[0][0]
    other = make_settings(jwt_secret=OTHER_SECRET)
    token, _ = tokens.issue_token(other, user_id, now())
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_a_token_for_an_account_that_does_not_exist_is_a_401(env):
    token, _ = tokens.issue_token(env.settings, "no-such-user-id", now())
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_logout_revokes_the_token_at_once(env):
    env.register()
    token = env.token()
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 200
    out = env.client.post("/auth/logout", headers=env.bearer(token))
    assert out.status_code == 204 and out.content == b""
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM revoked_tokens") == [(1,)]


def test_logging_out_twice_is_harmless_and_the_second_call_is_refused(env):
    env.register()
    token = env.token()
    env.client.post("/auth/logout", headers=env.bearer(token))
    assert env.client.post("/auth/logout", headers=env.bearer(token)).status_code == 401
    assert env.rows("SELECT COUNT(*) FROM revoked_tokens") == [(1,)]


def test_logging_out_one_token_leaves_the_users_other_token_working(env):
    env.register()
    first, second = env.token(), env.token()
    env.client.post("/auth/logout", headers=env.bearer(first))
    assert env.client.get("/auth/me", headers=env.bearer(second)).status_code == 200


def test_the_revoked_row_expires_with_the_token(env):
    env.register()
    token = env.token()
    env.client.post("/auth/logout", headers=env.bearer(token))
    claims = tokens.decode_token(env.settings, token)
    (expires,) = env.rows("SELECT expires_at FROM revoked_tokens")[0]
    assert expires == tokens.utc_text(claims.expires_at)


def test_a_deactivated_account_loses_access_at_once(env):
    user_id = env.make_user("asha_rao")
    token = env.token()
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 200
    connection = sqlite3.connect(env.path)
    connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_a_deleted_account_loses_access_at_once(env):
    user_id = env.make_user("asha_rao")
    token = env.token()
    connection = sqlite3.connect(env.path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()
    assert env.client.get("/auth/me", headers=env.bearer(token)).status_code == 401


def test_a_role_change_shows_at_once_in_an_existing_token(env):
    user_id = env.make_user("asha_rao")
    token = env.token()
    assert env.client.get("/auth/me", headers=env.bearer(token)).json()["role"] == "customer"
    connection = sqlite3.connect(env.path)
    connection.execute("UPDATE users SET role = 'staff' WHERE id = ?", (user_id,))
    connection.commit()
    connection.close()
    assert env.client.get("/auth/me", headers=env.bearer(token)).json()["role"] == "staff"


def test_me_returns_only_the_id_the_name_and_the_role(env):
    env.register()
    me = env.client.get("/auth/me", headers=env.bearer(env.token())).json()
    assert set(me) == {"id", "username", "role"}


def test_twenty_one_requests_a_minute_from_one_user_get_a_429(env):
    env.register()
    token = env.token()
    codes = [env.client.get("/auth/me", headers=env.bearer(token)).status_code for _ in range(21)]
    assert codes == [200] * 20 + [429]
    blocked = env.client.get("/auth/me", headers=env.bearer(token))
    assert blocked.json()["error"]["code"] == "too_many_requests"
    assert int(blocked.headers["retry-after"]) > 0


def test_another_user_is_not_slowed_by_the_first(env):
    env.register("asha_rao")
    env.register("ravi_k")
    first, second = env.token("asha_rao"), env.token("ravi_k")
    for _ in range(21):
        env.client.get("/auth/me", headers=env.bearer(first))
    assert env.client.get("/auth/me", headers=env.bearer(second)).status_code == 200


def test_a_garbage_token_does_not_touch_the_rate_limiter(env):
    for _ in range(30):
        env.client.get("/auth/me", headers=env.bearer("garbage"))
    assert env.redis.keys("ctr:rl:user:*") == []


# ---- audit rows ---------------------------------------------------------------------------------


def test_a_successful_login_writes_one_audit_row(env):
    user_id = env.register().json()["id"]
    env.login()
    expected = (user_id, "login", user_id, {"reason": "success", "result": "success"})
    assert audit(env) == [expected]


def test_a_wrong_password_writes_a_failed_row_for_the_known_user(env):
    user_id = env.register().json()["id"]
    env.login("asha_rao", "wrong password here")
    expected = (user_id, "login", user_id, {"reason": "bad_password", "result": "failed"})
    assert audit(env) == [expected]


def test_an_inactive_user_is_audited_as_inactive(env):
    user_id = env.make_user("asha_rao", active=False)
    env.login("asha_rao", PASSWORD)
    ((actor, _, _, details),) = audit(env)
    assert actor == user_id and details == {"reason": "inactive", "result": "failed"}


def test_an_unknown_username_is_stored_only_as_a_keyed_hash(env):
    env.login("Typed_Name_Here", "wrong password here")
    ((actor, action, target, details),) = audit(env)
    assert actor == "anonymous" and details == {"reason": "unknown_user", "result": "failed"}
    assert target == keyed_hash(AUDIT_KEY, "audit-username", "typed_name_here")
    assert "typed_name_here" not in everything_in_database(env).lower()


def test_a_password_typed_into_the_username_box_is_never_stored(env):
    env.login(PASSWORD, "whatever it was")
    assert PASSWORD not in everything_in_database(env)


def test_no_password_token_or_secret_is_in_any_stored_text(env):
    env.register()
    token = env.token()
    env.login("asha_rao", "wrong password here")
    env.client.post("/auth/logout", headers=env.bearer(token))
    dump = everything_in_database(env)
    assert PASSWORD not in dump and "wrong password here" not in dump
    assert token not in dump and JWT_SECRET not in dump and AUDIT_KEY not in dump


def test_login_audit_rows_need_no_registration_or_logout_rows(env):
    env.register()
    token = env.token()
    env.client.post("/auth/logout", headers=env.bearer(token))
    assert env.rows("SELECT DISTINCT action FROM audit_log") == [("login",)]


def test_a_failed_audit_write_fails_the_login_with_a_503_not_a_token(env, monkeypatch):
    from app.accounts import audit as audit_module

    env.register()

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(audit_module, "write_audit", broken)
    response = env.login()
    assert response.status_code == 503 and "access_token" not in response.text


def test_a_login_rehashes_a_password_stored_with_weaker_settings(env, monkeypatch):
    from argon2 import PasswordHasher

    from app.accounts import passwords

    env.register()
    weaker = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    connection = sqlite3.connect(env.path)
    connection.execute("UPDATE users SET password_hash = ?", (weaker.hash(PASSWORD),))
    connection.commit()
    connection.close()
    monkeypatch.setattr(passwords, "_HASHER", stronger)
    assert env.login().status_code == 200
    (stored,) = env.rows("SELECT password_hash FROM users")[0]
    assert "m=16,t=2" in stored  # upgraded in place
    assert env.login().status_code == 200  # and still works
