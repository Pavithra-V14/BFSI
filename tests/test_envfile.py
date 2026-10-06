"""The tiny .env loader used by scripts the owner runs by hand.

Every file and value here is made up at run time in a temporary folder. The real .env
is never opened.
"""

import secrets
from pathlib import Path

from app.gateway.config import ENV_NAMES
from scripts.envfile import example_names, load_env_file

ROOT = Path(__file__).resolve().parents[1]


def made_up(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def write_file(folder: Path, text: str, name: str = "sample.env") -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_only_allowed_names_and_returns_names_not_values(tmp_path, capsys):
    one, two, other = made_up("one"), made_up("two"), made_up("other")
    path = write_file(tmp_path, f"ALPHA_ONE={one}\nBETA_TWO={two}\nOTHER_NAME={other}\n")
    environ: dict[str, str] = {}
    names = load_env_file(path, {"ALPHA_ONE", "BETA_TWO"}, environ)
    assert names == ["ALPHA_ONE", "BETA_TWO"]
    assert environ == {"ALPHA_ONE": one, "BETA_TWO": two}  # OTHER_NAME was not allowed
    assert one not in repr(names) and two not in repr(names)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""  # it never prints anything


def test_parsing_rules(tmp_path):
    plain, quoted, single = made_up("plain"), made_up("quoted"), made_up("single")
    equals = made_up("a") + "=" + made_up("b")  # a value may contain '='
    text = (
        "﻿# a comment line\n"
        "\n"
        f"  ALPHA_ONE = {plain}  \n"
        f'export BETA_TWO="{quoted}"\n'
        f"GAMMA_THREE='{single}'\n"
        f"DELTA_FOUR={equals}\n"
        "EMPTY_ONE=\n"
        "lowercase_name=ignored\n"
        "NO_EQUALS_SIGN\n"
        "NOT A NAME=ignored\n"
    )
    path = tmp_path / "sample.env"
    path.write_bytes(text.encode("utf-8"))  # includes a byte-order mark
    allowed = {"ALPHA_ONE", "BETA_TWO", "GAMMA_THREE", "DELTA_FOUR", "EMPTY_ONE", "lowercase_name"}
    environ: dict[str, str] = {}
    names = load_env_file(path, allowed, environ)
    assert environ == {
        "ALPHA_ONE": plain,
        "BETA_TWO": quoted,
        "GAMMA_THREE": single,
        "DELTA_FOUR": equals,
    }
    assert names == ["ALPHA_ONE", "BETA_TWO", "DELTA_FOUR", "GAMMA_THREE"]


def test_a_variable_already_set_in_the_shell_wins(tmp_path):
    from_file, from_shell = made_up("file"), made_up("shell")
    path = write_file(tmp_path, f"ALPHA_ONE={from_file}\nBETA_TWO={from_file}\n")
    environ = {"ALPHA_ONE": from_shell}
    names = load_env_file(path, {"ALPHA_ONE", "BETA_TWO"}, environ)
    assert environ["ALPHA_ONE"] == from_shell and environ["BETA_TWO"] == from_file
    assert names == ["BETA_TWO"]
    environ = {"ALPHA_ONE": ""}  # an empty shell value does not count as set
    load_env_file(path, {"ALPHA_ONE"}, environ)
    assert environ["ALPHA_ONE"] == from_file


def test_it_never_writes_anything(tmp_path):
    value = made_up("secret")
    path = write_file(tmp_path, f"ALPHA_ONE={value}\n")
    before = path.read_bytes()
    listing = sorted(p.name for p in tmp_path.iterdir())
    environ: dict[str, str] = {}
    load_env_file(path, {"ALPHA_ONE"}, environ)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == listing


def test_the_returned_list_and_the_environment_mapping_are_the_only_outputs(tmp_path):
    value = made_up("value")
    path = write_file(tmp_path, f"ALPHA_ONE={value}\n")
    result = load_env_file(path, {"ALPHA_ONE"}, {})
    assert isinstance(result, list) and all(isinstance(n, str) for n in result)
    assert value not in "".join(result)


def test_example_names_reads_names_only(tmp_path):
    path = write_file(
        tmp_path,
        "# comment\nALPHA_ONE=\n\n# another\nBETA_TWO=\nnot_a_name=\n  INDENTED=\n",
        name="sample.example",
    )
    assert example_names(path) == {"ALPHA_ONE", "BETA_TWO"}


def test_every_gateway_variable_can_be_loaded_from_the_real_example_file():
    names = example_names(ROOT / ".env.example")  # the template holds names only
    assert set(ENV_NAMES) <= names
    assert "DATABASE_URL" in names and "DATABASE_URL" not in set(ENV_NAMES)
