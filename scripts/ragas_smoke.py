"""Does RAGAS work through our own judge path? One made-up question, no document, no customer data.

    uv run python scripts/ragas_smoke.py          # offline: a scripted judge, NO call, no file read
    uv run python scripts/ragas_smoke.py --run    # owner-run: the real judge, a few real calls
    uv run python scripts/ragas_smoke.py --run --case f01-penal-charge   # one stored real case

Offline it proves the wiring: RAGAS is installed, the metrics build, `strictness` is 1, every prompt
goes to OUR function (never to a provider) and a score comes back. With `--run` the same sample is
scored by the real judge (Groq with its own key, else OpenRouter; embeddings on the Gemini key) and
the script prints, per metric, the score or the reason it could not be scored, and how many judge
requests it used. It never prints a key. Refuses on Render. The calls count against the judge's
daily allowance (about 3 for faithfulness plus 1 for relevancy; a handful in all).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import MutableMapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUESTION = "What penal charge applies to an overdue amount?"
ANSWER = "A penal charge of 2.00 per cent per annum applies on the overdue amount only."
CONTEXTS = ["Penal charges of 2.00 per cent per annum apply on the overdue amount only."]
REFERENCE = "A penal charge of 2.00 per cent per annum applies on the overdue amount only."
# the two that need no reference, the three that do
METRICS = (
    "faithfulness", "answer_relevancy", "context_precision", "context_recall",
    "answer_correctness",
)  # fmt: skip


def line(name: str, status: str, **details: object) -> None:
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    print(f"{name:<24} {status}" + (f"  {extra}" if extra else ""))


def stored_case(case_id: str, db_path: Path) -> dict | None:
    """The newest stored, answered item of one case from the LOCAL evaluation database (read-only):
    its question and reference are the authored (synthetic) ones, its answer and passages are what
    the system produced then. Returns None when there is none."""
    import sqlite3

    from app.eval.judging import strip_fixed_text

    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute(
            "SELECT i.answer_text, i.contexts, c.turns, c.expect FROM eval_items i "
            "JOIN eval_cases c ON c.id = i.case_id WHERE i.case_id = ? "
            "AND i.answer_kind = 'answer' AND i.answer_text IS NOT NULL "
            "ORDER BY i.finished_at DESC LIMIT 1",
            (case_id,),
        ).fetchone()
    if row is None:
        return None
    answer, contexts, turns, expect = row
    return {
        "question": json.loads(turns)[-1],
        "answer": strip_fixed_text(answer),
        "contexts": [str(x) for x in json.loads(contexts or "[]")],
        "reference": json.loads(expect).get("reference"),
    }


async def _score(ask, embed, metrics=METRICS, sample=None) -> tuple[dict, dict]:
    from app.eval import ragas_adapter as ra

    sample = sample or {
        "question": QUESTION, "answer": ANSWER, "contexts": CONTEXTS, "reference": REFERENCE,
    }  # fmt: skip
    reasons: dict[str, str] = {}
    scores = await ra.score_sample(
        question=sample["question"],
        answer=sample["answer"],
        contexts=sample["contexts"],
        reference=sample["reference"],
        llm=ra.GatewayLLM(ask),
        embeddings=ra.GatewayEmbeddings(embed),
        metrics=metrics,
        reasons=reasons,
    )
    return scores, reasons


def offline() -> int:
    """A scripted judge: proves the library, the wrapper and strictness 1, without any call."""
    from app.eval import ragas_adapter as ra

    prompts: list[str] = []

    async def ask(prompt: str) -> str:
        prompts.append(prompt)
        low = prompt.lower()
        if "statements" in low and "verdict" in low:
            return json.dumps({"statements": [{"statement": "s", "reason": "r", "verdict": 1}]})
        if "break down" in low or "statements" in low:
            return json.dumps({"statements": [ANSWER]})
        return json.dumps({"question": QUESTION, "noncommittal": 0})

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, float(len(t) % 3)] for t in texts]

    line("ragas", "installed")
    line("strictness", "ok" if ra.STRICTNESS == 1 else "WRONG", value=ra.STRICTNESS)
    wanted = METRICS[:2]  # the scripted judge answers these two shapes only
    scores, reasons = asyncio.run(_score(ask, embed, wanted))
    for name in wanted:
        value = scores.get(name)
        line(
            name,
            "scored" if value is not None else "NOT SCORED",
            value=value,
            why=reasons.get(name),
        )
    line("judge prompts", "sent to our function", count=len(prompts))
    ok = ra.STRICTNESS == 1 and all(scores.get(name) is not None for name in wanted)
    print("done:", "RAGAS works through the wrapper (offline)" if ok else "something is wrong")
    return 0 if ok else 1


def real(environ: MutableMapping[str, str], case_id: str | None = None) -> int:
    from app.eval.judge import Judge
    from app.gateway import Gateway, GatewayConfig
    from app.gateway.errors import GatewayError
    from scripts.gateway_smoke import load_owner_env

    print(load_owner_env(environ, None, None))
    config = GatewayConfig.for_evaluation(environ)
    gateway = Gateway(config)
    judge = Judge(gateway)
    steps = gateway.judge_steps()
    if not steps:
        line("judge", "NOT SET")
        return 1
    line("judge chain", "set", steps=" > ".join(f"{s.provider}:{s.model}" for s in steps))
    count = {"text": 0}
    codes: list[str] = []  # why a judge call failed: provider:code pairs, never a message

    async def ask(prompt: str) -> str:
        count["text"] += 1
        try:
            return await judge.text(prompt, json_mode=True)
        except Exception as error:
            skipped = getattr(error.__cause__, "skipped", None) or []
            codes.append(",".join(f"{p}:{code}" for p, code in skipped) or type(error).__name__)
            raise

    sample = None
    if case_id:  # scored the way a run scores it: without the disclaimer our own code appended
        sample = stored_case(case_id, ROOT / "eval_data" / "eval.db")
        if sample is None:
            line("case", "NOT FOUND", id=case_id)
            return 1
        size = sum(len(x) for x in sample["contexts"])
        line("case", "loaded", id=case_id, passages=len(sample["contexts"]), passage_chars=size)
    try:
        scores, reasons = asyncio.run(_score(ask, judge.embed, METRICS, sample))
    except GatewayError as error:
        line(
            "judge call", "FAILED", error=type(error).__name__, reason=getattr(error, "reason", "")
        )
        return 1
    for name in METRICS:
        value = scores.get(name)
        line(
            name,
            "scored" if value is not None else "NOT SCORED",
            value=value,
            why=reasons.get(name),
        )
    line("judge requests", "used", count=count["text"])
    if codes:
        line("judge failures", "by cause", causes=" | ".join(sorted(set(codes))))
    done = all(scores.get(name) is not None for name in METRICS)
    print("done:", "RAGAS works with the real judge" if done else "some metrics were not scored")
    return 0 if done else 1


def main(argv: list[str], *, environ: MutableMapping[str, str] | None = None) -> int:
    if "--run" not in argv:
        return offline()
    from app.gateway.embeddings import running_on_render

    environ = os.environ if environ is None else environ
    if running_on_render(environ):
        print("refused: the evaluation keys never go on Render; run this on your own machine")
        return 1
    case_id = argv[argv.index("--case") + 1] if "--case" in argv[:-1] else None
    return real(environ, case_id)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
