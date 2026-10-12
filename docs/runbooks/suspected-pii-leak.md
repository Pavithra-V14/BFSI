# Runbook: suspected personal data in a log, a trace, a sample or an answer

**When:** A person's name, number or address was seen where it must not be: a log line, a Langfuse trace, the evaluation sample or the transcript.

**Who does it:** The admin, with the compliance advisor if real customer data is involved.

## Steps
1. **Stop the spread.** Set the rollout stage to `shadow` (Rollout section of the admin page).
2. **Find the extent.** Scan the log file you have: `uv run python -m app.observability.scan
   LOGFILE` (it prints rule names and line numbers only, never the text). For traces: Langfuse
   project, search by the time. For the sample: `uv run python -m app.eval status --production`.
3. **Remove it.**
   - **One chat (or a few):** the user can delete their own chat (its traces are queued for
     removal by hashed session id). There is **no admin command that removes one named chat**.
     If the person cannot do it, tell your helper: it is a database change made with care, not a
     step to improvise here.
   - **Everything older than the limit:** the retention command
     (`uv run python -m app.memory.retention`). **It is irreversible and it is not aimed at
     your leak**: it deletes every chat not used for 90 days, and audit rows, alert rows and
     hourly metric rows older than 30 days, and the masked evaluation samples older than 30 days.
     It cannot remove a recent chat. **Always run it with `--dry-run` first** (it only counts)
     and read the numbers; run it for real only if you want exactly that. It writes a
     `retention_sweep` row, which also resets the `retention_overdue` clock.
   - Evaluation sample: it holds masked copies only and is purged after 30 days by the same
     command (`EVAL_DATABASE_URL` in the shell). A single sample row has no command either.
   - A log on Render: logs are in Render's stream; ask Render to remove the log if it holds a real
     identifier, and shorten what is kept.
4. **Find the cause.** The masking is local and happens before any model call. If an identifier got
   through, add the case as a `pii` case in the evaluation set and fix the masker's rules; do not
   turn masking off.
5. **Real customer data?** The rule is: no real customer data until paid or zero-retention plans
   are in place. If it happened, tell the compliance advisor the same day (the DPDP Act 2023 may
   require a notice [T: confirm with the advisor]).

## Check
- The scan of the next log file reports no PII rule.
- The next sample row of the same kind of question is masked (`[PAN]`, `[PHONE]`, ...).

## Afterwards
A release record of kind `guardrail`. Rotate any key that was in the same place
(`docs/runbooks/key-leaked.md`).

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
