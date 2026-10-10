"""Putting text on the page, safely.

Everything that reaches the page from the API (an answer, a title, a question, a name) goes through
`plain()`: it is HTML-escaped, so it can never become markup or a link, whatever it holds. Fragments
are built here from CONSTANT markup plus escaped values and shown with `st.html`, which does no
markdown processing (so an address or `[text](url)` in an answer stays inert text) and sanitises
the markup again in the browser. There is no `unsafe_allow_html` for anything from the API.

Labels of Streamlit buttons are markdown, so text that becomes a label goes through `label_text()`.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any

import streamlit as st

from frontend import messages

MAX_LABEL = 48
_MARKDOWN_CHARS = re.compile(r"[\[\]()*_`~<>\\#|!]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")


def plain(value: Any) -> str:
    """The text as inert HTML text: escaped, control and direction characters removed."""
    text = _CONTROL.sub("", "" if value is None else str(value))
    return html.escape(text, quote=True)


def label_text(value: Any, limit: int = MAX_LABEL) -> str:
    """A short plain label for a button: no markdown characters, no line breaks, shortened."""
    text = _CONTROL.sub("", "" if value is None else str(value))
    text = _MARKDOWN_CHARS.sub("", " ".join(text.split()))
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Untitled chat"


# ---- fragments ------------------------------------------------------------------------------
def notice(kind: str, title: str, text: str = "", *, urgent: bool = False) -> str:
    """A banner. The title is a word ("Note", "Check this", ...), so the meaning never depends on
    colour alone."""
    role = "alert" if urgent else "status"
    body = f'<span class="fx-text">{plain(text)}</span>' if text else ""
    return (
        f'<div class="fx-notice fx-notice-{plain(kind)}" role="{role}">'
        f"<b>{plain(title)}</b>{body}</div>"
    )


def waiting(title: str, text: str) -> str:
    return (
        '<div class="fx-notice fx-notice-info" role="status"><div class="fx-wait">'
        f'<span class="fx-pulse" aria-hidden="true"></span><div><b>{plain(title)}</b>'
        f'<span class="fx-text">{plain(text)}</span></div></div></div>'
    )


def chip(text: str, kind: str = "") -> str:
    extra = f" fx-chip-{plain(kind)}" if kind else ""
    return f'<span class="fx-chip{extra}">{plain(text)}</span>'


def citation_label(citation: dict[str, Any]) -> str:
    parts = [str(citation.get("doc_id") or "document")]
    if citation.get("clause"):
        parts.append(f"clause {citation['clause']}")
    if citation.get("page") is not None:
        page = str(citation["page"])
        if citation.get("page_end") not in (None, citation["page"]):
            page = f"{page}-{citation['page_end']}"
        parts.append(f"page {page}")
    return " · ".join(parts)


def citation_chips(citations: Iterable[dict[str, Any]]) -> str:
    chips = "".join(chip(citation_label(c)) for c in citations if isinstance(c, dict))
    return f'<div class="fx-chips" aria-label="Sources">{chips}</div>' if chips else ""


def split_disclaimer(text: str, has_disclaimer: bool) -> tuple[str, str]:
    """The answer and its disclaimer footnote. The service puts the disclaimer as the last
    paragraph; the frontend only styles it differently and never writes its own wording."""
    if not has_disclaimer:
        return text, ""
    body, separator, last = text.rpartition("\n\n")
    if not separator or not last.strip():
        return text, ""
    return body, last


def message_card(who: str, text: str, *, footnote: str = "", extra: str = "") -> str:
    note = f'<div class="fx-footnote">{plain(footnote)}</div>' if footnote else ""
    return (
        f'<div class="fx-card"><div class="fx-who">{plain(who)}</div>'
        f'<p class="fx-text">{plain(text)}</p>{note}{extra}</div>'
    )


def answer_extras(meta: dict[str, Any]) -> str:
    """Banners and source chips under an answer, from the answer's fields."""
    parts: list[str] = []
    if meta.get("source_removed"):
        parts.append(notice("warn", "Source withdrawn", messages.SOURCE_REMOVED))
    if meta.get("grounded") is False:  # stored as not verified (resumed chats only)
        parts.append(notice("warn", messages.UNVERIFIED_TITLE, messages.UNVERIFIED_TEXT))
    for text in meta.get("warnings") or []:
        parts.append(notice("warn", "Check this", str(text)))
    if meta.get("confidence") == "low":
        parts.append(notice("warn", messages.LOW_CONFIDENCE_TITLE, ""))
    for code in meta.get("degraded") or []:
        parts.append(
            notice(
                "info", "Reduced mode", messages.DEGRADED_TEXT.get(code, messages.DEGRADED_DEFAULT)
            )
        )
    parts.append(citation_chips(meta.get("citations") or []))
    return "".join(parts)


# ---- showing ---------------------------------------------------------------------------------
def show(fragment: str) -> None:
    st.html(fragment)


def show_notice(kind: str, title: str, text: str = "", *, urgent: bool = False) -> None:
    show(notice(kind, title, text, urgent=urgent))
