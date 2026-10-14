"""Every place SQL text is built from parts is on the reviewed list (Phase 16, bandit B608).

The accepted-findings baseline accepts B608 as a class only because each site was read and puts
nothing but a name, a run of placeholders or a fixed fragment into the text. A new site is not
accepted until it is read and added to `docs/sql_sites.json`."""

import json
from pathlib import Path

import pytest

from scripts import sql_sites

ROOT = Path(__file__).resolve().parents[1]
LIST = json.loads((ROOT / "docs" / "sql_sites.json").read_text("utf-8"))["sites"]


def test_every_site_in_the_code_is_on_the_reviewed_list_and_nothing_else_is():
    sites = sql_sites.all_sites()
    assert sql_sites.new_sites(sites, LIST) == [], "read the new site, then add it to the list"
    assert sql_sites.new_sites(LIST, sites) == [], "a listed site no longer exists: remove it"
    assert len(sites) == len(LIST)


def test_the_list_agrees_with_what_the_static_scan_accepted():
    baseline = json.loads((ROOT / ".bandit-baseline.json").read_text("utf-8"))["results"]
    accepted = sorted(r["filename"] for r in baseline if r["test_id"] == "B608")
    assert accepted == sorted(s["file"] for s in LIST)


def test_every_entry_says_what_is_interpolated_and_why_it_is_safe():
    for site in LIST:
        assert site["interpolated"] and site["what"] and site["reviewed"], site["file"]
        assert len(site["what"]) >= 1
        for note in site["what"]:
            assert not note.lower().startswith("a request value"), site


def test_a_new_site_is_found_whatever_its_line_and_a_moved_one_is_not(tmp_path):
    code = tmp_path / "app" / "x.py"
    code.parent.mkdir()
    code.write_text(
        "def f(db, name, user_input):\n"
        '    db.execute(f"SELECT {name} FROM users WHERE id = ?", (1,))\n'
        '    db.execute("DELETE FROM sessions WHERE id = " + user_input)\n',
        "utf-8",
    )
    found = sql_sites.all_sites(tmp_path, ("app",))
    assert [s["interpolated"] for s in found] == [["name"], ["user_input"]]
    reviewed = [dict(found[0])]
    fresh = sql_sites.new_sites(found, reviewed)
    assert [s["interpolated"] for s in fresh] == [["user_input"]]
    # the same site with a blank line above it (a different line number) is still the same site
    code.write_text("\n\n" + code.read_text("utf-8"), "utf-8")
    moved = sql_sites.all_sites(tmp_path, ("app",))
    assert sql_sites.new_sites([moved[0]], reviewed) == []


def test_plain_sql_with_only_placeholders_is_not_a_site(tmp_path):
    code = tmp_path / "app" / "y.py"
    code.parent.mkdir()
    code.write_text(
        'def g(db):\n    db.query_all("SELECT id FROM users WHERE name = ?", ("x",))\n'
        '    return f"hello {db}"\n',
        "utf-8",
    )
    assert sql_sites.all_sites(tmp_path, ("app",)) == []


@pytest.mark.parametrize(
    "line",
    [
        'x = "INSERT INTO t (a) VALUES (" + value + ")"',
        'x = "UPDATE t SET a = %s" % value',
        'x = "SELECT * FROM t WHERE a = {}".format(value)',
    ],
)
def test_the_other_ways_of_building_a_statement_are_found(tmp_path, line):
    code = tmp_path / "app" / "z.py"
    code.parent.mkdir()
    code.write_text(f"value = 1\n{line}\n", "utf-8")
    assert len(sql_sites.all_sites(tmp_path, ("app",))) == 1
