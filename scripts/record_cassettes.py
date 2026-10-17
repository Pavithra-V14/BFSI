"""Record the provider cassettes of the recorded API tests (spec 16). OWNER-RUN, REAL CALLS.

    uv run python scripts/record_cassettes.py            # shows the plan, calls nothing
    uv run python scripts/record_cassettes.py --run      # five real calls, one per provider
    uv run python scripts/record_cassettes.py --run --only cohere_rerank
    uv run python scripts/record_cassettes.py --run --only gemini_embedding --explain

`--explain` adds one line to a failed call: the error class, its HTTP status and a shortened,
scrubbed message (your key values, a credential after `key`, `token` or `authorization`, query
strings and long token-like words are removed). Without it only the class name is printed.

Nothing is called without `--run`. The keys are read from this terminal's settings (names in
`scripts/cassette_tools.KEY_SETTINGS`); a value is never printed, logged or written: the
recording scrubs headers and query parameters, cuts embedding vectors, replaces long
high-entropy tokens, and then every finished file is checked again by `hygiene_problems` and
deleted if it fails. The output holds names and counts only.

Run `gitleaks dir --redact --no-banner tests/cassettes` (or the history scan of Run 24) on the
folder before committing it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `python scripts/record_cassettes.py` has no package path
    sys.path.insert(0, str(ROOT))

from scripts import cassette_tools as tools  # noqa: E402


def keys_from(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """(the keys that are set, the NAMES of the settings that are missing)."""
    keys: dict[str, str] = {}
    missing: list[str] = []
    for alias, setting in tools.KEY_SETTINGS.items():
        value = (env.get(setting) or "").split(",")[0].strip()
        if value:
            keys[alias] = value
        else:
            missing.append(setting)
    return keys, missing


_LONG_WORD = re.compile(r"[A-Za-z0-9_-]{24,}")
_CREDENTIAL = re.compile(
    r"(?i)\b(key|api[_-]?key|token|authorization|bearer|secret)\b[\s=:\"']+\S+"
)
_QUERY = re.compile(r"\?[^\s\"'<>]+")


def scrubbed_detail(error: BaseException, secrets: list[str], limit: int = 320) -> str:
    """One line about a failed call that is safe to print: the class, the HTTP status when the
    error has one, and a short message with every key value, credential, query string and long
    token-like word removed. The cause of the error is added when there is one."""
    parts = []
    for item in (error, getattr(error, "__cause__", None)):
        if item is None:
            continue
        status = getattr(item, "status_code", None)
        text = " ".join(str(item).split())
        label = type(item).__name__ + (f" status={status}" if status else "")
        parts.append(f"{label}: {text}")
    text = " | ".join(parts)
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "[KEY]")
    text = _CREDENTIAL.sub(lambda m: m.group(1) + "=[REMOVED]", text)
    text = _QUERY.sub("?[query removed]", text)
    text = _LONG_WORD.sub("[long token removed]", text)
    return text[:limit] + (" ..." if len(text) > limit else "")


def _main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
    scenarios: Mapping[str, tools.Scenario] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="record_cassettes", description=__doc__.split("\n")[0])
    parser.add_argument("--run", action="store_true", help="make the real calls")
    parser.add_argument("--only", choices=tools.NAMES, help="record one cassette")
    parser.add_argument(
        "--explain", action="store_true", help="print a scrubbed detail line for a failed call"
    )
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    chosen = [args.only] if args.only else list(tools.NAMES)
    scenarios = scenarios if scenarios is not None else tools.SCENARIOS

    from app.gateway.embeddings import running_on_render

    if running_on_render(env):
        out("refused: cassettes are recorded on the admin machine only, never on Render")
        return 1
    keys, missing = keys_from(env)
    needed = {tools.KEY_SETTINGS[tools.SCENARIO_KEY[n]] for n in chosen}
    absent = sorted(needed & set(missing))

    out(f"plan: {len(chosen)} real call(s), one per provider")
    for name in chosen:
        setting = tools.KEY_SETTINGS[tools.SCENARIO_KEY[name]]
        state = "key set" if setting not in missing else "key NOT set"
        out(f"  {name}: {setting} ({state})")
    if not args.run:
        out("nothing was called: add --run to record")
        return 0
    if absent:
        out("not started, these settings are missing: " + ", ".join(absent))
        return 1

    tools.CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    for name in chosen:
        path = tools.cassette_path(name)
        path.unlink(missing_ok=True)
        try:
            with tools.use_cassette(name, record_mode="all"):
                asyncio.run(scenarios[name](keys))
        except Exception as error:  # the class name only: a provider error may echo a request
            path.unlink(missing_ok=True)
            out(f"{name}: FAILED ({type(error).__name__}); nothing was written")
            if args.explain:
                out("  detail: " + scrubbed_detail(error, list(keys.values())))
            failed += 1
            continue
        problems = (
            tools.hygiene_problems(path.read_text("utf-8"), forbidden=list(keys.values()))
            if path.is_file()
            else ["none"]
        )
        if problems:
            path.unlink(missing_ok=True)
            out(f"{name}: DELETED, the recording broke the hygiene rules: {', '.join(problems)}")
            failed += 1
        else:
            out(f"{name}: recorded ({path.stat().st_size} bytes), hygiene rules passed")
    out(f"{len(chosen) - failed} of {len(chosen)} cassette(s) recorded")
    return 1 if failed else 0


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    out: Callable[[str], None] = print,
    scenarios: Mapping[str, tools.Scenario] | None = None,
    folder: Path | None = None,
) -> int:
    """`folder` is for the tests: it points the recording at a scratch folder for this call."""
    original = tools.CASSETTE_DIR
    if folder is not None:
        tools.CASSETTE_DIR = folder
    try:
        return _main(argv, env, out=out, scenarios=scenarios)
    finally:
        tools.CASSETTE_DIR = original


if __name__ == "__main__":
    raise SystemExit(main())
