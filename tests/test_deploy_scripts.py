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
class StatusError(Exception):
    """What qdrant-client raises for an HTTP error: an exception that carries the status code."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def snap(name, size=100, made="2026-10-10T00:00:00+00:00", checksum=None):
    return SimpleNamespace(name=name, size=size, creation_time=made, checksum=checksum)


class FakeQdrant:
    def __init__(self, items=None, refuse_with=None):
        self.items = (
            list(items)
            if items is not None
            else [
                snap("a.snapshot", 101, "2026-10-10T00:00:00+00:00"),
                snap("b.snapshot", 102, "2026-10-11T00:00:00+00:00"),
                snap("c.snapshot", 103, "2026-10-12T00:00:00+00:00"),
            ]
        )
        self.created = 0
        self.deleted: list[str] = []
        self.refuse_with = refuse_with

    def list_snapshots(self, collection):
        assert collection == "documents"
        if self.refuse_with:
            raise StatusError(self.refuse_with)
        return list(self.items)

    def create_snapshot(self, collection):
        if self.refuse_with:
            raise StatusError(self.refuse_with)
        self.created += 1
        return snap("new.snapshot", 1, "2026-10-20T00:00:00+00:00")

    def delete_snapshot(self, collection, name):
        self.deleted.append(name)


def run_snapshot(args, client=None, env=None, folder=None, fetch=None):
    out = Lines()
    code = qdrant_snapshot.main(
        args,
        QDRANT_ENV if env is None else env,
        out=out,
        client=client,
        folder=folder,
        fetch=fetch,
    )
    return code, out


def test_listing_changes_nothing_and_prints_names_sizes_times_and_whether_a_copy_is_here(tmp_path):
    client = FakeQdrant()
    (tmp_path / "b.snapshot").write_bytes(b"x" * 102)
    code, out = run_snapshot(["list"], client, folder=tmp_path)
    assert code == 0 and "3 snapshot(s)" in out.text
    assert out[1].startswith("  c.snapshot")  # newest first
    assert "b.snapshot  102 bytes" in out.text and "copy here" in out.text
    copy_rows = {line.split()[0]: line.rstrip().split("  ")[-1] for line in out[1:]}
    assert copy_rows == {
        "c.snapshot": "no copy",
        "b.snapshot": "copy here",
        "a.snapshot": "no copy",
    }
    assert client.created == 0 and client.deleted == []


def test_create_and_prune_and_download_need_the_run_flag(tmp_path):
    client = FakeQdrant()
    for args in (["create"], ["prune", "--keep", "1"], ["download", "a.snapshot"]):
        code, out = run_snapshot(args, client, folder=tmp_path)
        assert code == 0 and "add --run" in out.text
    assert client.created == 0 and client.deleted == [] and not list(tmp_path.iterdir())


def test_create_with_run_makes_one_snapshot():
    client = FakeQdrant()
    code, out = run_snapshot(["create", "--run"], client)
    assert code == 0 and client.created == 1 and "created new.snapshot" in out.text


# ---- download: a temporary name, kept only after the size (and checksum) match ---------------
def chunks(data, size=7):
    def fetch(name):
        for i in range(0, len(data), size):
            yield data[i : i + size]

    return fetch


def test_a_good_download_is_renamed_only_at_the_end_and_prints_the_checksum(tmp_path):
    import hashlib

    data = b"snapshot-bytes-" * 10
    client = FakeQdrant([snap("a.snapshot", len(data), checksum=hashlib.sha256(data).hexdigest())])
    code, out = run_snapshot(
        ["download", "a.snapshot", "--run"], client, folder=tmp_path, fetch=chunks(data)
    )
    assert code == 0 and (tmp_path / "a.snapshot").read_bytes() == data
    assert not list(tmp_path.glob("*.part"))
    assert hashlib.sha256(data).hexdigest()[:16] in out.text


def test_the_copy_is_written_under_a_temporary_name_and_renamed_only_at_the_end(tmp_path):
    seen: list[tuple[bool, bool]] = []

    def watching(name):
        for chunk in (b"abc", b"def"):
            yield chunk
            seen.append(
                ((tmp_path / "a.snapshot.part").exists(), (tmp_path / "a.snapshot").exists())
            )

    client = FakeQdrant([snap("a.snapshot", 6)])
    code, _out = run_snapshot(
        ["download", "a.snapshot", "--run"], client, folder=tmp_path, fetch=watching
    )
    assert code == 0 and seen == [(True, False), (True, False)], "never the final name early"
    assert (tmp_path / "a.snapshot").read_bytes() == b"abcdef"
    assert not (tmp_path / "a.snapshot.part").exists()


def test_a_name_with_folders_in_it_is_written_as_its_last_part_only(tmp_path):
    target = tmp_path / "backups"
    made = qdrant_snapshot.Snap("../../evil.snapshot", 3, None, None)
    path, size, _digest = qdrant_snapshot.download(made, target, lambda name: iter([b"abc"]))
    assert path == target / "evil.snapshot" and size == 3
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backups"], "nothing outside the folder"


def test_download_newest_takes_the_newest_by_time_not_the_first_the_server_lists(tmp_path):
    items = [
        snap("middle.snapshot", 3, "2026-10-05T00:00:00Z"),
        snap("newest.snapshot", 3, "2026-10-12T00:00:00Z"),
        snap("oldest.snapshot", 3, "2026-10-01T00:00:00Z"),
        snap("undated.snapshot", 3, ""),
    ]
    code, out = run_snapshot(
        ["download", "--newest", "--run"],
        FakeQdrant(items),
        folder=tmp_path,
        fetch=chunks(b"abc"),
    )
    assert code == 0 and "saved newest.snapshot" in out.text
    assert [p.name for p in tmp_path.iterdir()] == ["newest.snapshot"]


def test_download_needs_a_name_or_newest_but_not_both_and_changes_nothing_without_run(tmp_path):
    client = FakeQdrant()
    for args in (["download", "--run"], ["download", "a.snapshot", "--newest", "--run"]):
        code, out = run_snapshot(args, client, folder=tmp_path)
        assert code == 1 and "name" in out.text and "--newest" in out.text
    code, out = run_snapshot(["download", "--newest"], client, folder=tmp_path)
    assert code == 0 and "add --run to copy c.snapshot" in out.text and not list(tmp_path.iterdir())
    code, out = run_snapshot(
        ["download", "--newest", "--run"], FakeQdrant([snap("x", 1, "")]), folder=tmp_path
    )
    assert code == 1 and "no snapshot with a readable creation time" in out.text


def test_a_truncated_download_leaves_nothing_behind(tmp_path):
    data = b"y" * 100
    client = FakeQdrant([snap("a.snapshot", 100)])
    code, out = run_snapshot(
        ["download", "a.snapshot", "--run"], client, folder=tmp_path, fetch=chunks(data[:60])
    )
    assert code == 1 and "size" in out.text and "nothing was kept" in out.text
    assert not list(tmp_path.iterdir()), "neither the snapshot nor a .part file"


def test_a_download_whose_checksum_differs_is_deleted(tmp_path):
    data = b"z" * 50
    client = FakeQdrant([snap("a.snapshot", 50, checksum="0" * 64)])
    code, out = run_snapshot(
        ["download", "a.snapshot", "--run"], client, folder=tmp_path, fetch=chunks(data)
    )
    assert code == 1 and "checksum" in out.text and not list(tmp_path.iterdir())


def test_a_download_that_fails_half_way_leaves_nothing(tmp_path):
    def broken(name):
        yield b"abc"
        raise ConnectionError("https://secret.example.test dropped")

    client = FakeQdrant([snap("a.snapshot", 100)])
    code, out = run_snapshot(
        ["download", "a.snapshot", "--run"], client, folder=tmp_path, fetch=broken
    )
    assert code == 1 and "ConnectionError" in out.text and "secret.example" not in out.text
    assert not list(tmp_path.iterdir())


def test_download_takes_a_name_from_the_list_only(tmp_path):
    client = FakeQdrant()
    code, out = run_snapshot(
        ["download", "../../etc/passwd", "--run"], client, folder=tmp_path, fetch=chunks(b"")
    )
    assert code == 1 and "no such snapshot" in out.text and not list(tmp_path.iterdir())


# ---- prune: a reliable order, and only what is safe -----------------------------------------
def copy_of(folder, name, size):
    (folder / name).write_bytes(b"c" * size)


def test_prune_deletes_only_older_snapshots_that_have_a_copy_of_the_same_size(tmp_path):
    client = FakeQdrant(
        [
            snap("old1", 11, "2026-10-01T00:00:00Z"),
            snap("old2", 12, "2026-10-02T00:00:00Z"),
            snap("new1", 13, "2026-10-11T00:00:00Z"),
            snap("new2", 14, "2026-10-12T00:00:00Z"),
        ]
    )
    copy_of(tmp_path, "old1", 11)  # the same size: safe
    copy_of(tmp_path, "old2", 5)  # another size: not a confirmed copy
    code, out = run_snapshot(["prune", "--keep", "2", "--run"], client, folder=tmp_path)
    assert code == 0 and client.deleted == ["old1"]
    assert "kept old2: the downloaded copy has another size" in out.text


def test_prune_says_would_be_deleted_without_run_and_deleting_with_it(tmp_path):
    items = [snap("old", 5, "2026-10-01T00:00:00Z"), snap("new", 6, "2026-10-12T00:00:00Z")]
    copy_of(tmp_path, "old", 5)
    dry = FakeQdrant(list(items))
    _code, out = run_snapshot(["prune", "--keep", "1"], dry, folder=tmp_path)
    assert "1 would be deleted" in out.text and "nothing was deleted: add --run" in out.text
    assert dry.deleted == []
    real = FakeQdrant(list(items))
    _code, out = run_snapshot(["prune", "--keep", "1", "--run"], real, folder=tmp_path)
    assert "deleting 1" in out.text and "would be deleted" not in out.text
    assert "deleted old" in out.text and real.deleted == ["old"]


def test_prune_does_not_trust_a_copy_when_the_server_gave_no_size(tmp_path):
    client = FakeQdrant(
        [snap("old", 0, "2026-10-01T00:00:00Z"), snap("new", 6, "2026-10-12T00:00:00Z")]
    )
    (tmp_path / "old").write_bytes(b"")  # an empty file is not a confirmed copy
    code, out = run_snapshot(["prune", "--keep", "1", "--run"], client, folder=tmp_path)
    assert code == 0 and client.deleted == [] and "gave no size" in out.text


def test_prune_keeps_a_snapshot_that_has_no_copy_at_all(tmp_path):
    client = FakeQdrant(
        [snap("old", 5, "2026-10-01T00:00:00Z"), snap("new", 6, "2026-10-12T00:00:00Z")]
    )
    code, out = run_snapshot(["prune", "--keep", "1", "--run"], client, folder=tmp_path)
    assert code == 0 and client.deleted == [] and "kept old: no downloaded copy" in out.text


def test_prune_never_touches_a_snapshot_whose_time_it_cannot_read(tmp_path):
    client = FakeQdrant(
        [
            snap("mystery", 5, ""),
            snap("odd", 5, "not a time"),
            snap("new", 6, "2026-10-12T00:00:00Z"),
        ]
    )
    copy_of(tmp_path, "mystery", 5)
    copy_of(tmp_path, "odd", 5)
    code, out = run_snapshot(["prune", "--keep", "1", "--run"], client, folder=tmp_path)
    assert code == 0 and client.deleted == []
    assert out.text.count("no readable creation time") == 2


def test_prune_never_deletes_the_only_snapshot_or_the_newest_even_with_keep_zero(tmp_path):
    only = FakeQdrant([snap("only", 5, "2026-10-12T00:00:00Z")])
    copy_of(tmp_path, "only", 5)
    run_snapshot(["prune", "--keep", "0", "--run"], only, folder=tmp_path)
    assert only.deleted == []
    two = FakeQdrant([snap("a", 5, "2026-10-01T00:00:00Z"), snap("b", 5, "2026-10-12T00:00:00Z")])
    copy_of(tmp_path, "a", 5)
    copy_of(tmp_path, "b", 5)
    run_snapshot(["prune", "--keep", "0", "--run"], two, folder=tmp_path)
    assert two.deleted == ["a"], "keep is floored at 1: the newest stays"


def test_prune_orders_by_time_not_by_the_order_the_server_lists_them(tmp_path):
    client = FakeQdrant(
        [
            snap("middle", 5, "2026-10-05T00:00:00Z"),
            snap("newest", 5, "2026-10-12T00:00:00Z"),
            snap("oldest", 5, "2026-10-01T00:00:00Z"),
        ]
    )
    for name in ("middle", "newest", "oldest"):
        copy_of(tmp_path, name, 5)
    run_snapshot(["prune", "--keep", "1", "--run"], client, folder=tmp_path)
    assert sorted(client.deleted) == ["middle", "oldest"]


# ---- a rejected key is not "no snapshots on this plan" ---------------------------------------
@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_key_is_reported_as_such_with_its_own_exit_code(status):
    code, out = run_snapshot(["create", "--run"], FakeQdrant(refuse_with=status))
    assert code == qdrant_snapshot.KEY_REJECTED_EXIT == 3
    assert "key was rejected" in out.text and "QDRANT_API_KEY" in out.text
    assert "re-ingest" not in out.text


@pytest.mark.parametrize("status", [404, 405, 501])
def test_a_cluster_without_snapshot_routes_names_the_fallback(status):
    code, out = run_snapshot(["create", "--run"], FakeQdrant(refuse_with=status))
    assert code == qdrant_snapshot.UNSUPPORTED_EXIT == 2
    assert "re-ingest from originals/" in out.text and "restore-documents.md" in out.text


def test_a_key_error_while_downloading_is_also_a_rejected_key(tmp_path):
    def rejected(name):
        raise qdrant_snapshot.KeyRejected
        yield b""  # pragma: no cover

    code, out = run_snapshot(
        ["download", "a.snapshot", "--run"], FakeQdrant(), folder=tmp_path, fetch=rejected
    )
    assert code == 3 and not list(tmp_path.iterdir())


def test_an_unknown_error_prints_its_class_only():
    class Broken(FakeQdrant):
        def list_snapshots(self, collection):
            raise RuntimeError("connect to https://secret.example.test failed")

    code, out = run_snapshot(["list"], Broken())
    assert code == 1 and "RuntimeError" in out.text and "secret.example" not in out.text


# ---- restore-test: a downloaded copy restored into a throw-away collection --------------------
class RestoreQdrant(FakeQdrant):
    """A fake server that knows collections: the live one and, after an upload, the test one."""

    def __init__(self, live_points=10, restored_points=10, existing=(), delete_fails=False):
        super().__init__()
        self.live_points, self.restored_points = live_points, restored_points
        self.collections = {"documents", *existing}
        self.calls: list[tuple[str, str]] = []
        self.delete_fails = delete_fails

    def uploaded(self):
        self.collections.add("documents_restore_test")

    def collection_exists(self, name):
        return name in self.collections

    def count(self, name, exact=True):
        self.calls.append(("count", name))
        points = self.restored_points if name == "documents_restore_test" else self.live_points
        return SimpleNamespace(count=points)

    def delete_collection(self, name):
        self.calls.append(("delete", name))
        if self.delete_fails:
            raise RuntimeError("secret detail https://secret.example.test")
        self.collections.discard(name)


def restore(args, client, folder, upload=None):
    uploads: list[str] = []

    def default_upload(path):
        uploads.append(path.name)
        client.uploaded()

    out = Lines()
    code = qdrant_snapshot.main(
        args, QDRANT_ENV, out=out, client=client, folder=folder, upload=upload or default_upload
    )
    return code, out, uploads


def test_a_restore_test_uploads_counts_and_removes_the_throw_away_collection(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant(live_points=12, restored_points=12)
    code, out, uploads = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 0 and uploads == ["a.snapshot"]
    assert "points in documents_restore_test: 12" in out.text
    assert "points in documents: 12" in out.text and "removed documents_restore_test" in out.text
    assert "documents_restore_test" not in client.collections and "documents" in client.collections
    assert ("delete", "documents") not in client.calls, "the live collection is never touched"
    assert "the counts differ" not in out.text


def test_different_counts_are_reported_as_expected_not_as_a_failure(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant(live_points=15, restored_points=12)
    code, out, _ = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 0 and "the counts differ" in out.text


def test_an_empty_restore_is_a_failure_and_the_test_collection_is_still_removed(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant(live_points=12, restored_points=0)
    code, out, _ = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 1 and "EMPTY" in out.text
    assert "documents_restore_test" not in client.collections


def test_the_test_collection_is_removed_even_when_the_upload_fails(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant()

    def broken(path):
        client.uploaded()  # the server made the collection before it failed
        raise ConnectionError("https://secret.example.test dropped")

    code, out, _ = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path, broken)
    assert code == 1 and "ConnectionError" in out.text and "secret.example" not in out.text
    assert "documents_restore_test" not in client.collections
    assert "removed documents_restore_test" in out.text


def test_an_existing_test_collection_is_refused_and_left_alone(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant(existing=("documents_restore_test",))
    code, out, uploads = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 1 and "already exists" in out.text and uploads == []
    assert "documents_restore_test" in client.collections
    assert ("delete", "documents_restore_test") not in client.calls


def test_a_restore_test_needs_a_downloaded_copy_and_changes_nothing_without_run(tmp_path):
    client = RestoreQdrant()
    code, out, uploads = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 1 and "run `download` first" in out.text and uploads == []
    (tmp_path / "a.snapshot").write_bytes(b"")
    code, out, uploads = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 1 and uploads == [], "an empty file is not a copy"
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    code, out, uploads = restore(["restore-test", "a.snapshot"], client, tmp_path)
    assert code == 0 and "add --run" in out.text and uploads == [] and client.calls == []


def test_a_failure_to_remove_the_test_collection_is_loud_and_names_the_class_only(tmp_path):
    (tmp_path / "a.snapshot").write_bytes(b"snapshot")
    client = RestoreQdrant(delete_fails=True)
    code, out, _ = restore(["restore-test", "a.snapshot", "--run"], client, tmp_path)
    assert code == 1 and "COULD NOT REMOVE documents_restore_test (RuntimeError)" in out.text
    assert "secret.example" not in out.text


def test_the_upload_sends_the_file_with_the_key_to_the_test_collection_only(monkeypatch, tmp_path):
    import httpx

    (tmp_path / "a.snapshot").write_bytes(b"snapshot-bytes")
    seen = {}

    def post(url, params=None, headers=None, files=None, timeout=None):
        seen.update(url=url, params=params, headers=headers, name=files["snapshot"][0],
                    body=files["snapshot"][1].read())  # fmt: skip
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

    monkeypatch.setattr(httpx, "post", post)
    qdrant_snapshot._upload(QDRANT_ENV, tmp_path / "a.snapshot")
    assert seen["url"].endswith("/collections/documents_restore_test/snapshots/upload")
    assert "/collections/documents/" not in seen["url"]
    assert seen["headers"] == {"api-key": QDRANT_ENV["QDRANT_API_KEY"]}
    assert seen["body"] == b"snapshot-bytes" and seen["params"]["priority"] == "snapshot"


@pytest.mark.parametrize(("status", "error"), [(401, "KeyRejected"), (404, "Unsupported")])
def test_the_upload_maps_a_rejected_key_and_a_missing_route(monkeypatch, tmp_path, status, error):
    import httpx

    (tmp_path / "a.snapshot").write_bytes(b"x")
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: SimpleNamespace(status_code=status, raise_for_status=None)
    )
    with pytest.raises(getattr(qdrant_snapshot, error)):
        qdrant_snapshot._upload(QDRANT_ENV, tmp_path / "a.snapshot")


def test_the_throw_away_name_is_not_the_live_collection_nor_an_evaluation_collection():
    name = qdrant_snapshot.RESTORE_TEST_COLLECTION
    assert name == "documents_restore_test" and name != qdrant_snapshot.COLLECTION
    assert not name.endswith("_eval"), "the evaluation tools must never mistake it for theirs"


class FakeStream:
    def __init__(self, status, body=b""):
        self.status_code, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} from https://secret.example.test")

    def iter_bytes(self):
        yield self.body


def fake_httpx(monkeypatch, status, body=b""):
    import httpx

    seen = {}

    def stream(method, url, headers=None, timeout=None):
        seen.update(method=method, url=url, headers=headers or {})
        return FakeStream(status, body)

    monkeypatch.setattr(httpx, "stream", stream)
    return seen


@pytest.mark.parametrize("status", [401, 403])
def test_fetch_maps_a_rejected_key(monkeypatch, status):
    fake_httpx(monkeypatch, status)
    with pytest.raises(qdrant_snapshot.KeyRejected):
        list(qdrant_snapshot._fetch(QDRANT_ENV, "a.snapshot"))


@pytest.mark.parametrize("status", [404, 405, 501])
def test_fetch_maps_a_cluster_without_snapshots(monkeypatch, status):
    fake_httpx(monkeypatch, status)
    with pytest.raises(qdrant_snapshot.Unsupported):
        list(qdrant_snapshot._fetch(QDRANT_ENV, "a.snapshot"))


def test_fetch_sends_the_key_as_a_header_quotes_the_name_and_streams_the_bytes(monkeypatch):
    seen = fake_httpx(monkeypatch, 200, b"payload")
    got = list(qdrant_snapshot._fetch(QDRANT_ENV, "../a b.snapshot"))
    assert got == [b"payload"]
    assert seen["headers"] == {"api-key": QDRANT_ENV["QDRANT_API_KEY"]}
    assert seen["url"].endswith("/collections/documents/snapshots/a%20b.snapshot")
    assert QDRANT_ENV["QDRANT_API_KEY"] not in seen["url"]


def test_fetch_failure_of_another_kind_surfaces_and_the_command_prints_the_class_only(
    monkeypatch, tmp_path
):
    fake_httpx(monkeypatch, 500)
    out = Lines()
    code = qdrant_snapshot.main(
        ["download", "a.snapshot", "--run"],
        QDRANT_ENV,
        out=out,
        client=FakeQdrant([snap("a.snapshot", 7)]),
        folder=tmp_path,
    )
    assert code == 1 and "RuntimeError" in out.text and "secret.example" not in out.text
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


def test_the_inventory_prints_no_piece_of_a_key_only_a_hash_of_it():
    from app.gateway.keys import fingerprint

    key = made_up("gsk_")
    out = Lines()
    key_inventory.main([], {"GROQ_API_KEYS": key}, out=out)
    for piece in (key[:8], key[:12], key[-8:], key[4:16]):
        assert piece not in out.text, "no part of the key may appear"
    row = next(line for line in out if line.startswith("GROQ_API_KEYS"))
    assert fingerprint(key) in row and fingerprint(key) not in key


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


class RecordingDatabase(FakeDatabase):
    """Records every statement and has no way to change anything: no execute, no commit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statements: list[str] = []

    def query_one(self, sql, params=()):
        self.statements.append(sql)
        return super().query_one(sql, params)

    def query_all(self, sql, params=()):
        self.statements.append(sql)
        return super().query_all(sql, params)


