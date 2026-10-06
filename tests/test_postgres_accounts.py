"""The account code on a real Postgres. Runs only in CI, on the throw-away service container
(TEST_POSTGRES_URL); skipped everywhere else, so it never runs on the admin machine.

Written without being run against Postgres (none is installed on this machine): the first CI run
is its first run. It covers what SQLite cannot show: text times going into TIMESTAMPTZ columns,
booleans as parameters, `ON CONFLICT`, unique-violation detection, and the table lock that makes
the last-admin check atomic.
"""

import json
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest

from app.accounts import tokens, users
from app.api.main import create_app
from app.cache.store import CacheStore
from app.db.connection import connect
from app.db.migrate import run
from tests.api_fixtures import PASSWORD, make_settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set (these tests run in CI only)"
    ),
]

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def pg_url():
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    separator = "&" if "?" in POSTGRES_URL else "?"
    url = f"{POSTGRES_URL}{separator}options=-csearch_path%3D{schema}"
    run(url, MIGRATIONS, log=lambda line: None)
    yield url
    with psycopg.connect(POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def db(pg_url):
    database = connect(pg_url)
    yield database
    database.close()


def make(db, name="asha", role=None):
    with db.transaction(write=True):
        user = users.create_customer(
            db, username=name, email=f"{name}@example.test", password_hash="h", now=NOW
        )
        if role:
            db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user.id))
    return users.get_by_id(db, user.id)


def test_a_user_round_trips_with_times_and_booleans(db):
    user = make(db)
    assert user.role == "customer" and user.is_active is True
    assert user.created_at  # a real timestamp came back from TIMESTAMPTZ
    assert users.get_by_username(db, "ASHA").id == user.id


def test_a_duplicate_is_recognised_as_a_unique_violation(db):
    make(db)
    with pytest.raises(users.DuplicateAccount):
        with db.transaction(write=True):
            users.create_customer(
                db, username="Asha", email="other@example.test", password_hash="h", now=NOW
            )


def test_set_role_runs_under_the_table_lock_and_writes_its_audit_row(db):
    boss, member = make(db, "boss", "admin"), make(db, "ravi")
    assert users.set_role(db, actor=boss, target_id=member.id, new_role="staff", now=NOW) == (
        "customer",
        "staff",
    )
    rows = db.query_all("SELECT actor, action, target, details FROM audit_log")
    assert [(a, act, t, json.loads(d)) for a, act, t, d in rows] == [
        (boss.id, "role_change", member.id, {"from": "customer", "to": "staff"})
    ]


def test_set_active_uses_a_boolean_parameter(db):
    make(db, "boss", "admin")
    other = make(db, "ravi")
    assert users.set_active(db, username="ravi", active=False, now=NOW, actor="cli") is True
    assert users.get_by_id(db, other.id).is_active is False
    with pytest.raises(users.RoleChangeRefused):
        users.set_active(db, username="boss", active=False, now=NOW, actor="cli")


def test_set_role_never_touches_an_admin_even_when_two_try_at_once(pg_url):
    seed = connect(pg_url)
    one, two = make(seed, "boss1", "admin"), make(seed, "boss2", "admin")
    seed.close()
    barrier = threading.Barrier(2)
    outcomes = []

    def demote(actor_id, target_id):
        database = connect(pg_url)
        try:
            actor = users.get_by_id(database, actor_id)
            barrier.wait()
            try:
                users.set_role(
                    database, actor=actor, target_id=target_id, new_role="staff", now=NOW
                )
                outcomes.append("done")
            except users.RoleChangeRefused as refusal:
                outcomes.append(refusal.code)
        finally:
            database.close()

    threads = [
        threading.Thread(target=demote, args=(one.id, two.id)),
        threading.Thread(target=demote, args=(two.id, one.id)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = connect(pg_url)
    admins = check.query_one("SELECT COUNT(*) FROM users WHERE role = 'admin'")[0]
    check.close()
    assert admins == 2 and outcomes == ["target_is_admin", "target_is_admin"]


def test_revoking_and_purging_tokens(db):
    user = make(db)
    expired = tokens.Claims(user.id, "gone", NOW, NOW - timedelta(hours=1))
    live = tokens.Claims(user.id, "keep", NOW, NOW + timedelta(hours=1))
    with db.transaction(write=True):
        tokens.revoke(db, expired, NOW)
        tokens.revoke(db, live, NOW)
        tokens.revoke(db, live, NOW)  # twice: ON CONFLICT DO NOTHING
    assert tokens.is_revoked(db, "keep") and tokens.is_revoked(db, "gone")
    with db.transaction(write=True):
        assert tokens.purge_expired_revocations(db, NOW) == 1
    assert tokens.is_revoked(db, "keep") and not tokens.is_revoked(db, "gone")


def test_the_whole_api_flow_works_on_postgres(pg_url):
    from fastapi.testclient import TestClient

    cache = CacheStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    app = create_app(make_settings(), db_factory=lambda: connect(pg_url), cache=cache)
    client = TestClient(app, raise_server_exceptions=False, client=("203.0.113.5", 50000))
    body = {"username": "asha_rao", "email": "asha@example.test", "password": PASSWORD}
    assert client.post("/auth/register", json=body).status_code == 201
    assert client.post("/auth/register", json=body).status_code == 409
    login = client.post("/auth/login", json={"username": "asha_rao", "password": PASSWORD})
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert client.get("/auth/me", headers=headers).json()["role"] == "customer"
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_every_connection_runs_at_read_committed(db):
    assert db.query_one("SHOW transaction_isolation") == ("read committed",)


def test_read_committed_is_set_even_when_the_servers_default_is_stricter(pg_url):
    stricter = f"{pg_url}%20-cdefault_transaction_isolation%3Dserializable"
    database = connect(stricter)
    try:
        assert database.query_one("SHOW default_transaction_isolation") == ("serializable",)
        assert database.query_one("SHOW transaction_isolation") == ("read committed",)
    finally:
        database.close()


def test_two_admins_deactivating_each_other_at_once_leave_one_active_admin(pg_url):
    seed = connect(pg_url)
    make(seed, "boss1", "admin")
    make(seed, "boss2", "admin")
    seed.close()
    barrier = threading.Barrier(2)
    outcomes = []

    def deactivate(username):
        database = connect(pg_url)
        try:
            barrier.wait()
            try:
                users.set_active(database, username=username, active=False, now=NOW, actor="cli")
                outcomes.append("done")
            except users.RoleChangeRefused as refusal:
                outcomes.append(refusal.code)
        finally:
            database.close()

    threads = [threading.Thread(target=deactivate, args=(name,)) for name in ("boss1", "boss2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check = connect(pg_url)
    active = check.query_one("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active")[0]
    check.close()
    assert active == 1 and sorted(outcomes) == ["done", "last_admin"]
