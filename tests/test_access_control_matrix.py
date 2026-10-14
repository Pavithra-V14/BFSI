"""The access-control matrix (Phase 16, spec 13.1, 17.6 "the access-control audit").

Every route of the API is listed here with the level of access it needs: `public` (anyone),
`user` (any signed-in account) or `admin`. The test asks every route four ways (no token, a
customer, a staff member, an admin) and compares the answer with the table. A new route that is
not in the table fails; so does a route whose answer to the wrong person is not the refusal the
table says. `docs/ACCESS_CONTROL_AUDIT.md` prints the same table.

Together with Run 12b (a staff-only document hidden from a customer on the real collection) this
is the access-control audit of the pilot. An independent second review is still required before
any real customer data (see docs/READINESS.md).
"""

import uuid

PUBLIC, USER, ADMIN = "public", "user", "admin"

MATRIX: dict[tuple[str, str], str] = {
    ("GET", "/live"): PUBLIC,
    ("POST", "/auth/register"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/logout"): USER,
    ("GET", "/auth/me"): USER,
    ("POST", "/chat"): USER,
    ("POST", "/messages/{message_id}/feedback"): USER,
    ("GET", "/sessions"): USER,
    ("DELETE", "/sessions"): USER,
    ("GET", "/sessions/{session_id}"): USER,
    ("DELETE", "/sessions/{session_id}"): USER,
    ("GET", "/health"): ADMIN,
    ("GET", "/admin/alerts"): ADMIN,
    ("GET", "/admin/dashboards/cache"): ADMIN,
    ("GET", "/admin/dashboards/daily"): ADMIN,
    ("GET", "/admin/dashboards/deciders"): ADMIN,
    ("GET", "/admin/dashboards/ingestion"): ADMIN,
    ("GET", "/admin/dashboards/keys"): ADMIN,
    ("GET", "/admin/dashboards/usage"): ADMIN,
    ("GET", "/admin/diagnostics"): ADMIN,
    ("GET", "/admin/documents"): ADMIN,
    ("GET", "/admin/documents/{doc_id}"): ADMIN,
    ("GET", "/admin/entities"): ADMIN,
    ("PUT", "/admin/entities/{entity_id}"): ADMIN,
    ("GET", "/admin/evaluation/environments"): ADMIN,
    ("POST", "/admin/evaluation/estimates"): ADMIN,
    ("GET", "/admin/evaluation/overview"): ADMIN,
    ("GET", "/admin/evaluation/runs/{run_id}"): ADMIN,
    ("POST", "/admin/evaluation/runs/{run_id}/start"): ADMIN,
    ("GET", "/admin/review"): ADMIN,
    ("POST", "/admin/review/{item_id}/label"): ADMIN,
    ("GET", "/admin/shadow"): ADMIN,
    ("PUT", "/admin/shadow"): ADMIN,
    ("GET", "/admin/shadow/answers"): ADMIN,
    ("GET", "/admin/users"): ADMIN,
    ("PUT", "/admin/users/{user_id}/active"): ADMIN,
    ("PUT", "/admin/users/{user_id}/role"): ADMIN,
}

REFUSED = (401, 403)

# Routes an admin is refused on purpose, with the error code that says why (not the plain
# "forbidden" of a missing role): entity review is a job of the admin machine, never the host.
ADMIN_REFUSED_BY_DESIGN = {
    ("PUT", "/admin/entities/{entity_id}"): ("entity_review_disabled", "admin_machine_only"),
}


def app_routes(env) -> set[tuple[str, str]]:
    found = set()
    for path, operations in env.app.openapi()["paths"].items():
        for method in operations:
            if method.upper() not in ("HEAD", "OPTIONS"):
                found.add((method.upper(), path))
    return found


def _sample(env, schema: dict, spec: dict, depth: int = 0):
    """A value that satisfies the (simple) schema: enough to get past request validation."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _sample(env, spec["components"]["schemas"][name], spec, depth + 1)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return _sample(env, options[0], spec, depth + 1)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        needed = schema.get("required", [])
        props = schema.get("properties", {})
        return {n: _sample(env, props.get(n, {}), spec, depth + 1) for n in needed}
    if kind == "array":
        return []
    if kind == "integer":
        return max(int(schema.get("minimum", 1)), 1)
    if kind == "boolean":
        return True
    return "x" * max(int(schema.get("minLength", 1)), 1)


def request_body(env, method: str, path: str):
    spec = env.app.openapi()
    operation = spec["paths"][path][method.lower()]
    content = (operation.get("requestBody") or {}).get("content", {})
    schema = (content.get("application/json") or {}).get("schema")
    return None if schema is None else _sample(env, schema, spec)


def ask(env, method: str, path: str, token: str | None, *, full: bool = False):
    url = path
    for name in ("message_id", "session_id", "doc_id", "entity_id", "run_id", "item_id", "user_id"):
        url = url.replace("{" + name + "}", str(uuid.uuid4()))
    env.redis.flushall()  # the per-user rate limit is not what this test asks about
    headers = env.bearer(token) if token else {}
    body = request_body(env, method, path)
    response = env.client.request(method, url, headers=headers, json=body)
    return response if full else response.status_code


def audit(env, matrix=MATRIX) -> list[str]:
    """Every difference between the table and what the API does (empty list: none)."""
    problems: list[str] = []
    routes = app_routes(env)
    for route in sorted(routes - set(matrix)):
        problems.append(f"{route[0]} {route[1]}: not in the access-control table")
    for route in sorted(set(matrix) - routes):
        problems.append(f"{route[0]} {route[1]}: in the table but not in the API")

    env.admin_token()
    env.make_user("cust_one", role="customer")
    env.make_user("staff_one", role="staff")

    def fresh(username: str) -> str:
        """A token nobody has revoked yet (the logout route revokes the one it is given)."""
        env.redis.flushall()
        return env.token(username)

    for method, path in sorted(routes & set(matrix)):
        level = matrix[(method, path)]
        name = f"{method} {path}"
        nobody = ask(env, method, path, None)
        if level == PUBLIC:
            # a wrong password is a 401 by design; no public route may say "forbidden"
            if nobody == 403 or (nobody == 401 and path != "/auth/login"):
                problems.append(f"{name}: public, but a visitor without a token was refused")
            continue
        if nobody != 401:
            problems.append(f"{name}: needs sign-in, but no token gave {nobody}, not 401")
        for who, username in (("customer", "cust_one"), ("staff", "staff_one")):
            answer = ask(env, method, path, fresh(username))
            if level == ADMIN and answer != 403:
                problems.append(f"{name}: admin only, but a {who} got {answer}, not 403")
            if level == USER and answer in REFUSED:
                problems.append(f"{name}: any account may use it, but a {who} got {answer}")
        reply = ask(env, method, path, fresh("boss"), full=True)
        by_design = ADMIN_REFUSED_BY_DESIGN.get((method, path))
        if by_design:
            code = (reply.json().get("error") or {}).get("code") if reply.status_code else None
            if reply.status_code != 403 or code not in by_design:
                problems.append(
                    f"{name}: expected the refusal {by_design} for an admin, got {code}"
                )
        elif reply.status_code in REFUSED:
            problems.append(f"{name}: an admin was refused ({reply.status_code})")
    return problems


# ---- the real API ----------------------------------------------------------------------------
def test_every_route_answers_each_kind_of_caller_as_the_table_says(env):
    assert audit(env) == []


def test_the_table_has_every_level_and_the_admin_routes_are_the_majority():
    levels = list(MATRIX.values())
    assert {PUBLIC, USER, ADMIN} <= set(levels)
    assert levels.count(ADMIN) > levels.count(USER) > levels.count(PUBLIC) - 1
    assert all(path.startswith("/admin") for (_m, path), lvl in MATRIX.items() if lvl == ADMIN
               and path != "/health")  # fmt: skip


# ---- the audit would notice a mistake --------------------------------------------------------
def test_a_new_route_without_a_table_entry_is_found(env):
    @env.app.get("/new-feature")
    def new_feature():
        return {"ok": True}

    env.app.openapi_schema = None  # rebuild the schema with the new route
    found = audit(env)
    assert any("GET /new-feature: not in the access-control table" in p for p in found)


def test_an_admin_route_that_forgot_its_check_is_found(env):
    @env.app.get("/admin/leaky")
    def leaky():
        return {"secret": 1}

    env.app.openapi_schema = None
    matrix = {**MATRIX, ("GET", "/admin/leaky"): ADMIN}
    found = audit(env, matrix)
    assert any("GET /admin/leaky: needs sign-in, but no token gave 200" in p for p in found)
    assert any("admin only, but a customer got 200" in p for p in found)


def test_a_table_entry_for_a_route_that_is_gone_is_found(env):
    matrix = {**MATRIX, ("GET", "/admin/removed"): ADMIN}
    assert any("not in the API" in p for p in audit(env, matrix))


def test_a_public_route_that_demands_a_token_is_found(env):
    matrix = {**MATRIX, ("GET", "/auth/me"): PUBLIC}
    assert any("public, but a visitor without a token was refused" in p for p in audit(env, matrix))
