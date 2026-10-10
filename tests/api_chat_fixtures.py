"""A chat-capable API test environment: the pipeline rig (fake gateway, in-memory Qdrant, fakeredis,
SQLite) behind the real FastAPI app. Everything is local; no network, no real key."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.accounts.budget import UsageLimits
from app.api.main import create_app
from app.db.connection import connect
from tests.api_fixtures import PASSWORD, SOCKET_ADDRESS, Env, make_settings, now
from tests.pipeline_rig import PipelineRig

QUESTION = "What is the waiting period for illness claims?"


class ChatEnv(Env):
    """`Env` (users, tokens, rows) over the pipeline rig's own database."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        documents=None,
        gateway=None,
        stage: str | None = "open",
        on_render: bool = False,
        pipeline: bool = True,
        rollout_cache_seconds: float | None = 0.0,
        **settings,
    ):
        self.rig = PipelineRig(tmp_path, documents=documents, gateway=gateway)
        self.url = self.rig.rig.url
        self.path = Path(self.url[len("sqlite:///") :])
        self.settings = make_settings(**settings)
        self.redis = self.rig.rig.redis
        self.cache = self.rig.rig.cache
        self.gateway = self.rig.gateway
        self.limits = UsageLimits(self.cache, self.settings)
        self.rig.deps.usage = self.limits
        self.app = create_app(
            self.settings,
            db_factory=lambda: connect(self.url),
            cache=self.cache,
            gateway=self.gateway,
            vector_client=self.rig.deps.qdrant,
            limits=self.limits,
            pipeline=self.rig.pipeline if pipeline else None,
            on_render=on_render,
            rollout_cache_seconds=rollout_cache_seconds,
        )
        self.client = TestClient(
            self.app, raise_server_exceptions=False, client=(SOCKET_ADDRESS, 50000)
        )
        if stage is not None:
            self.set_stage_in_database(stage)

    # ---- helpers -------------------------------------------------------------------------------
    def close(self) -> None:
        self.rig.close()

    def execute(self, sql: str, params: tuple = ()) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def set_stage_in_database(self, stage: str) -> None:
        self.execute("DELETE FROM app_settings WHERE key = 'rollout_stage'")
        self.execute("INSERT INTO app_settings (key, value) VALUES ('rollout_stage', ?)", (stage,))

    def chat(self, token: str, message: str = QUESTION, session_id: str | None = None, **extra):
        body = {"message": message, **extra}
        if session_id is not None:
            body["session_id"] = session_id
        return self.client.post("/chat", json=body, headers=self.bearer(token))

    def make_user(self, username="asha_rao", role="customer", active=True):
        """Create an account straight in the database (registration is throttled to 5 an hour per
        address, and these tests need more accounts than that)."""
        from app.accounts import passwords, users

        db = connect(self.url)
        try:
            with db.transaction(write=True):
                user = users.create_customer(
                    db,
                    username=username,
                    email=f"{username}@example.test",
                    password_hash=passwords.hash_password(PASSWORD),
                    now=now(),
                )
                db.execute(
                    "UPDATE users SET role = ?, is_active = ? WHERE id = ?",
                    (role, active, user.id),
                )
        finally:
            db.close()
        return user.id

    def user_token(self, username: str, role: str = "customer") -> tuple[str, str]:
        user_id = self.make_user(username, role=role)
        return user_id, self.token(username)
