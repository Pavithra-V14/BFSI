"""Structure rules of the API (Phase 10): the hosted API never reaches the ingestion package or an
NLP / parsing library, and the chat route owns its database connection."""

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "app" / "api"
FORBIDDEN = ("app.ingest", "spacy", "presidio_analyzer", "torch", "unstructured", "pdfminer")


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_no_api_module_imports_the_ingestion_package_or_an_nlp_library():
    for path in API.rglob("*.py"):
        for name in imported_names(path):
            assert not any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN), (
                path.name,
                name,
            )


def test_the_admin_data_helpers_the_api_uses_do_not_import_the_ingestion_package_either():
    for relative in ("app/db/review_queue.py", "app/db/entities.py", "app/vector/status.py"):
        for name in imported_names(ROOT / relative):
            assert not name.startswith("app.ingest"), (relative, name)


def test_importing_the_api_loads_no_nlp_or_ingestion_library():
    code = (
        "import sys\n"
        "import app.api.main, app.api.routes.chat, app.api.routes.admin_ops\n"
        "import app.api.routes.sessions, app.api.routes.feedback, app.vector.status\n"
        "import app.pipeline.run, app.pipeline.startup  # what create_app_from_env builds\n"
        "bad = [n for n in ('spacy', 'presidio_analyzer', 'torch', 'unstructured', 'pdfminer',\n"
        "                   'app.ingest') if n in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=180
    )
    assert done.returncode == 0, done.stderr[-500:]
    assert done.stdout.strip() == ""


def test_the_chat_route_is_not_metered_twice_and_closes_its_own_connection():
    """The chat route does not use the metered `authenticated` dependency (the pipeline counts the
    chat once) and does not use `get_db` (it must keep its connection open for a late write)."""
    source = (API / "routes" / "chat.py").read_text("utf-8")
    assert "authenticated" not in source.replace("open_user_context", "")
    assert "Depends(get_db)" not in source
    assert "open_user_context" in source and "_close_when_idle" in source


def test_every_other_user_route_uses_the_metered_dependency():
    for name in ("sessions.py", "feedback.py"):
        source = (API / "routes" / name).read_text("utf-8")
        assert "Depends(authenticated)" in source, name
    source = (API / "routes" / "admin_ops.py").read_text("utf-8")
    assert source.count("Depends(require_admin)") >= 12
