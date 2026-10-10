"""The frontend's rules that are about its source and its settings, not its behaviour: it imports
nothing from `app/`, nothing from the API reaches the page as markup, nothing is logged, the
colours have enough contrast in both themes, and the service installs only its own packages."""

import ast
import re
import tomllib
from pathlib import Path

import pytest

from frontend import theme

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
FILES = sorted(FRONTEND.rglob("*.py"))


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text("utf-8"))


def calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


# ============================================================================ imports ====
def test_the_frontend_has_files_to_check():
    assert len(FILES) >= 10


def test_the_frontend_imports_nothing_from_the_api_or_its_libraries():
    forbidden = (
        "app", "torch", "spacy", "presidio_analyzer", "presidio_anonymizer", "unstructured",
        "litellm", "qdrant_client", "psycopg", "redis", "fastapi", "langgraph", "transformers",
    )  # fmt: skip
    for path in FILES:
        for node in ast.walk(parse(path)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert name.split(".")[0] not in forbidden, (path.name, name)


def test_the_service_installs_only_streamlit_and_httpx():
    lines = [
        ln.strip()
        for ln in (ROOT / "requirements-frontend.txt").read_text("utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    names = sorted(re.split(r"[=<>~! ]", ln)[0].lower() for ln in lines)
    assert names == ["httpx", "streamlit"]
    assert all("==" in ln for ln in lines)  # exact pins, like the other files


def test_the_pins_match_the_runtime_and_dev_files():
    def pin(path: str, name: str) -> str:
        for line in (ROOT / path).read_text("utf-8").splitlines():
            if line.lower().startswith(name + "=="):
                return line.strip()
        raise AssertionError(f"{name} is not pinned in {path}")

    assert pin("requirements-frontend.txt", "httpx") == pin("requirements.txt", "httpx")
    assert pin("requirements-frontend.txt", "streamlit") == pin("requirements-dev.txt", "streamlit")


def test_streamlit_is_not_in_the_api_runtime_file():
    runtime = (ROOT / "requirements.txt").read_text("utf-8").lower()
    assert "streamlit" not in runtime


# ============================================================ markup, logging, labels ====
def test_unsafe_html_is_used_in_one_place_only_for_the_static_stylesheet():
    uses = []
    for path in FILES:
        for call in calls(parse(path)):
            for keyword in call.keywords:
                if keyword.arg == "unsafe_allow_html":
                    uses.append((path.name, ast.unparse(call.args[0]) if call.args else ""))
    assert len(uses) == 1, uses
    name, argument = uses[0]
    assert name == "theme.py" and "stylesheet(" in argument and "<style>" in argument


def test_streamlit_markdown_is_never_used_outside_the_theme():
    for path in FILES:
        if path.name == "theme.py":
            continue
        for call in calls(parse(path)):
            assert dotted(call.func) not in ("st.markdown", "st.write", "st.text"), path.name


def test_model_and_api_text_goes_only_through_the_escaping_fragments():
    """`st.html` is called with a fragment from `render` only: a variable named `fragment`, or a
    call of one of render's own builders, never a raw string from the API."""
    allowed = {"fragment"}
    for path in FILES:
        for call in calls(parse(path)):
            if dotted(call.func) in ("st.html", "placeholder.html"):
                arg = call.args[0]
                ok = (isinstance(arg, ast.Name) and arg.id in allowed) or (
                    isinstance(arg, ast.Call) and dotted(arg.func).startswith("render.")
                )
                assert ok, (path.name, ast.unparse(arg))


def test_nothing_is_printed_or_logged_by_the_frontend():
    for path in FILES:
        tree = parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name != "logging" for a in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != "logging", path.name
        for call in calls(tree):
            assert dotted(call.func) not in ("print", "pprint", "st.write_stream"), path.name
            assert not dotted(call.func).startswith(("logging.", "logger.", "log.")), path.name


def test_every_button_has_a_text_label_not_only_an_icon():
    seen = 0
    for path in FILES:
        for call in calls(parse(path)):
            if dotted(call.func).split(".")[-1] not in ("button", "form_submit_button"):
                continue
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                seen += 1
                assert re.search(r"[A-Za-z]{3,}", first.value), (path.name, first.value)
    assert seen > 10


def test_no_label_is_built_from_an_unchecked_api_value():
    """A button label is markdown. A label that is not a literal must come from `label_text`."""
    for path in FILES:
        for call in calls(parse(path)):
            if dotted(call.func).split(".")[-1] != "button" or not call.args:
                continue
            first = call.args[0]
            if isinstance(first, ast.Constant):
                continue
            if isinstance(first, ast.IfExp) and all(
                isinstance(n, ast.Constant) for n in (first.body, first.orelse)
            ):
                continue  # a choice between two literal words
            text = ast.unparse(first)
            assert "label_text(" in text or text in ("label",), (path.name, text)


# ============================================================================ contrast ====
def test_the_contrast_function_matches_known_values():
    assert round(theme.contrast_ratio("#000000", "#FFFFFF"), 2) == 21.0
    assert round(theme.contrast_ratio("#777777", "#777777"), 2) == 1.0
    assert round(theme.contrast_ratio("#767676", "#FFFFFF"), 1) == 4.5  # the WCAG textbook case


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_text_and_background_pair_has_at_least_4_5_to_1(mode):
    tokens = theme.TOKENS[mode]
    for foreground, background in theme.TEXT_PAIRS:
        ratio = theme.contrast_ratio(tokens[foreground], tokens[background])
        assert ratio >= theme.MIN_TEXT_CONTRAST, (mode, foreground, background, round(ratio, 2))


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_focus_ring_stands_out_from_the_page_and_the_cards(mode):
    tokens = theme.TOKENS[mode]
    for foreground, background in theme.UI_PAIRS:
        assert theme.contrast_ratio(tokens[foreground], tokens[background]) >= 3.0


def test_both_themes_define_the_same_tokens_and_every_pair_uses_a_defined_one():
    assert set(theme.TOKENS["light"]) == set(theme.TOKENS["dark"])
    names = set(theme.TOKENS["light"])
    for pair in theme.TEXT_PAIRS + theme.UI_PAIRS:
        assert set(pair) <= names


def test_every_text_colour_the_stylesheet_uses_is_in_a_checked_pair():
    sheet = theme.stylesheet("light")
    used = set(re.findall(r"(?<![\w-])color:\s*var\(--fx-([a-z-]+)\)", sheet))
    checked = {a.replace("_", "-") for a, _ in theme.TEXT_PAIRS}
    assert used <= checked, used - checked


# ===================================================================== the stylesheet ====
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_stylesheet_has_a_visible_focus_ring_and_a_16px_gutter(mode):
    sheet = theme.stylesheet(mode)
    assert ":focus-visible" in sheet and "outline: 3px solid var(--fx-focus)" in sheet
    assert re.search(r"\.block-container\s*\{[^}]*padding:[^;}]*16px", sheet)
    assert "@media (max-width: 480px)" in sheet and "padding-left: 16px" in sheet


def test_text_wraps_and_nothing_has_a_fixed_width_wider_than_a_phone():
    sheet = theme.stylesheet("light")
    assert "overflow-wrap: anywhere" in sheet
    for value in re.findall(r"(?<![-\w])width:\s*(\d+)px", sheet):
        assert int(value) <= 328, value  # 360 px phone minus two 16 px gutters
    for value in re.findall(r"min-width:\s*(\d+)px", sheet):
        assert int(value) <= 328


def test_the_stylesheet_loads_nothing_from_another_site():
    sheet = theme.stylesheet("dark")
    assert "@import" not in sheet and "url(" not in sheet and "http" not in sheet


def test_motion_is_switched_off_for_people_who_ask():
    assert "prefers-reduced-motion: reduce" in theme.stylesheet("light")


def test_the_font_is_the_system_stack():
    assert "system-ui" in theme.FONT_STACK and "Segoe UI" in theme.FONT_STACK


# =================================================================== streamlit settings ====
CONFIG = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text("utf-8"))


def test_the_streamlit_settings_protect_the_service():
    assert CONFIG["server"]["enableXsrfProtection"] is True
    assert CONFIG["browser"]["gatherUsageStats"] is False
    assert CONFIG["client"]["showErrorDetails"] == "none"


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_the_streamlit_theme_colours_equal_the_token_colours(mode):
    tokens = theme.TOKENS[mode]
    section = CONFIG["theme"][mode]
    assert section["primaryColor"].upper() == tokens["primary"].upper()
    assert section["backgroundColor"].upper() == tokens["bg"].upper()
    assert section["secondaryBackgroundColor"].upper() == tokens["surface"].upper()
    assert section["textColor"].upper() == tokens["text"].upper()


def test_the_settings_file_holds_no_secret():
    lines = (ROOT / ".streamlit" / "config.toml").read_text("utf-8").lower().splitlines()
    settings = " ".join(ln for ln in lines if not ln.strip().startswith("#"))
    for word in ("password", "secret", "token", "api_key", "apikey"):
        assert word not in settings
