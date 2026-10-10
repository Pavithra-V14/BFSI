"""Retrieval (spec 7, 4.1 steps 8-11): role isolation on EVERY path, the active filter on both
branches, soft filters and their retry, the allowed values, RRF, 25 candidates and the top 5."""

import asyncio
from types import SimpleNamespace

import pytest
from qdrant_client import models

from app.pipeline.retrieval import build_filter, distinct_values, soft_filters
from app.pipeline.roles import VISIBLE_ROLES, UnknownRole, visible_roles
from app.vector.documents import COLLECTION, DENSE_VECTOR
from tests.ingest_fixtures import filler, para, parsed, title
from tests.pipeline_rig import FakeGateway, PipelineRig


def run(coro):
    return asyncio.run(coro)


def document(word: str, extra: str = "") -> object:
    return parsed(title(f"Section 1. About {word}"), para(f"The {word} rule applies. {extra}"))


class SpyQdrant:
    """Wraps the in-memory client: records every query_points call and what it returned."""

    def __init__(self, inner):
        self._inner = inner
        self.queries: list[dict] = []
        self.returned: list[list] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def query_points(self, collection, **kwargs):
        self.queries.append(kwargs)
        response = self._inner.query_points(collection, **kwargs)
        self.returned.append(list(response.points))
        return response


def build(tmp_path, documents, gateway=None):
    rig = PipelineRig(tmp_path, documents=documents, gateway=gateway or FakeGateway())
    spy = SpyQdrant(rig.deps.qdrant)
    rig.deps.qdrant = spy
    return rig, spy


@pytest.fixture
def three_roles(tmp_path):
    rig, spy = build(
        tmp_path,
        [
            ("doc-c", "customer", document("balloon")),
            ("doc-s", "staff", document("zeppelin")),
            ("doc-a", "admin", document("quokka")),
        ],
    )
    yield rig, spy
    rig.close()


def doc_ids(points):
    return {p.payload["doc_id"] for p in points}


# ---- roles ------------------------------------------------------------------------------------
def test_the_roles_map_to_the_access_roles_they_may_see():
    assert visible_roles("customer") == ("customer",)
    assert visible_roles("staff") == ("customer", "staff")
    assert visible_roles("admin") == ("customer", "staff", "admin")
    assert set(VISIBLE_ROLES) == {"customer", "staff", "admin"}
    for bad in ("", "root", None, "Customer"):
        with pytest.raises(UnknownRole):
            visible_roles(bad)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("customer", {"doc-c"}),
        ("staff", {"doc-c", "doc-s"}),
        ("admin", {"doc-c", "doc-s", "doc-a"}),
    ],
)
def test_a_role_only_ever_retrieves_the_documents_it_may_see(three_roles, role, allowed):
    rig, spy = three_roles
    run(rig.ask("balloon zeppelin quokka rule", role=role))
    assert spy.returned and doc_ids(spy.returned[0]) == allowed
    for query in spy.queries:  # the filter is the same on the fusion and on BOTH branches
        flt = query["query_filter"]
        assert len(query["prefetch"]) == 2
        assert {p.using for p in query["prefetch"]} == {"dense", "sparse"}
        for branch in query["prefetch"]:
            assert branch.filter == flt
        roles = [c.match.any for c in flt.must if getattr(c, "key", None) == "access_role"]
        assert roles == [list(visible_roles(role))]


@pytest.mark.parametrize("branch", ["sparse", "dense"])
def test_each_branch_alone_would_surface_a_staff_chunk_and_the_customer_still_never_gets_it(
    three_roles, branch
):
    """The staff-only chunk is the PERFECT match on one branch at a time: by its word (sparse) or by
    its exact vector (dense). A customer must not receive it through either branch."""
    rig, spy = three_roles
    staff_point = next(iter(rig.rig.points("doc-s", vectors=True).items()))
    if branch == "dense":
        exact = staff_point[1]["vector"]["dense"]
        rig.gateway.embed = _fixed_embed(rig.gateway, exact)
        query = "unrelated words only"
    else:
        query = "tell me about zeppelin"
    run(rig.ask(query, role="customer"))
    assert all("doc-s" not in doc_ids(points) for points in spy.returned)
    run(rig.ask(query, role="staff"))
    assert any(
        "doc-s" in doc_ids(points) for points in spy.returned
    )  # and it IS reachable by staff


