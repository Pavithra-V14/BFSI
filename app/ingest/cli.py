"""The ingestion command line (spec 6). Run by the owner, in his own terminal, on the admin machine:

    uv run python -m app.ingest ingest PDF --doc-id SLUG --doc-type policy --access-role customer
    uv run python -m app.ingest ingest PDF ... --dry-run     (no service is called)
    uv run python -m app.ingest status --doc-id SLUG         (counts only, changes nothing)
    uv run python -m app.ingest review entities --doc-id SLUG   (approve pending allow-list terms)
    uv run python -m app.ingest review chunks --doc-id SLUG     (chunks held back by the scan)
    uv run python -m app.ingest delete --doc-id SLUG

Settings come from your shell: DATABASE_URL, QDRANT_URL, QDRANT_API_KEY, UPSTASH_REDIS_URL, the
gateway's key variables, and optionally COMPANY_NAMES and COMPANY_DOMAINS (comma-separated). Add
`--load-env` to any command to fill the variables you have not set from the project's env file
(only the names this tool needs, never printed). Output is names and counts only: never document
text (the dry run lists the header/footer lines it removes, shortened), a key or a URL. `--doc-id`
is a slug you choose and keep; it is never taken from the path.

Exit codes: 0 done; 1 an error, or "ingest incomplete" (a cache step failed: run it again); 2 a
page failed, so the document was not activated (run it again once the page can be parsed); 130
interrupted. Every failure is safe to repeat: a document is never left half-activated.
"""

from __future__ import annotations

import argparse
import getpass
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.gateway.embeddings import running_on_render
from app.ingest import paths
from app.ingest.chunker import chunk_document
from app.ingest.delete import delete_document
from app.ingest.entities import AUTO_KINDS, Ner, extract_candidates, presidio_ner, variants_of
from app.ingest.envload import load_env_for_ingest
from app.ingest.index import IngestContext, ingest_document
from app.ingest.lock import DocumentBusy
from app.ingest.model import (
    ACCESS_ROLES,
    DOC_TYPES,
    DocRequest,
    IngestError,
    ParsedDocument,
    check_choice,
    check_doc_id,
    refuse_customer_document,
)
from app.ingest.originals import keep_original
from app.ingest.queue import flag_failed_pages, resolve_failed_pages
from app.ingest.review import review_chunks, review_entities
from app.ingest.sparse_refresh import refresh_sparse_vectors
from app.ingest.status import describe, document_status
from app.isolation import is_local_url
from app.store_errors import StoreRejected, StoreUnavailable
from app.vector.documents import COLLECTION

ORIGINALS_DIR = paths.ORIGINALS_DIR
LOCKS_DIR = paths.LOCKS_DIR
PENDING_DIR = paths.PENDING_DIR
EMBED_BATCH = 16  # the gateway embeds this many texts per call (app.gateway.embeddings)

# What a command needs. "stores" is the database and Qdrant, "cache" adds Upstash, "models" adds
# the gateway (embeddings, captions, labels) and the entity model.
NEEDS = {"stores": 0, "cache": 1, "models": 2}
STORE_SETTINGS = ("DATABASE_URL", "QDRANT_URL")
CACHE_SETTINGS = ("UPSTASH_REDIS_URL",)

Parse = Callable[[Path], ParsedDocument]


