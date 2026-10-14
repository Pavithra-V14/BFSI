"""Source hygiene (Phase 16 security scan): no hidden direction-changing character in the code.

"Trojan Source" hides code by putting bidirectional control characters in a source file, so the
text on screen reads differently from what runs. The static scanner (bandit, B613) flags it; this
test keeps it out for good. A program that needs such a character in a pattern writes it as an
escape (`\\u202a`), which is what `frontend/render.py` does.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("app", "scripts", "load", "frontend")
# bidirectional embeddings, overrides, isolates and the marks, zero-width characters, the BOM
HIDDEN = set(
    "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"
)


def hidden_characters(path: Path) -> list[tuple[int, str]]:
    found = []
    text = path.read_text("utf-8")
    for number, line in enumerate(text.split("\n"), start=1):
        for char in line:
            if char in HIDDEN and not (number == 1 and char == "\ufeff"):
                found.append((number, f"U+{ord(char):04X}"))
    return found


def test_no_source_file_holds_a_hidden_direction_or_zero_width_character():
    problems = []
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*.py")):
            for number, code in hidden_characters(path):
                problems.append(f"{path.relative_to(ROOT).as_posix()}:{number} {code}")
    assert not problems, problems


def test_the_scanner_finds_one_and_nothing_else(tmp_path):
    clean = tmp_path / "clean.py"
    clean.write_text('PATTERN = "\\u202a"\nprint("ok")\n', "utf-8")
    assert hidden_characters(clean) == []
    dirty = tmp_path / "dirty.py"
    dirty.write_text('x = 1\ny = "a\u202eb"\nz = "c\u200bd"\n', "utf-8")
    assert hidden_characters(dirty) == [(2, "U+202E"), (3, "U+200B")]


def test_the_frontend_pattern_still_removes_the_characters_it_is_meant_to():
    from frontend import render

    text = "a\u202eb\u200bc\ufeffd\u2066e\x07f"
    assert render.plain(text) == "abcdef"
