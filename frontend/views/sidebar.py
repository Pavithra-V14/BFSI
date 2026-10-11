"""The left column: new chat, my chats (resume, delete one, delete all), and the account."""

from __future__ import annotations

import streamlit as st

from frontend import messages, render, state
from frontend.api import ApiClient, SessionExpired
from frontend.views import chat as chat_view
from frontend.views.common import friendly, number

PAGE = 20
MAX_LIST = 100


def _open(client: ApiClient, session_id: str) -> None:
    reply = client.session(session_id)
    if reply.ok and isinstance(reply.data, dict):
        state.open_chat(session_id, chat_view.messages_from_session(reply.data))
    else:
        state.flash("warn", "Could not open that chat", friendly(reply))
    st.rerun()


def _delete_one(client: ApiClient, session_id: str) -> None:
    reply = client.delete_session(session_id)
    if state.session_id() == session_id:
        state.new_chat()
    if not reply.ok:
        state.flash("warn", "Could not delete", friendly(reply))
    st.session_state.pop(state.CONFIRM, None)
    st.rerun()


def _delete_all(client: ApiClient) -> None:
    reply = client.delete_all_sessions()
    state.new_chat()
    if not reply.ok:
        state.flash("warn", "Could not delete", friendly(reply))
    st.session_state.pop(state.CONFIRM, None)
    st.rerun()


def _confirm_box(client: ApiClient) -> None:
    pending = st.session_state.get(state.CONFIRM)
    if not pending:
        return
    if pending == "all":
        render.show_notice("warn", "Delete all my chats?", "This cannot be undone.")
    else:
        render.show_notice("warn", "Delete this chat?", "This cannot be undone.")
    yes, no = st.columns(2)
    if yes.button("Yes, delete", key="fx_confirm_yes"):
        if pending == "all":
            _delete_all(client)
        else:
            _delete_one(client, str(pending))
    if no.button("Keep", key="fx_confirm_no"):
        st.session_state.pop(state.CONFIRM, None)
        st.rerun()


def _sign_out(client: ApiClient) -> None:
    """End the sign-in at the service and drop it here. The token is dropped here whatever the
    service says; if it could not be told, the next page says so."""
    revoked = False
    try:
        revoked = client.logout().ok
    except SessionExpired:
        revoked = True  # the service had already stopped accepting it
    state.sign_out()
    if not revoked:
        state.flash("warn", messages.LOGOUT_FAILED_TITLE, messages.LOGOUT_FAILED_TEXT)
    st.rerun()


def render_sidebar(client: ApiClient) -> None:
    with st.sidebar:
        render.show(render.brand(messages.APP_NAME))
        if st.button("New chat", key="fx_new_chat", use_container_width=True):
            state.new_chat()
            st.rerun()
        render.show(render.who("My chats"))
        limit = min(int(st.session_state.get(state.SESSIONS_LIMIT, PAGE)), MAX_LIST)
        reply = client.sessions(limit=limit)
        if reply.ok and isinstance(reply.data, dict):
            rows = reply.data.get("sessions") or []
            if not rows:
                st.caption("No chats yet.")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                session_id = row["id"]
                if st.button(
                    render.label_text(row.get("title")),
                    key=f"fx_open_{session_id}",
                    use_container_width=True,
                ):
                    _open(client, session_id)
                if st.button("Delete this chat", key=f"fx_del_{session_id}"):
                    st.session_state[state.CONFIRM] = session_id
                    st.rerun()
            total = number(reply.data.get("total"))
            if total > limit and limit < MAX_LIST:
                if st.button("Show more chats", key="fx_more"):
                    st.session_state[state.SESSIONS_LIMIT] = min(limit + PAGE, MAX_LIST)
                    st.rerun()
            if total:
                if st.button("Delete all my chats", key="fx_del_all"):
                    st.session_state[state.CONFIRM] = "all"
                    st.rerun()
        else:
            render.show_notice("warn", "Chats unavailable", friendly(reply))
        _confirm_box(client)
        account = state.user() or {}
        role = messages.ROLE_TEXT.get(str(account.get("role")), "Customer")
        render.show(render.account_line(account.get("username", ""), role))
        if st.button("Sign out", key="fx_sign_out"):
            _sign_out(client)
