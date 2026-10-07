"""The owner-run real-Qdrant check, run here on Qdrant's in-memory mode only."""

import pytest
from qdrant_client import QdrantClient

from scripts import qdrant_check
from scripts.qdrant_check import RefusedCollection, guard_name, run_check, throwaway_name


def test_it_refuses_the_real_collection_and_any_other_name():
    for name in ("documents", "check_", "check_abc", "other_0123456789ab", "Documents"):
        with pytest.raises(RefusedCollection):
            guard_name(name)
    assert guard_name(throwaway_name()).startswith("check_")


def test_throwaway_names_are_unique():
    assert len({throwaway_name() for _ in range(50)}) == 50


def test_the_check_passes_on_in_memory_and_deletes_its_collection():
    client = QdrantClient(":memory:")
    lines = []
    name = throwaway_name()
    problems = run_check(client, name, lambda *a, **k: lines.append((a, k)), expect_indexes=False)
    assert problems == 0 and lines
    assert not client.collection_exists(name)
    assert client.get_collections().collections == []


def test_the_collection_is_deleted_even_when_a_step_fails(monkeypatch):
    client = QdrantClient(":memory:")

    def boom(*args, **kwargs):
        raise RuntimeError("step failed")

    monkeypatch.setattr(client, "upsert", boom)
    name = throwaway_name()
    with pytest.raises(RuntimeError):
        run_check(client, name, lambda *a, **k: None)
    assert not client.collection_exists(name)


def test_nothing_happens_without_run(capsys):
    assert qdrant_check.main([]) == 0
    assert "Nothing was done" in capsys.readouterr().out


def test_a_missing_url_is_reported_without_a_traceback(capsys):
    assert qdrant_check.main(["--run"], env={}) == 1
    assert "QDRANT_URL is not set" in capsys.readouterr().err


def test_the_real_collection_name_never_reaches_the_client():
    client = QdrantClient(":memory:")
    with pytest.raises(RefusedCollection):
        run_check(client, "documents", lambda *a, **k: None)
    assert client.get_collections().collections == []
