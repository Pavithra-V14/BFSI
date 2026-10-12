# Runbook: restoring the documents (re-ingest or snapshot)

**When:** The vector collection is damaged, deleted or empty, or you are rehearsing the restore.

**Who does it:** The admin, on the admin machine.

## Read this first
- **The tested route is B: re-ingest from `originals/`.** Route A (a Qdrant snapshot) is
  **UNPROVEN**: restoring a downloaded snapshot has not been done on your cluster. It stays
  unproven until Run 30 has passed (a downloaded snapshot restored into the throw-away collection
  `documents_restore_test`, the point count checked, then removed). Even then, restoring over
  `documents` itself has not been rehearsed. Use route B unless you have a reason not to.
- **A restore discards what is not in the source it restores from.** Route A brings back the
  collection AS IT WAS when the snapshot was taken: everything ingested since is gone until you
  ingest it again (step A4 does that). Route B brings back exactly the PDFs in `originals/`.
- **A restore must never bring a withdrawn document back, and the system is not "restored" until
  step C passes.** Both routes can bring one back (a snapshot taken before the withdrawal; a PDF
  still in `originals/`). The delete command records every withdrawn id in the database and moves
  its PDFs to `originals/_withdrawn/`, so route B does not see them; step C is the check.
- **Snapshots still hold the text of a document withdrawn after they were taken**, on the cluster
  and in `backups/qdrant/`, until they age out (known limitation: see
  `docs/runbooks/document-withdrawn.md`, step 3).

## Steps
**A. A snapshot (unproven: read the first section).**
1. List: `uv run python scripts/qdrant_snapshot.py list`. Choose the newest good one.
2. Restore it in Qdrant Cloud's console (the collection `documents`, its snapshots, restore) or
   with Qdrant's own restore from an uploaded file. The menu names are unverified [T]; if
   anything looks different from this, stop and ask your helper. **This overwrites the live
   collection.**
3. Run `uv run python -m app.ingest status --doc-id DOC_ID` for each document and compare the
   active chunk counts with what you expect.
4. **Bring it up to date from `originals/`:** do step B3 for every document. A document the
   snapshot lacks, or has an older version of, is brought up to date; that bumps the corpus
   version and removes the cached answers that cited it. Unchanged documents are skipped. (A
   Qdrant restore does not change the corpus version by itself, so without this step a cached
   answer can cite a passage the restored collection no longer has, for up to 24 hours.)
5. Go to step C.

**B. Re-ingest from `originals/`** (the tested route; also the answer when snapshots are refused).
1. Check `originals/` is there and complete: one folder per document id, inside it
   `<sha256>.pdf`. It is **the only copy** of the PDFs: the backup is the external copy you made
   (see "Where `originals/` is backed up" below). Ignore the folder `originals/_withdrawn/`: it
   holds withdrawn documents and is never re-ingested.
2. Create the collection if it is gone: `uv run python -m app.vector.documents --ensure`.
3. For each document run the ingest command of Run 10 with its id, type, role and product line.
   Unchanged documents are skipped; a document that exists is brought up to date. An id that was
   withdrawn is refused by the command, by design (it names `--reinstate`; do not use it here:
   a restore never brings a withdrawn id back, and the flag is refused on Render).
4. Ingestion bumps the corpus version, which makes every cached answer stale. Nothing more to do.
5. Go to step C.

**C. Withdrawn documents stay withdrawn (both routes, before you call it restored).**
1. `uv run python -m app.ingest withdrawn`. It lists every withdrawn id and says `gone` or
   `STILL STORED: N chunk(s)`, and exits 1 while any is still stored.
2. For each id that is still stored: `uv run python -m app.ingest delete --doc-id DOC_ID` (it
   asks you to type the id). Repeat step 1 until it exits 0 and prints no `STILL STORED`.
3. If the database itself was lost (not only the vectors), the list is empty. The second line of
   defence is the folder `originals/_withdrawn/`: its sub-folders are the withdrawn ids. Run
   `delete --doc-id ID --even-if-not-stored` for each, which records them again and finds
   nothing stored (without the flag a delete of an id that holds nothing records nothing).

**Where `originals/` is backed up** (fill this in once, and keep it true; the backup must include
`originals/_withdrawn/`):
- Copy 1: this machine, `E:\Claude\BFSI\originals\` (the working copy).
- Copy 2: ______________________ (an external drive, date of the last copy: __________).
- Copy 3: ______________________ (another place, date of the last copy: __________).

**The restore test** (do it once, then once a quarter): ingest ONE document into a scratch
collection with `--collection documents_eval` from the backup copy, and compare `status` of the
copy with the original: the same number of active chunks. Route A has its own test, Run 30.

## Check
- `uv run python -m app.ingest withdrawn` exits 0 and shows no `STILL STORED` line.
- `status` of each document shows its active chunks (counts only).
- A question that cites each document is answered with a citation; a question that used to cite a
  withdrawn document no longer does.

## Afterwards
Record the restore in the rehearsal table (date, which path, minutes, any surprise).

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
