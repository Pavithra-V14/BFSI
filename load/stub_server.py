"""The stubbed-model API for the load test (spec 16, "stubbed model: safe; finds code, database and
host limits"). The owner starts it in their own terminal:

    uv run python load/stub_server.py [--port 8099] [--latency-ms 300]

It is the real API application (routes, security, accounts, pipeline) with everything behind it
replaced by local stand-ins: a scripted model (answers the active evaluation questions, with a
fixed delay per call), the sample corpus in an in-memory vector store, a throw-away SQLite file
and no cache. No key is read, no network is used, and it refuses to start when any store address
in the shell points at a remote host.

What it shows: how the CODE behaves under concurrency (threads, the event loop, connection use,
the password-hashing gate, locks). What it does not show: the capacity of any host, a cloud
database, or a real provider's limits. The report says so.

Two settings are loosened on purpose, and named here: the per-IP registration and login
throttles (every virtual user comes from this machine, one address).
"""

from __future__ import annotations

import argparse
import secrets
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class NotLocal(SystemExit):
    """The server refuses to start (names only)."""


def check_local(host: str, env=None) -> None:
    """Refuse a non-local bind address and any remote store address in the shell."""
    from app.isolation import remote_variables

    if host not in LOCAL_HOSTS:
        raise NotLocal("refused: the stub server listens on this machine only (127.0.0.1)")
    names = remote_variables(env)
    if names:
        raise NotLocal(
            "refused: these settings point at a remote host: "
            + ", ".join(names)
            + ". Unset them in this window and start again."
        )


def build_app(workdir: Path, latency_ms: float = 300.0):
    """(the FastAPI app, the rig that holds its stores). Nothing here touches a network."""
    from app.accounts.throttle import Throttle, ThrottleDecision
    from app.api.main import create_app
    from app.api.settings import ApiSettings
    from app.db.connection import connect
    from tests.eval_rig import EvalRig

    rig = EvalRig(workdir)
    seconds = max(latency_ms, 0.0) / 1000.0
    rig.gateway.delay.update(
        {"decide": seconds / 3, "embed": seconds / 3, "rerank": seconds / 3, "answer": seconds}
    )

    class OneAddressThrottle(Throttle):
        """Every virtual user shares this machine's address, so the per-address limits on
        registering and logging in would only ever measure themselves: they are off here."""

        def check_register(self, ip):  # type: ignore[override]
            return ThrottleDecision(True)

        def check_login(self, *args, **kwargs):  # type: ignore[override]
            return ThrottleDecision(True)

    settings = ApiSettings(
        jwt_secret=secrets.token_urlsafe(48),
        audit_hash_key=secrets.token_urlsafe(48),
    )
    url = rig.url
    app = create_app(
        settings,
        db_factory=lambda: connect(url),
        throttle=OneAddressThrottle(None, settings.audit_hash_key),
        pipeline=rig.pipeline,
    )
    return app, rig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the stubbed-model API for the load test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--latency-ms", type=float, default=300.0, help="delay of the answer call")
    args = parser.parse_args(argv)
    check_local(args.host)

    import uvicorn

    with tempfile.TemporaryDirectory(prefix="bfsi-load-") as folder:
        app, rig = build_app(Path(folder), args.latency_ms)
        print(
            f"stub server on http://{args.host}:{args.port} (scripted model, "
            f"{args.latency_ms:.0f} ms per answer, SQLite, in-memory vector store)",
            flush=True,
        )
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        finally:
            rig.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
