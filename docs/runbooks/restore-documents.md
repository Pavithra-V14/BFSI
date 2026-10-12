# Runbook: restoring the documents (snapshot or re-ingest)

**When:** The vector collection is damaged, deleted or empty, or you are rehearsing the restore.

**Who does it:** The admin, on the admin machine.

## Steps
**A. If snapshots work** (Run 30 shows whether the free cluster allows them):
1. List: `uv run python scripts/qdrant_snapshot.py list`.
2. Restore the snapshot in the Qdrant Cloud console (Collections, `documents`, Snapshots,
   Restore), choose the newest good one.
3. Run `uv run python -m app.ingest status --doc-id DOC_ID` for each document.

**B. If snapshots are refused, or none is good: re-ingest from `originals/`.**
1. Check `originals/` is there and complete: one folder per document id, inside it
   `<sha256>.pdf`. It is **the only copy**: the backup is the external copy you made
   (see "Where `originals/` is backed up" below).
2. Create the collection if it is gone: `uv run python -m app.vector.documents --ensure`.
3. For each document run the ingest command of Run 10 with its id, type, role and product line.
   Unchanged documents are skipped; a document that exists is brought up to date.
4. Refresh the cache: ingestion bumps the corpus version, which makes every cached answer stale.

**Where `originals/` is backed up** (fill this in once, and keep it true):
- Copy 1: this machine, `E:\Claude\BFSI\originals\` (the working copy).
- Copy 2: ______________________ (an external drive, date of the last copy: __________).
- Copy 3: ______________________ (another place, date of the last copy: __________).

**The restore test** (do it once, then once a quarter): ingest ONE document into a scratch
collection with `--collection documents_eval` from the backup copy, and compare `status` of the
copy with the original: the same number of active chunks.

## Check
- `status` of each document shows its active chunks (counts only).
- A question that cites each document is answered with a citation.

## Afterwards
Record the restore in the rehearsal table (date, which path, minutes, any surprise).

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
