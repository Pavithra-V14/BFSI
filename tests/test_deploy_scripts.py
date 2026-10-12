"""The owner-run scripts of Phase 15: snapshots, key inventory, connection count, cost projection,
release records. None of them calls a real service in these tests (fakes only), none prints a key,
and none changes anything without its flag."""

import json
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts import (
    cost_projection,
    db_connections,
    key_inventory,
    qdrant_snapshot,
    release_record,
)


def made_up(prefix=""):
    return prefix + secrets.token_urlsafe(30)


class Lines(list):
    def __call__(self, text):
        self.append(str(text))

    @property
    def text(self):
        return "\n".join(self)


QDRANT_ENV = {"QDRANT_URL": "https://q.example.test:6333", "QDRANT_API_KEY": made_up()}


# ================================================================================ snapshots
class Refused(Exception):
    """What qdrant-client raises for a 403: an exception that carries the status code."""

    status_code = 403


class FakeQdrant:
    def __init__(self, names=("a.snapshot", "b.snapshot", "c.snapshot"), refuse=False):
        self.items = [
            SimpleNamespace(name=n, size=100 + i, creation_time=f"2026-10-1{i}T00:00:00")
            for i, n in enumerate(names)
        ]
        self.created = 0
        self.deleted: list[str] = []
        self.refuse = refuse

    def list_snapshots(self, collection):
        assert collection == "documents"
        return list(self.items)

    def create_snapshot(self, collection):
        if self.refuse:
            raise Refused("the plan does not include snapshots")
        self.created += 1
        return SimpleNamespace(name="new.snapshot", size=1, creation_time="2026-10-20T00:00:00")

    def delete_snapshot(self, collection, name):
        self.deleted.append(name)


def run_snapshot(args, client=None, env=None, folder=None):
    out = Lines()
    code = qdrant_snapshot.main(
        args, QDRANT_ENV if env is None else env, out=out, client=client, folder=folder
    )
    return code, out


def test_listing_changes_nothing_and_prints_names_sizes_times():
    client = FakeQdrant()
    code, out = run_snapshot(["list"], client)
    assert code == 0 and "3 snapshot(s)" in out.text and "a.snapshot" in out.text
    assert client.created == 0 and client.deleted == []


def test_create_and_prune_and_download_need_the_run_flag():
    client = FakeQdrant()
    for args in (["create"], ["prune", "--keep", "1"], ["download", "a.snapshot"]):
        code, out = run_snapshot(args, client)
        assert code == 0 and "add --run" in out.text
    assert client.created == 0 and client.deleted == []


def test_create_with_run_makes_one_snapshot():
    client = FakeQdrant()
    code, out = run_snapshot(["create", "--run"], client)
    assert code == 0 and client.created == 1 and "created new.snapshot" in out.text


def test_prune_keeps_the_newest_and_deletes_the_older_ones():
    client = FakeQdrant(names=("old1", "old2", "new1", "new2"))
    code, out = run_snapshot(["prune", "--keep", "2", "--run"], client)
    assert code == 0 and client.deleted == ["old1", "old2"]
    code, _ = run_snapshot(["prune", "--keep", "0", "--run"], FakeQdrant())  # never keeps zero
    assert code == 0


def test_a_server_that_refuses_snapshots_says_so_and_names_the_fallback():
    client = FakeQdrant(refuse=True)
    code, out = run_snapshot(["create", "--run"], client)
    assert code == qdrant_snapshot.REFUSED_EXIT == 2
    assert "re-ingest from originals/" in out.text and "restore-documents.md" in out.text


def test_an_unknown_error_prints_its_class_only():
    class Broken(FakeQdrant):
        def list_snapshots(self, collection):
            raise RuntimeError("connect to https://secret.example.test failed")

    code, out = run_snapshot(["list"], Broken())
    assert code == 1 and "RuntimeError" in out.text and "secret.example" not in out.text


