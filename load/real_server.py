"""The API for a REAL-provider load run. Owner-run, only after the owner's explicit yes:

    uv run python scripts/load_estimate.py --steps 10x20,20x20   # the estimate and the ticket
    # (set LOAD_STEPS and LOAD_CONFIRM to what it printed, and the provider and QDRANT settings)
    uv run python load/real_server.py [--port 8099]

It is the real API application with the real gateway (the owner's own provider keys, free-tier
quota), and everything that WRITES is local to this machine:

- accounts, sessions, messages: a throw-away SQLite file in a temporary folder, deleted when the
  server stops (never `DATABASE_URL`);
- counters, rate limits, the answer cache: an in-process Redis stand-in (never Upstash);
- the vector store: the cloud collection is only READ, through a wrapper that refuses every
  call that could write, and only a collection whose name matches `^[a-z0-9]+_eval$`.

It refuses to start when `DATABASE_URL`, `UPSTASH_REDIS_URL` or any other store address in this
window points at a remote host, when the collection is not an `_eval` one, when the host is not
loopback, and when the ticket is missing, wrong, from another day, older than 30 minutes or used.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from load import plan, seed  # noqa: E402

READ_PREFIXES = ("query", "search", "scroll", "count", "retrieve", "get_", "collection_exists")


class ReadOnlyViolation(RuntimeError):
    """Something tried to write through the read-only vector store wrapper."""


class ReadOnlyQdrant:
    """Lets the read calls of a Qdrant client through and refuses everything else (upsert,
    delete, set_payload, update_vectors, create_collection, ...). The cloud collection is only
    ever read by a load run."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or not name.startswith(READ_PREFIXES):
            if name == "close":
                return self._client.close
            raise ReadOnlyViolation(f"the load run may not call {name} on the vector store")
        return getattr(self._client, name)


def check_start(env: Any, host: str) -> None:
    """Every refusal that can be decided before anything is built."""
    if not plan.is_loopback(host):
        raise SystemExit("refused: the real server listens on this machine only (127.0.0.1)")
    problems = plan.real_isolation_problems(env)
    if not (env.get("QDRANT_URL") or "").strip():
        problems.append("QDRANT_URL is not set: the test collection is read from the vector store")
    if problems:
        raise SystemExit("refused: " + "; ".join(problems))


def build_app(env: Any, workdir: Path, users: int):
    """(app, the temporary stores). The gateway is real; everything written is local."""
    import fakeredis

    from app.accounts.budget import UsageLimits
    from app.api.main import create_app
    from app.api.settings import ApiSettings
    from app.cache.store import CacheStore
    from app.db.connection import connect
    from app.db.migrate import run as migrate
    from app.gateway import Gateway, GatewayConfig
    from app.gateway.tokens import TokenCounter
    from app.guardrails.domains import curated_domains
    from app.pipeline.run import Pipeline
    from app.pipeline.startup import MaskerHolder
    from app.pipeline.state import Deps
    from app.vector.documents import client_from_env
    from load.stub_server import one_address_throttle

    url = f"sqlite:///{(workdir / 'load.db').as_posix()}"
    migrate(url, ROOT / "migrations", log=lambda line: None)
    db = connect(url)
    try:
        names, password = seed.seed_users(db, users)
    finally:
        db.close()
    seed.write_users_file(plan.users_path(), names, password)

    settings = ApiSettings(
        jwt_secret=secrets.token_urlsafe(48), audit_hash_key=secrets.token_urlsafe(48)
    )
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    cache = CacheStore(redis)
    limits = UsageLimits(cache, settings)
    holder = MaskerHolder()
    holder.start()
    deps = Deps(
        gateway=Gateway(GatewayConfig.from_env(env)),
        qdrant=ReadOnlyQdrant(client_from_env(env)),
        cache=cache,
        masker=holder,
        counter=TokenCounter(),
        usage=limits,
        curated_domains=curated_domains(env),
        collection=plan.collection_from(env),
    )
    app = create_app(
        settings,
        db_factory=lambda: connect(url),
        cache=cache,
        throttle=one_address_throttle()(cache, settings.audit_hash_key),
        limits=limits,
        pipeline=Pipeline(deps),
        vector_client=deps.qdrant,
    )
    return app, holder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the real-provider API for a load run")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args(argv)
    env = os.environ
    check_start(env, args.host)
    try:
        steps = plan.parse_steps(env.get("LOAD_STEPS"))
    except plan.PlanError as error:
        raise SystemExit(f"refused: {error}") from None

    import uvicorn

    with tempfile.TemporaryDirectory(prefix="bfsi-load-real-") as folder:
        app, holder = build_app(env, Path(folder), plan.max_users(steps))
        try:
            ticket = plan.consume_ticket(env.get("LOAD_CONFIRM", ""), steps)
        except plan.PlanError as error:
            raise SystemExit(f"refused: {error}") from None
        plan.write_marker(ticket, args.port)
        print(plan.mode_banner("real"), flush=True)
        print(
            f"real server on http://{args.host}:{args.port} for the plan {ticket.steps} "
            f"({plan.max_users(steps)} accounts in a temporary SQLite file)",
            flush=True,
        )
        if not holder.wait(300):
            raise SystemExit("the name model did not load")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
