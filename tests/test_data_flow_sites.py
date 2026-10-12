"""The data-flow page must stay true (spec 13.4, Phase 15).

`docs/DATA_FLOW.md` says what each outside vendor receives. This test lists the places in the
application that can reach a network and fails when a new one appears that the page does not
cover: a module that imports a network library, or a hard-coded web address of a vendor. To make
it pass, someone has to decide what is sent there, write it on the page, and add the module here
with its vendor.

It cannot read intent: it finds new CALL SITES, not new FIELDS in an existing call. The page's own
"When this page must change" section covers that (a release record of kind guardrail or config).
"""

import ast
import re
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "DATA_FLOW.md").read_text("utf-8")

NETWORK_LIBRARIES = {
    "litellm", "httpx", "requests", "urllib.request", "redis", "psycopg", "psycopg2",
    "qdrant_client", "langfuse", "aiohttp", "smtplib", "socket", "openai", "anthropic",
    "groq", "cohere", "boto3",
}  # fmt: skip

# module -> (vendor words that must be on the data-flow page, what the module does with it)
SITES: dict[str, tuple[tuple[str, ...], str]] = {
    # the model providers, through LiteLLM (every model call goes through the gateway module)
    "app/gateway/backend.py": (("Groq", "Gemini", "OpenRouter"), "chat and embedding calls"),
    "app/gateway/context.py": (("Groq", "Gemini", "OpenRouter"), "context-window sizes (no call)"),
    "app/gateway/cost.py": (("Groq", "Gemini", "OpenRouter"), "list prices (no call)"),
    "app/gateway/errors.py": (("Groq", "Gemini", "OpenRouter"), "classifies provider errors"),
    "app/gateway/gateway.py": (("Groq", "Gemini", "OpenRouter"), "the one place that calls"),
    "app/gateway/decisions.py": (("Jev",), "the typed decisions API of OpenRouter"),
    "app/gateway/rerank.py": (("Cohere", "Jina"), "rerank requests"),
    # the stores
    "app/cache/store.py": (("Upstash",), "the answer cache and the counters"),
    "app/db/connection.py": (("Neon",), "the main database"),
    "app/db/migrate.py": (("Neon",), "migrations (admin machine)"),
    "app/accounts/users.py": (("Neon",), "accounts (database errors)"),
    "app/api/errors.py": (("Neon",), "database errors"),
    "app/memory/store.py": (("Neon",), "transcripts and summaries"),
    "app/store_errors.py": (("Neon", "Qdrant"), "classifies store errors"),
    "app/pipeline/retrieval.py": (("Qdrant",), "the passage search"),
    "app/vector/documents.py": (("Qdrant",), "the collection (admin machine)"),
    "app/vector/sparse.py": (("Qdrant",), "sparse vectors"),
    "app/vector/status.py": (("Qdrant",), "document counts"),
    "app/ingest/delete.py": (("Qdrant",), "delete (admin machine)"),
    "app/ingest/index.py": (("Qdrant",), "ingest (admin machine)"),
    "app/ingest/sparse_refresh.py": (("Qdrant",), "refresh (admin machine)"),
    "app/eval/scenarios.py": (("Qdrant",), "test collection edits (admin machine)"),
    # tracing and alerts
    "app/pipeline/langfuse_sink.py": (("Langfuse",), "per-stage records, no text"),
    "app/observability/traces.py": (("Langfuse",), "deletion of a chat's traces by hashed id"),
    "app/observability/webhook.py": (("webhook",), "the alert's name and a count"),
}

# every vendor web address written into the code, and who it is
HOSTS = {
    "openrouter.ai": "Jev",
    "api.cohere.com": "Cohere",
    "api.jina.ai": "Jina",
}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            for library in NETWORK_LIBRARIES:
                if name == library or name.startswith(library + "."):
                    found.add(name)
    return found


def _modules_with_network_imports() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        names = _imports(path)
        # urllib.parse is not a network call; only urllib.request is
        if names:
            out[path.relative_to(ROOT).as_posix()] = names
    return out


def test_every_module_that_can_reach_a_network_is_on_the_list():
    found = _modules_with_network_imports()
    unknown = sorted(set(found) - set(SITES))
    assert not unknown, (
        "a module that can reach a network is not in the data-flow list: decide what it sends, "
        "write it in docs/DATA_FLOW.md and add the module to SITES here: " + ", ".join(unknown)
    )


def test_every_listed_module_still_exists_and_still_has_a_network_import():
    found = _modules_with_network_imports()
    stale = sorted(path for path in SITES if path not in found)
    assert not stale, "remove from SITES (no longer a network site): " + ", ".join(stale)


def test_every_vendor_of_the_list_is_named_on_the_data_flow_page():
    words = {word for vendors, _why in SITES.values() for word in vendors}
    words |= set(HOSTS.values())
    missing = sorted(word for word in words if word not in DOC)
    assert not missing, missing


def test_every_vendor_web_address_in_the_code_is_a_known_vendor():
    hosts: dict[str, set[str]] = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            value = node.value
            if re.match(r"^https?://[A-Za-z0-9.-]+(/|$)", value):  # a web address, not a pattern
                host = urlparse(value).hostname
                if host:
                    hosts.setdefault(host, set()).add(path.relative_to(ROOT).as_posix())
    unknown = sorted(set(hosts) - set(HOSTS))
    assert not unknown, f"a web address of an unlisted vendor: {unknown}"
    for host, vendor in HOSTS.items():
        assert host in hosts, f"{host} is no longer in the code: remove it from HOSTS"
        assert vendor in DOC


def test_the_scan_really_sees_a_new_call_site(tmp_path):
    """The test would be worthless if it could not fail: a module with a new import is found."""
    sample = tmp_path / "new_module.py"
    sample.write_text("import httpx\nfrom litellm import acompletion\nimport json\n", "utf-8")
    assert _imports(sample) == {"httpx", "litellm"}
    other = tmp_path / "plain.py"
    other.write_text("import json\nimport urllib.parse\n", "utf-8")
    assert _imports(other) == set()


def test_the_page_states_the_two_things_a_reader_must_not_miss():
    assert "no real customer data" in DOC.lower()
    assert "RAW question is in transit" in DOC, "Render sees the question before it is masked"
