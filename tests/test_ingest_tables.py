"""Real-parser fixes found on the sample PDFs: table cells from the text layer, page furniture,
clause labels. The synthetic tests need nothing installed; the sample-PDF tests need pdfminer
(installed with requirements-ingest.txt) and skip without it."""

from pathlib import Path

import pytest

from app.ingest.chunker import chunk_document, clause_label
from app.ingest.model import Element
from app.ingest.parse import drop_page_furniture, parse_pdf
from app.ingest.table_grid import PageLayout, Word, box_from_pixels, grid_rows
from app.ingest.unstructured_backend import element_from_unstructured
from tests.ingest_fixtures import para, parsed, table, title, words

SAMPLES = Path(__file__).resolve().parents[1] / "spec" / "samples" / "company"


# ---- page furniture --------------------------------------------------------------------------
def three_pages():
    header, footer = "Insurance | AuraVance | Policy Guide", "Document code AV-1 | Page {} of 3"
    elements = []
    for page in (1, 2, 3):
        elements += [
            Element("title", header, page),
            Element("title", f"Part {page}. Real heading", page),
            Element("text", f"Real body text for page {page}.", page),
            Element("text", footer.format(page), page),
        ]
    return elements


def test_running_headers_and_footers_are_dropped_and_real_content_is_kept():
    kept, dropped = drop_page_furniture(three_pages(), 3)
    assert dropped == 6
    assert [e.text for e in kept if e.page == 2] == [
        "Part 2. Real heading",
        "Real body text for page 2.",
    ]


def test_a_line_on_too_few_pages_is_content_not_furniture():
    elements = three_pages() + [Element("text", "Only on one page.", 1)]
    kept, _ = drop_page_furniture(elements, 3)
    assert any(e.text == "Only on one page." for e in kept)
    two_of_ten = [Element("text", "Seen twice.", 1), Element("text", "Seen twice.", 4)]
    assert drop_page_furniture(two_of_ten, 10)[1] == 0  # 2 of 10 pages is below 60%


def test_tables_and_images_are_never_dropped_as_furniture():
    same = [Element("table", "a b", p, rows=(("a", "b"),)) for p in (1, 2, 3)]
    same += [Element("image", "", p, image=b"x") for p in (1, 2, 3)]
    assert drop_page_furniture(same, 3) == (same, 0)


def test_a_one_page_document_has_no_furniture():
    elements = [Element("text", "Same.", 1), Element("text", "Same.", 1)]
    assert drop_page_furniture(elements, 1) == (elements, 0)


def test_the_parser_reports_how_much_furniture_it_removed():
    pages = {n: [e for e in three_pages() if e.page == n] for n in (1, 2, 3)}
    document = parse_pdf(Path("x.pdf"), page_count=3, parse_page=lambda p, n: pages[n])
    assert document.furniture_dropped == 6 and len(document.elements) == 6


def test_a_failed_page_does_not_hide_furniture_on_the_others():
    pages = {n: [e for e in three_pages() if e.page == n] for n in (1, 2, 3)}

    def parse_page(path, n):
        if n == 3:
            raise ValueError("bad")
        return pages[n]

    document = parse_pdf(Path("x.pdf"), page_count=3, parse_page=parse_page)
    assert document.furniture_dropped == 4 and [f.page for f in document.failed_pages] == [3]


def test_unstructured_headers_and_footers_are_not_content():
    assert element_from_unstructured("Header", "Page header", 1) is None
    assert element_from_unstructured("Footer", "Page 2 of 3", 1) is None


def test_a_footer_no_longer_ends_up_inside_the_last_clause():
    elements = [
        title("Section 5. Role"), para("AuraVance solicits and services this policy."),
        para("Document code AV-1 | Page 1 of 3"),
        title("Section 6. Claims"), para("Notify the insurer at once."),
        para("Document code AV-1 | Page 2 of 3"),
        para("Document code AV-1 | Page 3 of 3", 3),
    ]  # fmt: skip
    elements = [
        Element(e.kind, e.text, 1 if i < 3 else 2 if i < 6 else 3) for i, e in enumerate(elements)
    ]
    kept, _ = drop_page_furniture(elements, 3)
    chunks = chunk_document(parsed(*kept), words).chunks
    assert all("Document code" not in c.text for c in chunks)


