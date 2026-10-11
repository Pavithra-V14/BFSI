"""Sign in and create an account (customers only: staff and admin roles are given by an admin)."""

from __future__ import annotations

import streamlit as st

from frontend import messages, progress, render, state
from frontend.api import ApiClient, Reply
from frontend.config import Settings
from frontend.views.common import friendly


def _with_wake_retry(settings: Settings, call) -> Reply:  # noqa: ANN001
    """Run `call`; if the service is still asleep, wait for it (calmly) and run it once more."""
    reply = call()
    if not reply.waking:
        return reply
    slot = st.empty()
    awake = progress.wait_until_awake(
        ApiClient(settings),
        slot,
        max_seconds=settings.wake_max_seconds,
        poll_seconds=settings.wake_poll_seconds,
    )
    return call() if awake else reply


def _sign_in(settings: Settings, username: str, password: str) -> None:
    reply = _with_wake_retry(settings, lambda: ApiClient(settings).login(username, password))
    if not reply.ok:
        render.show_notice("danger", "Could not sign in", friendly(reply), urgent=True)
        return
    token = str(reply.data.get("access_token", "")) if isinstance(reply.data, dict) else ""
    who = ApiClient(settings, token).me()
    if not token or not who.ok or not isinstance(who.data, dict):
        render.show_notice("danger", "Could not sign in", messages.GENERIC_ERROR, urgent=True)
        return
    state.sign_in(token, who.data)
    st.rerun()


def _register(settings: Settings, username: str, email: str, password: str) -> None:
    reply = _with_wake_retry(
        settings, lambda: ApiClient(settings).register(username, email, password)
    )
    if reply.ok:
        render.show_notice("ok", "Account created", "You can sign in now.")
        return
    render.show_notice("danger", "Could not create the account", friendly(reply), urgent=True)


def render_auth(settings: Settings) -> None:
    render.show(render.brand(messages.APP_NAME))
    render.show(render.muted(messages.TAGLINE))
    flash = state.take_flash()
    if flash:
        render.show_notice(*flash)
    sign_in_tab, register_tab = st.tabs(["Sign in", "Create account"])
    with sign_in_tab:
        with st.form("fx_sign_in", clear_on_submit=True):
            username = st.text_input("Username", max_chars=256)
            password = st.text_input("Password", type="password", max_chars=512)
            submitted = st.form_submit_button("Sign in")
        if submitted:
            if username.strip() and password:
                _sign_in(settings, username.strip(), password)
            else:
                render.show_notice("warn", "Check this", "Enter your username and password.")
    with register_tab:
        st.caption("New accounts are customer accounts.")
        with st.form("fx_register", clear_on_submit=True):
            username = st.text_input("Choose a username", max_chars=256, key="fx_reg_user")
            email = st.text_input("Email address", max_chars=512, key="fx_reg_email")
            password = st.text_input(
                "Choose a password", type="password", max_chars=512, key="fx_reg_pass"
            )
            submitted = st.form_submit_button("Create account")
        if submitted:
            if username.strip() and email.strip() and password:
                _register(settings, username.strip(), email.strip(), password)
            else:
                render.show_notice("warn", "Check this", "Fill in all three fields.")