def test_the_connection_count_only_asks_two_read_only_questions():
    database = RecordingDatabase([("active", 1)], 50)
    assert db_connections.main({}, out=Lines(), database=database) == 0
    assert len(database.statements) == 2
    first, second = database.statements
    assert first.strip().upper().startswith("SHOW ")
    assert second.strip().upper().startswith("SELECT ")
    for statement in database.statements:
        assert ";" not in statement
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TERMINATE", "KILL"):
            assert word not in statement.upper()
    assert database.closed, "and the connection is closed"


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


PRICING = {
    "default_reference": {
        "model": "groq/openai/gpt-oss-120b",
        "input_per_m": 1.0,
        "output_per_m": 2.0,
    },
    "models": {
        "groq/openai/gpt-oss-120b": {"input_per_m": 1.0, "output_per_m": 2.0},
        "typesafe/jev-1.13": {"input_per_m": 0.6, "output_per_m": 0.0},
        "gemini/gemini-embedding-2": {"input_per_m": 0.1, "output_per_m": 0.0},
    },
}


@pytest.fixture
def known_prices(monkeypatch):
    """Prices the test controls: no LiteLLM map, no file."""
    from app.gateway import cost

    monkeypatch.setattr(cost, "load_pricing", lambda: PRICING)
    monkeypatch.setattr(cost, "_litellm_prices", lambda model: None)


