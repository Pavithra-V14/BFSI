"""Chunk identity (spec 6.3) and the project's sparse tokenizer (spec 6.6)."""

import uuid

from qdrant_client import models

from app.ingest.chunker import chunk_document
from app.ingest.identity import assign_identities, content_hash, image_source, point_id
from app.ingest.model import Chunk
from app.vector import sparse
from tests.ingest_fixtures import filler, para, parsed, sample_elements, title, words


def ids_of(elements, doc_id="doc-a"):
    chunks = chunk_document(parsed(*elements), words).chunks
    return {i.chunk.clause_ref: i for i in assign_identities(doc_id, chunks)}


def test_whitespace_only_changes_do_not_change_the_hash():
    assert content_hash("a  b\n c ") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")


def test_the_point_id_depends_on_document_clause_and_part_only():
    one = point_id("doc-a", "3.2", 0)
    assert one == point_id("doc-a", "3.2", 0)
    assert one != point_id("doc-b", "3.2", 0)
    assert one != point_id("doc-a", "3.3", 0)
    assert one != point_id("doc-a", "3.2", 1)
    uuid.UUID(one)  # a valid UUID, as Qdrant requires


def test_page_numbers_are_never_part_of_identity():
    on_page_one = [title("Section 1. A", 1), para("Some text here.", 1)]
    on_page_nine = [title("Section 1. A", 9), para("Some text here.", 9)]
    one, nine = ids_of(on_page_one)["1"], ids_of(on_page_nine)["1"]
    assert (one.chunk_id, one.content_hash) == (nine.chunk_id, nine.content_hash)


def test_inserting_a_clause_in_the_middle_changes_no_other_identity():
    before = ids_of(sample_elements())
    elements = sample_elements()
    elements[2:2] = [title("Section 1A. Inserted"), para("A brand new clause is added here.")]
    after = ids_of(elements)
    for ref in ("1", "2", "3"):
        assert (after[ref].chunk_id, after[ref].content_hash) == (
            before[ref].chunk_id,
            before[ref].content_hash,
        )
    assert "1A" in after


def test_a_chunk_without_a_clause_number_uses_its_heading_and_content():
    elements = [title("Part A"), para(filler(80))]
    [ident] = ids_of(elements).values()
    assert ident.key.startswith("h:part a:") and ident.content_hash[:16] in ident.key
    changed = ids_of([title("Part A"), para(filler(80, "z"))])
    assert next(iter(changed.values())).chunk_id != ident.chunk_id


def test_a_repeated_clause_number_still_gets_unique_ids_in_document_order():
    elements = [
        title("Section 1. First"),
        para("Alpha text here."),
        title("Section 1. Again"),
        para("Beta text here."),
    ]
    chunks = chunk_document(parsed(*elements), words).chunks
    idents = assign_identities("doc-a", chunks)
    assert len({i.chunk_id for i in idents}) == 2
    assert idents[0].key == "1" and idents[1].key == "1~2"


def test_an_image_caption_chunk_is_identified_by_the_image_not_the_caption():
    def caption_chunk(caption):
        return Chunk(
            kind="image_caption",
            text=caption,
            source=image_source(b"same-picture"),
            clause_ref="2",
            heading_path=("Section 2. Chart",),
            page_start=2,
            page_end=2,
        )

    first, second = (
        assign_identities("doc-a", [caption_chunk(t)])[0] for t in ("a bar chart", "x")
    )
    assert first.chunk_id == second.chunk_id and first.content_hash == second.content_hash
    other = assign_identities(
        "doc-a", [Chunk("image_caption", "", image_source(b"other"), "2", (), 2, 2)]
    )[0]
    assert other.chunk_id != first.chunk_id


def test_the_tokenizer_lowercases_strips_punctuation_and_lightly_stems():
    assert sparse.tokenize("Claims, CLAIMED; claiming!") == ["claim", "claim", "claim"]
    assert sparse.tokenize("Waiting-period: 30 days (IRDAI)") == [
        "wait",
        "period",
        "30",
        "day",
        "irdai",
    ]
    assert sparse.tokenize("Premium  ") == ["premium"]
    assert sparse.tokenize("class loss") == ["class", "loss"]
    assert sparse.tokenize("") == []


def test_the_same_word_is_the_same_index_on_both_sides():
    document = sparse.document_vector("Claims are settled quickly.")
    query = sparse.query_vector("claim settlement")
    assert set(query.indices) & set(document.indices)
    assert sparse.token_index("claim") == sparse.token_index("claim")
    assert all(0 <= i < 2**32 for i in document.indices)


def test_document_vectors_weight_repeats_and_query_vectors_do_not():
    document = sparse.document_vector("claim claim claim other")
    query = sparse.query_vector("claim claim claim other")
    by_index = dict(zip(document.indices, document.values, strict=True))
    assert by_index[sparse.token_index("claim")] > by_index[sparse.token_index("other")] == 1.0
    assert set(query.values) == {1.0}
    assert isinstance(document, models.SparseVector)


def test_an_empty_text_gives_an_empty_vector():
    vector = sparse.document_vector("...")
    assert vector.indices == [] and vector.values == []
