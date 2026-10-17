"""The Dashboards admin section (spec 5.3, 5.4, 15.6, 18).

Every chart has a TABLE of the same numbers next to it (above it), so nothing depends on seeing a
colour or a bar. Only numbers and fixed words come from the service (decider and provider names,
states); they pass through `render.safe_text` / `admin.cell` like every other API value. Billed cost
and shadow cost are always separate columns with their own heading.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import render
from frontend.api import ApiClient
from frontend.views.admin import _load, _table, cell
from frontend.views.common import number

PANELS = (
    "Decider in use",
    "Key pools",
    "Tokens and cost",
    "Cache",
    "Ingestion health",
    "Daily metrics",
)
WINDOWS = {"Last hour": "hour", "Last day": "day"}
ROLE_CHOICES = {"All users": None, "Customers": "customer", "Staff": "staff"}


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f} %"
    except (TypeError, ValueError):
        return "0.0 %"


def _limit(value: Any) -> Any:
    """A daily limit or what is left of it; 'not set' when the provider has no configured limit."""
    return "not set" if value is None else value


def _chart(rows: list[dict[str, Any]], x: str, y: str) -> None:
    """A bar chart of rows, drawn only when there is something to draw."""
    clean = [{x: render.safe_text(r.get(x)), y: float(r.get(y) or 0)} for r in rows]
    if clean and any(r[y] for r in clean):
        st.bar_chart(clean, x=x, y=y)


def deciders(client: ApiClient) -> None:
    data = _load(client.dash_deciders())
    if data is None:
        return
    render.show(render.who("Decider in use, per step"))
    health = data.get("health") or {}
    _table(
        [
            {
                "provider": name,
                "state": info.get("state"),
                "keys": info.get("keys"),
                "cooling": info.get("cooling"),
            }
            for name, info in sorted(health.items())
        ]
    )
    steps = data.get("steps") or {}
    source = steps if steps else (data.get("from_requests") or {}).get("hour", {})
    if steps:
        rows = [
            {"step": step, "window": window, "decider": decider, "share": _percent(share)}
            for step, windows in sorted(steps.items())
            for window in ("hour", "day")
            for decider, share in sorted((windows.get(window) or {}).items())
        ]
    else:
        rows = [
            {"step": step, "window": "hour", "decider": d, "share": _percent(share)}
            for step, shares in sorted(source.items())
            for d, share in sorted(shares.items())
        ]
    _table(rows)
    chart_rows = [
        {"label": f"{r['step']} / {r['decider']}", "share": float(r["share"].split()[0])}
        for r in rows
        if r["window"] == "hour"
    ]
    _chart(chart_rows, "label", "share")


def key_pools(client: ApiClient) -> None:
    data = _load(client.dash_keys())
    if data is None:
        return
    render.show(render.who("Key pools"))
    providers = data.get("providers") or []
    st.metric("Keys in all pools", sum(number(p.get("keys")) for p in providers))
    _table(
        [
            {
                "provider": p.get("provider"),
                "state": p.get("state"),
                "keys": p.get("keys"),
                "cooling down": p.get("cooling"),
                "calls today": p.get("calls_today"),
                "tokens today": p.get("tokens_today"),
                "daily calls per key": _limit(p.get("daily_calls_per_key")),
                "daily tokens per key": _limit(p.get("daily_tokens_per_key")),
            }
            for p in providers
        ]
    )
    render.show(render.who("Each key, today"))
    _table(
        [
            {
                "provider": p.get("provider"),
                "key": position,
                "fingerprint": k.get("fingerprint"),
                "calls today": k.get("calls"),
                "calls left today": _limit(k.get("calls_left")),
                "tokens today": k.get("tokens"),
                "tokens left today": _limit(k.get("tokens_left")),
            }
            for p in providers
            for position, k in enumerate(p.get("per_key") or [], start=1)
        ]
    )
    render.caption(f"Counts of the UTC day {data.get('day')}.")
    st.caption(
        "A day starts at 00:00 UTC, which may not be when a provider resets its own limit. "
        "A key is never shown: the fingerprint is eight characters of a hash that only names it "
        "(the same name the key inventory script prints). 'not set' means no daily limit is "
        "configured for that provider."
    )


def tokens_and_cost(client: ApiClient) -> None:
    left, right = st.columns(2)
    window = WINDOWS[left.selectbox("Window", list(WINDOWS), index=1, key="fx_dash_window")]
    role = ROLE_CHOICES[right.selectbox("Who", list(ROLE_CHOICES), key="fx_dash_role")]
    data = _load(client.dash_usage(window, role))
    if data is None:
        return
    render.show(render.who("Tokens and cost"))
    _table(
        [
            {
                "requests": data.get("requests"),
                "tokens per request p50": data.get("tokens_p50"),
                "p95": data.get("tokens_p95"),
                "p99": data.get("tokens_p99"),
                "input tokens": data.get("tokens_in"),
                "output tokens": data.get("tokens_out"),
            }
        ]
    )
    _table(
        [
            {
                "billed cost (USD)": data.get("billed_cost"),
                "billed per answer": data.get("billed_per_answer"),
                "shadow cost (USD, reference price)": data.get("shadow_cost"),
                "shadow per answer": data.get("shadow_per_answer"),
            }
        ]
    )
    st.caption(
        "Billed cost is what was charged (free models report 0). Shadow cost is what the same "
        "tokens would cost at the reference prices. They are never added together."
    )
    stages = data.get("tokens_per_stage") or {}
    rows = [{"stage": s, "tokens": number(t)} for s, t in sorted(stages.items())]
    _table(rows)
    _chart(rows, "stage", "tokens")


def cache_panel(client: ApiClient) -> None:
    window = WINDOWS[st.selectbox("Window", list(WINDOWS), index=1, key="fx_dash_cache_window")]
    data = _load(client.dash_cache(window))
    if data is None:
        return
    render.show(render.who("Cache"))
    if not data.get("cache_configured"):
        render.show_notice("warn", "No cache", "No cache is configured: every answer is generated.")
    rows = [
        {"measure": "requests", "value": data.get("requests")},
        {"measure": "cache hit rate", "value": _percent(data.get("cache_hit_rate"))},
        {"measure": "reduced-mode rate", "value": _percent(data.get("degraded_rate"))},
        {"measure": "low-confidence rate", "value": _percent(data.get("low_confidence_rate"))},
        {"measure": "answered", "value": data.get("answered")},
        {"measure": "refused", "value": data.get("refused")},
        {"measure": "unavailable", "value": data.get("unavailable")},
    ]
    _table(rows)


def ingestion(client: ApiClient) -> None:
    data = _load(client.dash_ingestion())
    if data is None:
        return
    render.show(render.who("Ingestion health"))
    _table(
        [
            {
                "documents": len(data.get("documents") or []),
                "chunks held back": data.get("held_back"),
                "sparse vectors out of date": data.get("sparse_out_of_date"),
                "failed pages waiting": data.get("failed_pages_open"),
            }
        ]
    )
    _table(
        [
            {
                "document": d.get("doc_id"),
                "active": d.get("chunks_active"),
                "held back": d.get("chunks_held_back"),
                "out of date": d.get("sparse_vectors_out_of_date"),
                "version": d.get("document_version"),
            }
            for d in data.get("documents") or []
        ]
    )
    render.show(render.who("Last ingest, delete and reindex"))
    _table(
        [
            {"action": r.get("action"), "document": r.get("document"), "at": r.get("at")}
            for r in data.get("recent") or []
        ]
    )
    render.caption(data.get("note", ""))


def daily(client: ApiClient) -> None:
    days = st.selectbox("Days", [7, 14, 30, 60], index=1, key="fx_dash_days")
    data = _load(client.dash_daily(days))
    if data is None:
        return
    render.show(render.who("Daily metrics, customers and staff"))
    rows: list[dict[str, Any]] = []
    for item in data.get("days") or []:
        for role in ("customer", "staff"):
            values = (item.get("roles") or {}).get(role) or {}
            rows.append(
                {
                    "day": item.get("day"),
                    "who": role,
                    "active users": values.get("active_users", 0),
                    "chats": values.get("chats_started", 0),
                    "questions": values.get("questions", 0),
                    "verified answers": values.get("answers_verified", 0),
                    "refused": values.get("refused", 0),
                    "thumbs up share": _percent(values.get("thumbs_up_share")),
                    "cache hit rate": _percent(values.get("cache_hit_rate")),
                    "tokens per answer": values.get("tokens_per_answer", 0),
                    "billed per answer": values.get("billed_per_answer", 0),
                    "shadow per answer": values.get("shadow_per_answer", 0),
                    "seat use": _percent(values.get("seat_utilisation")) if role == "staff" else "",
                }
            )
    _table(rows)
    chart = [
        {"label": f"{r['day']} {r['who']}", "questions": float(cell(r["questions"]) or 0)}
        for r in rows
    ]
    _chart(chart, "label", "questions")
    st.caption(
        "Counts and ratios only. A day's numbers are saved by the service every few minutes, so "
        "today's row can be a little behind."
    )


def render_dashboards(client: ApiClient) -> None:
    panel = st.radio("Panel", PANELS, horizontal=True, key="fx_dash_panel")
    if panel == "Decider in use":
        deciders(client)
    elif panel == "Key pools":
        key_pools(client)
    elif panel == "Tokens and cost":
        tokens_and_cost(client)
    elif panel == "Cache":
        cache_panel(client)
    elif panel == "Ingestion health":
        ingestion(client)
    else:
        daily(client)
