"""Shared setup for the API and account tests. Everything is local: a SQLite file with foreign
keys on, an in-process fake Redis, a fake gateway, and FastAPI's in-process test client. No
network, no real key, no real service."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.api.settings import ApiSettings
from app.cache.store import CacheStore
from app.db.connection import connect
from app.db.migrate import run

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
PASSWORD = "correct horse battery staple"
SOCKET_ADDRESS = "203.0.113.5"

# Fake secrets, built from plain words at run time (never typed out as one long string): they
# satisfy the rules (43 or more characters, many different characters, two different values).
_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike".split()
JWT_SECRET = "-".join(["jwt"] + _WORDS[:9])
AUDIT_KEY = "-".join(["audit"] + _WORDS[3:12])
OTHER_SECRET = "-".join(["other"] + _WORDS[2:11])  # a third key, for "signed by someone else"


def make_settings(**overrides) -> ApiSettings:
    values = {"jwt_secret": JWT_SECRET, "audit_hash_key": AUDIT_KEY}
    values.update(overrides)
    return ApiSettings(**values)


class FakeGateway:
    def __init__(self, report=None):
        self.report = report if report is not None else {}

    def health(self):
        return self.report


class Env:
    """Everything one test needs: the app, a client, the database file, the cache."""

    def __init__(self, tmp_path, **settings):
        self.path = tmp_path / "api.db"
        self.url = f"sqlite:///{self.path}"
        run(self.url, MIGRATIONS, log=lambda line: None)
        self.settings = make_settings(**settings)
        self.server = fakeredis.FakeServer()
        self.redis = fakeredis.FakeRedis(server=self.server, decode_responses=True)
        self.cache = CacheStore(self.redis)
        self.gateway = FakeGateway()
        self.app = create_app(
            self.settings,
            db_factory=lambda: connect(self.url),
            cache=self.cache,
            gateway=self.gateway,
        )
        self.client = TestClient(
            self.app, raise_server_exceptions=False, client=(SOCKET_ADDRESS, 50000)
        )

    def db(self):
        return connect(self.url)

    def rows(self, sql, params=()):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    @staticmethod
    def body(username="asha_rao", email=None, password=PASSWORD, **extra):
        """A registration body, with any extra field added (a role, for instance)."""
        email = f"{username}@example.test" if email is None else email
        return {"username": username, "email": email, "password": password, **extra}

    def register(self, username="asha_rao", email=None, password=PASSWORD, client=None):
        email = f"{username}@example.test" if email is None else email
        return (client or self.client).post(
            "/auth/register", json={"username": username, "email": email, "password": password}
        )

    def login(self, username="asha_rao", password=PASSWORD, client=None, headers=None):
        return (client or self.client).post(
            "/auth/login", json={"username": username, "password": password}, headers=headers
        )

    def token(self, username="asha_rao", password=PASSWORD):
        response = self.login(username, password)
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    def make_user(self, username="asha_rao", role="customer", active=True):
        """Register through the API, then set the role and flag directly in the database."""
        response = self.register(username)
        assert response.status_code == 201, response.text
        user_id = response.json()["id"]
        connection = sqlite3.connect(self.path)
        connection.execute(
            "UPDATE users SET role = ?, is_active = ? WHERE id = ?", (role, active, user_id)
        )
        connection.commit()
        connection.close()
        return user_id

    def admin_token(self, username="boss"):
        self.make_user(username, role="admin")
        return self.token(username)


def now() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())
