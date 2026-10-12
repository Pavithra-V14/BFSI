# Runbook: a document must be withdrawn

**When:** A document is out of date, wrong, or must not be shown any more (a legal or compliance reason).

**Who does it:** The admin, on the admin machine.

## Steps
1. Find its id: `uv run python -m app.ingest status --doc-id DOC_ID` (names and counts only).
2. Delete it: `uv run python -m app.ingest delete --doc-id DOC_ID --reason REASON`, where REASON
   is one of `outdated`, `incorrect`, `compliance`, `replaced`, `other` (a fixed list, never free
   text; the default is `other`). It asks you to type the id. It then:
   - removes the document's vectors, removes the saved answers that cited it at once, marks the
     chats that cited it ("This answer cited a document that has since been withdrawn"), clears
     the chat summaries that used it and writes an audit row;
   - **records the id as withdrawn** (the id, the time and the reason code; no text; permanent).
     A restore re-runs the delete for every recorded id, and an ingest of that id is refused, so
     **the id is not used again by accident** (see step 4 for the deliberate way back);
   - **moves the original PDFs** from `originals/DOC_ID/` to `originals/_withdrawn/DOC_ID/`, so a
     re-ingest from `originals/` cannot bring the document back.
3. **Clean up what the command does not touch** (by hand):
   - The PDF is in `originals/_withdrawn/DOC_ID/`. If it must be destroyed, delete the file there
     and in every external copy of `originals/`. Do not remove the database record.
   - **Qdrant snapshots still hold the document's text**: the ones on the cluster
     (`uv run python scripts/qdrant_snapshot.py list`) and the downloaded files in
     `backups/qdrant/`. They age out as `prune --keep 5` removes them. If the text must be gone
     now, delete the older snapshots in Qdrant Cloud's console and the files in `backups/qdrant/`,
     then take a fresh one (`create --run`, then `download --newest --run`) so you are not left
     without a backup. This is a known limitation, not a fault.
   - Open failed-page rows of that document in the review queue are removed by hand.
4. A replacement: a **new version of a document that is still live** is ingested with the same id
   (`ingest`): only the changed clauses are re-embedded and the old version is retired. A document
   you **withdrew** and now want back (or its replacement) is best given a **new id**. To bring
   the SAME id back on purpose, add `--reinstate DOC_ID --confirm-id DOC_ID` (the id typed
   twice) to the `ingest` command: it removes the withdrawn record and writes an audit row
   (`reinstate`), then ingests. It is refused on Render and without both ids, and the plain
   ingest is refused without it. A restore never uses it, and a rehearsal never needs it (use a
   throw-away id such as `rehearsal-ecas-1`).
5. **A document withdrawn before this record existed** (before migration 0010): run
   `delete --doc-id DOC_ID --reason REASON --even-if-not-stored`. It finds nothing stored, so
   without the flag it prints `id not stored, nothing recorded` and changes nothing (a mistyped
   id never burns an id); with the flag it records the id and moves the PDF.

## Check
- `uv run python -m app.ingest status --doc-id DOC_ID` says there is no active chunk.
- `uv run python -m app.ingest withdrawn` lists the id as `gone`.
- Ask a question that used to cite it: the answer no longer cites it (or is a refusal).
- Resume an old chat that cited it: the notice about the withdrawn document is shown.

## Afterwards
Record it: a release record of kind `corpus`. Tell the business owner. The corpus version
changed, not the code.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
