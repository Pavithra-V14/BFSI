"""Gateway smoke test with REAL provider calls. Off by default; the owner runs it by hand.

It makes a few tiny calls and prints names and status only, never a key, a prompt
or a model's text:
  1. one Jev decision through the Decisions API (answers summary, served model
     snapshot, token usage and cost),
  2. one Gemini embedding (vector length and norm),
  3. one text call each to Gemini, Groq and OpenRouter, when their keys are present. They use
     the real answer limit and print whether the text is non-empty, the finish reason, and the
     reasoning and output token counts. An empty answer is reported as EMPTY.

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

    # Text calls use the real answer limit (no max_tokens), so what you see here is what a real
    # answer gets. An empty answer counts as a problem even though the call itself worked.
    for name, provider, keys, variable in TEXT_CALLS:
        if not getattr(config, keys):
            line(name, "SKIPPED", reason=f"{variable} is not set")
            continue
        try:
            completion = await gateway.complete(
                [{"role": "user", "content": "Reply with the single word: ok"}],
                only_provider=provider,
            )
        except GatewayError as error:
            problems += 1
            skipped = getattr(error, "skipped", None) or []
            why = ",".join(f"{provider}:{reason}" for provider, reason in skipped)
            line(name, "FAILED", reason=error.reason, why=why or None)
            continue
        has_text = bool(completion.text.strip())
        problems += 0 if has_text else 1
        line(
            name,
            "OK" if has_text else "EMPTY",
            model=completion.model,
            text_non_empty=has_text,
            finish_reason=completion.finish_reason,
            reasoning_tokens=completion.reasoning_tokens,
            output_tokens=completion.output_tokens,
            input_tokens=completion.input_tokens,
            truncated=completion.truncated,
            cost=completion.cost_billed,
            cost_source=completion.cost_source,
        )
    return problems


# (label, provider name in the gateway, config field holding its keys, variable to set)
TEXT_CALLS = [
    ("gemini text call", "gemini", "gemini_keys", "GEMINI_API_KEYS"),
    ("groq text call", "groq", "groq_keys", "GROQ_API_KEYS"),
    ("openrouter text call", "openrouter", "openrouter_keys", "OPENROUTER_API_KEYS"),
]


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
        print("Nothing was called and no file was read. Add --run to make the real calls.")
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
