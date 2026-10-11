"""The look of the frontend: colour tokens for the light and dark theme, and the one stylesheet.

The tokens are the single source: the stylesheet is built from them, `.streamlit/config.toml`
repeats
the main colours (a test keeps them equal), and a test computes the contrast ratio of every
text-and-background pair in both themes (at least 4.5 to 1, the WCAG AA level for normal text).

This is the ONLY place where `unsafe_allow_html` is used, and only for the static stylesheet built
here from constants. Nothing that comes from the API ever goes through it.
"""

from __future__ import annotations

import streamlit as st

# Colour tokens. Names are the same in both themes.
TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#F6F7F9",
        "surface": "#FFFFFF",
        "text": "#14202B",
        "muted": "#556575",
        "primary": "#0E6B68",
        "on_primary": "#FFFFFF",
        "border": "#C9D1D9",
        "focus": "#0B57D0",
        "warn_text": "#7A4F00",
        "warn_bg": "#FFF4D6",
        "danger_text": "#B42318",
        "danger_bg": "#FDECEA",
        "ok_text": "#17663F",
        "ok_bg": "#E6F4EC",
        "info_text": "#1D4F91",
        "info_bg": "#E8F0FB",
    },
    "dark": {
        "bg": "#0E1418",
        "surface": "#161F26",
        "text": "#E7EDF2",
        "muted": "#9AAAB7",
        "primary": "#4DB6AC",
        "on_primary": "#06201E",
        "border": "#3A4854",
        "focus": "#7AB0FF",
        "warn_text": "#F2C14E",
        "warn_bg": "#2B2410",
        "danger_text": "#FF9C94",
        "danger_bg": "#33171A",
        "ok_text": "#6FD3A0",
        "ok_bg": "#10281C",
        "info_text": "#8EB8F5",
        "info_bg": "#14233A",
    },
}

# Every pair a piece of text is shown on, as (text token, background token).
TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    ("text", "bg"),
    ("text", "surface"),
    ("muted", "bg"),
    ("muted", "surface"),
    ("primary", "bg"),
    ("primary", "surface"),
    ("on_primary", "primary"),
    ("warn_text", "warn_bg"),
    ("danger_text", "danger_bg"),
    ("danger_text", "surface"),
    ("ok_text", "ok_bg"),
    ("info_text", "info_bg"),
)
# Non-text parts that must stand out (the focus ring), WCAG asks for 3 to 1.
UI_PAIRS: tuple[tuple[str, str], ...] = (("focus", "bg"), ("focus", "surface"))

MIN_TEXT_CONTRAST = 4.5
MIN_UI_CONTRAST = 3.0

FONT_STACK = (
    'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif'
)
MONO_STACK = (
    'ui-monospace, "Cascadia Mono", "Segoe UI Mono", Consolas, "Liberation Mono", monospace'
)


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    h = colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colours (1 to 21)."""
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _variables(theme: str) -> str:
    return "".join(
        f"--fx-{name.replace('_', '-')}: {value};" for name, value in TOKENS[theme].items()
    )


# The rules. Built from variables only, so one sheet serves both themes.
_RULES = """
.stApp, .stApp p, .stApp label, .stApp li, .stApp button, .stApp input, .stApp textarea {
  font-family: __FONT__;
}
.stApp { background: var(--fx-bg); color: var(--fx-text); }
.block-container {
  max-width: 64rem; padding: 1.25rem 16px 4rem 16px; width: 100%; box-sizing: border-box;
}
section[data-testid="stSidebar"] { background: var(--fx-surface); }
a, button, input, textarea, select, [role="tab"], [tabindex] { outline-offset: 2px; }
a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible,
select:focus-visible, [role="tab"]:focus-visible, [tabindex]:focus-visible {
  outline: 3px solid var(--fx-focus) !important;
}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p {
  color: var(--fx-muted);
}
.fx-brand { font-weight: 700; font-size: 1.15rem; color: var(--fx-primary); margin: 0 0 .5rem 0; }
.fx-muted { color: var(--fx-muted); font-size: .9rem; }
.fx-card {
  background: var(--fx-surface); border: 1px solid var(--fx-border); border-radius: 12px;
  padding: 14px 16px; margin: 8px 0; max-width: 100%; box-sizing: border-box;
}
.fx-who { font-size: .8rem; font-weight: 600; color: var(--fx-muted); margin-bottom: 4px; }
.fx-text {
  white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; line-height: 1.6;
  max-width: 72ch; margin: 0;
}
.fx-footnote {
  color: var(--fx-muted); font-size: .85rem; border-top: 1px solid var(--fx-border);
  margin-top: 10px; padding-top: 8px; white-space: pre-wrap; overflow-wrap: anywhere;
}
.fx-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.fx-chip {
  display: inline-block; max-width: 100%; overflow-wrap: anywhere; font-family: __MONO__;
  font-size: .8rem; padding: 2px 8px; border-radius: 999px; background: var(--fx-info-bg);
  color: var(--fx-info-text); border: 1px solid var(--fx-border);
}
.fx-chip-warn { background: var(--fx-warn-bg); color: var(--fx-warn-text); }
.fx-chip-ok { background: var(--fx-ok-bg); color: var(--fx-ok-text); }
.fx-notice {
  border-radius: 10px; padding: 10px 14px; margin: 8px 0; border: 1px solid var(--fx-border);
  overflow-wrap: anywhere; max-width: 100%; box-sizing: border-box;
}
.fx-notice b { display: block; margin-bottom: 2px; }
.fx-notice-info { background: var(--fx-info-bg); color: var(--fx-info-text); }
.fx-notice-warn { background: var(--fx-warn-bg); color: var(--fx-warn-text); }
.fx-notice-danger { background: var(--fx-danger-bg); color: var(--fx-danger-text); }
.fx-notice-ok { background: var(--fx-ok-bg); color: var(--fx-ok-text); }
.fx-wait { display: flex; gap: 10px; align-items: center; }
.fx-pulse {
  width: 10px; height: 10px; border-radius: 50%; background: var(--fx-primary);
  animation: fxpulse 1.2s ease-in-out infinite;
}
@keyframes fxpulse { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .fx-pulse { animation: none; } }
@media (max-width: 480px) {
  .block-container { padding-left: 16px; padding-right: 16px; }
  .fx-card { padding: 12px; }
  .fx-text { max-width: 100%; }
}
"""


def stylesheet(theme: str) -> str:
    rules = _RULES.replace("__FONT__", FONT_STACK).replace("__MONO__", MONO_STACK)
    return f":root {{ {_variables(theme)} }}\n{rules}"


def current_theme() -> str:
    """`light` or `dark`, as the viewer's Streamlit theme says (light when it cannot be told)."""
    try:
        kind = st.context.theme.type
    except Exception:
        return "light"
    return "dark" if kind == "dark" else "light"


def apply() -> None:
    """Put the stylesheet on the page. Static text built from constants only."""
    st.markdown(f"<style>{stylesheet(current_theme())}</style>", unsafe_allow_html=True)
