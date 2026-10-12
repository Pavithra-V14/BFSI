# Runbook: a wrong answer was reported

**When:** A user's thumbs-down, a staff member's message or your own reading found an answer that is wrong or unsafe.

**Who does it:** The admin (the owner of prompt and document changes).

## Steps
1. **Contain it if it is harmful.** Hide answers from everyone but the admin: the admin page,
   Rollout, set the stage to `shadow` (or `PUT /admin/shadow` with `{"stage":"shadow"}`). The
   turn is still generated and stored for you to read.
2. **Find it.** Admin page, Review queue: a thumbs-down creates an item with the MASKED question.
   Open the item. The answer's trace is in Langfuse (masked), found by the time and the stage
   timings.
3. **Decide the cause:**
   - the **document is wrong or out of date**: fix the source, re-ingest it (`uv run python -m
     app.ingest ingest FILE ...`, see Run 10). A changed document removes its cached answers at
     once.
   - the **document is right but not found**: a retrieval or filter problem. Label the item
     `should_have_answered`.
   - the **model made it up**: label `wrong_answer`. The guardrails (numeric and citation checks)
     should have refused it: note that, it is a guardrail bug.
4. **Label and add to the evaluation set.** Review queue, set the label from the fixed list,
   **Add to evaluation set**. It becomes a candidate for the next dataset version.
5. If a prompt, model, retrieval or guardrail changes: make a release record
   (`uv run python scripts/release_record.py --title "..." --kind prompt`), run the active
   evaluation set (Run 21's estimate and code), and compare with the baseline and the variance band
   before the change goes out.

## Check
- The question, asked again, now gets the right answer or a refusal, never the same wrong one.
- The item is `labelled` or `added_to_eval` in the review queue.

## Afterwards
Move the stage back from `shadow` only after the check above and a release record.

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