def test_the_shadow_cost_is_priced_per_group_from_the_tokens_and_the_report_says_billed_is_zero(
    known_prices,
):
    out = Lines()
    assert cost_projection.main([], out=out) == 0
    assert "expected $0" in out.text and "[T placeholder]" in out.text
    # 100 queries a day, 20 % follow-ups. answer: 376,000 tokens, 85 % input.
    #   319,600 * $1 / 1M + 56,400 * $2 / 1M = $0.4324
    # jev: 70,000 tokens, 59,500 input * $0.6 / 1M = $0.0357. embedding: 12,000 input * $0.1 / 1M
    costs = cost_projection.shadow_costs(cost_projection.project(100))
    assert costs["answer"].dollars == pytest.approx(0.4324)
    assert costs["jev"].dollars == pytest.approx(0.0357)
    assert costs["embedding"].dollars == pytest.approx(0.0012)
    assert "priced groups (answer, embedding, jev): $0.4693 a day, $14.08 for 30 days" in out.text


def test_a_group_without_a_price_is_shown_as_unpriced_and_never_counted_as_zero(known_prices):
    costs = cost_projection.shadow_costs(cost_projection.project(100))
    assert costs["rerank"].dollars is None and not costs["rerank"].priced
    assert "per search" in costs["rerank"].source
    out = Lines()
    cost_projection.main([], out=out)
    assert "rerank: UNPRICED (cohere/rerank-v3.5)" in out.text
    assert "NOT INCLUDED, no price known: rerank" in out.text and "lower bound" in out.text
    data = json.loads(_json_of(["--json"]))
    assert "rerank" not in data["shadow_cost_day"] and "rerank" in data["unpriced"]
    assert set(data["priced"]) == {"answer", "embedding", "jev"}
    assert data["priced"]["jev"]["source"] == "pricing.json"


