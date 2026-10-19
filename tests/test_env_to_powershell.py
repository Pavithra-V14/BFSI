"""The one-step loader of the owner's env file into a PowerShell window
(scripts/env_to_powershell.py): the values go only into the pipe, never to the screen, and only
the names listed in .env.example load."""

import pytest

from scripts import env_to_powershell as tool

SECRET = "sk-" + "test" + "-secret-value-0123456789"


def files(tmp_path, body, example="A_KEY=\nB_URL=\nC_PLAIN=\nQUOTED=\nEMPTY=\nSET_ALREADY=\n"):
    env = tmp_path / "settings.txt"  # the tests never touch the real file
    env.write_text(body, "utf-8")
    sample = tmp_path / "example.txt"
    sample.write_text(example, "utf-8")
    return env, sample


def run(tmp_path, body, argv=("--emit",), environ=None, screen=False):
    env, sample = files(tmp_path, body)
    lines, errors = [], []
    code = tool.main(
        list(argv),
        {} if environ is None else environ,
        out=lines.append,
        err=errors.append,
        stdout_is_screen=lambda: screen,
        env_path=env,
        example_path=sample,
    )
    return code, "".join(lines), "\n".join(errors)


BODY = (
    "# a comment\n"
    f"A_KEY={SECRET}\n"
    "B_URL=https://db.example/x?a=1&b=2#frag\n"
    "export C_PLAIN=plain value with spaces\n"
    'QUOTED="it\'s quoted"\n'
    "EMPTY=\n"
    "NOT_LISTED=should-not-load\n"
    "SET_ALREADY=from-the-file\n"
)


def test_it_prints_one_powershell_line_per_variable_to_the_pipe(tmp_path):
    code, out, err = run(tmp_path, BODY, environ={"SET_ALREADY": "from-the-window"})
    assert code == 0
    assert f"$env:A_KEY = '{SECRET}'\n" in out
    assert "$env:B_URL = 'https://db.example/x?a=1&b=2#frag'\n" in out
    assert "$env:C_PLAIN = 'plain value with spaces'\n" in out
    assert "$env:QUOTED = 'it''s quoted'\n" in out  # a quote is doubled, nothing is interpreted
    assert "EMPTY" not in out and "NOT_LISTED" not in out
    assert "SET_ALREADY" not in out  # the window wins over the file


def test_without_emit_nothing_with_a_value_is_ever_printed_even_into_a_pipe(tmp_path):
    for argv in ((), ("--list",)):
        code, out, err = run(tmp_path, BODY, argv=argv)
        assert out == "" and SECRET not in err
        assert code == (2 if not argv else 0)


def test_the_screen_message_holds_names_and_never_a_value(tmp_path):
    _, _, err = run(tmp_path, BODY, environ={"SET_ALREADY": "x"})
    assert "loaded 4 variable(s)" in err and "A_KEY" in err and "B_URL" in err
    for value in (SECRET, "db.example", "plain value", "quoted", "from-the-file"):
        assert value not in err


def test_it_refuses_to_print_values_to_the_screen(tmp_path):
    code, out, err = run(tmp_path, BODY, screen=True)
    assert code == 2 and out == ""
    assert "Invoke-Expression" in err and SECRET not in err


def test_list_shows_names_only_and_works_on_the_screen(tmp_path):
    code, out, err = run(tmp_path, BODY, argv=["--list"], screen=True)
    assert code == 0 and out == ""
    assert "would load: A_KEY, B_URL, C_PLAIN, QUOTED" in err and SECRET not in err


def test_clear_removes_every_listed_name_and_needs_no_values(tmp_path):
    code, out, _ = run(tmp_path, BODY, argv=["--clear"], screen=True)
    assert code == 0 and SECRET not in out
    assert "Remove-Item Env:A_KEY -ErrorAction SilentlyContinue\n" in out
    assert out.count("Remove-Item Env:") == 6
    assert "NOT_LISTED" not in out


def test_a_value_with_a_non_ascii_character_or_a_control_character_is_skipped_by_name(tmp_path):
    body = "A_KEY=café\nB_URL=fine\nC_PLAIN=tab\there\n"
    code, out, err = run(tmp_path, body)
    assert code == 0 and out == "$env:B_URL = 'fine'\n"
    assert "C_PLAIN" in err
    assert "A_KEY" in err and "skipped" in err and "caf" not in err


def test_a_missing_file_is_said_plainly(tmp_path):
    lines, errors = [], []
    code = tool.main(
        [], {}, out=lines.append, err=errors.append, stdout_is_screen=lambda: False,
        env_path=tmp_path / "nope.txt", example_path=tmp_path / "example.txt",
    )  # fmt: skip
    assert code == 1 and lines == [] and "no .env file found" in errors[0]


def test_nothing_is_written_anywhere_but_the_pipe(tmp_path):
    files(tmp_path, BODY)
    before = sorted(p.name for p in tmp_path.iterdir())
    run(tmp_path, BODY)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    source = open(tool.__file__, encoding="utf-8").read()
    assert "open(" not in source.replace("open(tool", "") and "write_text" not in source


@pytest.mark.parametrize("value", ["a'b", "''", "$env:X", "`n", "x;y"])
def test_every_value_is_quoted_so_that_powershell_does_not_interpret_it(value):
    assert tool.powershell_quote(value) == "'" + value.replace("'", "''") + "'"
