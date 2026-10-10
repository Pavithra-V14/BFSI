---
name: build-phase
description: Run one build phase from spec/build.md section 21, for example "/build-phase 1". It plans, builds only what the spec says, tests, reports, then stops. Started only by the owner; never invoke it automatically.
argument-hint: "[phase number 1-16]"
disable-model-invocation: true
---

# Build one phase

The phase number is given as the skill argument. If it is missing or is not a
number from 1 to 16, ask the owner which phase to run and do nothing else.

The build rules, the owner instructions and the invariants are in `CLAUDE.md`.
Follow them as written. They are not repeated here.

## Steps
1. Read the entry for this phase in `spec/build.md` section 21, then every spec
   section that entry depends on. Read the diagrams if the phase touches the
   pipeline or the architecture.
2. List the items tagged [P] and [T] that this phase touches. Ask the owner for
   any [T] value that is needed now. Do not invent a value.
3. Plan before building. Show the owner:
   - the files you will create or change, with one line on what each does,
   - the tests you will write and run,
   - anything the spec leaves unclear.
   Wait for the owner's approval of the plan.
4. Build only what the spec says for this phase. Check section 22 and leave out
   anything listed there.
5. Run the tests for this phase. Ask first before any run that needs real
   provider keys.
6. Report to the owner:
   - the files created or changed, and what each does,
   - the test results, including any failure, stated plainly,
   - the [P] and [T] items this phase touched,
   - spec ambiguities or decisions you made that the owner should confirm.
7. If the phase has any check that needs real keys or real services, append it
   to `docs/OWNER_RUNS.md` in the standard shape (see the file's section C and
   `CLAUDE.md`, "Definition of done"), and add it to the table at the top. A
   phase with such a check is not closed until it is there.
8. Update the line "Current phase: ..." in `README.md` to show this phase as
   finished and awaiting approval.
9. Stop. Do not start the next phase. Wait for the owner's approval.