def _json_of(args):
    out = Lines()
    assert cost_projection.main(args, out=out) == 0
    return out.text


def test_the_reference_price_prices_only_the_model_it_belongs_to(monkeypatch):
    """A model that is neither in the table nor in LiteLLM's map must not borrow another model's
    price: it is unpriced. The reference model itself keeps the reference price."""
    from app.gateway import cost

    pricing = {**PRICING, "models": {"typesafe/jev-1.13": PRICING["models"]["typesafe/jev-1.13"]}}
    monkeypatch.setattr(cost, "load_pricing", lambda: pricing)
    monkeypatch.setattr(cost, "_litellm_prices", lambda model: None)
    costs = cost_projection.shadow_costs(cost_projection.project(100))
    assert costs["answer"].priced and "reference price" in costs["answer"].source
    assert not costs["embedding"].priced and "no list price" in costs["embedding"].source


def test_a_price_from_litellm_counts_and_says_where_it_came_from(monkeypatch):
    from app.gateway import cost

    monkeypatch.setattr(cost, "load_pricing", lambda: PRICING)
    prices = {"gemini/gemini-embedding-2": None, "cohere/rerank-v3.5": (2e-6, 0.0)}
    monkeypatch.setattr(cost, "_litellm_prices", lambda model: prices.get(model))
    costs = cost_projection.shadow_costs(cost_projection.project(100))
    assert costs["rerank"].priced and costs["rerank"].source == "LiteLLM's price map"
    assert costs["rerank"].dollars == pytest.approx(250_000 * 2e-6)  # all tokens are input


