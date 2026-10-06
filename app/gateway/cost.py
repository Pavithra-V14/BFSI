"""Billed cost and shadow cost (spec 5.4). They are always kept apart.

Billed cost is what the provider or LiteLLM reports for the call, with a
cost_source label. Free models report 0, so a shadow cost is computed from a
reference price table: the paid equivalent of the model, or the default
reference (Groq gpt-oss-120b) when there is none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRICING_FILE = Path(__file__).with_name("pricing.json")


@dataclass(frozen=True)
class Cost:
    billed: float | None
    billed_source: str
    shadow: float
    shadow_source: str


def load_pricing(path: Path = PRICING_FILE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _litellm_prices(model: str) -> tuple[float, float] | None:
    import litellm

    info = litellm.model_cost.get(model)
    if not info:
        return None
    return (
        float(info.get("input_cost_per_token") or 0.0),
        float(info.get("output_cost_per_token") or 0.0),
    )


def shadow_cost(
    model: str, input_tokens: int, output_tokens: int, pricing: dict[str, Any]
) -> tuple[float, str]:
    entry = pricing.get("models", {}).get(model)
    if entry is not None:
        per_in = entry["input_per_m"] / 1_000_000
        per_out = entry["output_per_m"] / 1_000_000
        source = "pricing_table"
    else:
        found = _litellm_prices(model)
        if found is not None and (found[0] > 0 or found[1] > 0):
            per_in, per_out = found
            source = "litellm_map"
        else:
            ref = pricing["default_reference"]
            per_in = ref["input_per_m"] / 1_000_000
            per_out = ref["output_per_m"] / 1_000_000
            source = "default_reference"
    return input_tokens * per_in + output_tokens * per_out, source


def build_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, Any],
    *,
    billed: float | None,
    billed_source: str,
) -> Cost:
    shadow, shadow_source = shadow_cost(model, input_tokens, output_tokens, pricing)
    return Cost(billed, billed_source, shadow, shadow_source)
