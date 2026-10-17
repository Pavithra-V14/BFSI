"""ONE real call through the evaluation judge key (EVAL_OPENROUTER_API_KEYS). Owner-run, off by
default.

    uv run python scripts/judge_smoke.py --run

It makes exactly one tiny call with a fixed question ("reply with the single word OK") through
the same code path an evaluation run uses for the judge: `GatewayConfig.for_evaluation` reads the
EVAL_* variables, `Gateway.complete_judge` sends the call on the `eval_openrouter` key pool and the
`EVAL_JUDGE_MODEL`. It prints the status, the model that answered, the key's short fingerprint,
the token counts, the finish reason and the time; it never prints a key. The answer text is
printed too, because the prompt is ours and trivial.

Nothing happens without `--run`. With `--run` it reads your `.env` file itself, but only the
gateway variables that `.env.example` lists, only into this process, and it never prints or
writes a value (the same rule as `scripts/gateway_smoke.py`). A variable already set in your
shell wins over the file. It refuses on Render. One call uses one request of the key's free-tier
daily allowance.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable, MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.gateway import Gateway, GatewayConfig  # noqa: E402
from app.gateway.embeddings import running_on_render  # noqa: E402
from app.gateway.errors import GatewayError  # noqa: E402
from scripts.gateway_smoke import load_owner_env  # noqa: E402

PROMPT = [{"role": "user", "content": "Reply with the single word OK."}]


def line(name: str, status: str, **details: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{name:<22} {status}" + (f"  {extra}" if extra else ""))


async def one_call(gateway: Gateway) -> int:
    config = gateway.config
    if not config.eval_openrouter_keys:
        line("judge key", "NOT SET", variable="EVAL_OPENROUTER_API_KEYS")
        return 1
    if not config.eval_judge_model:
        line("judge model", "NOT SET", variable="EVAL_JUDGE_MODEL")
        return 1
    line("judge key", "set", keys=len(config.eval_openrouter_keys))
    line("judge model", "set", model=config.eval_judge_model)
    try:
        result = await gateway.complete_judge(PROMPT, max_tokens=16, timeout_s=30.0)
    except GatewayError as error:
        # the class and the reason code only: a provider's message can hold an address
        line(
            "judge call", "FAILED", error=type(error).__name__, reason=getattr(error, "reason", "")
        )
        return 1
    text = (result.text or "").strip()
    line(
        "judge call",
        "OK" if text else "EMPTY",
        provider=result.provider,
        model=result.model,
        key=result.key_fingerprint,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        finish=result.finish_reason,
        seconds=f"{result.latency_s:.2f}",
        answer=repr(text[:40]),
    )
    return 0 if text else 1


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
        print("Nothing was called and no file was read. Add --run to make the one call.")
        return 0
    environ = os.environ if environ is None else environ
    if running_on_render(environ):
        print("refused: the evaluation keys never go on Render; run this on your own machine")
        return 1
    print(load_owner_env(environ, env_path, example_path))
    config = GatewayConfig.for_evaluation(environ)
    gateway = make_gateway(config) if make_gateway else Gateway(config)
    code = asyncio.run(one_call(gateway))
    print("done:", "the judge key works" if code == 0 else "the call did not work")
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