class _NotAvailable:
    """Stands in for a service the command does not need: using it is a clear error, not a crash."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __getattr__(self, name: str):
        raise IngestError(f"{self._what} is not set up for this command")

    def __call__(self, *args, **kwargs):
        raise IngestError(f"{self._what} is not set up for this command")


class _EvalCache:
    """The cache of an ingest into the EVALUATION collection: there is none. The production cache
    (Upstash) is never told about a test collection, so the corpus version and the cached answers
    of the live service are not touched. Every staleness step succeeds and does nothing."""

    def preflight(self) -> bool:
        return True

    def bump_corpus_version(self) -> int:
        return 0

    def invalidate_chunk(self, chunk_id: str) -> int:
        return 0

    def invalidate_doc(self, doc_id: str) -> int:
        return 0


@dataclass
class Services:
    ctx: IngestContext
    close: Callable[[], None] = lambda: None


def os_actor() -> str:
    """The audit "who" for a command-line run: "cli:<operating-system login name>" (until the
    admin accounts of Phase 10 can be named)."""
    try:
        name = getpass.getuser()
    except Exception:
        name = "unknown"
    return "cli:" + (re.sub(r"[^A-Za-z0-9._-]", "_", name)[:40] or "unknown")


def parse_with_unstructured(path: Path) -> ParsedDocument:
    from app.ingest import unstructured_backend as backend
    from app.ingest.parse import parse_pdf

    return parse_pdf(path, page_count=backend.count_pages(path), parse_page=backend.parse_page)


def eval_collection(env: Mapping[str, str]) -> str | None:
    """The evaluation collection this run targets, or None for the production one. The name is
    checked: it must be an evaluation collection, never `documents`."""
    from app.eval.constants import check_eval_collection

    name = (env.get("INGEST_COLLECTION") or "").strip()
    if not name or name == COLLECTION:
        return None
    return check_eval_collection(name)


def missing_settings(env: Mapping[str, str], needs: str) -> list[str]:
    """The NAMES of the settings a command needs and does not have (never their values)."""
    level = NEEDS[needs]
    required = [*STORE_SETTINGS]
    if level >= 1 and eval_collection(env) is None:
        required += [*CACHE_SETTINGS]
    missing = [name for name in required if not (env.get(name) or "").strip()]
    if level >= 2:
        if not (env.get("GEMINI_API_KEYS") or "").strip():
            missing.append("GEMINI_API_KEYS (image captions)")
        if not any(
            (env.get(name) or "").strip()
            for name in ("GEMINI_EMBEDDING_KEY_A", "GEMINI_EMBEDDING_KEY_B")
        ):
            missing.append("GEMINI_EMBEDDING_KEY_A (or _B)")
    return missing


def build_services(env: Mapping[str, str], needs: str = "models") -> Services:
    """The real services, from the shell. Only the owner's own run reaches this."""
    from app.cache.store import store_from_env
    from app.db.connection import connect
    from app.vector.documents import client_from_env

    missing = missing_settings(env, needs)
    if missing:
        raise IngestError(
            "these settings are not set: "
            + ", ".join(missing)
            + " (set them in this terminal, or add --load-env to read them from the env file)"
        )
    level = NEEDS[needs]
    target = eval_collection(env)
    database_url = (env.get("DATABASE_URL") or "").strip()
    if target is not None and not is_local_url(database_url):
        # the allow-list and audit rows of a test ingest must never reach the main database
        raise IngestError(
            "an ingest into the evaluation collection needs DATABASE_URL to be a local SQLite "
            "file (never the main database)"
        )
    db = connect(database_url)
    closers: list[Callable[[], None]] = [db.close]
    if target is not None:
        cache: Any = _EvalCache()
    else:
        cache = store_from_env(env) if level >= 1 else _NotAvailable("the cache")
    embed = _NotAvailable("embedding")
    count = len
    enrich = None
    if level >= 2:
        from app.gateway import Gateway, GatewayConfig
        from app.gateway.tokens import TokenCounter
        from app.ingest.cache import DiskCache
        from app.ingest.embedding import make_embedder
        from app.ingest.enrich import EnrichTools, make_enricher
        from app.ingest.entities import (
            DEFAULT_COMPANY_DOMAINS,
            DEFAULT_COMPANY_NAMES,
        )
        from app.ingest.llm import LlmTools
        from app.ingest.runner import LoopRunner

        runner = LoopRunner()
        closers.append(runner.close)
        gateway = Gateway(GatewayConfig.from_env(env))
        llm = LlmTools(gateway, runner)

        def listed(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
            values = tuple(v.strip() for v in (env.get(name) or "").split(",") if v.strip())
            return values or default

        embed = make_embedder(gateway, runner)  # development vectors are never allowed
        count = TokenCounter().count_text
        enrich = make_enricher(
            EnrichTools(
                caption=llm.caption,
                summarise=llm.summarise,
                label=llm.label,
                classify=llm.classify,
                ner=presidio_ner(),
                db=db,
                cache=DiskCache(paths.CACHE_DIR),
                company_names=listed("COMPANY_NAMES", DEFAULT_COMPANY_NAMES),
                company_domains=listed("COMPANY_DOMAINS", DEFAULT_COMPANY_DOMAINS),
            )
        )
    ctx = IngestContext(
        qdrant=client_from_env(env),
        cache=cache,
        db=db,
        embed=embed,
        count=count,
        lock_dir=LOCKS_DIR if target is None else paths.ROOT / "eval_data" / "ingest" / "locks",
        marker_dir=PENDING_DIR
        if target is None
        else paths.ROOT / "eval_data" / "ingest" / "pending",
        enrich=enrich,
        collection=target or COLLECTION,
        actor=os_actor(),
    )

    def close() -> None:
        for closer in closers:
            closer()

    return Services(ctx, close)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _estimate(
    parsed: ParsedDocument,
    request: DocRequest,
    count: Callable[[str], int],
    ner_factory: Callable[[], Ner] | None,
    out: Callable[[str], None],
) -> None:
    """What a real run on a fresh document would call. Nothing here reaches a service: the entity
    count runs the local NER model on the parsed chunks."""
    chunked = chunk_document(parsed, count)
    texts = len(chunked.chunks)
    images = len(chunked.images)
    tables = sum(1 for c in chunked.chunks if c.kind == "table")
    out(f"pages parsed: {parsed.page_count - len(parsed.failed_pages)} of {parsed.page_count}")
    for failed in parsed.failed_pages:
        out(f"  page {failed.page} failed to parse ({failed.reason})")
    out(f"running headers and footers removed: {parsed.furniture_dropped}")
    for page, line in parsed.furniture:  # the owner's check that no real text went with them
        shown = " ".join(line.split())
        out(f"  removed, page {page}: {shown[:80]}{'...' if len(shown) > 80 else ''}")
    for warning in parsed.warnings:
        out(f"warning: {warning}")
    out(f"chunks: {texts} (text {texts - tables}, table {tables}); images: {images}")
    out(
        f"if every chunk is new: embedding texts {texts + images}, "
        f"embedding calls {math.ceil((texts + images) / EMBED_BATCH)}"
    )
    questions = "not counted (no NER available)"
    if ner_factory is not None:
        candidates = extract_candidates(
            [(c.text, c.heading_path) for c in chunked.chunks], request, ner_factory()
        )
        asked = [c for c in candidates if c.kind not in AUTO_KINDS and c.kind != "contact"]
        waiting = sum(1 for c in candidates if c.kind == "contact")
        questions = (
            f"{len(asked)} of {len(candidates)} candidates "
            f"({waiting} contact(s) go straight to review; "
            f"{sum(len(variants_of(c)) for c in candidates)} allow-list rows)"
        )
    out(f"image caption calls: {images}; table summary calls: {tables}")
    out(f"clause label calls: {texts + images}; entity questions: {questions}")
    out("no service was called (dry run)")


def _ingest(args, env, services_factory, parse: Parse, ner_factory, out, err) -> int:
    check_doc_id(args.doc_id)
    check_choice(args.doc_type, DOC_TYPES, "doc-type")
    check_choice(args.access_role, ACCESS_ROLES, "access-role")
    refuse_customer_document(args.doc_type)
    path = Path(args.pdf)
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise IngestError("the file must be an existing .pdf")
    request = DocRequest(
        doc_id=args.doc_id,
        source=path.name,
        title=args.title,
        doc_type=args.doc_type,
        access_role=args.access_role,
        product_line=args.product_line,
        effective_date=args.effective_date or _today(),
    )
    if args.dry_run:
        from app.gateway.tokens import TokenCounter

        _estimate(parse(path), request, TokenCounter().count_text, ner_factory, out)
        return 0
    out(
        "note: clause text, table text and images of this document are sent to model "
        "providers; use documents without customer data only"
    )
    services = services_factory(env, "models")
    try:
        keep_original(path, ORIGINALS_DIR, args.doc_id)
        parsed = parse(path)
        for warning in parsed.warnings:
            out(f"warning: {warning}")
        result = ingest_document(services.ctx, request, parsed)
        now = datetime.now(UTC)
        if result.status == "failed_pages":
            flag_failed_pages(services.ctx.db, result.doc_id, result.failed_pages, now)
        else:
            resolve_failed_pages(services.ctx.db, result.doc_id, now)
    finally:
        services.close()
    if result.status == "failed_pages":
        out(f"document '{result.doc_id}' was NOT activated; the old version (if any) stays active")
        for failed in result.failed_pages:
            out(f"  page {failed.page} failed ({failed.reason})")
        out("each failed page is in the review queue; run the same command again to retry")
        return 2
    out(
        f"{result.doc_id}: new {result.new}, changed {result.changed}, "
        f"unchanged {result.unchanged}, removed {result.removed}, version {result.doc_version}"
    )
    if result.sparse_refreshed:
        out(
            f"sparse vectors rewritten for {result.sparse_refreshed} unchanged chunk(s) "
            "(no embedding)"
        )
    if result.status == "unchanged":
        out("nothing changed: no write, no version bump, no audit row")
    if result.status == "finished":
        out("finished the cache steps an earlier run had left undone")
    if result.images_skipped:
        out(f"{result.images_skipped} image(s) were not captioned in this run")
    if result.incomplete:
        for reason in result.incomplete:
            err(f"  {reason}")
        err("ingest incomplete: run the same command again to finish it")
        return 1
    return 0


def _review(args, env, services_factory, ask, out, err) -> int:
    check_doc_id(args.doc_id)
    services = services_factory(env, "stores" if args.what == "entities" else "cache")
    try:
        if args.what == "entities":
            result = review_entities(services.ctx, args.doc_id, ask, out)
            out(
                f"approved {result.approved}, rejected {result.rejected}, "
                f"left pending {result.skipped}"
            )
            return 0
        held = review_chunks(services.ctx, args.doc_id, ask, out)
    finally:
        services.close()
    out(f"approved {held.approved}, rejected {held.rejected}, left held back {held.skipped}")
    if held.incomplete:
        for reason in held.incomplete:
            err(f"  {reason}")
        err("review incomplete: a cache step failed; run the review again to finish it")
        return 1
    return 0


def _delete(args, env, services_factory, confirm, out, err) -> int:
    check_doc_id(args.doc_id)
    out(
        f"This removes every vector and allow-list entry of '{args.doc_id}' "
        "and flags chats that cited it."
    )
    if confirm(f"Type the document id ({args.doc_id}) to confirm: ").strip() != args.doc_id:
        out("not confirmed; nothing was deleted")
        return 1
    services = services_factory(env, "cache")
    try:
        result = delete_document(services.ctx, args.doc_id)
    finally:
        services.close()
    out(
        f"{result.doc_id}: removed {result.chunks} chunk(s), {result.allowlist_entries} allow-list "
        f"entr(ies); {result.transcripts_flagged} chat message(s) flagged 'source removed'"
    )
    if result.incomplete:
        for reason in result.incomplete:
            err(f"  {reason}")
        err("delete incomplete: run the same command again to finish it")
        return 1
    return 0


def _status(args, env, services_factory, out) -> int:
    check_doc_id(args.doc_id)
    services = services_factory(env, "stores")
    try:
        status = document_status(services.ctx, args.doc_id)
    finally:
        services.close()
    for line in describe(status):
        out(line)
    return 0


def _refresh_sparse(args, env, services_factory, out, err) -> int:
    if args.doc_id:
        check_doc_id(args.doc_id)
    services = services_factory(env, "cache")
    try:
        result = refresh_sparse_vectors(services.ctx, args.doc_id)
    finally:
        services.close()
    out(
        f"sparse vectors refreshed: {result.chunks_refreshed} chunk(s) in {result.documents} "
        f"document(s) (checked {result.chunks_checked}); no embedding call was made"
    )
    if result.chunks_without_text:
        out(f"{result.chunks_without_text} chunk(s) have no stored text and were left alone")
    if result.redone:
        out(
            f"finished the corpus-version bump of an earlier refresh "
            f"({len(result.redone)} marker(s))"
        )
    if result.foreign:
        out(
            f"{len(result.foreign)} document(s) have an unfinished ingest, delete or review "
            "waiting: run that command again to finish it (its marker was left alone)"
        )
    if not result.chunks_refreshed and not result.redone:
        out("nothing was out of date: no write, no version bump, no audit row")
    if result.incomplete:
        for reason in result.incomplete:
            err(f"  {reason}")
        err("refresh incomplete: run the same command again to finish it")
        return 1
    return 0


def describe_failure(error: Exception) -> str:
    """One safe line for an unexpected failure: class names, reason codes and setting names only,
    never a key, a URL, a message from a provider or any document text."""
    if isinstance(error, DocumentBusy | IngestError):
        return f"error: {error}"
    if isinstance(error, StoreUnavailable):
        return f"error: a store could not be reached ({error.cause}); safe to run again"
    if isinstance(error, StoreRejected):
        return f"error: a store rejected the request ({error.reason}); check the key or setting"
    if isinstance(error, ImportError):
        return (
            f"error: a package is missing ({getattr(error, 'name', None) or 'unknown'}): run "
            "uv pip install -r requirements-ingest.txt"
        )
    from app.cache.store import CacheConfigError
    from app.gateway.errors import GatewayError
    from app.gateway.keys import ConfigError
    from app.vector.documents import VectorConfigError

    if isinstance(error, ConfigError | CacheConfigError | VectorConfigError):
        return f"error: a setting is wrong: {error}"  # these messages name the variable only
    if isinstance(error, GatewayError):
        return (
            f"error: the model providers could not answer ({type(error).__name__}, "
            f"{error.reason}); free-tier limits can cause this: safe to run again"
        )
    return f"error: the run stopped ({type(error).__name__}); no key, address or text is shown"


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    services_factory: Callable[[Mapping[str, str], str], Services] = build_services,
    parse: Parse = parse_with_unstructured,
    ner_factory: Callable[[], Ner] | None = presidio_ner,
    confirm: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda text: print(text, file=sys.stderr),
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--collection",
        help="evaluation only: the test collection to use (for example documents_eval). The "
        "default is the production collection. Needs DATABASE_URL to be a local SQLite file.",
    )
    common.add_argument(
        "--load-env",
        action="store_true",
        help="fill settings you have not set from the project's env file (off by default)",
    )
    parser = argparse.ArgumentParser(prog="python -m app.ingest", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser(
        "ingest", parents=[common], help="parse, chunk, embed and index one PDF"
    )
    ingest.add_argument("pdf")
    ingest.add_argument("--doc-id", required=True)
    ingest.add_argument("--doc-type", required=True, choices=DOC_TYPES)
    ingest.add_argument("--access-role", required=True, choices=ACCESS_ROLES)
    ingest.add_argument("--product-line")
    ingest.add_argument("--title")
    ingest.add_argument("--effective-date", help="YYYY-MM-DD; default today (UTC)")
    ingest.add_argument("--dry-run", action="store_true", help="parse and count; call no service")
    review = commands.add_parser(
        "review", parents=[common], help="approve or reject held-back items of a document"
    )
    review.add_argument("what", choices=["entities", "chunks"])
    review.add_argument("--doc-id", required=True)
    delete = commands.add_parser(
        "delete", parents=[common], help="remove a document everywhere (asks to confirm)"
    )
    delete.add_argument("--doc-id", required=True)
    status = commands.add_parser(
        "status", parents=[common], help="counts for one document (read-only, no text)"
    )
    status.add_argument("--doc-id", required=True)
    refresh = commands.add_parser(
        "refresh-sparse",
        parents=[common],
        help="rewrite out-of-date sparse vectors from the stored text (no PDF, no embedding)",
    )
    refresh.add_argument("--doc-id", help="only this document (default: every out-of-date chunk)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    if getattr(args, "collection", None):
        env = {**env, "INGEST_COLLECTION": args.collection}
    try:
        if running_on_render(env):
            raise IngestError("ingestion runs only on the admin machine, never on Render")
        if args.load_env:
            out(load_env_for_ingest(env, env_path, example_path))  # type: ignore[arg-type]
        if args.command == "ingest":
            return _ingest(args, env, services_factory, parse, ner_factory, out, err)
        if args.command == "review":
            return _review(args, env, services_factory, confirm, out, err)
        if args.command == "status":
            return _status(args, env, services_factory, out)
        if args.command == "refresh-sparse":
            return _refresh_sparse(args, env, services_factory, out, err)
        return _delete(args, env, services_factory, confirm, out, err)
    except KeyboardInterrupt:
        err("interrupted: no document is left half-activated; run the same command again")
        return 130
    except Exception as error:
        err(describe_failure(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
