"""Gateway smoke test with REAL provider calls. Off by default; the owner runs it by hand.

It makes three tiny calls and prints names and status only, never a key, a prompt
or a model's text:
  1. one Jev decision through the Decisions API (answers summary, served model
     snapshot, token usage and cost),
  2. one Gemini embedding (vector length and norm),
  3. one Gemini text call (token counts, finish reason, whether it was truncated).

Run it in your own terminal:
    uv run python scripts/gateway_smoke.py --run
Nothing happens without --run. With --run it reads your .env file itself, but only the
gateway variables that .env.example lists, only into this process, and it never prints
or writes a value. A variable already set in your shell wins over the file.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.gateway import Gateway, GatewayConfig, questions  # noqa: E402
from app.gateway.config import ENV_NAMES  # noqa: E402
from app.gateway.embeddings import DIMENSIONS, norm  # noqa: E402
from app.gateway.errors import GatewayError  # noqa: E402
from scripts.envfile import example_names, load_env_file  # noqa: E402


def line(name: str, status: str, **details: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{name:<22} {status}" + (f"  {extra}" if extra else ""))


def load_owner_env(
    environ: MutableMapping[str, str],
    env_path: Path | None = None,
    example_path: Path | None = None,
) -> str:
    """Load the gateway variables from .env into `environ`. Returns a message with names only."""
    path = env_path or ROOT / ".env"
    if not path.is_file():
        return "no .env file found: using the variables already set in this shell"
    allowed = example_names(example_path or ROOT / ".env.example") & set(ENV_NAMES)
    names = load_env_file(path, allowed, environ)
    listed = ", ".join(names) or "none"
    return f"loaded {len(names)} variable(s) from .env: {listed} (values are never shown)"


async def smoke(gateway: Gateway) -> int:
    config = gateway.config
    problems = 0

    if not config.jev_key:
        line("jev decision", "SKIPPED", reason="JEV_OPENROUTER_API_KEY is not set")
    else:
        result = await gateway.decide(
            [questions.domain_relevance()],
            {"message": "what is the waiting period for pre-existing conditions"},
        )
        if result.decider == "jev":
            answer = result.answers["domain_relevance"]
            line(
                "jev decision",
                "OK",
                decider=result.decider,
                model=result.model,
                snapshot=result.model_snapshot,
                probability=round(answer.probability, 2),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost=result.cost_billed,
                calibrated=result.calibrated,
            )
        else:
            problems += 1
            line("jev decision", "FELL BACK", decider=result.decider, reason=result.fallback_reason)

    if len(config.embedding_keys) == 0:
        line("gemini embedding", "SKIPPED", reason="GEMINI_EMBEDDING_KEY_A is not set")
    else:
        try:
            embedding = await gateway.embed(["waiting period"], task="query")
            vector = embedding.vectors[0]
            line(
                "gemini embedding",
                "OK",
                model=embedding.model,
                length=len(vector),
                expected=DIMENSIONS,
                norm=round(norm(vector), 4),
                key=embedding.key_fingerprint,
            )
        except GatewayError as error:
            problems += 1
            line("gemini embedding", "FAILED", reason=error.reason)

    if not config.gemini_keys:
        line("gemini text call", "SKIPPED", reason="GEMINI_API_KEYS is not set")
    else:
        try:
            completion = await gateway.complete(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=64,
                only_provider="gemini",
            )
            line(
                "gemini text call",
                "OK",
                model=completion.model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                reasoning_tokens=completion.reasoning_tokens,
                text_length=len(completion.text),
                truncated=completion.truncated,
                cost=completion.cost_billed,
                cost_source=completion.cost_source,
            )
        except GatewayError as error:
            problems += 1
            line("gemini text call", "FAILED", reason=error.reason)
    return problems


def main(
    argv: list[str],
    *,
    env_path: Path | None = None,
    example_path: Path | None = None,
    environ: MutableMapping[str, str] | None = None,
    make_gateway: Callable[[GatewayConfig], Gateway] | None = None,
) -> int:
    if "--run" not in argv:
        print(__doc__)
        print("Nothing was called and no file was read. Add --run to make the three real calls.")
        return 0
    environ = os.environ if environ is None else environ
    print(load_owner_env(environ, env_path, example_path))
    config = GatewayConfig.from_env(environ)
    gateway = make_gateway(config) if make_gateway else Gateway(config)
    problems = asyncio.run(smoke(gateway))
    print("done:", "all calls worked" if problems == 0 else f"{problems} call(s) did not work")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