# ---- clause labels -----------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "ref", "label"),
    [
        ("5. Penal Charges and Prepayment. Charges apply.", "5", "5. Penal Charges and Prepayment"),
        ("10. SARFAESI Act: Disclosures. Where the Company", "10", "10. SARFAESI Act: Disclosures"),
        ("Clause 4. Grievances. Text here.", "4", "4. Grievances"),
        ("3.2 The waiting period is 30 days and applies to all illness claims made.", "3.2", "3.2"),
        ("3.1 First rule applies here.", "3.1", "3.1"),
    ],
)  # fmt: skip
def test_a_numbered_paragraph_is_labelled_with_its_short_title(text, ref, label):
    assert clause_label(text, ref) == label


def test_a_table_under_a_numbered_paragraph_carries_the_clause_title():
    chunks = chunk_document(
        parsed(para("5. Penal Charges. Charges apply as below."), table([["a", "b"], ["1", "2"]])),
        words,
    ).chunks
    [tbl] = [c for c in chunks if c.kind == "table"]
    assert tbl.text.startswith("5. Penal Charges\n") and tbl.heading_path == ("5. Penal Charges",)


# ---- table cells from the text layer -------------------------------------------------------------
def layout(words_, h, v):
    return PageLayout(
        0, 0, 600, 800,
        words=[Word(*w) for w in words_],
        h_rules=[(y, 0, 300) for y in h],
        v_rules=[(x, 0, 100) for x in v],
    )  # fmt: skip


BOX = (0, 0, 300, 100)


def simple_grid():
    """Three rows by three columns; the header cell "Secured Prov." wraps over two lines."""
    rules_y, rules_x = [0, 30, 60, 90], [0, 100, 200, 300]
    cells = [
        (10, 75, 90, 85, "Asset"),
        (110, 80, 140, 88, "Secured"),
        (110, 70, 140, 78, "Prov."),
        (210, 75, 260, 85, "Rate"),
        (10, 45, 80, 55, "Standard"),
        (110, 45, 140, 55, "70.00"),
        (210, 45, 240, 55, "0.40"),
        (10, 15, 60, 25, "SMA-0"),
        (110, 15, 140, 25, "70.00"),
        (210, 15, 240, 25, "0.40"),
    ]
    return layout(cells, rules_y, rules_x)


def test_cells_are_rebuilt_from_words_and_ruling_lines_top_row_first():
    rows = grid_rows(simple_grid(), BOX)
    assert rows == (
        ("Asset", "Secured Prov.", "Rate"),
        ("Standard", "70.00", "0.40"),
        ("SMA-0", "70.00", "0.40"),
    )


def test_no_ruling_lines_means_no_grid_so_the_caller_keeps_the_ocr_cells():
    assert grid_rows(layout([(10, 10, 20, 20, "x")], [], []), BOX) is None
    assert grid_rows(layout([(10, 10, 20, 20, "x")], [0, 50, 100], [0, 300]), BOX) is None


def test_words_above_a_top_rule_that_is_not_drawn_join_the_top_row_and_none_is_lost():
    base = simple_grid()
    base.words += [Word(110, 95, 160, 99, "Quarterly"), Word(165, 95, 190, 99, "figures")]
    base.h_rules = [r for r in base.h_rules if r[0] != 90]  # the top rule is not drawn
    rows = grid_rows(base, BOX)
    assert rows is not None
    assert rows[0] == ("Asset", "Quarterly figures Secured Prov.", "Rate")
    assert rows[1:] == (("Standard", "70.00", "0.40"), ("SMA-0", "70.00", "0.40"))