def test_download_takes_a_bare_name_only_and_an_unknown_name_is_refused(tmp_path):
    client = FakeQdrant()
    code, out = run_snapshot(["download", "../../etc/passwd", "--run"], client, folder=tmp_path)
    assert code == 1 and "no such snapshot" in out.text
    assert not list(tmp_path.iterdir())


def test_snapshots_are_refused_on_render_and_without_the_settings():
    code, out = run_snapshot(["list"], FakeQdrant(), env={**QDRANT_ENV, "RENDER": "true"})
    assert code == 1 and "never on Render" in out.text
    out = Lines()
    assert qdrant_snapshot.main(["list"], {}, out=out) == 1
    assert "QDRANT_URL is not set" in out.text


# ============================================================================ key inventory
def test_the_inventory_prints_names_counts_and_fingerprints_never_a_key():
    first, second, jev = made_up(), made_up(), made_up()
    env = {"GROQ_API_KEYS": f"{first}, {second}", "JEV_OPENROUTER_API_KEY": jev}
    out = Lines()
    assert key_inventory.main([], env, out=out) == 0
    for value in (first, second, jev):
        assert value not in out.text
    row = next(line for line in out if line.startswith("GROQ_API_KEYS"))
    assert " | 2 | " in row and len(row.split(" | ")[2].split(", ")) == 2
    unset = next(line for line in out if line.startswith("COHERE_API_KEY"))
    assert " | 0 | - | " in unset


def test_the_same_key_in_two_settings_is_a_warning_and_a_non_zero_exit():
    shared = made_up()
    out = Lines()
    code = key_inventory.main([], {"GROQ_API_KEYS": shared, "COHERE_API_KEY": shared}, out=out)
    assert code == 1 and "WARNING: the same key" in out.text
    assert "GROQ_API_KEYS[1]" in out.text and "COHERE_API_KEY[1]" in out.text


def test_evaluation_keys_are_shown_only_with_all_and_the_note_about_render_appears():
    env = {"EVAL_GROQ_API_KEYS": made_up()}
    plain, with_all = Lines(), Lines()
    key_inventory.main([], env, out=plain)
    key_inventory.main(["--all"], env, out=with_all)
    assert "EVAL_GROQ_API_KEYS" not in plain.text and "never be set on Render" in plain.text
    assert "EVAL_GROQ_API_KEYS | 1 |" in with_all.text


def test_a_rotated_key_changes_only_its_own_fingerprint():
    old, new = made_up(), made_up()
    before, after = Lines(), Lines()
    key_inventory.main([], {"COHERE_API_KEY": old, "JINA_API_KEY": "j" * 20}, out=before)
    key_inventory.main([], {"COHERE_API_KEY": new, "JINA_API_KEY": "j" * 20}, out=after)
    pick = lambda lines, name: next(x for x in lines if x.startswith(name))  # noqa: E731
    assert pick(before, "COHERE_API_KEY") != pick(after, "COHERE_API_KEY")
    assert pick(before, "JINA_API_KEY") == pick(after, "JINA_API_KEY")


# =============================================================================== connections
class FakeDatabase:
    def __init__(self, states, limit, fail=False):
        self.states, self.limit, self.fail, self.closed = states, limit, fail, False

    def query_one(self, sql, params=()):
        if self.fail:
            raise RuntimeError("secret detail")
        return (str(self.limit),)

    def query_all(self, sql, params=()):
        return list(self.states)

    def close(self):
        self.closed = True


def test_the_connection_count_prints_numbers_and_the_share():
    database = FakeDatabase([("active", 2), ("idle", 6)], 100)
    out = Lines()
    assert db_connections.main({}, out=out, database=database) == 0
    assert "connections open now: 8 of 100" in out.text and "share of the limit: 8 %" in out.text
    assert "active: 2" in out.text and database.closed


