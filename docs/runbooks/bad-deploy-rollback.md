# Runbook: a bad deploy (go back)

**When:** After a deploy, the service is broken: errors, the wrong behaviour, the alert banner is red.

**Who does it:** The admin. Target: back to the last good build in under 5 minutes.

## Steps
1. Start a stopwatch.
2. Render dashboard, `bfsi-api`, **Deploys**. Find the last deploy marked **Live** before the bad
   one, **Rollback** (it redeploys that build; no new build is made).
3. If the frontend was also changed, do the same on `bfsi-frontend`.
4. Ask `https://YOUR-API.onrender.com/live`: `{"status":"ok"}`.
5. Sign in and ask one question.
6. **Migrations:** every migration of this project only adds (tables, columns, indexes), so the
   previous build still works against the newer schema. If a deploy included a migration that
   changes or removes something, STOP and ask your helper before rolling back.
7. Stop the stopwatch and write the time in the rehearsal table.

## Check
- `/live` answers; one question is answered; the admin banner has no new critical alert.
- The CI job `post-deploy-smoke` of the next push is green.

## Afterwards
Open an issue for the bad change. Do not push the same commit again until a fix is on `main`
(Render deploys after the checks pass, so a revert commit also redeploys).

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