def test_a_group_the_script_has_no_model_for_is_unpriced_not_dropped(known_prices):
    day = cost_projection.project(100)
    day["judge"] = {"requests": 5.0, "tokens": 9000.0}
    costs = cost_projection.shadow_costs(day)
    assert not costs["judge"].priced and "no model is assigned" in costs["judge"].source


def test_the_projection_json_and_the_limits_of_the_number(known_prices):
    data = json.loads(_json_of(["--json"]))
    assert {"day", "shadow_cost_day", "priced", "unpriced"} <= set(data)
    assert cost_projection.main(["--queries-per-day", "0"], out=Lines()) == 1
    assert cost_projection.main(["--queries-per-day", "1000001"], out=Lines()) == 1
    assert cost_projection.main(["--queries-per-day", "1000000"], out=Lines()) == 0


def test_the_projection_prints_no_key_and_reads_no_environment(known_prices, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_should_never_appear_0123456789")
    assert "gsk_should_never_appear" not in _json_of([])


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


def _facts(
    monkeypatch,
    *,
    commit="abc1234",
    branch="release-x",
    changed=("app/a.py", "docs/b.md"),
    ancestors=(),
):
    """Fake git: the facts of HEAD, the changed files, and which commits are ancestors."""
    calls: list[tuple[str, ...]] = []

    def git(*args):
        calls.append(args)
        if args[:2] == ("rev-parse", "--short"):
            return commit
        if args[:2] == ("rev-parse", "--abbrev-ref"):
            return branch
        if args[0] == "diff":
            return "\n".join(changed)
        return ""

    monkeypatch.setattr(release_record, "_git", git)
    monkeypatch.setattr(
        release_record,
        "_git_ok",
        lambda *args: args[:2] == ("merge-base", "--is-ancestor") and args[2] in ancestors,
    )
    return calls


def make_record(folder, name, commit):
    (folder / name).write_text(
        f"# Release record: earlier\n\n- **Commit:** {commit} on main\n", "utf-8"
    )


WHEN = datetime(2026, 10, 12, tzinfo=UTC)


def test_the_record_holds_the_commit_the_branch_and_the_changed_files(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    _facts(monkeypatch)
    out = Lines()
    code = release_record.main(["--title", "T", "--kind", "code"], out=out, folder=folder, now=WHEN)
    text = (folder / "2026-10-12-t.md").read_text("utf-8")
    assert code == 0
    assert "- **Commit:** abc1234 on release-x" in text
    assert "- app/a.py\n- docs/b.md" in text
    assert "since the commit before HEAD (no earlier record found)" in text
    assert "for commit abc1234" in out.text


def test_the_diff_starts_at_the_commit_of_the_newest_earlier_record(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    make_record(folder, "2026-09-01-old.md", "1111111")
    make_record(folder, "2026-10-01-newer.md", "2222222")
    calls = _facts(monkeypatch, ancestors=("1111111", "2222222"))
    release_record.main(["--title", "T", "--kind", "code"], out=Lines(), folder=folder, now=WHEN)
    diff = next(c for c in calls if c[0] == "diff")
    assert diff == ("diff", "--name-only", "2222222", "HEAD")
    text = (folder / "2026-10-12-t.md").read_text("utf-8")
    assert "since commit 2222222" in text


def test_an_earlier_record_that_is_not_an_ancestor_is_skipped_and_since_wins(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    make_record(folder, "2026-09-01-old.md", "1111111")
    make_record(folder, "2026-10-01-forgotten-branch.md", "9999999")
    calls = _facts(monkeypatch, ancestors=("1111111",))
    release_record.main(["--title", "T", "--kind", "code"], out=Lines(), folder=folder, now=WHEN)
    assert next(c for c in calls if c[0] == "diff")[2] == "1111111"
    calls.clear()
    release_record.main(
        ["--title", "T2", "--kind", "code", "--since", "abcdef0"],
        out=Lines(),
        folder=folder,
        now=WHEN,
    )
    assert next(c for c in calls if c[0] == "diff")[2] == "abcdef0"


def test_the_template_and_the_readme_are_never_taken_for_an_earlier_record(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    (folder / "README.md").write_text("- **Commit:** 3333333 on main\n", "utf-8")
    assert release_record.previous_commit(folder, lambda *a: True) is None


def test_the_evaluation_line_depends_on_the_kind(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    _facts(monkeypatch)
    for kind in release_record.KINDS:
        release_record.main(
            ["--title", f"k {kind}", "--kind", kind], out=Lines(), folder=folder, now=WHEN
        )
        text = next(folder.glob(f"2026-10-12-k-{kind}.md")).read_text("utf-8")
        always = kind in release_record.ALWAYS_RECORDED
        assert ("yes: always" in text) is always
        assert ("if it can change answers" in text) is not always


def test_the_four_kinds_that_are_always_recorded_are_the_four_the_spec_names():
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[1] / "spec" / "build.md").read_text("utf-8")
    line = next(
        ln for ln in spec.splitlines() if "release record" in ln and "always get an entry" in ln
    )
    named = {kind for kind in release_record.KINDS if kind in line.lower()}
    assert named == set(release_record.ALWAYS_RECORDED)
    assert release_record.slug("A  Title: with / odd chars!") == "a-title-with-odd-chars"
    assert release_record.slug("???") == "release"


def test_a_title_must_be_one_line_and_a_since_must_be_a_commit_id(tmp_path, monkeypatch):
    folder = make_folder(tmp_path)
    _facts(monkeypatch)
    for title in ("", "   ", "two\nlines"):
        out = Lines()
        code = release_record.main(
            ["--title", title, "--kind", "code"], out=out, folder=folder, now=WHEN
        )
        assert code == 1 and "one line" in out.text
    out = Lines()
    code = release_record.main(
        ["--title", "T", "--kind", "code", "--since", "main; rm -rf"],
        out=out,
        folder=folder,
        now=WHEN,
    )
    assert code == 1 and "commit id" in out.text
    assert not list(folder.glob("2026-*.md"))