def test_a_border_that_is_not_drawn_is_the_edge_of_the_words_beyond_it():
    # a two-column key/value table whose right border is missing
    cells = [(10, 65, 60, 75, "Pillar"), (110, 65, 200, 75, "Banking"),
             (10, 35, 60, 45, "Type"), (110, 35, 220, 45, "Agreement")]  # fmt: skip
    lay = layout(cells, [0, 30, 60, 90], [0, 100])
    assert grid_rows(lay, BOX) == (("Pillar", "Banking"), ("Type", "Agreement"))


def test_a_border_drawn_in_short_pieces_is_still_a_border():
    lay = simple_grid()
    whole = [(x, a, b) for x, a, b in lay.v_rules if x == 100]
    lay.v_rules = [r for r in lay.v_rules if r[0] != 100]
    lay.v_rules += [(100, 0, 30), (100, 30, 60), (100, 60, 100)]  # three pieces, same line
    assert whole and grid_rows(lay, BOX) == grid_rows(simple_grid(), BOX)


def test_a_short_stray_line_is_not_a_border():
    lay = simple_grid()
    lay.v_rules.append((150, 40, 50))  # 10 points of a 100-point table: a mark, not a border
    assert grid_rows(lay, BOX) == grid_rows(simple_grid(), BOX)


def test_an_empty_column_is_dropped_and_an_empty_row_is_dropped():
    lay = simple_grid()
    lay.v_rules.append((250, 0, 100))  # an extra, empty column
    lay.h_rules.append((100, 0, 300))  # an extra, empty row at the top
    assert grid_rows(lay, (0, 0, 300, 100)) == grid_rows(simple_grid(), BOX)


def test_pixel_points_become_pdf_coordinates_with_the_top_of_the_page_at_y_zero():
    lay = PageLayout(0, 0, 600, 800)
    box = box_from_pixels(lay, [(100, 200), (100, 400), (300, 400), (300, 200)], 1200, 1600)
    assert box == (50.0, 600.0, 150.0, 700.0)  # 2 px per point; y flips


def test_the_ocr_cells_are_used_only_when_there_is_no_grid():
    html = "<tr><td>Garbled</td><td>cell</td></tr>"
    ocr = element_from_unstructured("Table", "Clean text", 1, html=html)
    assert ocr.rows == (("Garbled", "cell"),) and ocr.text == "Clean text"
    grid = (("Asset", "Rate"), ("Standard", "0.40"))
    assert element_from_unstructured("Table", "t", 1, html=html, grid=grid).rows == grid


# ---- the real sample PDFs (need pdfminer, installed with requirements-ingest.txt) --------------
@pytest.fixture
def pdfminer():
    return pytest.importorskip("pdfminer")


def page_two_of(name, tmp_path):
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(str(SAMPLES / name))
    writer = pypdf.PdfWriter()
    writer.add_page(reader.pages[1])
    path = tmp_path / "page.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_the_banking_provisioning_table_is_read_cleanly_from_the_pdf(pdfminer, tmp_path):
    from app.ingest.table_grid import read_layout

    lay = read_layout(page_two_of("banking_rtl_standard_facility_agreement.pdf", tmp_path))
    # Unstructured's box for the first table of this page, in pixels of a 2894 x 4093 render.
    box = box_from_pixels(lay, [(202, 566), (202, 1175), (2700, 1175), (2700, 566)], 2894, 4093)
    rows = grid_rows(lay, box)
    assert rows is not None and len(rows) == 10 and {len(r) for r in rows} == {8}
    assert rows[-1][0] == "Loss asset" or rows[-1][0].startswith("Loss")
    assert rows[0] == (
        "Asset Category", "Default / Ageing Window", "Secured Portion (INR cr)",
        "Unsecured Portion (INR cr)", "Secured Prov. (%)", "Unsecured Prov. (%)",
        "Provision (INR cr)", "Effective Prov. (%)",
    )  # fmt: skip
    assert rows[1] == (
        "Standard asset",
        "0 days past due",
        "70.00",
        "30.00",
        "0.40",
        "0.40",
        "0.40",
        "0.40",
    )
    assert rows[2][0] == "SMA-0 (special mention)"  # the OCR read this as "SMA-O"
