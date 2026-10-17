"""The sidebar's "Dark mode" switch: it starts from Streamlit's own theme, it redraws the page's
colours with the dark or light tokens, and it adds the rules for Streamlit's own widgets only when
the page is drawn in a different theme from the one Streamlit shows."""

from frontend import theme
from tests.frontend_fixtures import FakeApi, signed_in

DARK_BG = "--fx-bg: " + theme.TOKENS["dark"]["bg"]
LIGHT_BG = "--fx-bg: " + theme.TOKENS["light"]["bg"]
NATIVE_MARK = "filter: invert(1) hue-rotate(180deg)"


def stylesheet_of(at) -> str:
    return " ".join(str(m.value) for m in at.markdown if "<style>" in str(m.value))


def page(monkeypatch):
    at = signed_in(FakeApi(), monkeypatch).run()
    assert not at.exception, at.exception
    return at


# ---- the sheet -----------------------------------------------------------------------------
def test_the_native_rules_are_added_only_when_the_two_themes_differ():
    assert NATIVE_MARK not in theme.stylesheet("dark")
    assert NATIVE_MARK not in theme.stylesheet("dark", native="dark")
    assert NATIVE_MARK not in theme.stylesheet("light", native="light")
    assert NATIVE_MARK in theme.stylesheet("dark", native="light")
    assert NATIVE_MARK in theme.stylesheet("light", native="dark")


def test_the_native_rules_use_the_same_tokens_as_the_rest_of_the_sheet():
    sheet = theme.stylesheet("dark", native="light")
    assert theme.TOKENS["dark"]["bg"] in sheet and theme.TOKENS["dark"]["surface"] in sheet
    rules = sheet.split(theme._NATIVE_RULES.strip().splitlines()[0], 1)[1]
    assert "#" not in rules.replace("#1", "")  # colours come from variables, not literals


# ---- the choice ----------------------------------------------------------------------------
def test_without_the_switch_the_page_follows_streamlits_own_theme(monkeypatch):
    monkeypatch.setattr(theme, "current_theme", lambda: "dark")
    assert theme.chosen_theme() == "dark"
    monkeypatch.setattr(theme, "current_theme", lambda: "light")
    assert theme.chosen_theme() == "light"


def test_the_switch_wins_over_streamlits_theme(monkeypatch):
    import streamlit as st

    monkeypatch.setattr(theme, "current_theme", lambda: "light")
    monkeypatch.setattr(st, "session_state", {theme.DARK_KEY: True})
    assert theme.chosen_theme() == "dark"
    monkeypatch.setattr(st, "session_state", {theme.DARK_KEY: False})
    monkeypatch.setattr(theme, "current_theme", lambda: "dark")
    assert theme.chosen_theme() == "light"


# ---- the page ------------------------------------------------------------------------------
def test_the_sidebar_has_a_dark_mode_switch_that_starts_from_the_native_theme(monkeypatch):
    at = page(monkeypatch)
    assert [t.label for t in at.toggle] == ["Dark mode"]
    assert at.toggle[0].value is False  # Streamlit shows the light theme here
    assert LIGHT_BG in stylesheet_of(at) and DARK_BG not in stylesheet_of(at)


def test_switching_it_on_draws_the_page_in_the_dark_tokens_and_repaints_the_widgets(monkeypatch):
    at = page(monkeypatch)
    at.toggle[0].set_value(True).run()
    assert not at.exception, at.exception
    sheet = stylesheet_of(at)
    assert DARK_BG in sheet and LIGHT_BG not in sheet
    assert (
        NATIVE_MARK in sheet
    )  # Streamlit shows light, the page is dark: the widgets are repainted


def test_switching_it_back_off_returns_to_the_light_tokens_without_the_repaint(monkeypatch):
    at = page(monkeypatch)
    at.toggle[0].set_value(True).run()
    at.toggle[0].set_value(False).run()
    sheet = stylesheet_of(at)
    assert LIGHT_BG in sheet and DARK_BG not in sheet and NATIVE_MARK not in sheet


def test_the_switch_is_shown_to_an_admin_too_and_survives_a_screen_change(monkeypatch):
    api = FakeApi()
    at = signed_in(api, monkeypatch, role="admin", username="boss").run()
    at.toggle[0].set_value(True).run()
    at.radio[0].set_value("Alerts").run()
    assert not at.exception, at.exception
    assert at.toggle[0].value is True and DARK_BG in stylesheet_of(at)
