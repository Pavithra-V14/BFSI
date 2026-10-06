# BFSI RAG Assistant

A read-only question-answering assistant over a company's banking, financial
services and insurance (BFSI) policy and regulatory documents. Every answer is
grounded in the documents and cites clauses. Customers, staff and admins see
different documents.

Current phase: Phase 4 built, awaiting the owner's approval (next is Phase 5)

## Where things are
- `spec/` : the full specification (`build.md`), two diagrams and three
  sample company PDFs. This is the source of truth.
- `CLAUDE.md` : the rules every Claude Code session reads.
- `.claude/` : Claude Code settings, hook scripts and any approved skills or
  agents.
- `app/` : application code. Phase 1 holds the isolation guard and the migration runner.
  Phase 2 adds the gateway in `app/gateway/`: every model, embedding and rerank call.
  Phase 3 adds the data layer: `app/vector/` (the Qdrant `documents` collection),
  `app/cache/` (the Upstash cache primitives) and `app/store_errors.py` ("temporarily unavailable").
  Phase 4 adds accounts and the API: `app/accounts/` (passwords, login tokens, users, throttling,
  budgets, audit rows, the owner's `admin_cli`) and `app/api/` (the FastAPI app and its security layer).
- `scripts/` : helper scripts you run by hand, such as the gateway smoke test (it makes real
  provider calls, so it is off unless you add `--run`).
- `tests/` : automated tests. They only use SQLite and local resources.
- `migrations/` : numbered SQL files for the database (Phase 3 holds the full schema).
- `.github/workflows/ci.yml` : the checks that run on GitHub.
- `requirements*.txt` : the four dependency files. `.env.example` : the settings template.
- More folders are added phase by phase.

## How to work with Claude Code in this project
1. Open this folder in VS Code.
2. Start Claude Code. It reads `CLAUDE.md` automatically.
3. Say "begin Phase N" (or run `/build-phase N`) when you are ready.
4. Claude Code follows the build order in `spec/build.md` section 21.
5. It stops for your approval after each phase. It never starts the next
   phase on its own.

Changes to `spec/` need the owner's approval. Items tagged [P] (proposed) and
[T] (to be decided) in the spec are the ones to confirm with the owner.

## Local setup
1. Create the environment once: `uv venv --python 3.12`
2. Install runtime and development packages:
   `uv pip install -r requirements.txt -r requirements-dev.txt`
3. Run the tests: `uv run pytest`
4. Check the code: `uv run ruff check .` and `uv run ruff format --check .`

The database is a cloud Postgres (Neon or Supabase). Nothing is installed on
your machine, and local tests use SQLite.

Ingestion, entity extraction, evaluation and load tests run on the admin
machine only. The hosted service answers queries only.

## Secrets
- Copy `.env.example` to `.env` and fill in your own keys. `.env.example`
  is created in Phase 1 and contains variable names and comments only.
- Never commit `.env`. Never paste a key into a chat, a ticket or a log.
- Evaluation keys stay on the admin machine and are never put on the host.

## First run on the real services (owner only, in your own PowerShell)
Claude never runs these. Settings are read from the shell, never from a file, so each
`$env:NAME = ...` below lasts only for that terminal window. Use one window for all steps.
Keep real values in a password manager; never paste one into a chat, a file or a ticket.

**1. Make the two keys.** They must be different, random and at least 32 characters. These
commands put each key straight into the shell without printing it:

    $env:JWT_SECRET_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
    $env:AUDIT_HASH_KEY = (uv run python -c "import secrets; print(secrets.token_urlsafe(32))")

Copy them into your password manager before closing the window (type `$env:JWT_SECRET_KEY` to
show one on your own screen). Render later needs the same two values. If you change
`AUDIT_HASH_KEY`, old audit rows can no longer be matched to usernames, so keep it.
Needs: nothing else.

**2. Apply migration 0004** (adds `users.is_active`). Use the database's direct (non-pooled) URL
with `sslmode=require`:

    $env:DATABASE_URL = "<direct URL from Neon, ends with ?sslmode=require>"
    uv run python -m app.db.migrate --dry-run   # lists the pending files, changes nothing
    uv run python -m app.db.migrate             # applies them
    uv run python -m app.db.migrate --dry-run   # again: it should list nothing pending

Needs: `DATABASE_URL` only. It is safe to run twice; applied files are skipped.

**3. Create the first admin.** It asks for the password with a hidden prompt:

    uv run python -m app.accounts.admin_cli create-admin --username NAME --email ADDRESS

Needs: `DATABASE_URL` only (same window as step 2). Only this command can make an admin; the
API cannot. `set-active --username NAME --active no` turns an account off.

**4. Run the API locally:**

    $env:UPSTASH_REDIS_URL = "<rediss:// URL from Upstash>"
    uv run uvicorn app.api.main:create_app_from_env --factory

Needs: `DATABASE_URL`, `UPSTASH_REDIS_URL`, `JWT_SECRET_KEY`, `AUDIT_HASH_KEY`. It refuses to
start if any is missing, short or a placeholder. Optional, with defaults: `JWT_EXPIRE_MINUTES`,
`TOKEN_BUDGET_CUSTOMER`, `TOKEN_BUDGET_STAFF`, `PASSWORD_HASH_CONCURRENCY` (2),
`CORS_ALLOWED_ORIGIN` (none). It serves on http://127.0.0.1:8000; check `/live`. Stop with Ctrl+C.
Do not trust `X-Forwarded-For` or deploy behind a proxy until Phase 15 sets the proxy options.

## Rules in plain words
- Tests and evaluations never touch production data.
- Ask the owner before any load test or evaluation that uses real provider
  keys, because it can rate-limit every model.
- Quality checks raise alerts. They never block a release by themselves.
- No real customer data until paid or zero-retention plans are in place.

## More documents (created later)
- `DEPLOY.md` : click-by-click deployment steps (Phase 15).
- Runbooks : what to do when keys fail, a wrong answer is reported, a
  document is withdrawn, PII leaks, a key leaks, or a deploy goes bad
  (Phase 15).
