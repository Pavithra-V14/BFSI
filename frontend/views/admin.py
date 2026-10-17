"""The admin sections (spec 24). Shown only to an admin; the API checks the role on every call.

Where each one runs [P]: documents status, review queue, users and roles, the alerts banner and the
rollout stage work against the hosted API. Ingest is a COMMAND shown for the admin
machine (the browser never starts it); a document is deleted by the Delete button, which works
on the admin machine only and asks for the document id; and entity review is shown only when
`ADMIN_MACHINE=true`
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
    "Maintenance",
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
DELETE_PICK = "fx_doc_delete_pick"  # the document whose Delete button was pressed
DELETE_NOTE = "fx_doc_del_note"  # what the last delete said (kept until the next one)


def _document_rows(client: ApiClient, rows: list[dict[str, Any]]) -> None:
    """One line per ingested document with its Delete button beside it (admin machine only)."""
    if not rows:
        st.caption("No documents are stored.")
        return
    widths = [3, 2, 2, 1, 1, 1, 2]
    head = st.columns(widths)
    head[0].caption("document")
    head[1].caption("access")
    head[2].caption("product line")
    head[3].caption("active")
    head[4].caption("held back")
    head[5].caption("version")
    for row in rows:
        doc_id = str(row.get("doc_id") or "")
        cells = st.columns(widths)
        cells[0].write(render.safe_text(doc_id))
        cells[1].write(render.safe_text(row.get("access_role")))
        cells[2].write(render.safe_text(row.get("product_line")))
        cells[3].write(render.safe_text(row.get("chunks_active")))
        cells[4].write(render.safe_text(row.get("chunks_held_back")))
        cells[5].write(render.safe_text(row.get("document_version")))
        if cells[6].button("Delete", key=f"fx_doc_del_{doc_id}"):
            st.session_state[DELETE_PICK] = doc_id
            st.session_state.pop(DELETE_NOTE, None)
            st.rerun()


def _delete_panel(client: ApiClient) -> None:
    """The confirmation of a delete: the id typed again, a reason from a fixed list, then the
    command runs on this machine. Nothing is deleted before the typed id matches."""
    note = st.session_state.get(DELETE_NOTE)
    if note:
        _show_delete_note(note)
    doc_id = st.session_state.get(DELETE_PICK)
    if not isinstance(doc_id, str) or not doc_id:
        return
    render.show_notice(
        "danger",
        "Delete this document?",
        f"{render.safe_text(doc_id)} will be removed from the search (every stored passage and "
        "allow-list entry), chats that cited it are marked 'source removed', and its id is "
        "recorded as withdrawn so a restore cannot bring it back. This cannot be undone here.",
        urgent=True,
    )
    reason = st.selectbox(
        "Reason",
        list(messages.DELETE_REASONS),
        format_func=lambda v: messages.DELETE_REASON_TEXT[v],
        index=len(messages.DELETE_REASONS) - 1,
        key="fx_doc_del_reason",
    )
    typed = st.text_input("Type the document id to confirm", key="fx_doc_del_typed")
    left, right = st.columns(2)
    if right.button("Cancel", key="fx_doc_del_cancel"):
        st.session_state.pop(DELETE_PICK, None)
        st.rerun()
    if left.button("Delete permanently", key="fx_doc_del_go", disabled=typed.strip() != doc_id):
        reply = client.delete_document(doc_id, typed.strip(), reason)
        if reply.ok and isinstance(reply.data, dict):
            lines = [str(x) for x in reply.data.get("lines") or []]
            if reply.data.get("deleted"):
                st.session_state[DELETE_PICK] = None
                st.session_state[DELETE_NOTE] = ("ok", "Document deleted", lines)
            else:
                st.session_state[DELETE_NOTE] = (
                    "warn",
                    "The delete did not finish",
                    lines + ["Press Delete again to finish it; it is safe to repeat."],
                )
            st.rerun()
        render.show_notice("danger", "Not deleted", friendly(reply), urgent=True)


def _show_delete_note(note: tuple[str, str, list[str]]) -> None:
    kind, title, lines = note
    render.show_notice(kind, title, " ".join(render.safe_text(line) for line in lines))


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
    rows = [d for d in data.get("documents") or [] if isinstance(d, dict)]
    if settings.admin_machine:
        _document_rows(client, rows)
        _delete_panel(client)
        st.subheader("Ingest a document")
        st.caption(
            "Ingesting runs on this machine, in your own terminal; the page never starts it. "
            "Replace the words in capitals. The Delete button above runs the delete command on "
            "this machine for you, after you type the document id."
        )
        st.code(
            "uv run python -m app.ingest ingest PDF_FILE --doc-id SLUG "
            "--doc-type policy --access-role customer",
            language="text",
        )
        st.code("uv run python -m app.ingest status --doc-id SLUG", language="text")
        st.code("uv run python -m app.ingest delete --doc-id SLUG", language="text")
    else:
        _table(rows)
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


def _reason_box(name: str, reason: Any, codes: Any) -> None:
    """The reason of one alert or event, in an expander: what it means, what to do, and the reason
    codes the running service saw. All of it is fixed text or a class name, shown as plain text."""
    if not isinstance(reason, dict):
        return
    with st.expander(render.label_text("View reason: " + name.replace("_", " "), 80)):
        render.caption(f"What it means: {reason.get('meaning')}")
        render.caption(f"What to do: {reason.get('action')}")
        if isinstance(codes, dict) and codes:
            seen = ", ".join(f"{code} ({times})" for code, times in codes.items())
            render.caption(f"Reason codes seen: {seen}")


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
    reasons = data.get("reasons") if isinstance(data.get("reasons"), dict) else {}
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
                "what it means": (reasons.get(a.get("name")) or {}).get("meaning"),
            }
            for a in recent
        ]
    )
    for name in sorted({str(a.get("name")) for a in recent}):
        _reason_box(name, reasons.get(name), {})
    st.subheader("Events seen by this running service")
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    _table(
        [
            {
                "event": e.get("name"),
                "count": e.get("count"),
                "reason codes": ", ".join(
                    f"{code} ({times})" for code, times in (e.get("codes") or {}).items()
                ),
            }
            for e in events
        ]
    )
    for event in events:
        _reason_box(str(event.get("name")), reasons.get(str(event.get("name"))), event.get("codes"))
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


PURGE_OPEN = "fx_purge_open"  # the confirmation panel of the purge is showing
PURGE_NOTE = "fx_purge_note"  # what the last purge said (kept until the next one)
PURGE_LABELS = (
    ("chunks", "document chunks (vectors)"),
    ("chats", "chats"),
    ("messages", "messages"),
    ("feedback", "thumbs"),
    ("users", "customer and staff accounts"),
    ("review_items", "review-queue items"),
    ("alert_rows", "alert rows"),
    ("metric_rows", "metric rows"),
    ("counter_rows", "per-key call count rows"),
)


def maintenance(client: ApiClient, settings: Settings) -> None:
    """The purge of all data: a button, then a red panel with the counts, the warning, a box to
    tick and the phrase to type. Admin machine only; the API refuses it anywhere else."""
    render.show(render.who("Maintenance"))
    if not settings.admin_machine:
        st.caption("Erasing data is done on the admin machine.")
        return
    note = st.session_state.get(PURGE_NOTE)
    if note:
        kind, title, lines = note
        render.show_notice(kind, title, " ".join(render.safe_text(line) for line in lines))
    st.subheader("Purge database data")
    st.caption(
        "Erases every document vector, every cached answer, every chat, every customer and staff "
        "account, the review queue, the alert rows and the metrics. Admin accounts, the audit "
        "log, the allow-list entries, the withdrawn-document list and the settings are kept. "
        "Take a Qdrant snapshot first if you may want the documents back."
    )
    if not st.session_state.get(PURGE_OPEN):
        if st.button("Purge Database Data", key="fx_purge_start"):
            st.session_state[PURGE_OPEN] = True
            st.session_state.pop(PURGE_NOTE, None)
            st.rerun()
        return
    plan = _load(client.purge_plan())
    if plan is None:
        if st.button("Cancel", key="fx_purge_cancel_error"):
            st.session_state.pop(PURGE_OPEN, None)
            st.rerun()
        return
    phrase = str(plan.get("phrase") or "")
    counts = plan.get("counts") if isinstance(plan.get("counts"), dict) else {}
    render.show_notice("danger", "Erase everything?", str(plan.get("warning") or ""), urgent=True)
    _table([{"will be erased": label, "count": counts.get(key)} for key, label in PURGE_LABELS])
    kept = ", ".join(str(k) for k in plan.get("kept") or [])
    other = ", ".join(str(k) for k in plan.get("not_touched") or [])
    render.caption(
        f"Kept: {kept} ({number(counts.get('admins_kept'))} admin account(s)). "
        f"Not touched: {other}."
    )
    understood = st.checkbox("I understand that this cannot be undone.", key="fx_purge_understood")
    typed = st.text_input(render.label_text(f"Type {phrase} to confirm"), key="fx_purge_typed")
    left, right = st.columns(2)
    if right.button("Cancel", key="fx_purge_cancel"):
        st.session_state.pop(PURGE_OPEN, None)
        st.rerun()
    if left.button(
        "Erase everything",
        key="fx_purge_go",
        disabled=not (understood and phrase and typed.strip() == phrase),
    ):
        reply = client.purge(typed.strip())
        if reply.ok and isinstance(reply.data, dict):
            lines = [str(x) for x in reply.data.get("lines") or []]
            if reply.data.get("erased"):
                st.session_state[PURGE_NOTE] = ("ok", "The data was erased", lines)
            else:
                st.session_state[PURGE_NOTE] = (
                    "warn",
                    "The purge did not finish",
                    lines + ["Open the purge again and repeat it; it is safe to repeat."],
                )
            st.session_state.pop(PURGE_OPEN, None)
            st.rerun()
        render.show_notice("danger", "Not erased", friendly(reply), urgent=True)


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
    elif name == "Maintenance":
        maintenance(client, settings)
