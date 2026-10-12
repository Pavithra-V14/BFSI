# Release records

A lightweight record per change (spec 17.3): what changed, the evaluation result against the
baseline, who approved. A prompt, model, retrieval or guardrail change **always** gets one.

Make one:

```
uv run python scripts/release_record.py --title "Tighten the refusal wording" --kind prompt --approved-by "the admin"
```

It copies `TEMPLATE.md` to `YYYY-MM-DD-<title>.md`, fills in the git facts (commit, branch, the
names of the files changed) and leaves the evaluation result and the approval for you to write.
Commit the file with the change it describes. Corpus and code ship separately: a re-ingest is a
record of kind `corpus`.
