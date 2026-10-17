"""Pipeline smoke test with REAL services. Off by default; the owner runs it by hand.

It asks ONE question through the whole pipeline (the 17 stages) against your real Qdrant and your
real model keys, and prints names, timings and counts only: the stage, how it ended, the decider,
the model name, the provider, the key FINGERPRINT, token counts, then the kind of result, how many
citations and warnings, and the total. It never prints the question, the answer, a citation's text
or any key.

What it uses:
- your real Gateway keys, your real Qdrant (read only: it searches, it writes nothing);
- the cache only with --with-cache (otherwise none: nothing is written to your Upstash);
- a THROW-AWAY SQLite file in a temporary folder for the chat tables: your real database is never
  opened and DATABASE_URL is never read. The folder is deleted when the script ends.

Run it in your own terminal:
    uv run python scripts/pipeline_smoke.py --run
    uv run python scripts/pipeline_smoke.py --run --question "your question" --role staff
    uv run python scripts/pipeline_smoke.py --run --with-cache
    uv run python scripts/pipeline_smoke.py --run --langfuse
    uv run python scripts/pipeline_smoke.py --run --load-env
With --langfuse the trace of this one question (one observation per stage, with its input, output
and metadata: the MASKED stored question and answer, the retrieved passages, stage names, timings,
models, key fingerprints, token counts and flags; never the raw question) is sent through the
Langfuse SDK to YOUR Langfuse with your own keys, and the script prints only "SENT", or the
exception type if it failed.
It needs LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST and LANGFUSE_USER_HASH_KEY.

What it touches, in plain words:
- Your real Qdrant is READ (searches, a read-back of cited chunks, the distinct values). It never
  writes to Qdrant: no point, no payload, no collection is created or changed.
- Your real Upstash is not touched unless you add --with-cache (then it reads and may write one
  cached answer, exactly as the live service would).
- Real model providers are called with your own keys: this is the point of the script.

After the RESULT line it prints two more lines: "cited doc ids: ..." (the ids of the documents the
answer cites, never a title or text; "none" for a refusal) and "review rows: N" (how many rows the
run wrote to its throw-away review queue, read before the file is deleted: a refusal for no
reliable context writes 1).

Nothing happens without --run. By default it uses only the variables already set in your shell
(settings come from the shell, as for the API). With --load-env it also reads your .env file itself,
only the variables that .env.example lists and that this script needs, only into this process, and
never prints a value; a variable already set in your shell wins over the file. The masker (the name
model) loads first; that takes about a minute on a small machine and the script says how long it
took.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.cache.store import store_from_env  # noqa: E402
from app.db.connection import Database, connect  # noqa: E402
from app.db.migrate import run as run_migrations  # noqa: E402
from app.gateway import Gateway, GatewayConfig  # noqa: E402
from app.gateway.config import ENV_NAMES  # noqa: E402
from app.gateway.tokens import TokenCounter  # noqa: E402
from app.guardrails.domains import curated_domains  # noqa: E402
from app.pipeline import langfuse_sink  # noqa: E402
from app.pipeline.run import Pipeline  # noqa: E402
from app.pipeline.startup import MaskerHolder  # noqa: E402
from app.pipeline.state import Deps, Request  # noqa: E402
from app.pipeline.trace import safe_value  # noqa: E402
from app.vector.documents import client_from_env  # noqa: E402
from scripts.envfile import example_names, load_env_file  # noqa: E402

OWN_NAMES = {
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "UPSTASH_REDIS_URL",
    "CACHE_TTL_SECONDS",
    "COMPANY_DOMAINS",
    *langfuse_sink.ENV_NAMES,  # only used with --langfuse
    "JWT_SECRET_KEY",  # read only so that the Langfuse user hash key can be checked against them
    "AUDIT_HASH_KEY",
}
DEFAULT_QUESTION = "What is the waiting period for pre-existing conditions?"
MASKER_WAIT_SECONDS = 240.0

Setup = tuple[Deps, Database, str]


def line(name: str, status: str, **details: object) -> None:
    """One output line. A detail is printed only if it is a number, a flag or an id-like word."""
    shown = []
    for key, value in details.items():
        safe = safe_value(value)
        if safe is None and value is not None:
            safe = "<hidden>"
        shown.append(f"{key}={safe}")
    print(f"{name:<20} {status:<8}" + ("  " + " ".join(shown) if shown else ""))


def load_owner_env(
    environ: MutableMapping[str, str],
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> str:
    """Load the variables this script needs from .env into `environ`. Names only in the message."""
    path = env_path or ROOT / ".env"
    if not path.is_file():
        return "no .env file found: using the variables already set in this shell"
    allowed = example_names(example_path or ROOT / ".env.example") & (set(ENV_NAMES) | OWN_NAMES)
    names = load_env_file(path, allowed, environ)
    listed = ", ".join(names) or "none"
    return f"loaded {len(names)} variable(s) from .env: {listed} (values are never shown)"


def real_setup(
    environ: MutableMapping[str, str], workdir: Path, with_cache: bool, role: str
) -> Setup:
    """Real gateway, real Qdrant, a throw-away SQLite file, the cache only when asked for."""
    gateway = Gateway(GatewayConfig.from_env(environ))
    url = f"sqlite:///{workdir / 'smoke.db'}"
    run_migrations(url, ROOT / "migrations", log=lambda text: None)
    db = connect(url)
    user_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO users (id, username, email, password_hash, role) VALUES (?, ?, ?, ?, ?)",
        (user_id, "smoke", "smoke@example.test", "x", role),
    )
    cache = store_from_env(environ) if with_cache else None
    deps = Deps(
        gateway=gateway,
        qdrant=client_from_env(environ),
        cache=cache,
        masker=MaskerHolder(),
        counter=TokenCounter(),
        curated_domains=curated_domains(environ),
    )
    return deps, db, user_id


def report_langfuse(sink: langfuse_sink.LangfuseSink | None, off: bool) -> int:
    """One line: SENT, or the exception type that stopped it. Never a message, a key or a value."""
    if off or sink is None:
        line("langfuse", "OFF", note="all four variables are needed")
        return 1
    flushed = sink.flush(20.0)
    sink.stop()
    if sink.sent_batches >= 1 and sink.last_error_type is None:
        line("langfuse", "SENT")
        return 0
    line(
        "langfuse",
        "FAILED",
        error=sink.last_error_type or ("Timeout" if not flushed else "NotSent"),
    )
    return 1


def cited_doc_ids(citations: list) -> str:
    """The distinct document ids the answer cites, comma-separated, or "none". Ids only: never a
    title, a source name, a clause or any text. An id that does not look like an id is hidden."""
    found = {str(c.get("doc_id")) for c in citations if isinstance(c, dict) and c.get("doc_id")}
    if not found:
        return "none"
    shown = [safe_value(doc_id) or "<hidden>" for doc_id in sorted(found)]
    return ",".join(str(item) for item in shown)


def review_row_count(db: Database) -> str:
    """How many rows the run put in its throw-away review queue (a count: the rows hold the masked
    question and are deleted with the file). "unknown" if it cannot be read."""
    try:
        row = db.query_one("SELECT COUNT(*) FROM review_queue")
        return str(int(row[0])) if row else "0"
    except Exception:
        return "unknown"


async def smoke(deps: Deps, db: Database, user_id: str, role: str, question: str) -> int:
    started = time.perf_counter()
    if not deps.masker.ready:
        deps.masker.start()
        ready = await asyncio.to_thread(deps.masker.wait, MASKER_WAIT_SECONDS)
        line("masker", "OK" if ready else "FAILED", seconds=round(time.perf_counter() - started, 1))
        if not ready:
            return 1
    spans: list = []
    deps.on_span = spans.append
    events: list[str] = []
    deps.on_event = lambda name, fields: events.append(name)
    pipeline = Pipeline(deps)
    index = await asyncio.to_thread(pipeline.check_index)  # counts only: Phase 9 start-up check
    if index is None:
        print("sparse vectors out of date: unknown")
    else:
        print(f"sparse vectors out of date: {index.out_of_date} of {index.total}")
        if not index.ok:
            print(
                "warning: some sparse vectors were made by another tokenizer version; run "
                "`uv run python -m app.ingest refresh-sparse` (no embedding call)"
            )
    result = await pipeline.answer(Request(user_id=user_id, role=role, query=question), db)
    for span in spans:
        line(
            span.stage,
            span.status.upper(),
            ms=span.duration_ms,
            decider=span.decider,
            model=span.model,
            provider=span.provider,
            key=span.key_fingerprint,
            tokens_in=span.input_tokens,
            tokens_out=span.output_tokens,
            error=span.error_code,
        )
    line(
        "RESULT",
        result.kind.upper(),
        reason=result.reason,
        citations=len(result.citations),
        warnings=len(result.warnings),
        degraded=",".join(result.degraded) or None,
        cached=result.cached,
        confidence=result.confidence,
        tokens=result.tokens,
        seconds=round(time.perf_counter() - started, 1),
    )
    print(f"cited doc ids: {cited_doc_ids(result.citations)}")
    print(f"review rows: {review_row_count(db)}")
    if events:
        line("events", "SEEN", names=",".join(sorted(set(events))))
    return 0 if result.kind == "answer" else 1


def main(
    argv: list[str],
    *,
    env_path: Path | None = None,
    example_path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    make_setup: Callable[[MutableMapping[str, str], Path, bool, str], Setup] | None = None,
    make_sink: Callable[..., langfuse_sink.LangfuseSink | None] | None = None,
) -> int:
    if "--run" not in argv:
        print(__doc__)
        print("Nothing was called and no file was read. Add --run to make the real calls.")
        return 0
    parser = argparse.ArgumentParser(prog="pipeline_smoke.py")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--role", default="customer", choices=("customer", "staff", "admin"))
    parser.add_argument("--with-cache", action="store_true")
    parser.add_argument("--langfuse", action="store_true")
    parser.add_argument("--load-env", action="store_true")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    if args.load_env:  # opt-in: otherwise only what is already set in the shell is used
        print(load_owner_env(environ, env_path, example_path))
    else:
        print("using the variables already set in this shell (add --load-env to read your .env)")
    print("NOT PRINTED: the question, the answer, any citation text, any key or value.")
    workdir = tempfile.TemporaryDirectory(prefix="pipeline-smoke-")
    db = None
    try:
        deps, db, user_id = (make_setup or real_setup)(
            environ, Path(workdir.name), args.with_cache, args.role
        )
        sink = None
        refused = False  # the configuration was refused: already reported, nothing to send
        if args.langfuse:
            try:
                sink = (make_sink or langfuse_sink.sink_from_env)(environ)
            except langfuse_sink.SinkConfigError as error:
                line("langfuse", "FAILED", error=type(error).__name__)
                refused = True
            deps.langfuse = sink
        code = asyncio.run(smoke(deps, db, user_id, args.role, args.question))
        if args.langfuse and not refused:
            code = max(code, report_langfuse(sink, sink is None))
        elif refused:
            code = 1
    finally:
        if db is not None:
            db.close()
        try:
            workdir.cleanup()
        except OSError:  # Windows may hold the file a moment longer: the folder is only temporary
            pass
    print("done:", "the pipeline answered" if code == 0 else "the pipeline did not answer")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
