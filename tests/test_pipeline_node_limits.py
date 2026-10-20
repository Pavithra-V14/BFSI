"""The cache lookup's time limit depends on where the service runs: one second on Render (next to
its stores), 2.5 seconds on any other machine, where the stores are far away and the lookup's
three network calls in a row need more. Nothing else about the limits differs."""

import time

import fakeredis
import pytest
from qdrant_client import QdrantClient

from app.cache.store import CacheStore
from app.pipeline.config import (
    LOCAL_CACHE_LOOKUP_SECONDS,
    LOCAL_SLICE_SECONDS,
    NODE_LIMITS,
    SLICE_SECONDS,
    PipelineConfig,
    node_limits_for,
    slice_seconds_for,
)


def test_on_render_the_limits_are_the_standard_ones():
    assert node_limits_for(True) == NODE_LIMITS
    assert node_limits_for(True)["cache_lookup"][0] == 1.0


LONGER_LOCALLY = ("cache_lookup", "authenticate", "local_scan")


def test_everywhere_else_only_the_store_nodes_are_longer():
    local = node_limits_for(False)
    assert local["cache_lookup"][0] == LOCAL_CACHE_LOOKUP_SECONDS == 2.5
    assert local["authenticate"][0] == 3.0 and local["local_scan"][0] == 4.0
    for name in LONGER_LOCALLY:
        assert local[name][1] == NODE_LIMITS[name][1]  # the same minimum to start
    others = {name: limit for name, limit in local.items() if name not in LONGER_LOCALLY}
    assert others == {n: v for n, v in NODE_LIMITS.items() if n not in LONGER_LOCALLY}


def test_a_network_call_gets_two_seconds_locally_and_four_tenths_on_render():
    assert slice_seconds_for(False) == LOCAL_SLICE_SECONDS == 2.0
    assert slice_seconds_for(True) == SLICE_SECONDS == 0.4


def test_the_standard_table_is_not_changed_by_asking_for_the_local_one():
    node_limits_for(False)
    assert NODE_LIMITS["cache_lookup"][0] == 1.0


def test_the_longer_lookup_still_fits_inside_the_whole_request_budget():
    config = PipelineConfig(node_limits=node_limits_for(False))
    assert config.limits_for("cache_lookup")[0] < config.budget_seconds / 4


# ---- the pipeline as the service builds it ---------------------------------------------------
@pytest.fixture
def build(tmp_path):
    from app.api import main as api_main
    from app.pipeline import startup
    from tests.api_fixtures import AUDIT_KEY, JWT_SECRET

    class QuietHolder(startup.MaskerHolder):
        def __init__(self, factory=None, **kwargs):
            super().__init__(lambda: (_ for _ in ()).throw(RuntimeError("not loaded")), **kwargs)

    def make(**extra):
        env = {
            "JWT_SECRET_KEY": JWT_SECRET,
            "AUDIT_HASH_KEY": AUDIT_KEY,
            "DATABASE_URL": "sqlite:///" + str(tmp_path / "limits.db"),
            "UPSTASH_REDIS_URL": "redis://localhost:6379",
            "QDRANT_URL": "http://localhost:6333",
            **extra,
        }
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                api_main,
                "store_from_env",
                lambda env, *, on_event=None: CacheStore(fakeredis.FakeRedis()),
            )
            patch.setattr(startup, "MaskerHolder", QuietHolder)
            patch.setattr(
                "app.vector.documents.client_from_env",
                lambda env: QdrantClient(location=":memory:"),
            )
            app = api_main.create_app_from_env(env)
        time.sleep(0.05)
        return app.state.services.pipeline.deps.config

    return make


def test_a_local_service_waits_up_to_two_and_a_half_seconds_for_the_cache(build):
    assert build().limits_for("cache_lookup")[0] == 2.5


def test_a_service_on_render_keeps_the_one_second_limit(build):
    assert build(RENDER="true").limits_for("cache_lookup")[0] == 1.0


def test_the_other_limits_are_the_same_in_both(build):
    local, hosted = build(), build(RENDER="true")
    for name in NODE_LIMITS:
        if name not in LONGER_LOCALLY:
            assert local.limits_for(name) == hosted.limits_for(name), name


def test_the_service_uses_the_slice_of_where_it_runs(build):
    assert build().slice_seconds == 2.0
    assert build(RENDER="true").slice_seconds == 0.4
    assert build().limits_for("local_scan")[0] == 4.0
    assert build(RENDER="true").limits_for("local_scan")[0] == 2.0
