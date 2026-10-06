# BFSI RAG Assistant

A read-only question-answering assistant over a company's banking, financial
services and insurance (BFSI) policy and regulatory documents. Every answer is
grounded in the documents and cites clauses. Customers, staff and admins see
different documents.

Current phase: Phase 3 built, awaiting the owner's approval (next is Phase 4)

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
