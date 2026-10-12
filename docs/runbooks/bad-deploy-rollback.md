# Runbook: a bad deploy (go back)

**When:** After a deploy, the service is broken: errors, the wrong behaviour, the alert banner is red.

**Who does it:** The admin. Target: back to the last good build in under 5 minutes.

## Steps
1. Start a stopwatch.
2. Render dashboard, `bfsi-api`, **Deploys**. Find the last deploy marked **Live** before the bad
   one, **Rollback** (it redeploys that build; no new build is made).
3. If the frontend was also changed, do the same on `bfsi-frontend`.
3a. **Check Auto-Deploy is on again** [T: verify on the day]: Render may switch automatic
   deploys off for a service you rolled back, so that the next push does not undo it. Service,
   **Settings**, **Auto-Deploy**. Until it is on, a fix pushed to `main` is not deployed: use
   **Manual Deploy**.
4. Ask `https://YOUR-API.onrender.com/live`: `{"status":"ok"}`.
5. Sign in and ask one question.
6. **Migrations:** the migrations 0001 to 0010 add tables, columns and indexes, so the previous
   build still works against the newer schema, with one exception that is already behind us:
   0007 deleted duplicate review items once, while it was applied. Read the newest file in
   `migrations/` before a rollback: if a migration changes or removes something (a drop, a
   rename, a delete, a type change) STOP and ask your helper before rolling back. Migrations are
   run by you from your machine, never by a deploy.
7. Stop the stopwatch and write the time in the rehearsal table.

## Check
- `/live` answers; one question is answered; the admin banner has no new critical alert.
- The workflow `After a deploy` of the next push is green (it only shows that `/live` answers).

## Afterwards
Open an issue for the bad change. Do not push the same commit again until a fix is on `main`
(Render deploys after the checks pass, so a revert commit also redeploys, **if Auto-Deploy is on**:
step 3a).

## Last rehearsal
Never rehearsed. Write the date, who did it and how long it took in `docs/runbooks/README.md`
after each rehearsal.
