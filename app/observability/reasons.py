"""Fixed plain-words reasons for every alert and event the service reports (Change round 1, step 3).

The Alerts screen shows a reason next to each alert: what it means, and what to do. The text is
FIXED here, written by us, one entry per name. It never holds an exception message, a stack trace,
a user, a question or a key: the only thing taken from a running event is its reason CODE (a class
name or a fixed code), and only when it looks like one (`clean_code`).

A test (`tests/test_alert_reasons.py`) fails when the code emits an event, or declares an alert,
that has no entry here, so a new alert cannot reach the screen without its reason.
"""

from __future__ import annotations

import re
from typing import Any

# name -> (what it means, what to do)
REASONS: dict[str, tuple[str, str]] = {
    # ---- critical alerts (banner and webhook) --------------------------------------------
    "all_keys_exhausted": (
        "Every key of one provider is cooling down or over its limit, so no model of that provider "
        "can answer right now. The service tries the next provider.",
        "Wait a minute and look at the key panel (Dashboards, Key pools). If it repeats, a daily "
        "free limit is used up: add a key from another account or wait for the reset.",
    ),
    "embedding_unavailable": (
        "The question could not be turned into a vector (both embedding keys failed), so no "
        "document can be searched and the user gets the unavailable reply.",
        "Check GEMINI_EMBEDDING_KEY_A and GEMINI_EMBEDDING_KEY_B and their daily quota.",
    ),
    "masker_load_failed": (
        "The local name-detection model did not load. Questions are refused rather than sent on "
        "unmasked (the service fails closed).",
        "Restart the service. On the free host, check that its memory is not full.",
    ),
    "masker_unavailable": (
        "The name-detection model is not ready or failed, so questions are refused unmasked.",
        "Restart the service and check its memory.",
    ),
    "cache_quota_exceeded": (
        "The cache (Upstash) refused a request because its plan limit is used up. Answers are "
        "still given, but not cached, and counters may be off.",
        "Open the Upstash dashboard: check the daily request limit and the plan.",
    ),
    "cache_out_of_memory": (
        "The cache (Upstash) is full and refused to store more.",
        "Open the Upstash dashboard and clear or enlarge the database.",
    ),
    "cache_read_only": (
        "The cache (Upstash) accepts reads but not writes, so nothing new is cached.",
        "Open the Upstash dashboard: the database may be paused or over its limit.",
    ),
    "vector_store_rejected": (
        "Qdrant refused a request (a wrong key, a wrong collection or vector size, or a limit). "
        "Search or ingestion is failing.",
        "Check QDRANT_URL, QDRANT_API_KEY and the collection; use the Qdrant dashboard.",
    ),
    "budget_charge_failed": (
        "The tokens a request used could not be added to the daily counter, so the budget figures "
        "are too low.",
        "Check the cache (Upstash) status; the counter lives there.",
    ),
    "graph_failed": (
        "A bug or an unexpected error stopped a request inside the pipeline. The user got the "
        "fixed unavailable reply. The reason code below is the error's class name.",
        "Look in the service log for the correlation id of the failed request and send the class "
        "name to your advisor.",
    ),
    "sparse_version_mismatch": (
        "The keyword vectors stored in Qdrant were built with another tokenizer version than the "
        "code uses now, so keyword search may rank wrongly.",
        "Run the sparse refresh (docs/OWNER_RUNS.md, Run 15).",
    ),
    "persist_queue_overflow": (
        "More turns are waiting to be saved than the in-memory queue holds, so the oldest were "
        "dropped. The database has been down or slow for a while.",
        "Check the Postgres (Neon) status and connection.",
    ),
    "persist_job_dead_lettered": (
        "A turn could not be saved after several tries and was set aside. That chat's last "
        "exchange is not stored.",
        "Check Postgres and the service log; the reason code is the error's class name.",
    ),
    "trace_deletion_failed": (
        "The traces of a deleted chat could not be removed from Langfuse. This is a privacy "
        "obligation, so it is critical. It is retried automatically.",
        "Check the Langfuse keys and host (LANGFUSE_*), then watch for it to clear.",
    ),
    "trace_queue_failed": (
        "A chat was deleted, but its traces could not even be queued for removal.",
        "Check Postgres, then remove the traces by session in Langfuse by hand.",
    ),
    "trace_deletion_cap_reached": (
        "One deleted chat has more traces than a single pass removes. The rest stay for the next "
        "pass.",
        "No action unless it repeats for days; then check the Langfuse connection.",
    ),
    "error_rate": (
        "Too large a share of recent requests ended unavailable (over the threshold in the "
        "window). Users are seeing the unavailable reply.",
        "Open the key panel and the decider panel to see which provider or store is failing.",
    ),
    # ---- warnings (banner only) -----------------------------------------------------------
    "jev_unavailable": (
        "The Jev decision service did not answer. Decisions fell to the next model or to plain "
        "rules. Answers continue.",
        "Check JEV_OPENROUTER_API_KEY and its credit on OpenRouter.",
    ),
    "decision_fell_to_rules": (
        "No model could decide, so fixed rules decided. Answers are a little less precise.",
        "Look at the decider panel: usually Jev and the backup models were all unavailable.",
    ),
    "rerank_failed": (
        "The rerank providers (Cohere, Jina) failed, so passages were ordered by the plain keyword "
        "method instead.",
        "Check COHERE_API_KEY and JINA_API_KEY and their limits.",
    ),
    "langfuse_failed": (
        "Traces could not be sent to Langfuse. Answers are not affected.",
        "Check the Langfuse keys and host, and its status page.",
    ),
    "alert_webhook_failed": (
        "The chat webhook did not accept a message, so a critical alert may not have reached you.",
        "Run the webhook test (python -m app.observability.webhook --test) and check the address.",
    ),
    "fallback_rate": (
        "Too large a share of recent answers ran in a reduced mode (a backup model or a skipped "
        "step).",
        "Look at the key panel and the decider panel: a primary provider is probably limited.",
    ),
    "guardrail_spike": (
        "The guardrails refused an unusually large share of recent questions. This can be an "
        "attack, a broken rule, or a real change in what people ask.",
        "Read the review queue and the refusal reasons; compare with the drift report.",
    ),
    "budget_variance": (
        "Today's tokens passed the planned amount by more than the allowed margin.",
        "Open Dashboards, Tokens and cost, and see which stage grew.",
    ),
    "retention_overdue": (
        "The clean-up task (python -m app.memory.retention) has not run for longer than the "
        "allowed number of days, so old chats and logs are still stored.",
        "Run it yourself: first with --dry-run, then without (docs/OWNER_RUNS.md, Run 18).",
    ),
    # ---- events the service notes (shown as counts) ------------------------------------------
    "persist_queued": (
        "A turn could not be saved at once (the database was slow or down) and was queued to be "
        "saved with the next request. The user still got the answer.",
        "No action if it clears. If it repeats, check Postgres.",
    ),
    "cache_staleness_write_failed": (
        "After a document changed or was deleted, the cache could not be told to stop serving "
        "answers that cited it. Old cached answers may still be shown until it is reachable.",
        "Check the cache (Upstash), then run the ingestion or delete for that document again "
        "(it is safe to repeat), so the clean-up is retried.",
    ),
    "internal_error": (
        "A bug in our own code stopped a call to a provider (it is not an outage of the provider). "
        "The call was not retried. The reason code is the error's class name.",
        "Send the class name to your advisor if it keeps rising.",
    ),
    "document_delete_failed": (
        "A delete started from the admin page did not finish (a store was slow or refused part "
        "of it). The document may be partly removed.",
        "Press Delete for the same document again: the command is safe to repeat and finishes "
        "the work.",
    ),
    "purge_failed": (
        "The purge started from the admin page did not finish (a store was slow or refused part of "
        "it). Part of the data may already be erased.",
        "Press the purge again: the command is safe to repeat and finishes the work.",
    ),
    "persist_flush_skipped": (
        "Queued turns were not retried this time because there was no time left in the request.",
        "No action; they are retried on a later request.",
    ),
    "persist_masking_failed": (
        "A turn was not stored because its masked copy could not be made (nothing is stored "
        "unmasked).",
        "Look at the masker alerts; restart if the masker is down.",
    ),
    "observability_failed": (
        "Recording the metrics or alerts of a request failed. The request itself was not "
        "affected. The reason code is the error's class name.",
        "Check Postgres; if the counts keep rising, send the class name to your advisor.",
    ),
    "cache_unavailable": (
        "The cache could not be reached, so the request went on without it.",
        "Expected when no cache is set up locally. In production, check Upstash.",
    ),
    "cache_error": (
        "A cache call failed in an unexpected way. The request went on without the cache.",
        "Check Upstash if it repeats.",
    ),
    "cache_corrupt": (
        "A cached entry could not be read and was ignored.",
        "No action; it is replaced the next time that question is answered.",
    ),
    "cache_entry_discarded": (
        "A cached answer was thrown away because it no longer matched the current documents.",
        "No action; this is the cache staying correct.",
    ),
    "cache_write_skipped": (
        "A new answer was not cached (the documents changed while it was made, a passage had no "
        "content hash, or its masked copy could not be made). The reason code says which.",
        "No action unless it repeats; then check the last ingestion.",
    ),
    "rate_limit_unavailable": (
        "The per-user rate limiter could not reach its store, so the request was allowed (it fails "
        "open).",
        "Expected without a cache locally. In production, check Upstash.",
    ),
    "budget_check_unavailable": (
        "The daily budget could not be checked, so the request was allowed.",
        "Check the cache (Upstash) if it repeats.",
    ),
    "budget_unavailable": (
        "The budget counter could not be reached.",
        "Check the cache (Upstash) if it repeats.",
    ),
    "sparse_check_unavailable": (
        "The check that stored keyword vectors match the code could not reach Qdrant this time.",
        "No action unless it repeats; then check Qdrant.",
    ),
    "request_budget_exhausted": (
        "A request used its whole 30-second budget. If an answer was ready it was returned.",
        "If it repeats, look at stage latency in the Evaluation page for the slow stage.",
    ),
    "session_unavailable": (
        "The chat's stored history could not be loaded, so the question was answered without it.",
        "Check Postgres if it repeats.",
    ),
    "session_pending_in_queue": (
        "A follow-up arrived while the chat's first turn was still waiting to be saved. The chat "
        "was kept as one conversation.",
        "No action.",
    ),
    "allowlist_unavailable": (
        "The approved-names list could not be loaded, so masking used an empty list (stricter, "
        "never looser).",
        "Check Postgres if it repeats.",
    ),
    "filter_values_unavailable": (
        "The list of product lines and clause types could not be loaded, so filters were skipped.",
        "Check Qdrant and Postgres if it repeats.",
    ),
    "stage_failed": (
        "One pipeline stage failed and the request used its fallback for that stage.",
        "Look at the reason code (a class name) and the stage latency view.",
    ),
    "step_skipped": (
        "One provider step was skipped (its keys were limited, its circuit was open, or no time "
        "was left) and the next provider was tried. The reason code says which.",
        "Look at the key panel and the decider panel if it repeats.",
    ),
    "limiter_full": (
        "Our own per-key limiter had no room, so that provider was skipped for this call.",
        "Look at the key panel: the daily or per-minute limit of that provider is near.",
    ),
    "retry_skipped_no_capacity": (
        "A retry was not made because no key had room.",
        "Look at the key panel.",
    ),
    "retry_skipped_no_time": (
        "A retry was not made because the request had no time left.",
        "No action.",
    ),
    "retry_skipped_window": (
        "A retry was not made because the model's context window was too small for it.",
        "No action.",
    ),
    "soft_retry_failed": (
        "The relaxed second search failed.",
        "Check Qdrant if it repeats.",
    ),
    "soft_retry_skipped": (
        "The relaxed second search was not needed or had no time.",
        "No action.",
    ),
    "rewrite_failed": (
        "The step that makes a follow-up question stand alone failed, so the question was used as "
        "it was.",
        "Look at the decider panel if it repeats.",
    ),
    "rewrite_rejected_injection": (
        "The rewritten question looked like an instruction to the system and was thrown away.",
        "No action; this is the guardrail working.",
    ),
    "rewrite_unreadable": (
        "The rewrite step answered in a form that could not be read and was ignored.",
        "No action unless it repeats.",
    ),
    "summary_fold_failed": (
        "Summarising the older part of a chat failed; the chat continues without a new summary.",
        "No action unless it repeats.",
    ),
    "summary_rejected_injection": (
        "A summary looked like an instruction to the system and was thrown away.",
        "No action; this is the guardrail working.",
    ),
    "triage_failed": (
        "The triage decision failed, so a backup decider or the rules were used.",
        "Look at the decider panel.",
    ),
    "triage_probability_invalid": (
        "The triage model returned a confidence that is not a valid number and it was ignored.",
        "No action unless it repeats.",
    ),
    "login_throttled": (
        "Too many sign-in attempts from one place; further attempts were slowed.",
        "No action unless it is a sign of an attack on one account.",
    ),
    "register_throttled": (
        "Too many sign-up attempts from one place; further attempts were slowed.",
        "No action unless it is a sign of abuse.",
    ),
    "langfuse_disabled": (
        "Tracing to Langfuse is switched off (no keys set).",
        "Set the LANGFUSE_* variables if you want traces.",
    ),
    "langfuse_dropped": (
        "A trace was dropped because the sending queue was full. Answers are not affected.",
        "No action unless it repeats.",
    ),
    "tracing_switched_off": (
        "Automatic tracing was found switched on and was turned off, because it could capture "
        "text.",
        "No action; this is a safety rule.",
    ),
    "dev_embedding_refused": (
        "The development-only fallback embedding was refused (it is never allowed in production).",
        "Make sure the embedding keys are set.",
    ),
    "eval_sample_failed": (
        "A production answer could not be added to the evaluation sample. Nothing else is "
        "affected.",
        "Check EVAL_DATABASE_URL if it repeats.",
    ),
    "rollout_stage_invalid": (
        "The saved rollout stage was not one of shadow, staff_beta or open, so the safest stage "
        "was used.",
        "Set the stage again on the Rollout screen.",
    ),
    "rollout_stage_unreadable": (
        "The rollout stage could not be read, so the safest stage was used.",
        "Check Postgres, then set the stage again.",
    ),
}

NO_REASON = (
    "An event the service reports. No further description has been written for it.",
    "Send its name to your advisor if it keeps rising.",
)

_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")
CODE_FIELDS = ("reason", "error", "exception", "code")
MAX_CODES = 5


def explain(name: str) -> dict[str, str]:
    """The fixed reason of an alert or event: what it means and what to do."""
    meaning, action = REASONS.get(name, NO_REASON)
    return {"meaning": meaning, "action": action}


def clean_code(value: Any) -> str | None:
    """A reason code (a class name or a fixed code), or None for anything else: a message, a
    sentence, a number or an address never gets through."""
    if isinstance(value, str) and _CODE.match(value):
        return value
    return None


def reason_codes(fields_list: list[dict[str, Any]]) -> dict[str, int]:
    """How often each reason code appears in the fields of the events of one name (the most common
    few). Only values that look like a class name or a fixed code are counted."""
    counts: dict[str, int] = {}
    for fields in fields_list:
        if not isinstance(fields, dict):
            continue
        for field in CODE_FIELDS:
            code = clean_code(fields.get(field))
            if code:
                counts[code] = counts.get(code, 0) + 1
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_CODES]
    return dict(top)
