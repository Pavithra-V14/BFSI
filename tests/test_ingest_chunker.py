"""Chunking (spec 6.2): clauses, sizes, overlap, tables, fragments, images."""

import pytest

from app.ingest.chunker import (
    HARD_MAX_TOKENS,
    MIN_FRAGMENT_TOKENS,
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    chunk_document,
    clause_number,
)
from tests.ingest_fixtures import filler, image, para, parsed, sample_elements, table, title, words


def chunks_of(*elements):
    return chunk_document(parsed(*elements), words).chunks


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("Section 3. Waiting periods", "title", "3"),
        ("Clause 2.1 Cover", "title", "2.1"),
        ("3.2 The waiting period is 30 days.", "text", "3.2"),
        ("3. The insurer will pay.", "text", "3"),
        ("4 Definitions", "title", "4"),
        ("Section 1A. Inserted", "title", "1A"),
        ("2.3B Extra", "text", "2.3B"),
        ("100 crore is the limit.", "text", None),  # a bare number in running text is not a clause
        ("2026 was a good year.", "text", None),
        ("Waiting periods", "title", None),
    ],
)
def test_clause_numbers(text, kind, expected):
    assert clause_number(text, kind) == expected


def test_each_numbered_clause_is_its_own_chunk_with_its_clause_ref():
    chunks = chunks_of(*sample_elements())
    assert [c.clause_ref for c in chunks] == ["1", "2", "3"]
    assert chunks[1].text.startswith("Section 2. Waiting periods\n")
    assert chunks[1].heading_path == ("Section 2. Waiting periods",)


def test_a_numbered_paragraph_starts_a_clause_without_a_title():
    chunks = chunks_of(para("3.1 First rule applies here."), para("3.2 Second rule applies there."))
    assert [c.clause_ref for c in chunks] == ["3.1", "3.2"]


def test_a_sub_clause_sits_under_its_parent_in_the_heading_path():
    chunks = chunks_of(
        title("Section 2. Cover"),
        para("Cover applies."),
        title("2.1 Waiting"),
        para("Wait 30 days."),
    )
    assert chunks[1].clause_ref == "2.1"
    assert chunks[1].heading_path == ("Section 2. Cover", "2.1 Waiting")


def test_a_clause_under_the_hard_maximum_is_never_split():
    body = filler(HARD_MAX_TOKENS - 20)
    chunks = chunks_of(title("Section 1. Big"), para(body))
    assert len(chunks) == 1 and chunks[0].part_index == 0


def test_an_oversized_clause_splits_at_sentences_with_heading_and_overlap():
    body = filler(1500)
    chunks = chunks_of(title("Section 5. Long clause"), para(body))
    assert len(chunks) >= 3
    assert [c.part_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.clause_ref == "5"
        assert chunk.text.startswith("Section 5. Long clause\n")  # the heading is repeated
        assert words(chunk.text) <= HARD_MAX_TOKENS
    # every piece but the last ends at a sentence boundary
    assert all(c.text.rstrip().endswith(".") for c in chunks)
    # the next piece starts with the tail of the previous one (overlap), within the limit
    first_words = chunks[0].text.split()
    second = chunks[1].text.split("\n", 1)[1].split()
    shared = 0
    for size in range(1, OVERLAP_TOKENS + 15):
        if first_words[-size:] == second[:size]:
            shared = size
    assert 0 < shared <= OVERLAP_TOKENS + 10
    # the target size is respected for the pieces that are not the last
    assert all(words(c.text) <= TARGET_TOKENS + 12 for c in chunks[:-1])


def test_a_short_clause_has_no_overlap_text_at_all():
    chunks = chunks_of(title("Section 1. Short"), para("One sentence. Another sentence."))
    assert len(chunks) == 1 and chunks[0].text.count("One sentence") == 1


def test_a_table_that_fits_is_one_chunk_with_rows_kept_together():
    rows = [["Plan", "Limit"], ["Gold", "5 lakh"], ["Silver", "3 lakh"]]
    chunks = chunks_of(title("Section 4. Limits"), table(rows))
    assert len(chunks) == 1 and chunks[0].kind == "table"
    assert chunks[0].text == "Section 4. Limits\nPlan | Limit\nGold | 5 lakh\nSilver | 3 lakh"
    assert chunks[0].clause_ref == "4" and chunks[0].ordinal == 0


def test_a_big_table_splits_by_rows_with_the_header_repeated():
    rows = [["Code", "Meaning"]] + [[f"C{n}", filler(30, f"m{n}x")] for n in range(60)]
    chunks = [c for c in chunks_of(title("Section 9. Codes"), table(rows)) if c.kind == "table"]
    assert len(chunks) > 1
    for chunk in chunks:
        lines = chunk.text.split("\n")
        assert lines[0] == "Section 9. Codes" and lines[1] == "Code | Meaning"
    all_rows = [line for c in chunks for line in c.text.split("\n")[2:]]
    assert len(all_rows) == 60  # no row lost, none repeated
    assert [c.part_index for c in chunks] == list(range(len(chunks)))


def test_two_tables_under_one_clause_get_different_ordinals():
    chunks = chunks_of(title("Section 4. Limits"), table([["a", "b"]]), table([["c", "d"]]))
    assert [(c.kind, c.ordinal) for c in chunks] == [("table", 0), ("table", 1)]


def test_tiny_unnumbered_fragments_merge_into_a_neighbour_under_the_same_heading():
    chunks = chunks_of(
        title("Part A"), para(filler(80)), title("Note"), para("See the schedule."), title("Part B")
    )
    assert len(chunks) == 1
    assert "Note" in chunks[0].text and "See the schedule." in chunks[0].text


def test_a_tiny_fragment_with_no_earlier_neighbour_merges_forward():
    chunks = chunks_of(title("Note"), para("Short."), title("Part A"), para(filler(80)))
    assert len(chunks) == 1 and chunks[0].clause_ref is None
    assert "Short." in chunks[0].text


def test_a_tiny_unnumbered_fragment_does_not_merge_into_a_numbered_clause():
    chunks = chunks_of(
        title("Section 1. A"), para(filler(80)), title("Note"), para("See the schedule.")
    )
    assert [c.clause_ref for c in chunks] == ["1", None]
    assert "See the schedule." not in chunks[0].text


def test_numbered_clauses_are_never_merged_however_small():
    chunks = chunks_of(
        title("Section 1. A"), para("Tiny."), title("Section 2. B"), para("Tiny too.")
    )
    assert [c.clause_ref for c in chunks] == ["1", "2"]
    assert all(words(c.text) < MIN_FRAGMENT_TOKENS for c in chunks)


def test_pages_are_recorded_for_display():
    chunks = chunks_of(title("Section 1. A", 1), para("Text one.", 1), para("More text.", 2))
    assert (chunks[0].page_start, chunks[0].page_end) == (1, 2)


def test_images_are_collected_with_their_place_and_make_no_chunk_yet():
    result = chunk_document(
        parsed(title("Section 2. Chart", 2), para("Look at the chart.", 2), image(b"png-bytes", 2)),
        words,
    )
    assert len(result.chunks) == 1
    [item] = result.images
    assert (item.page, item.clause_ref, item.data) == (2, "2", b"png-bytes")
    assert item.heading_path == ("Section 2. Chart",)


def test_text_before_any_heading_is_kept():
    chunks = chunks_of(para("An opening paragraph with no heading at all."))
    assert len(chunks) == 1 and chunks[0].clause_ref is None and chunks[0].heading_path == ()


def test_a_title_with_no_body_makes_no_chunk():
    assert chunks_of(title("Section 1. Empty")) == []
