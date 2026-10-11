"""The dev-only live stage printer (spec 14, `DEV_STAGE_TRACE`).

Terminal only, OFF by default and OFF in production. While a query runs it prints each stage as it
starts and as it finishes with its time, the decider / model / provider and key FINGERPRINT used and
the token counts, and at the end a waterfall: each stage's share of the request, the slowest stage
and the time no stage accounts for. It prints stage names, timings and counts only: a span holds
no text at all (see `app.pipeline.trace`), and nothing here reads the question or the answer.

It is refused on Render and whenever the flag is not exactly "true"; the refusal is code, not a
comment (`printer_from_env`).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Mapping
from typing import Any, TextIO

ENV_FLAG = "DEV_STAGE_TRACE"
BAR_WIDTH = 24


class StagePrinter:
    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out or sys.stderr
        self._lock = threading.Lock()

    def _line(self, text: str) -> None:
        with self._lock:
            print(text, file=self._out, flush=True)

    def stage_started(self, stage: str) -> None:
        self._line(f"  > {stage}")

    def stage_finished(self, span: Any) -> None:
        who = " ".join(
            part for part in (span.decider, span.provider, span.model, span.key_fingerprint) if part
        )
        tokens = ""
        if span.input_tokens or span.output_tokens:
            tokens = f"  tokens in={int(span.input_tokens)} out={int(span.output_tokens)}"
        note = f"  [{span.status}]" if span.status != "ok" else ""
        self._line(
            f"  < {span.stage:<18} {float(span.duration_ms):>8.1f} ms"
            f"{('  ' + who) if who else ''}{tokens}{note}"
        )

    def waterfall(self, spans: list[Any], total_ms: float) -> None:
        """Each stage's share of the request, the slowest stage and the unaccounted time."""
        if not spans:
            return
        total = max(float(total_ms), 1e-9)
        lines = ["  waterfall:"]
        for span in spans:
            share = max(0.0, min(1.0, float(span.duration_ms) / total))
            bar = "#" * max(1, round(share * BAR_WIDTH)) if span.duration_ms else ""
            lines.append(
                f"    {span.stage:<18} {float(span.duration_ms):>8.1f} ms "
                f"{share * 100:>5.1f}%  {bar}"
            )
        accounted = sum(float(s.duration_ms) for s in spans)
        slowest = max(spans, key=lambda s: float(s.duration_ms))
        lines.append(f"    slowest stage: {slowest.stage} ({float(slowest.duration_ms):.1f} ms)")
        lines.append(f"    unaccounted: {max(0.0, float(total_ms) - accounted):.1f} ms")
        lines.append(f"    total: {float(total_ms):.1f} ms")
        self._line("\n".join(lines))


def printer_from_env(
    env: Mapping[str, str], *, running_on_render: bool, out: TextIO | None = None
) -> StagePrinter | None:
    """The printer, or None. Only the exact word "true" switches it on, and never on Render."""
    if (env.get(ENV_FLAG) or "").strip().lower() != "true":
        return None
    if running_on_render:
        return None
    return StagePrinter(out)
