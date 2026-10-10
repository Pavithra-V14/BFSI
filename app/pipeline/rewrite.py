"""Stage 5: rewrite (answer tier), only when needed (spec 4.1 step 5, 4.3, 4.4).

The rewrite runs only if (a) the triage says the question needs history AND the session has some, or
(b) it is a vague first message of a session (rewrite-then-clarify). A new session has no history,
so
a first message that "needs history" is treated like a vague one.

The prompt is `prompts/rewrite.v1.md` with the summary and the recent turns of Phase 7 (a fold may
make one decide-tier call) and the known facts (`render_facts`, never the unclear count). What comes
back is untrusted model text: it is made plain text, masked again, scanned for instruction-like text
and length-checked before it becomes THE question for the rest of the pipeline. If the model says
the question is still unclear, the answer is the clarification (what it understood and what it
needs)
and the turn counts as an unclear one (two in a row end with the fixed message, Phase 7). A failed
rewrite keeps the original question and the pipeline goes on.
"""

from __future__ import annotations

from typing import Any

from app.gateway.decisions import extract_json
from app.guardrails.injection import scan_query
from app.guardrails.limits import MAX_QUERY_TOKENS
from app.guardrails.plain_text import to_plain_text
from app.guardrails.prompts import assemble_rewrite, get_prompt
from app.guardrails.scan import ModelCopy
from app.memory.facts import render_facts
from app.memory.history import prepare_history
from app.pipeline import messages
from app.pipeline.state import PersistPlan, PipelineState, Result
from app.pipeline.trace import Span

MAX_REWRITE_TOKENS = MAX_QUERY_TOKENS * 2


def needs_rewrite(ctx: PipelineState) -> bool:
    cfg = ctx.deps.config
    triage = ctx.triage
    wants_history = (triage.needs_history or 0.0) >= cfg.history_probability
    vague = (triage.vague or 0.0) >= cfg.history_probability
    has_history = ctx.session_state is not None and not ctx.first_message
    return (wants_history and has_history) or (ctx.first_message and (vague or wants_history))


def _clean(ctx: PipelineState, value: Any) -> str:
    """Plain text, masked, one line of reasonable length; "" when it is not usable."""
    if not isinstance(value, str):
        return ""
    text = " ".join(to_plain_text(value, ()).split())
    if not text:
        return ""
    text = ctx.deps.masker.masker.mask(text).text
    return text if ctx.deps.counter.count_text(text) <= MAX_REWRITE_TOKENS else ""


def parse_rewrite(raw: Any) -> dict[str, Any] | None:
    data = raw
    if isinstance(raw, str):
        try:
            data = extract_json(raw)
        except Exception:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("clear"), bool):
        return None
    return data


async def rewrite_stage(ctx: PipelineState, span: Span, timeout_s: float) -> None:
    if not needs_rewrite(ctx):
        span.status = "skipped"
        return
    summary, recent = "", ""
    if ctx.session_state is not None and not ctx.first_message:
        history = await prepare_history(
            ctx.db,
            ctx.deps.gateway,
            user_id=ctx.request.user_id,
            session_id=ctx.session_state.info.id,
            config=ctx.deps.memory,
            counter=ctx.deps.counter,
            masker=ctx.deps.masker.masker,
            on_event=ctx.deps.on_event,
        )
        summary, recent = history.summary, history.recent
        facts = render_facts(ctx.session_state.facts)
        if facts:
            summary = f"{summary}\nKnown facts: {facts}".strip()
        ctx.history_used = bool(summary or recent)
    ctx.trace.record(prompt_rewrite=get_prompt("rewrite").short_hash)
    prompt = assemble_rewrite(
        get_prompt("rewrite"), summary, recent, ctx.pass1.text if ctx.pass1 else ""
    )
    try:
        completion = await ctx.deps.gateway.complete(
            [{"role": "user", "content": prompt}],
            tier="answer",
            json_mode=True,
            timeout_s=timeout_s,
        )
    except Exception as error:  # keep the original question and go on
        span.status = "error"
        span.error_code = type(error).__name__
        ctx.emit("rewrite_failed", reason=type(error).__name__)
        return
    span.decider = completion.decider
    span.model = completion.model
    span.provider = completion.provider
    span.key_fingerprint = completion.key_fingerprint
    span.input_tokens = completion.input_tokens
    span.output_tokens = completion.output_tokens
    span.cost_billed = completion.cost_billed
    span.shadow_cost = completion.shadow_cost
    data = parse_rewrite(getattr(completion, "parsed", None) or completion.text)
    if data is None:
        ctx.emit("rewrite_unreadable")
        return
    standalone = _clean(ctx, data.get("standalone_question"))
    if standalone and scan_query(standalone).strong:  # a rewrite that attacks us is ignored
        ctx.emit("rewrite_rejected_injection")
        standalone = ""
        data = {**data, "clear": False, "standalone_question": ""}
    if data["clear"] and standalone:
        ctx.question = ModelCopy(standalone)
        ctx.rewritten = True
        return
    understood = _clean(ctx, data.get("understood"))
    ask = _clean(ctx, data.get("clarifying_question")) or "What would you like to know?"
    lead = f"I understood your question as: {understood}. " if understood else ""
    text = messages.CLARIFY.format(understood=lead, question=ask)
    ctx.exited = "rewrite"
    ctx.result = Result("refusal", text, reason="unclear")
    ctx.plan = PersistPlan("fixed_reply", text, unclear=True)
