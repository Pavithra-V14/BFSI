"""The Streamlit entry point.  Run from the project root:

    uv run streamlit run frontend/app.py

Settings come from the shell: `API_BASE_URL` (default: the API on this machine) and, on the
admin machine only, `ADMIN_MACHINE=true`. No secret is read or needed here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# `streamlit run frontend/app.py` puts this folder, not the project root, on the path.
ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from frontend import messages, render, state, theme  # noqa: E402
from frontend.api import ApiClient, SessionExpired  # noqa: E402
from frontend.config import ConfigError, config_file_found, load_settings  # noqa: E402
from frontend.views import admin  # noqa: E402
from frontend.views.auth import render_auth  # noqa: E402
from frontend.views.chat import render_chat  # noqa: E402
from frontend.views.sidebar import render_sidebar  # noqa: E402


def main() -> None:
    st.set_page_config(page_title=messages.APP_NAME, layout="wide", initial_sidebar_state="auto")
    theme.apply()
    try:
        settings = load_settings()
    except ConfigError as error:
        render.show_notice("danger", "Set-up problem", str(error), urgent=True)
        return
    if not config_file_found():
        render.show_notice(
            "warn", messages.CONFIG_MISSING_TITLE, messages.CONFIG_MISSING_TEXT, urgent=True
        )
    if settings.insecure_api:
        render.show_notice(
            "danger", messages.INSECURE_API_TITLE, messages.INSECURE_API_TEXT, urgent=True
        )
    token = state.token()
    if token is None:
        render_auth(settings)
        return
    client = ApiClient(settings, token)
    try:
        _signed_in_page(client, settings)
    except SessionExpired:
        # One place for every API call: the service no longer accepts this sign-in.
        state.sign_out()
        state.flash("warn", "Signed out", messages.SIGNED_OUT)
        st.rerun()


def _signed_in_page(client: ApiClient, settings) -> None:  # noqa: ANN001
    render_sidebar(client)
    flash = state.take_flash()
    if flash:
        render.show_notice(*flash)
    if not state.is_admin():
        render_chat(client, settings)
        return
    admin.alerts_banner(client)
    section = st.radio(
        "Section",
        admin.SECTIONS,
        horizontal=True,
        label_visibility="collapsed",
        key="fx_section",
    )
    if section == "Chat":
        render_chat(client, settings)
    else:
        admin.render_section(section, client, settings)


main()
