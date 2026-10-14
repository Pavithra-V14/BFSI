# Access-control audit (pilot)

Spec 13.1 and 17.6 ("the access-control audit"). This page says what was checked, how, and what
is **not** covered. For the pilot the audit is the route-role matrix test plus Run 12b.

## 1. Who may do what

| Level | Meaning |
|---|---|
| public | anyone, no sign-in |
| user | any signed-in account (customer, staff or admin) |
| admin | admin accounts only |

Roles are assigned only by an admin; self-registration makes a customer only; the role is read
from the database on every request, so a role change or a deactivation takes effect at once.

## 2. The route-role matrix (a test, run on every push)

`tests/test_access_control_matrix.py` lists every route of the API with its level and asks each
one four ways: **no token, a customer, a staff member, an admin**. It fails when:

- a route exists that is not in the table (a new route has to be decided);
- a route in the table no longer exists;
- a route that needs sign-in answers a visitor with anything but 401;
- an admin route answers a customer or a staff member with anything but 403;
- a user route refuses a signed-in account, or an admin is refused anywhere (the one exception,
  entity approval, is refused on purpose on the hosted service with its own error code).

| Level | Routes |
|---|---|
| public | `GET /live`, `POST /auth/register`, `POST /auth/login` |
| user | `POST /auth/logout`, `GET /auth/me`, `POST /chat`, `POST /messages/{id}/feedback`, `GET` and `DELETE /sessions`, `GET` and `DELETE /sessions/{id}` |
| admin | `GET /health`, everything under `/admin/` (alerts, dashboards, diagnostics, documents, entities, evaluation, review queue, shadow switch, users and roles) |

## 3. Role isolation on data (Run 12b, the real collection)

A staff-only document must never be retrieved, cited or answered from for a customer, and must be
for staff; deleted, both roles refuse. The `access_role` filter is applied on the dense and the
sparse branch (an invariant with its own tests). The role-isolation cases of the evaluation set
and of the red-team set measure it with the **zero rule** (any leak is an alert).

## 4. What this audit does NOT cover

- It is a first-party check made by the people who built the system. **An independent second
  review is required before any real customer data** is used: someone who did not write the code
  reads the matrix, the filters, the account flow and the audit log.
- Rate limits, tokens and sessions are covered by their own tests, not by this matrix.
- The admin machine's own commands are not an API: they rely on the owner's machine and shell.
- A route added later is only as protected as its table entry is honest: the test forces the
  decision, not the right answer.

## 5. Record

| Date | Who | What | Result |
|---|---|---|---|
| | | route-role matrix test (CI) | |
| | | Run 12b on the real collection | |
| | | independent second review (before real customer data) | not done |
