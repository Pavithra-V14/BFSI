"""Names and placeholder values of the evaluation (spec 15). Nothing here calls a service.

Values tagged [T] are placeholders until the first baseline run (spec 15, build rule "targets stay
baseline"). They are constants here so a test can see them and the owner can find them.
"""

from __future__ import annotations

import re

from app.vector.documents import COLLECTION as PRODUCTION_COLLECTION

# The collection an evaluation run reads. Never the production one: `check_eval_collection`.
EVAL_COLLECTION = "documents_eval"

# The categories the 60 cases cover (spec 15.2).
CATEGORIES = (
    "factual",
    "table_lookup",
    "image_derived",
    "multi_turn",
    "vague",
    "greeting",
    "out_of_domain",
    "injection",
    "pii",
    "role_isolation",
    "edit_delete",
    "conflicting_versions",
    "source_removed",
)

# What a case expects the system to do with its LAST turn.
BEHAVIOURS = ("answer", "refuse", "greeting", "clarify", "block")
ROLES = ("customer", "staff")
CASE_STATUSES = ("set", "candidate", "retired")
ENVIRONMENTS = ("local", "production")
RUN_KINDS = ("active", "full", "variance", "calibration", "samples")

TOTAL_CASES = 60  # spec 15.2
ACTIVE_CASES = 20
CALIBRATION_ITEMS = 30
CALIBRATION_REVIEW_SAMPLE = 10  # [T] items an expert should re-label (the owner's sample)

# [T] Jev is used for a metric only when its agreement with the labels is at least this.
CALIBRATION_THRESHOLD = 0.85
# A run stops when it has used more than the confirmed estimate plus this share (owner addition a).
OVER_ESTIMATE_MARGIN = 0.25
# A batch is planned to use at most this share of the tightest daily limit (the rest is room).
DAILY_LIMIT_SAFETY = 0.9  # owner decision: was 0.8; the estimate itself is on the high side
SAMPLE_RATE = 0.05  # [T] about 5 % of live answers (spec 15.4)
SAMPLE_RETENTION_DAYS = 30  # [T] masked samples are purged by the retention command
VARIANCE_RUNS = 3  # spec 15.4: a band needs this many runs of the active set
VARIANCE_SIGMAS = 2.0  # the band is about two standard deviations

# [T] A confirmation code belongs to its own estimate and starts that run only within this long
# after the estimate was made. A confirmed run's later batches (the next days) continue under it.
CONFIRMATION_MINUTES = 30
# A run that says "running" but has made no progress for this long can be taken over (a crash).
HEARTBEAT_STALE_MINUTES = 10
# [T] Jev decides a metric only if at least this share of its calibration items got a verdict
# from it (an outage counts as a miss) and its agreement with the labels reached the threshold.
MIN_COUNTED_SHARE = 5 / 6

# Statuses of a run.
RUN_ESTIMATED = "estimated"
RUN_STARTING = "starting"  # the page started it: its own process is on the way
RUN_RUNNING = "running"
RUN_PAUSED = "paused"  # a batch finished, more batches wait (next day's limits)
RUN_FINISHED = "finished"
RUN_ABORTED = "aborted_over_estimate"
RUN_FAILED = "failed"


# letters and digits, then exactly `_eval`: `documents_eval`, `release2_eval`
EVAL_COLLECTION_NAME = re.compile(r"^[a-z0-9]+_eval$")


class EvalConfigError(Exception):
    """A setting or a case that must be fixed before anything runs. Names only, no values."""


def check_eval_collection(name: str) -> str:
    """The collection of an evaluation run: never the production collection, and always one that
    says it is for evaluation. Returns the name."""
    value = (name or "").strip()
    if not value or value == PRODUCTION_COLLECTION:
        raise EvalConfigError("an evaluation run must not use the production collection")
    if not EVAL_COLLECTION_NAME.match(value):
        raise EvalConfigError(
            "the evaluation collection name must match ^[a-z0-9]+_eval$ "
            "(lowercase letters and digits, then _eval)"
        )
    return value
