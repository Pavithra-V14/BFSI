# Runbook: a document must be withdrawn

**When:** A document is out of date, wrong, or must not be shown any more (a legal or compliance reason).

**Who does it:** The admin, on the admin machine.

## Steps
1. Find its id: `uv run python -m app.ingest status --doc-id DOC_ID` (names and counts only).
2. Delete it: `uv run python -m app.ingest delete --doc-id DOC_ID`. It asks you to type the id.
   It removes the document's vectors, removes the saved answers that cited it at once, marks the
   chats that cited it ("This answer cited a document that has since been withdrawn"), clears
   the chat summaries that used it and writes an audit row.
3. The original PDF is **not** deleted (`originals/DOC_ID/...`): remove it by hand if it must go,
   and from your backups. Open failed-page rows of that document in the review queue are removed
   by hand too.
4. A replacement version: ingest the new file with the same id (`ingest`): only the changed
   clauses are re-embedded and the old version is retired.

## Check
- `uv run python -m app.ingest status --doc-id DOC_ID` says there is no active chunk.
- Ask a question that used to cite it: the answer no longer cites it (or is a refusal).
- Resume an old chat that cited it: the notice about the withdrawn document is shown.

## Afterwards
Record it: a release record of kind `corpus`. Tell the business owner. The corpus version
changed, not the code.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
