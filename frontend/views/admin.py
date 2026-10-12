"""The admin sections (spec 24). Shown only to an admin; the API checks the role on every call.

Where each one runs [P]: documents status, review queue, users and roles, the alerts banner and the
rollout stage work against the hosted API. Ingest and delete are COMMANDS shown for the admin
machine (the browser never starts them), and entity review is shown only when `ADMIN_MACHINE=true`
(the API also refuses it on the host and unless `ENTITY_REVIEW_ENABLED=true`). The evaluation
dashboard is a shell until Phase 13.

Text that comes from the API reaches a Streamlit label only through `render.label_text` /
`render.safe_text`, and a table cell that starts with a formula character is prefixed so a
downloaded copy cannot run as a spreadsheet formula.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import messages, render, state
from frontend.api import ApiClient, Reply
from frontend.config import Settings
from frontend.views.common import friendly, number

SECTIONS = (
    "Chat",
    "Documents",
    "Entities",
    "Review queue",
    "Users",
    "Alerts",
    "Dashboards",
    "Rollout",
    "Evaluation",
)
PAGE = 20
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _load(reply: Reply) -> dict[str, Any] | None:
    if reply.ok and isinstance(reply.data, dict):
        return reply.data
    render.show_notice("danger", "Could not load", friendly(reply), urgent=True)
    return None


def cell(value: Any) -> str:
    """A table cell as text. One that starts with `=`, `+`, `-` or `@` gets a leading apostrophe,
    so the CSV a table can be downloaded as does not run as a spreadsheet formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(FORMULA_START) else text