def _fixed_embed(gateway, vector):
    async def embed(texts, **kwargs):
        from app.gateway.gateway import EmbeddingResult

        gateway.log("embed", count=len(texts), text=texts[0], timeout_s=kwargs.get("timeout_s"))
        return EmbeddingResult([list(vector)], "embed-model", key_fingerprint="fpE", input_tokens=3)

    return embed


def test_an_inactive_chunk_is_never_retrieved_on_either_branch(three_roles):
    rig, spy = three_roles
    for point_id in rig.rig.points("doc-c"):
        rig.rig.qdrant.set_payload(
            COLLECTION, payload={"effective_to": "2026-10-02T00:00:00+00:00"}, points=[point_id]
        )
    result = run(rig.ask("balloon rule", role="admin"))
    assert all("doc-c" not in doc_ids(points) for points in spy.returned)
    for query in spy.queries:
        for branch in query["prefetch"]:
            assert any(isinstance(c, models.IsEmptyCondition) for c in branch.filter.must)
    assert result.kind in ("answer", "refusal")


def test_the_unknown_role_is_refused_before_anything_runs(three_roles):
    rig, spy = three_roles
    result = run(rig.ask("balloon rule", role="superuser", user=rig.new_user()))
    assert (result.kind, result.reason) == ("refusal", "unknown_role")
    assert rig.gateway.model_calls() == 0 and spy.queries == []


# ---- fusion, candidates, top 5
# ---------------------------------------------------------------------
def test_one_query_api_call_with_rrf_k_60_and_25_candidates(three_roles):
    rig, spy = three_roles
    run(rig.ask("balloon rule", role="customer"))
    assert len(spy.queries) == 1  # ONE call runs both searches
    query = spy.queries[0]
    assert isinstance(query["query"], models.RrfQuery) and query["query"].rrf.k == 60
    assert query["limit"] == 25 and all(b.limit == 25 for b in query["prefetch"])


def test_25_candidates_are_reranked_and_the_top_5_reach_the_generator(tmp_path):
    many = parsed(
        *[
            item
            for n in range(30)
            for item in (
                title(f"Section {n}. Premium"),
                para(f"Premium rule {n}. " + filler(12, f"p{n}x")),
            )
        ]
    )
    rig, spy = build(tmp_path, [("doc-m", "customer", many)])
    try:
        run(rig.ask("premium rule", role="customer"))
        assert rig.gateway.calls_of("rerank")[0]["count"] == 25
        prompt = rig.gateway.calls_of("complete", what="answer")[0]["prompt"]
        assert prompt.count("<<<DOCUMENT id=") == 5  # the top 5, fixed for now
    finally:
        rig.close()


# ---- soft filters
# ---------------------------------------------------------------------------------------
@pytest.fixture
def two_lines(tmp_path):
    rig, spy = build(
        tmp_path,
        [
            (
                "doc-h",
                "customer",
                document("premium", "health waiting"),
                {"product_line": "health"},
            ),
            ("doc-m", "customer", document("premium", "motor damage"), {"product_line": "motor"}),
            ("doc-t", "staff", document("premium", "travel cover"), {"product_line": "travel"}),
        ],
    )
    yield rig, spy
    rig.close()


def retrieve_span(rig):
    return [s for s in rig.spans if s.stage == "retrieve"][-1]


