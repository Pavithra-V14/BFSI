"""Structure rules of the API (Phase 10, tightened in the Phase 10 hardening round).

The hosted API never reaches the ingestion package or a parsing / OCR / model library. spaCy and
Presidio are allowed in exactly one place, `app/guardrails` (the masker, spec 9.2), which the API
needs. The scan covers the whole `app` tree, not only `app/api`, because the API reaches the
pipeline, the memory store and the helpers; `app/ingest` is the one package that may use them (it
runs on the admin machine only)."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
API = APP / "api"

# Never in anything but app/ingest.
HEAVY = (
    "app.ingest", "torch", "unstructured", "transformers", "pdfminer", "pypdf", "PyPDF2", "fitz",
    "pymupdf", "pdfplumber", "pytesseract", "docling", "easyocr", "paddleocr", "ocrmypdf",
    "pdf2image", "camelot", "tabula", "layoutparser", "sentence_transformers", "cv2",
)  # fmt: skip
# Only in app/guardrails (and app/ingest, which has its own use of Presidio for names).
NLP = ("spacy", "presidio_analyzer", "presidio_anonymizer", "en_core_web_sm")


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def matches(name: str, banned: tuple[str, ...]) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in banned)


def outside_ingest():
    for path in APP.rglob("*.py"):
        relative = path.relative_to(APP).parts
        if relative[0] != "ingest":
            yield path, relative


def test_no_module_outside_the_ingestion_package_imports_it_or_a_parsing_or_model_library():
    checked = 0
    for path, _ in outside_ingest():
        checked += 1
        for name in imported_names(path):
            assert not matches(name, HEAVY), (str(path.relative_to(ROOT)), name)
    assert checked > 50  # the scan really walked the tree


def test_spacy_and_presidio_are_imported_only_inside_the_guardrails_package():
    found = []
    for path, relative in outside_ingest():
        for name in imported_names(path):
            if matches(name, NLP):
                found.append((relative[0], path.name))
                assert relative[0] == "guardrails", (str(path.relative_to(ROOT)), name)
    assert found, "the masker should import Presidio: the scan found nothing, so it is blind"


def test_the_scan_would_catch_a_forbidden_import(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    import torch\n    from app.ingest import cli\n", "utf-8")
    names = imported_names(bad)
    assert any(matches(n, HEAVY) for n in names) and "app.ingest" in names


def test_the_admin_data_helpers_the_api_uses_do_not_import_the_ingestion_package_either():
    for relative in ("app/db/review_queue.py", "app/db/entities.py", "app/vector/status.py"):
        for name in imported_names(ROOT / relative):
            assert not name.startswith("app.ingest"), (relative, name)


FRESH_PROCESS = """
import sys
LIBS = {libs!r}
import presidio_analyzer, spacy  # what these two load by themselves is not ours to judge
def loaded():
    return {{n for n in sys.modules if n.split('.')[0] in LIBS}}
before = loaded()
import app.api.main, app.api.routes.chat, app.api.routes.admin_ops
import app.api.routes.sessions, app.api.routes.feedback, app.vector.status
import app.pipeline.run, app.pipeline.startup  # what create_app_from_env builds
from app.pipeline.startup import MaskerHolder
holder = MaskerHolder()
holder.start()
ready = holder.wait(180)
extra = sorted(loaded() - before)
ingest = sorted(n for n in sys.modules if n == 'app.ingest' or n.startswith('app.ingest.'))
print('RESULT', 'READY' if ready else 'NOT_READY', ','.join(extra), '|', ','.join(ingest))
"""


def test_the_api_process_loads_no_ingestion_or_parsing_library_before_or_after_the_masker():
    libs = {name.split(".")[0] for name in HEAVY if not name.startswith("app.")}
    done = subprocess.run(
        [sys.executable, "-c", FRESH_PROCESS.format(libs=libs)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert done.returncode == 0, done.stderr[-800:]
    line = [ln for ln in done.stdout.splitlines() if ln.startswith("RESULT")][-1]
    head, _, ingest = line.partition("|")
    parts = head.split()
    # The masker's own load (spaCy model, Presidio) must not pull in a parsing or model library,
    # whether it finished (READY) or not, and nothing may load `app.ingest`.
    assert parts[1] in ("READY", "NOT_READY"), line
    assert len(parts) == 2, line  # no extra library after the state word
    assert ingest.strip() == "", line


def test_every_other_user_route_uses_the_metered_dependency():
    for name in ("sessions.py", "feedback.py"):
        source = (API / "routes" / name).read_text("utf-8")
        assert "Depends(authenticated)" in source, name
    source = (API / "routes" / "admin_ops.py").read_text("utf-8")
    assert source.count("Depends(require_admin)") >= 12
