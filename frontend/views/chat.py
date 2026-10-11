"""The chat screen: the conversation, the question box, thumbs, and the waiting states.

Everything from the API is shown escaped (`render`). The question and the answer are held only in
the browser session's state for the length of the conversation; nothing is logged or written.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import messages, progress, render, state
from frontend.api import ApiClient, Reply
from frontend.config import Settings
from frontend.views.common import friendly

MAX_QUESTION_CHARACTERS = 2_000  # the service's own limit: the box simply stops there


# ---- turning replies into messages ------------------------------------------------------------
def _notice_message(kind: str, title: str, text: str) -> dict[str, Any]:
    return {"role": "notice", "kind": kind, "title": title, "text": text}


def message_from_reply(reply: Reply) -> dict[str, Any]:
    """The conversation entry for a finished `/chat` call."""
    if reply.ok and isinstance(reply.data, dict):
        body = reply.data
        if body.get("answer_visible") is False:  # shadow mode: the fixed notice, nothing else
            return _notice_message("info", messages.TEST_PERIOD_TITLE, str(body.get("text", "")))
        return {
            "role": "assistant",
            "text": str(body.get("text", "")),
            "id": body.get("message_id") if isinstance(body.get("message_id"), str) else None,
            "kind": str(body.get("kind", "answer")),
            "meta": {
                "citations": body.get("citations") or [],
                "warnings": body.get("warnings") or [],
                "confidence": body.get("confidence"),
                "degraded": body.get("degraded") or [],
                "disclaimer": bool(body.get("disclaimer")),
            },
        }
    return _notice_message("danger", "Could not answer", friendly(reply))


def messages_from_session(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The conversation entries for a resumed session."""
    out: list[dict[str, Any]] = []
    for item in data.get("messages") or []:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            out.append({"role": "user", "text": str(item.get("text", ""))})
            continue
        notice = item.get("notice")
        if notice == "shadowed":
            out.append(
                _notice_message("info", messages.TEST_PERIOD_TITLE, str(item.get("text", "")))
            )
            continue
        out.append(
            {
                "role": "assistant",
                "text": str(item.get("text", "")),
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                # "fixed_reply" = one of our own texts (a greeting, a refusal ...): no "not fully
                # verified" notice and no thumbs. A missing marker (an older service) = an answer.
                "kind": "fixed_reply" if item.get("kind") == "fixed_reply" else "answer",
                "resumed": True,
                "meta": {
                    "citations": item.get("citations") or [],
                    "source_removed": bool(item.get("source_removed")),
                    "grounded": None if item.get("kind") == "fixed_reply" else item.get("grounded"),
                },
            }
        )
    return out


# ---- showing ---------------------------------------------------------------------------------
def _thumbs(client: ApiClient, message: dict[str, Any], index: int) -> None:
    message_id = message.get("id")
    if not message_id:
        st.caption(messages.NO_FEEDBACK_YET)
        return
    given = state.rated().get(message_id)
    if given:
        st.caption(messages.FEEDBACK_THANKS)
        return
    left, right, _ = st.columns([2, 2, 3])
    for column, rating, label in ((left, "up", "👍 Helpful"), (right, "down", "👎 Not helpful")):
        if column.button(label, key=f"fx_{rating}_{index}_{message_id}"):
            reply = client.feedback(message_id, rating)
            if reply.ok:
                state.rated()[message_id] = rating
                st.rerun()
            render.show_notice("danger", "Could not save", friendly(reply), urgent=True)


def render_message(client: ApiClient, message: dict[str, Any], index: int = 0) -> None:
    role = message.get("role")
    if role == "user":
        render.show(render.message_card("You", str(message.get("text", ""))))
    elif role == "notice":
        render.show_notice(
            str(message.get("kind", "info")),
            str(message.get("title", "Note")),
            str(message.get("text", "")),
            urgent=message.get("kind") == "danger",
        )
    else:
        meta = message.get("meta") or {}
        text = str(message.get("text", ""))
        body, footnote = render.split_disclaimer(text, bool(meta.get("disclaimer")))
        if meta.get("source_removed"):  # the service sends its own notice text; show it as one
            render.show_notice("warn", "Source withdrawn", text)
        else:
            render.show(
                render.message_card(
                    "Assistant", body, footnote=footnote, extra=render.answer_extras(meta)
                )
            )
        if message.get("kind", "answer") == "answer":
            _thumbs(client, message, index)


def render_history(client: ApiClient) -> None:
    history = state.messages()
    if not history:
        render.show(render.muted(messages.EMPTY_CHAT))
    for index, message in enumerate(history):
        render_message(client, message, index)


# ---- sending ---------------------------------------------------------------------------------
def _drop_interrupted_question() -> bool:
    """A run that found `PENDING` still set was cut short while an answer was on its way (the
    user sent something else, or reloaded). Its question has no answer on this page: remove it,
    clear the flag and say so. Returns True when that happened."""
    if not st.session_state.pop(state.PENDING, None):
        return False
    history = state.messages()
    if history and history[-1].get("role") == "user":
        history.pop()
    return True


def _send(client: ApiClient, settings: Settings, question: str) -> None:
    history = state.messages()
    history.append({"role": "user", "text": question})
    st.session_state[state.PENDING] = True  # cleared when the answer (or an error) is in
    render.show(render.message_card("You", question))
    slot = st.empty()
    session_id = state.session_id()
    reply = progress.wait_with_progress(lambda: client.chat(question, session_id), slot)
    if reply.waking:  # a cold start: wait for the service, then ask once more
        awake = progress.wait_until_awake(
            client,
            slot,
            max_seconds=settings.wake_max_seconds,
            poll_seconds=settings.wake_poll_seconds,
        )
        if awake:
            reply = progress.wait_with_progress(lambda: client.chat(question, session_id), slot)
        else:
            history.append(_notice_message("warn", messages.WAKING_TITLE, messages.WAKING_GAVE_UP))
            st.session_state.pop(state.PENDING, None)
            st.rerun()
            return
    st.session_state.pop(state.PENDING, None)
    if reply.kind == "error" and reply.code == "not_found" and session_id:
        history.append(
            _notice_message(
                "warn", "Conversation not found", "That conversation is gone. Start a new one."
            )
        )
        state.new_chat()
        st.rerun()
        return
    if reply.ok and isinstance(reply.data, dict):
        new_session = reply.data.get("session_id")
        if isinstance(new_session, str):
            st.session_state[state.SESSION_ID] = new_session
    history.append(message_from_reply(reply))
    st.rerun()


def render_chat(client: ApiClient, settings: Settings) -> None:
    interrupted = _drop_interrupted_question()
    render_history(client)
    if interrupted:
        render.show_notice("warn", messages.INTERRUPTED_TITLE, messages.INTERRUPTED_TEXT)
    question = st.chat_input("Ask about a policy or regulation", max_chars=MAX_QUESTION_CHARACTERS)
    if question and question.strip():
        if interrupted:
            return  # the earlier question may still be answered: the user asks this one again
        _send(client, settings, question.strip())
