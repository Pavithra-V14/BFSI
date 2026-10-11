"""The audit log is complete (spec 12.1, 14): every audited action is listed, every list entry is
written somewhere, and every admin route that changes state writes a row.

The test fails when someone adds an audit call with a new action name without listing it in
`app.accounts.audit.AUDITED_ACTIONS`, lists a name nothing writes, or adds an admin route that
changes state without an audit row."""

import ast
from pathlib import Path

import pytest

from app.accounts.audit import AUDITED_ACTIONS

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
# Calls that write the audit row themselves (the function's own body holds the write_audit call).
AUDITING_HELPERS = {"set_role", "set_active_by_id", "set_stage", "set_active", "write_audit"}


def tree_of(path: Path) -> ast.AST:
    return ast.parse(path.read_text("utf-8"))


def action_names(value: ast.AST) -> list[str]:
    """The literal names an `action=` argument can have: a string, or a choice of two strings."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.IfExp):
        return action_names(value.body) + action_names(value.orelse)
    return ["<not a literal>"]


def audit_call_actions() -> dict[str, list[str]]:
    """Action name -> files that write it, from every `write_audit(... action="x" ...)`."""
    found: dict[str, list[str]] = {}
    for path in APP.rglob("*.py"):
        for node in ast.walk(tree_of(path)):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if name != "write_audit":
                continue
            for keyword in node.keywords:
                if keyword.arg == "action":
                    for label in action_names(keyword.value):
                        found.setdefault(label, []).append(str(path.relative_to(ROOT)))
    return found


def test_every_audit_call_uses_a_literal_action_name():
    assert "<not a literal>" not in audit_call_actions()


def test_every_action_written_is_listed():
    written = set(audit_call_actions())
    assert written - AUDITED_ACTIONS == set(), "list the new action in AUDITED_ACTIONS"


def test_every_listed_action_is_written_somewhere():
    written = set(audit_call_actions())
    assert AUDITED_ACTIONS - written == set(), "an action is listed that nothing writes"


def test_the_expected_actions_of_the_spec_are_there():
    # spec 12.1: every ingest, reindex, delete, role change, login and evaluation run
    assert {
        "ingest",
        "reindex",
        "delete",
        "role_change",
        "login",
        "evaluation_run",
    } <= AUDITED_ACTIONS


def test_the_list_is_what_it_was_when_phase_13_was_written():
    """A change to this list is a decision: it has to be made here as well as in the code.
    Phase 13 added `evaluation_run` (an estimate or a start from the admin page)."""
    assert sorted(AUDITED_ACTIONS) == sorted(
        [
            "login", "role_change", "account_activated", "account_deactivated", "ingest",
            "reindex", "delete", "allowlist_review", "chunk_review", "entity_review",
            "review_label", "rollout_stage_change", "memory_clear", "retention_sweep",
            "audit_purge", "evaluation_run",
        ]
    )  # fmt: skip


def state_changing_admin_routes() -> list[tuple[str, str, ast.FunctionDef]]:
    out = []
    for path in sorted((APP / "api" / "routes").glob("admin*.py")):
        for node in ast.walk(tree_of(path)):
            if not isinstance(node, ast.FunctionDef):
                continue
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr in ("put", "post", "delete", "patch")
                ):
                    out.append((path.name, node.name, node))
    return out


def audits(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            target = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if target in AUDITING_HELPERS:
                return True
    return False


def test_the_check_sees_the_admin_routes_that_change_state():
    names = {name for _, name, _ in state_changing_admin_routes()}
    assert {
        "change_role",
        "set_user_active",
        "label_review_item",
        "review_entity",
        "set_stage",
    } <= names


@pytest.mark.parametrize(
    ("file", "name", "function"),
    state_changing_admin_routes(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_admin_route_that_changes_state_writes_an_audit_row(file, name, function):
    assert audits(function), f"{file}:{name} changes state but writes no audit row"


def test_the_route_check_would_catch_a_route_without_one():
    source = (
        "@router.put('/x')\n"
        "def change_something(db):\n"
        "    db.execute('UPDATE users SET role = 1')\n"
    )
    function = ast.parse(source).body[0]
    assert audits(function) is False


def test_an_audit_call_with_a_new_name_would_be_caught(tmp_path):
    source = "write_audit(db, actor='a', action='brand_new_action', target=None, details={}, now=n)"
    call = ast.parse(source).body[0].value
    names = [k.value.value for k in call.keywords if k.arg == "action"]
    assert names == ["brand_new_action"] and "brand_new_action" not in AUDITED_ACTIONS
