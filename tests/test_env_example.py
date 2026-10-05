""".env.example holds names and comments only, each tagged with where it is used."""

import re
from pathlib import Path

from app.isolation import URL_VARIABLES

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
WHERE = re.compile(r"^# Where: (Render and local|admin machine only)$")
VARIABLE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
# Set by the CI service container only, so it is not part of the owner's template.
CI_ONLY = {"TEST_POSTGRES_URL"}


def _lines():
    return ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()


def test_every_variable_is_empty():
    for line in _lines():
        match = VARIABLE.match(line)
        if match:
            assert match.group(2) == "", f"{match.group(1)} must have no value"


def test_every_variable_has_a_comment_and_a_where_tag():
    lines = _lines()
    for index, line in enumerate(lines):
        match = VARIABLE.match(line)
        if not match:
            continue
        block = []
        cursor = index - 1
        # Walk up through the variables of the same group, then the comment block.
        while cursor >= 0 and (VARIABLE.match(lines[cursor]) or lines[cursor].startswith("#")):
            if not VARIABLE.match(lines[cursor]):
                block.append(lines[cursor])
            elif block:
                break
            cursor -= 1
        assert block, f"{match.group(1)} has no comment above it"
        assert any(WHERE.match(text) for text in block), f"{match.group(1)} has no Where tag"


def test_every_guarded_url_variable_is_documented():
    names = {m.group(1) for m in map(VARIABLE.match, _lines()) if m}
    assert set(URL_VARIABLES) - CI_ONLY <= names


def test_names_are_unique():
    names = [m.group(1) for m in map(VARIABLE.match, _lines()) if m]
    assert len(names) == len(set(names))
