"""Smaller ingestion parts: parsing, the Unstructured conversions, originals, the embedder wrapper,
and the rule that the API never imports ingestion."""

import base64
import hashlib
from pathlib import Path

import pytest

from app.gateway.embeddings import DevEmbedderForbidden
from app.ingest import unstructured_backend as backend
from app.ingest.embedding import make_embedder
from app.ingest.model import Element, IngestError, check_doc_id
from app.ingest.originals import keep_original
from app.ingest.parse import parse_pdf
from app.ingest.runner import LoopRunner

ROOT = Path(__file__).resolve().parents[1]


# ---- parse ------------------------------------------------------------------------------------
def test_one_failing_page_is_reported_and_the_others_are_still_parsed():
    def page_parser(path, number):
        if number == 2:
            raise ValueError("secret page text must not be copied")
        return [Element("text", f" page {number} text ", 99)]

    document = parse_pdf(Path("x.pdf"), page_count=3, parse_page=page_parser)
    assert [(f.page, f.reason) for f in document.failed_pages] == [(2, "ValueError")]
    assert [(e.page, e.text) for e in document.elements] == [(1, "page 1 text"), (3, "page 3 text")]
    assert "secret" not in repr(document.failed_pages)  # the reason is a class name only


def test_empty_elements_are_dropped_and_pages_are_taken_from_the_page_loop():
    elements = [Element("text", "   ", 1), Element("table", "", 1, rows=(("a", "b"),))]
    document = parse_pdf(Path("x.pdf"), page_count=1, parse_page=lambda p, n: elements)
    assert len(document.elements) == 1 and document.elements[0].kind == "table"


# ---- the Unstructured conversions (no Unstructured needed) ---------------------------------------
def test_table_html_becomes_rows():
    html = (
        "<table><tr><th>Plan</th><th>Limit</th></tr>"
        "<tr><td>Gold</td><td> 5\n lakh </td></tr></table>"
    )
    assert backend.rows_from_html(html) == (("Plan", "Limit"), ("Gold", "5 lakh"))
    assert backend.rows_from_html(None) == () and backend.rows_from_html("<p>none</p>") == ()


def test_unstructured_categories_map_to_our_kinds():
    make = backend.element_from_unstructured
    assert make("Title", " Section 1 ", 2) == Element("title", "Section 1", 2)
    assert make("NarrativeText", "Body.", 1).kind == "text"
    assert make("ListItem", "- item", 1).kind == "list_item"
    assert make("Footer", "page 3 of 9", 1) is None and make("PageNumber", "3", 1) is None
    assert make("Title", "   ", 1) is None
    table = make("Table", "a b", 1, html="<tr><td>a</td><td>b</td></tr>")
    assert table.kind == "table" and table.rows == (("a", "b"),)


def test_an_image_element_carries_its_decoded_bytes_and_a_bad_payload_is_dropped():
    encoded = base64.b64encode(b"\x89PNG-bytes").decode()
    assert (
        backend.element_from_unstructured("Image", "", 3, image_base64=encoded).image
        == b"\x89PNG-bytes"
    )
    assert backend.element_from_unstructured("Image", "", 3, image_base64="!!not base64!!") is None
    assert backend.element_from_unstructured("Image", "", 3) is None


# ---- originals ---------------------------------------------------------------------------------
def test_an_original_is_copied_once_under_its_hash_and_never_overwritten(tmp_path):
    source = tmp_path / "policy.pdf"
    source.write_bytes(b"%PDF original")
    kept = keep_original(source, tmp_path / "originals", "doc-a")
    digest = hashlib.sha256(b"%PDF original").hexdigest()
    assert kept == tmp_path / "originals" / "doc-a" / f"{digest}.pdf"
    assert kept.read_bytes() == b"%PDF original"
    kept.write_bytes(b"edited by hand")
    keep_original(source, tmp_path / "originals", "doc-a")  # a second run does not overwrite it
    assert kept.read_bytes() == b"edited by hand"
    source.write_bytes(b"%PDF new version")  # a new version is a new file; the old one stays
    other = keep_original(source, tmp_path / "originals", "doc-a")
    assert other != kept and kept.exists() and other.exists()


def test_the_original_is_filed_by_doc_id_never_by_the_path(tmp_path):
    source = tmp_path / "some" / "deep" / "name.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF")
    kept = keep_original(source, tmp_path / "originals", "my-doc")
    assert "deep" not in str(kept.relative_to(tmp_path)) and "my-doc" in kept.parts
    with pytest.raises(IngestError):
        keep_original(source, tmp_path / "originals", "../escape")


@pytest.mark.parametrize("good", ["a", "doc-1", "insurance-flexi-property", "9"])
def test_good_doc_ids(good):
    assert check_doc_id(good) == good


# ---- the embedder wrapper ----------------------------------------------------------------------
class FakeGateway:
    def __init__(self, dev=False):
        self.dev, self.calls = dev, []

    async def embed(self, texts, *, task, titles, max_wait_s):
        self.calls.append((list(texts), task, list(titles), max_wait_s))

        class Result:
            vectors = [[1.0] + [0.0] * 767 for _ in texts]
            is_dev_fallback = self.dev

        return Result()


def test_the_embedder_asks_the_gateway_for_document_vectors_with_the_title():
    runner, gateway = LoopRunner(), FakeGateway()
    try:
        vectors = make_embedder(gateway, runner)(["a", "b"], "Policy A")
    finally:
        runner.close()
    assert len(vectors) == 2
    texts, task, titles, wait = gateway.calls[0]
    assert (texts, task, titles) == (["a", "b"], "document", ["Policy A", "Policy A"]) and wait > 0


def test_development_vectors_are_never_indexed():
    runner = LoopRunner()
    try:
        with pytest.raises(DevEmbedderForbidden):
            make_embedder(FakeGateway(dev=True), runner)(["a"], None)
        assert make_embedder(FakeGateway(dev=True), runner, allow_dev=True)(["a"], None)
    finally:
        runner.close()


# ---- local-only rules ------------------------------------------------------------------------
def test_runtime_requirements_hold_no_ingestion_package_but_do_hold_the_masking_ones():
    lines = (ROOT / "requirements.txt").read_text("utf-8").lower().splitlines()
    runtime = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
    for name in ("unstructured", "pypdf", "tesseract", "torch", "transformers", "en_core_web_lg"):
        assert name not in runtime
    # Phase 6: query masking runs in production, with the SMALL model only
    for name in ("presidio-analyzer==", "spacy==", "en_core_web_sm @"):
        assert name in runtime
