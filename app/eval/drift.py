"""The weekly drift report (spec 15.5a). Alerts only.

Compares the distributions of this week's production sample (the masked sample queue, 15.4) with a
baseline window, with the population stability index (PSI) and the Jensen-Shannon divergence, over
the data-drift signals of 15.5a that the sample holds:

- model mix (share of answers by ACTUAL model: mixed models make this the highest risk),
- prompt version mix (the prompt hash in each trace identifies a prompt change),
- corpus version mix,
- result mix (answers, refusals, clarifications, greetings) and the refusal reasons
  (out-of-domain rate, "no context" rate),
- query length buckets (a cheap stand-in for the distance of query embeddings from the baseline),
- low-confidence share (a stand-in for the share of queries whose best score is below the floor:
  coverage gaps) and reduced-mode (degraded) share,
- cost and latency creep (mean tokens and p95 latency against the baseline).

The index is a smoothed one (a small floor for empty buckets). The flag threshold of 0.25 is the
usual "large shift" reading and is a [T] placeholder until the baseline run.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

PSI_ALERT = 0.25  # [T] "a large shift"
PSI_WATCH = 0.10
FLOOR = 1e-4
LENGTH_BUCKETS = ((0, 40), (40, 80), (80, 160), (160, 320), (320, 10_000))
MIN_SAMPLES = 20  # fewer than this and a week is too small to compare


def _shares(counts: Mapping[str, int], keys: Sequence[str]) -> list[float]:
    total = sum(counts.get(k, 0) for k in keys)
    if total <= 0:
        return [FLOOR for _ in keys]
    return [max(counts.get(k, 0) / total, FLOOR) for k in keys]


def psi(expected: Mapping[str, int], actual: Mapping[str, int]) -> float | None:
    """Population stability index over the union of the buckets; None when either side is empty."""
    if sum(expected.values()) == 0 or sum(actual.values()) == 0:
        return None
    keys = sorted(set(expected) | set(actual))
    e, a = _shares(expected, keys), _shares(actual, keys)
    return round(sum((ai - ei) * math.log(ai / ei) for ei, ai in zip(e, a, strict=True)), 6)


def jensen_shannon(expected: Mapping[str, int], actual: Mapping[str, int]) -> float | None:
    """Jensen-Shannon divergence in bits (0 identical, 1 disjoint)."""
    if sum(expected.values()) == 0 or sum(actual.values()) == 0:
        return None
    keys = sorted(set(expected) | set(actual))
    total_e, total_a = sum(expected.values()), sum(actual.values())
    p = [expected.get(k, 0) / total_e for k in keys]
    q = [actual.get(k, 0) / total_a for k in keys]
    m = [(pi + qi) / 2 for pi, qi in zip(p, q, strict=True)]

    def kl(x: list[float], y: list[float]) -> float:
        return sum(xi * math.log2(xi / yi) for xi, yi in zip(x, y, strict=True) if xi > 0)

    return round((kl(p, m) + kl(q, m)) / 2, 6)


def _bucket_length(text: str) -> str:
    n = len(text or "")
    for low, high in LENGTH_BUCKETS:
        if low <= n < high:
            return f"{low}-{high}"
    return f"{LENGTH_BUCKETS[-1][0]}+"


def distributions(samples: Iterable[Mapping[str, Any]]) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = {
        "model": Counter(),
        "prompt": Counter(),
        "corpus": Counter(),
        "result": Counter(),
        "refusal_reason": Counter(),
        "length": Counter(),
        "confidence": Counter(),
        "reduced_mode": Counter(),
    }
    for s in samples:
        models = s.get("models") or ["unknown"]
        for model in models:
            out["model"][str(model)] += 1
        out["prompt"][str(s.get("prompt_hash") or "unknown")] += 1
        out["corpus"][str(s.get("corpus_version") or "unknown")] += 1
        out["result"][str(s.get("result_kind") or "unknown")] += 1
        if s.get("reason"):
            out["refusal_reason"][str(s["reason"])] += 1
        out["length"][_bucket_length(str(s.get("question") or ""))] += 1
        out["confidence"]["low" if s.get("confidence") == "low" else "normal"] += 1
        out["reduced_mode"]["reduced" if s.get("degraded") else "normal"] += 1
    return out


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[min(max(1, math.ceil(0.95 * len(ordered))), len(ordered)) - 1])


def _creep(samples: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    tokens = [float(s["tokens"]) for s in samples if s.get("tokens") is not None]
    latency = [float(s["latency_ms"]) for s in samples if s.get("latency_ms") is not None]
    return {
        "tokens_mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
        "latency_p95_ms": _p95(latency),
    }


def drift_report(
    recent: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
    *,
    window: tuple[datetime, datetime],
    baseline_window: tuple[datetime, datetime] | None,
) -> dict[str, Any]:
    """The measures and the alerts. `recent` and `baseline` are sample rows (see
    `store.samples_between`)."""
    report: dict[str, Any] = {
        "window": [window[0].isoformat(), window[1].isoformat()],
        "baseline_window": [d.isoformat() for d in baseline_window] if baseline_window else None,
        "samples": len(recent),
        "baseline_samples": len(baseline),
        "measures": {},
        "alerts": [],
        "notes": [],
    }
    if len(recent) < MIN_SAMPLES or len(baseline) < MIN_SAMPLES:
        report["notes"].append("too few samples to compare: at least 20 in each window")
        report["distribution"] = {k: dict(v) for k, v in distributions(recent).items()}
        report["creep"] = _creep(recent)
        return report
    now_d, base_d = distributions(recent), distributions(baseline)
    for name in now_d:
        score = psi(base_d[name], now_d[name])
        divergence = jensen_shannon(base_d[name], now_d[name])
        level = "ok"
        if score is not None and score >= PSI_ALERT:
            level = "alert"
            report["alerts"].append(f"{name}_shift")
        elif score is not None and score >= PSI_WATCH:
            level = "watch"
        report["measures"][name] = {"psi": score, "js": divergence, "level": level}
    report["creep"] = {"recent": _creep(recent), "baseline": _creep(baseline)}
    for key in ("tokens_mean", "latency_p95_ms"):
        was, now = report["creep"]["baseline"][key], report["creep"]["recent"][key]
        if was and now and now > 1.3 * was:  # [T] 30 % creep
            report["alerts"].append(f"{key}_creep")
    report["distribution"] = {k: dict(v) for k, v in now_d.items()}
    return report


def week_windows(
    end: datetime, *, days: int = 7, baseline_days: int = 28
) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
    """(this week, the baseline window right before it)."""
    start = end - timedelta(days=days)
    return (start, end), (start - timedelta(days=baseline_days), start)


def classify_category_dip(
    deltas: Mapping[str, float], *, width: float, system_changed: bool
) -> str | None:
    """15.5a: 'model_drift' when EVERY category dipped by more than the noise width while nothing
    changed on the project side; 'prompt_or_data_change' when only some dipped and the system
    changed; None otherwise."""
    if not deltas or width is None:
        return None
    dipped = [name for name, delta in deltas.items() if delta < -width]
    if len(dipped) == len(deltas) and not system_changed:
        return "model_drift"
    if dipped and system_changed:
        return "prompt_or_data_change"
    return None