def test_a_confident_product_line_is_applied_as_a_soft_filter(two_lines):
    rig, spy = two_lines
    rig.gateway.product_line = ("health", 0.9)
    run(rig.ask("premium rule", role="customer"))
    assert doc_ids(spy.returned[0]) == {"doc-h"}
    assert retrieve_span(rig).extra["filters"] == ["access_role", "active", "product_line"]
    flt = spy.queries[0]["query_filter"]
    assert any(getattr(c, "key", None) == "product_line" for c in flt.must)


def test_a_probability_under_0_80_is_not_a_filter(two_lines):
    rig, spy = two_lines
    rig.gateway.product_line = ("health", 0.79)
    run(rig.ask("premium rule", role="customer"))
    assert doc_ids(spy.returned[0]) == {"doc-h", "doc-m"}
    assert retrieve_span(rig).extra["filters"] == ["access_role", "active"]


def test_the_threshold_is_exactly_0_80():
    triage = SimpleNamespace(
        product_line="health",
        product_line_probability=0.8,
        clause_type="x",
        clause_type_probability=0.7999,
    )
    assert soft_filters(triage, 0.8) == {"product_line": "health"}


def test_a_filtered_search_that_finds_nothing_is_repeated_once_without_the_soft_filters(two_lines):
    rig, spy = two_lines
    rig.gateway.product_line = ("travel", 0.95)  # only staff may see travel: nothing for a customer
    result = run(rig.ask("premium rule", role="customer"))
    assert len(spy.queries) == 2  # the retry
    assert retrieve_span(rig).extra["filters"] == ["access_role", "active", "soft_filters_dropped"]
    assert result.kind == "answer" and doc_ids(spy.returned[1]) == {"doc-h", "doc-m"}
    flt = spy.queries[1]["query_filter"]
    assert not any(getattr(c, "key", None) == "product_line" for c in flt.must)


def test_the_choice_questions_offer_the_distinct_values_stored_in_qdrant(two_lines):
    rig, spy = two_lines
    run(rig.ask("premium rule", role="customer"))
    options = rig.gateway.calls_of("decide")[0]["options"]
    assert options["product_line"] == ["health", "motor", "travel"]


def test_the_values_are_refreshed_when_the_corpus_version_changes_and_not_before(two_lines):
    rig, spy = two_lines
    calls = {"n": 0}
    real = rig.deps.qdrant._inner.facet

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    rig.deps.qdrant._inner.facet = counting
    run(rig.ask("premium rule", role="customer"))
    first = calls["n"]
    run(rig.ask("premium rule again", role="customer"))
    assert calls["n"] == first  # same corpus version: the values are reused
    rig.rig.ingest(document("premium", "pet cover"), doc_id="doc-p", access_role="customer",
                   product_line="pet")  # fmt: skip
    run(rig.ask("premium rule third", role="customer"))
    assert calls["n"] > first
    assert "pet" in rig.gateway.calls_of("decide")[-1]["options"]["product_line"]


def test_distinct_values_fall_back_to_a_scroll_when_facet_is_not_available(two_lines):
    rig, spy = two_lines
    client = rig.deps.qdrant._inner

    def no_facet(*args, **kwargs):
        raise NotImplementedError("local mode")

    client.facet = no_facet
    assert distinct_values(client, COLLECTION, "product_line") == ["health", "motor", "travel"]


def test_only_active_values_are_offered(two_lines):
    rig, spy = two_lines
    for point_id in rig.rig.points("doc-t"):
        rig.rig.qdrant.set_payload(
            COLLECTION, payload={"effective_to": "2026-10-02T00:00:00+00:00"}, points=[point_id]
        )
    assert distinct_values(rig.deps.qdrant._inner, COLLECTION, "product_line") == [
        "health",
        "motor",
    ]


def test_the_hard_filter_is_built_from_the_roles_and_the_active_condition():
    flt = build_filter(("customer", "staff"), {"product_line": "health"})
    keys = [getattr(c, "key", None) for c in flt.must]
    assert keys == ["access_role", None, "product_line"]
    assert isinstance(flt.must[1], models.IsEmptyCondition)
    assert DENSE_VECTOR == "dense"
