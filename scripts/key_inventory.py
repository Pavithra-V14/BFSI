"""Which keys does THIS window hold? Names, counts and short fingerprints only (spec 13.3).

    uv run python scripts/key_inventory.py            # the production settings
    uv run python scripts/key_inventory.py --all      # also the evaluation keys

For each key setting it prints whether it is set, how many keys it holds and the eight-character
fingerprint of each (the first characters of the key's SHA-256: they cannot be turned back into
the key). Use it for a key rotation (docs/KEY_ROTATION.md): run it before and after, and the
fingerprint that changed is the key that changed. It also names a key that appears in two
settings. It never prints a key, and it reads nothing from a file.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# setting -> (what it is for, the most keys it may hold)
PRODUCTION_KEYS: dict[str, str] = {
    "GROQ_API_KEYS": "Groq answers and triage",
    "GEMINI_API_KEYS": "Gemini answers, triage and image captions",
    "OPENROUTER_API_KEYS": "OpenRouter answers and triage",
    "JEV_OPENROUTER_API_KEY": "Jev decisions (its own key)",
    "GEMINI_EMBEDDING_KEY_A": "embeddings, first account",
    "GEMINI_EMBEDDING_KEY_B": "embeddings, second account",
    "COHERE_API_KEY": "rerank (Cohere)",
    "JINA_API_KEY": "rerank (Jina)",
}
EVALUATION_KEYS: dict[str, str] = {
    "EVAL_GROQ_API_KEYS": "evaluation: Groq",
    "EVAL_GEMINI_API_KEYS": "evaluation: Gemini",
    "EVAL_OPENROUTER_API_KEYS": "evaluation: OpenRouter and the judge",
}
# provider -> the settings that may hold its keys (the one-key-per-provider note counts these)
PROVIDERS: dict[str, tuple[str, ...]] = {
    "Groq": ("GROQ_API_KEYS",),
    "Gemini (answers)": ("GEMINI_API_KEYS",),
    "OpenRouter": ("OPENROUTER_API_KEYS",),
    "Gemini (embeddings)": ("GEMINI_EMBEDDING_KEY_A", "GEMINI_EMBEDDING_KEY_B"),
}
OTHER_SECRETS: dict[str, str] = {
    "JWT_SECRET_KEY": "signs sign-in tokens",
    "AUDIT_HASH_KEY": "keys the hashes of addresses and names",
    "QDRANT_API_KEY": "the vector store",
    "LANGFUSE_SECRET_KEY": "traces",
    "LANGFUSE_USER_HASH_KEY": "keys the hashed chat ids of trace deletion",
}


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(description="key names, counts and fingerprints")
    parser.add_argument("--all", action="store_true", help="include the evaluation keys")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    from app.gateway.keys import fingerprint

    settings = {**PRODUCTION_KEYS, **OTHER_SECRETS}
    if args.all:
        settings.update(EVALUATION_KEYS)
    seen: dict[str, list[str]] = defaultdict(list)
    out("setting | keys | fingerprints | used for")
    for name, purpose in settings.items():
        raw = (env.get(name) or "").strip()
        keys = [part.strip() for part in raw.split(",") if part.strip()]
        prints = [fingerprint(key) for key in keys]
        for index, mark in enumerate(prints):
            seen[mark].append(f"{name}[{index + 1}]")
        shown = ", ".join(prints) if prints else "-"
        out(f"{name} | {len(keys)} | {shown} | {purpose}")
    clashes = {mark: names for mark, names in seen.items() if len(names) > 1}
    for mark, names in sorted(clashes.items()):
        out(f"WARNING: the same key ({mark}) is in {', '.join(names)}")
    # Owner decision (Phase 16, docs/READINESS.md "Terms of service"): one key per provider, from
    # one account. More than one distinct key is only for the overlap of a rotation. A note, not
    # an error: the exit code is for a key that appears twice.
    for provider, names in PROVIDERS.items():
        distinct = {
            key
            for name in names
            for key in (part.strip() for part in (env.get(name) or "").split(","))
            if key
        }
        if len(distinct) > 1:
            out(
                f"NOTE: {provider} holds {len(distinct)} different keys; the rule is one key per "
                "provider from one account (docs/READINESS.md). Keep a second one only while a "
                "rotation overlaps, and do not count it as extra free quota."
            )
    if not args.all and any((env.get(n) or "").strip() for n in EVALUATION_KEYS):
        out("note: evaluation keys are set in this window; they must never be set on Render")
    return 1 if clashes else 0


if __name__ == "__main__":
    sys.exit(main())
