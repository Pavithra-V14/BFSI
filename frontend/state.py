"""What one browser session remembers (Streamlit's `session_state`, kept on the server).

The token lives only here. A browser refresh starts a new Streamlit session, so the user signs in
again; there is no browser storage, and the token is never written to a file, a URL or a log. It
is also never part of anything rendered: `render` and the views take the client, not the token.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

TOKEN = "fx_token"
USER = "fx_user"
SESSION_ID = "fx_session_id"
MESSAGES = "fx_messages"
RATED = "fx_rated"
SESSIONS_LIMIT = "fx_sessions_limit"
CONFIRM = "fx_confirm"
FLASH = "fx_flash"
PENDING = "fx_pending"  # an answer is on its way; still set at the start of a run = interrupted


def token() -> str | None:
    value = st.session_state.get(TOKEN)
    return value if isinstance(value, str) and value else None


def user() -> dict[str, Any] | None:
    value = st.session_state.get(USER)
    return value if isinstance(value, dict) else None


def is_admin() -> bool:
    current = user()
    return bool(current and current.get("role") == "admin")


def sign_in(access_token: str, account: dict[str, Any]) -> None:
    sign_out()
    st.session_state[TOKEN] = access_token
    st.session_state[USER] = {
        "id": str(account.get("id", "")),
        "username": str(account.get("username", "")),
        "role": str(account.get("role", "customer")),
    }


def sign_out() -> None:
    for key in list(st.session_state.keys()):
        if str(key).startswith("fx_"):
            del st.session_state[key]


def messages() -> list[dict[str, Any]]:
    return st.session_state.setdefault(MESSAGES, [])


def session_id() -> str | None:
    value = st.session_state.get(SESSION_ID)
    return value if isinstance(value, str) else None


def new_chat() -> None:
    st.session_state[SESSION_ID] = None
    st.session_state[MESSAGES] = []
    st.session_state.pop(CONFIRM, None)


def open_chat(session_id: str, loaded: list[dict[str, Any]]) -> None:
    st.session_state[SESSION_ID] = session_id
    st.session_state[MESSAGES] = loaded
    st.session_state.pop(CONFIRM, None)


def rated() -> dict[str, str]:
    return st.session_state.setdefault(RATED, {})


def flash(kind: str, title: str, text: str = "") -> None:
    """A message to show once, on the next run."""
    st.session_state[FLASH] = (kind, title, text)


def take_flash() -> tuple[str, str, str] | None:
    return st.session_state.pop(FLASH, None)