def test_the_count_refuses_sqlite_and_a_missing_address_and_hides_error_text():
    out = Lines()
    assert db_connections.main({"DATABASE_URL": "sqlite:///x.db"}, out=out) == 1
    assert "SQLite" in out.text
    out = Lines()
    assert db_connections.main({}, out=out) == 1 and "not set" in out.text
    out = Lines()
    code = db_connections.main({}, out=out, database=FakeDatabase([], 1, fail=True))
    assert code == 1 and "RuntimeError" in out.text and "secret detail" not in out.text


def test_the_two_queries_are_read_only():
    import inspect

    source = inspect.getsource(db_connections)
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"):
        assert word not in source


# ================================================================================ projection
def test_the_projection_scales_with_the_queries_and_makes_no_call():
    small = cost_projection.project(100)
    large = cost_projection.project(400)
    for who, row in small.items():
        assert large[who]["requests"] == pytest.approx(4 * row["requests"])
        assert large[who]["tokens"] == pytest.approx(4 * row["tokens"])
    assert {"answer", "jev", "embedding", "rerank"} <= set(small)


def test_follow_ups_only_add_the_rewrite_calls():
    none = cost_projection.project(100, share_follow_up=0.0)
    some = cost_projection.project(100, share_follow_up=0.5)
    assert some["answer"]["requests"] > none["answer"]["requests"]
    assert some["jev"] == none["jev"] and some["embedding"] == none["embedding"]


def test_the_shadow_cost_is_positive_and_the_report_says_billed_is_zero():
    out = Lines()
    assert cost_projection.main([], out=out) == 0
    assert "shadow cost" in out.text and "expected $0" in out.text and "[T placeholder]" in out.text
    assert cost_projection.shadow_costs(cost_projection.project(100))["jev"] > 0


def test_the_projection_json_and_a_bad_number():
    out = Lines()
    assert cost_projection.main(["--json"], out=out) == 0
    data = json.loads(out.text)
    assert "day" in data and "shadow_cost_day" in data
    assert cost_projection.main(["--queries-per-day", "0"], out=Lines()) == 1


# ============================================================================ release records
def make_folder(tmp_path):
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / "docs" / "releases" / "TEMPLATE.md"
    ).read_text("utf-8")
    (tmp_path / "TEMPLATE.md").write_text(template, "utf-8")
    return tmp_path


def test_a_record_is_made_from_the_template_with_the_git_facts(tmp_path):
    folder = make_folder(tmp_path)
    out = Lines()
    code = release_record.main(
        [
            "--title",
            "Tighten the refusal wording",
            "--kind",
            "prompt",
            "--approved-by",
            "the admin",
        ],
        out=out,
        folder=folder,
        now=datetime(2026, 10, 12, tzinfo=UTC),
    )
    assert code == 0
    made = folder / "2026-10-12-tighten-the-refusal-wording.md"
    text = made.read_text("utf-8")
    assert "# Release record: Tighten the refusal wording" in text
    assert "yes: always" in text and "the admin" in text and "{{" not in text


def test_a_record_is_never_overwritten_and_a_missing_template_is_reported(tmp_path):
    folder = make_folder(tmp_path)
    when = datetime(2026, 10, 12, tzinfo=UTC)
    args = ["--title", "Same", "--kind", "code"]
    assert release_record.main(args, out=Lines(), folder=folder, now=when) == 0
    out = Lines()
    assert release_record.main(args, out=out, folder=folder, now=when) == 1
    assert "nothing was overwritten" in out.text
    empty = tmp_path / "empty"
    empty.mkdir()
    assert release_record.main(args, out=Lines(), folder=empty, now=when) == 1


def test_a_prompt_change_is_always_recorded_with_an_evaluation():
    assert set(release_record.ALWAYS_RECORDED) == {"prompt", "model", "retrieval", "guardrail"}
    assert release_record.slug("A  Title: with / odd chars!") == "a-title-with-odd-chars"