def _pager(key: str, total: int, limit: int = PAGE) -> int:
    """Previous / Next buttons; returns the offset to load."""
    slot = f"fx_page_{key}"
    offset = int(st.session_state.get(slot, 0))
    offset = max(0, min(offset, max(total - 1, 0) // limit * limit))
    if total > limit:
        left, right, _ = st.columns([1, 1, 3])
        if left.button("Previous", key=f"{slot}_prev", disabled=offset == 0):
            st.session_state[slot] = max(0, offset - limit)
            st.rerun()
        if right.button("Next", key=f"{slot}_next", disabled=offset + limit >= total):
            st.session_state[slot] = offset + limit
            st.rerun()
        render.caption(f"{offset + 1} to {min(offset + limit, total)} of {total}")
    return offset


def _table(rows: list[dict[str, Any]]) -> None:
    safe = [{k: cell(v) for k, v in row.items()} for row in rows]
    if safe:
        st.dataframe(safe, hide_index=True, use_container_width=True)
    else:
        st.caption("Nothing to show.")


# ---- alerts banner (all admin sections) -------------------------------------------------------
def alerts_banner(client: ApiClient) -> None:
    reply = client.alerts()
    if reply.ok and isinstance(reply.data, dict) and reply.data.get("banner"):
        names = ", ".join(str(n) for n in reply.data.get("critical") or [])
        render.show_notice(
            "danger", "Alert: something needs attention", f"Critical events: {names}", urgent=True
        )
    elif reply.ok and isinstance(reply.data, dict) and reply.data.get("warnings"):
        # a warning is information, not an emergency: a calm line, not a red banner
        names = ", ".join(str(n) for n in reply.data.get("warnings") or [])
        render.show_notice("info", "Heads-up", f"Warnings in the last 24 hours: {names}")


# ---- sections --------------------------------------------------------------------------------
def documents(client: ApiClient, settings: Settings) -> None:
    render.show(render.who("Documents"))
    first = _load(client.documents(limit=1))
    if first is None:
        return
    total = number(first.get("total"))
    offset = _pager("documents", total)
    data = _load(client.documents(limit=PAGE, offset=offset))
    if data is None:
        return
    _table(data.get("documents") or [])
    if settings.admin_machine:
        st.subheader("Ingest or delete a document")
        st.caption(
            "These run on this machine, in your own terminal. The page never starts them. "
            "Replace the words in capitals."
        )
        st.code(
            "uv run python -m app.ingest ingest PDF_FILE --doc-id SLUG "
            "--doc-type policy --access-role customer",
            language="text",
        )
        st.code("uv run python -m app.ingest status --doc-id SLUG", language="text")
        st.code("uv run python -m app.ingest delete --doc-id SLUG", language="text")
    else:
        st.caption("Ingesting and deleting documents is done on the admin machine.")


def entities(client: ApiClient, settings: Settings) -> None:
    render.show(render.who("Allow-list entries waiting for review"))
    if not settings.admin_machine:
        st.caption("Entity review is done on the admin machine. This list is read-only here.")
    status = st.selectbox("Show", ["pending", "approved", "rejected"], key="fx_ent_status")
    first = _load(client.entities(status=status, limit=1))
    if first is None:
        return
    offset = _pager("entities", number(first.get("total")))
    data = _load(client.entities(status=status, limit=PAGE, offset=offset))
    if data is None:
        return
    rows = data.get("entities") or []
    _table(
        [
            {k: r.get(k) for k in ("text", "entity_type", "doc_id", "status", "approved_at")}
            for r in rows
        ]
    )
    if not settings.admin_machine or not rows or status != "pending":
        return
    by_id = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}
    chosen = st.selectbox(
        "Entry",
        list(by_id),
        format_func=lambda i: render.label_text(by_id[i].get("text"), 60),
        key="fx_ent_pick",
    )
    # The id is in the key, so a tick given to one entry is never carried over to another.
    confirm = st.checkbox(
        "I confirm this one-word name (needed to approve a surname on its own)",
        key=f"fx_ent_conf_{chosen}",
    )
    approve, reject = st.columns(2)
    for column, outcome, label in (
        (approve, "approved", "Approve"),
        (reject, "rejected", "Reject"),
    ):
        if column.button(label, key=f"fx_ent_{outcome}_{chosen}"):
            reply = client.set_entity(chosen, outcome, confirm_single_word=confirm)
            if reply.ok:
                st.rerun()
            render.show_notice("danger", "Not changed", friendly(reply), urgent=True)


def review_queue(client: ApiClient) -> None:
    render.show(render.who("Review queue"))
    status = st.selectbox(
        "Show", ["open", "labelled", "added_to_eval", "dismissed"], key="fx_rev_status"
    )
    first = _load(client.review(status=status, limit=1))
    if first is None:
        return
    offset = _pager("review", number(first.get("total")))
    data = _load(client.review(status=status, limit=PAGE, offset=offset))
    if data is None:
        return
    items = data.get("items") or []
    _table(
        [
            {
                "kind": i.get("kind"),
                "created": i.get("created_at"),
                "overdue": "yes" if i.get("overdue") else "",
                "label": i.get("label"),
                "question": (i.get("details") or {}).get("question"),
            }
            for i in items
        ]
    )
    if not items:
        return
    by_id = {i["id"]: i for i in items if isinstance(i.get("id"), str)}
    chosen = st.selectbox(
        "Item",
        list(by_id),
        format_func=lambda i: render.label_text(
            f"{by_id[i].get('kind')} · {str(by_id[i].get('created_at'))[:10]}", 60
        ),
        key="fx_rev_pick",
    )
    label = st.selectbox(
        "Label",
        list(messages.REVIEW_LABELS),
        format_func=lambda v: messages.LABEL_TEXT[v],
        key=f"fx_rev_label_{chosen}",
    )
    left, right = st.columns(2)
    if left.button("Save label", key="fx_rev_save"):
        reply = client.label(chosen, label)
        if reply.ok:
            st.rerun()
        render.show_notice("danger", "Not saved", friendly(reply), urgent=True)
    if right.button("Add to evaluation set", key="fx_rev_eval"):
        reply = client.label(chosen, label, status="added_to_eval")
        if reply.ok:
            result = str((reply.data or {}).get("evaluation_candidate") or "")
            st.session_state["fx_rev_notice"] = (
                messages.CANDIDATE_TITLE.get(result, "Label saved, not added"),
                messages.CANDIDATE_TEXT.get(result, ""),
                "ok" if result in ("added", "already_added") else "warn",
            )
            st.rerun()
        render.show_notice("danger", "Not added", friendly(reply), urgent=True)
    note = st.session_state.pop("fx_rev_notice", None)
    if note:
        title, text, kind = note
        render.show_notice(kind, title, text)


def users(client: ApiClient) -> None:
    render.show(render.who("Users and roles"))
    first = _load(client.users(limit=1))
    if first is None:
        return
    offset = _pager("users", number(first.get("total")), 50)
    data = _load(client.users(limit=50, offset=offset))
    if data is None:
        return
    rows = data.get("users") or []
    _table(
        [
            {
                "username": r.get("username"),
                "role": messages.ROLE_TEXT.get(str(r.get("role")), r.get("role")),
                "active": "yes" if r.get("is_active") else "no",
                "created": r.get("created_at"),
            }
            for r in rows
        ]
    )
    by_id = {r["id"]: r for r in rows if isinstance(r.get("id"), str)}
    if not by_id:
        return
    me = (state.user() or {}).get("id")
    chosen = st.selectbox(
        "Account",
        list(by_id),
        format_func=lambda i: render.label_text(by_id[i].get("username"), 40),
        key="fx_user_pick",
    )
    target = by_id[chosen]
    if chosen == me:
        st.caption("Your own account cannot be changed here.")
        return
    if target.get("role") == "admin":
        st.caption("Admin accounts cannot be changed here.")
        return
    # The id is in the key, so a role picked for one account is never carried over to another.
    role = st.selectbox(
        "Role",
        ["customer", "staff"],
        index=0 if target.get("role") == "customer" else 1,
        format_func=lambda v: messages.ROLE_TEXT[v],
        key=f"fx_user_role_{chosen}",
    )
    left, right = st.columns(2)
    if left.button("Save role", key=f"fx_user_save_{chosen}"):
        reply = client.set_role(chosen, role)
        if reply.ok:
            st.rerun()
        render.show_notice("danger", "Not saved", friendly(reply), urgent=True)
    active = bool(target.get("is_active"))
    if right.button("Switch off" if active else "Switch on", key=f"fx_user_active_{chosen}"):
        reply = client.set_active(chosen, not active)
        if reply.ok:
            st.rerun()
        render.show_notice("danger", "Not saved", friendly(reply), urgent=True)


def alerts(client: ApiClient) -> None:
    render.show(render.who("Alerts"))
    data = _load(client.alerts())
    if data is None:
        return
    if data.get("banner"):
        names = ", ".join(str(n) for n in data.get("critical") or [])
        render.show_notice("danger", "Critical events", names, urgent=True)
    elif data.get("warnings"):
        names = ", ".join(str(n) for n in data.get("warnings") or [])
        render.show_notice("info", "Warnings", names)
    else:
        render.show_notice("ok", "All quiet", "No critical event among the recent ones.")
    st.subheader("Alerts of the last 24 hours")
    recent = [a for a in data.get("recent") or [] if isinstance(a, dict)]
    _table(
        [
            {
                "alert": a.get("name"),
                "severity": a.get("severity"),
                "count": a.get("count"),
                "sent to webhook": "yes" if a.get("notified") else "no",
                "at": a.get("at"),
            }
            for a in recent
        ]
    )
    st.subheader("Events seen by this running service")
    _table([{"event": e.get("name"), "count": e.get("count")} for e in data.get("events") or []])
    render.caption(
        f"The alerts above are saved in the database and survive a restart. The event counts "
        f"(the last {number(data.get('window'))} events of the past 24 hours) are kept in memory "
        "by the running service and a restart clears them."
    )


def rollout(client: ApiClient) -> None:
    render.show(render.who("Rollout stage (shadow mode)"))
    data = _load(client.stage())
    if data is None:
        return
    current = str(data.get("stage"))
    stages = [s for s in data.get("stages") or [] if s in messages.STAGE_TEXT]
    render.show_notice("info", "Current stage", messages.STAGE_TEXT.get(current, current))
    if stages:
        chosen = st.selectbox(
            "Change to",
            stages,
            index=stages.index(current) if current in stages else 0,
            format_func=lambda v: messages.STAGE_TEXT[v],
            key="fx_stage_pick",
        )
        if st.button("Change stage", key="fx_stage_save", disabled=chosen == current):
            reply = client.set_stage(chosen)
            if reply.ok:
                st.rerun()
            render.show_notice("danger", "Not changed", friendly(reply), urgent=True)
    st.subheader("Answers hidden from their users")
    first = _load(client.shadow_answers(limit=1))
    if first is None:
        return
    offset = _pager("shadow", number(first.get("total")))
    page = _load(client.shadow_answers(limit=PAGE, offset=offset))
    if page is None:
        return
    for item in page.get("answers") or []:
        with st.expander(render.label_text(item.get("question"), 70)):
            render.show(render.message_card("Question (masked)", str(item.get("question", ""))))
            render.show(render.hidden_answer(item))
    if not page.get("answers"):
        st.caption("Nothing hidden yet.")


def render_section(name: str, client: ApiClient, settings: Settings) -> None:
    if name == "Documents":
        documents(client, settings)
    elif name == "Entities":
        entities(client, settings)
    elif name == "Review queue":
        review_queue(client)
    elif name == "Users":
        users(client)
    elif name == "Alerts":
        alerts(client)
    elif name == "Dashboards":
        from frontend.views.dashboards import render_dashboards

        render_dashboards(client)
    elif name == "Rollout":
        rollout(client)
    elif name == "Evaluation":
        from frontend.views.evaluation import render_evaluation

        render_evaluation(client, settings)
