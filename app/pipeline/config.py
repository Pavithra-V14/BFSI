"""Thresholds, budgets and timeouts of the pipeline. Every number is [T]: a starting value for the
Phase 16 baseline run, except where the spec fixes it (25 candidates, top 5, RRF k = 60).

Time: the whole request has `budget_seconds` (spec 4: 30 s). A node gets the SMALLER of its own
timeout and the time left, and a node that needs a call is not started unless at least
`min_start_seconds` of it is left ("never start a call it cannot finish").
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUDGET_SECONDS = 30.0  # [T] spec 4, recalibrated after load tests

# node name -> (timeout seconds, minimum time left to start it). [T]
NODE_LIMITS: dict[str, tuple[float, float]] = {
    "authenticate": (1.0, 0.3),
    "local_scan": (2.0, 0.3),
    "triage": (8.0, 1.5),
    "rewrite": (8.0, 2.0),
    "cache_lookup": (1.0, 0.3),
    "embed": (6.0, 1.5),
    "retrieve": (4.0, 1.0),
    "rerank": (6.0, 1.5),
    "generate": (12.0, 6.0),
    "persist": (6.0, 0.3),
}

# On a machine that is NOT on Render (the owner's own PC, checking the service live) the stores are
# far away: the cache lookup is three network calls in a row (the corpus version, the entry, the
# Qdrant read-back that verifies a hit) and one second is not enough, so a valid hit never gets
# served. A hit saves about four seconds, so the local limit is longer. On Render the API is meant
# to run next to its stores and the limit stays at one second. [T]
LOCAL_CACHE_LOOKUP_SECONDS = 2.5
# The same reason holds for the steps before it: from the owner's PC one Neon or Upstash call takes
# 0.4 to 0.6 s, so with a 0.4 s slice the allow-list, the session history and the budget check all
# timed out on every request (no history, so no rewrite and no summary). Longer limits for these
# nodes and a longer slice, off Render only. [T]
LOCAL_NODE_SECONDS = {"authenticate": 3.0, "local_scan": 4.0}
LOCAL_SLICE_SECONDS = 2.0


def node_limits_for(on_render: bool) -> dict[str, tuple[float, float]]:
    """The node limits of this deployment: the standard ones on Render, and longer ones for the
    cache lookup, authenticate and local_scan everywhere else."""
    limits = dict(NODE_LIMITS)
    if not on_render:
        limits["cache_lookup"] = (LOCAL_CACHE_LOOKUP_SECONDS, NODE_LIMITS["cache_lookup"][1])
        for node, seconds in LOCAL_NODE_SECONDS.items():
            limits[node] = (seconds, NODE_LIMITS[node][1])
    return limits


def slice_seconds_for(on_render: bool) -> float:
    """The own time slice of one network call in a local node: 0.4 s on Render, 2 s elsewhere."""
    return SLICE_SECONDS if on_render else LOCAL_SLICE_SECONDS


# The network parts of authenticate and local_scan each get their own slice inside the node's
# timeout, so that a slow Upstash or Postgres degrades (fails open) instead of using the whole node.
# [P] (spec 4.5). The slice is also capped by what is left of the node.
SLICE_SECONDS = 0.4
# How often a queued transcript is tried while the database is reachable but the job still fails,
# before it is set aside with an alert so that it cannot block the queue. [P]
PERSIST_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class PipelineConfig:
    budget_seconds: float = BUDGET_SECONDS
    node_limits: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(NODE_LIMITS))
    candidates: int = 25  # spec 7
    top_k: int = 5  # spec 4.1 step 10: fixed for now
    rrf_k: int = 60  # spec 7
    relevance_floor: float = 0.30  # [T] spec 4.1 step 11: applies to Cohere and Jina scores
    lexical_floor: float = 0.50  # [T] the lexical fallback is stricter: share of query words found
    low_confidence_score: float = 0.40  # [T] best reranked score under this: low confidence
    out_of_domain_probability: float = 0.60  # [T] Jev's out-of-scope probability at or above this
    soft_filter_probability: float = 0.80  # [T] triage probability needed to apply a soft filter
    history_probability: float = 0.50  # [T] needs-history / vague at or above this
    estimated_request_tokens: int = 3_400  # [T] spec 5.4 capacity note, for the budget pre-check
    generator_retries: int = 1  # invalid generator JSON: one retry, only if time allows
    persist_queue_size: int = 200  # [P] bounded in-process queue for a failed transcript write
    persist_max_attempts: int = PERSIST_MAX_ATTEMPTS  # [P] then the job is dead-lettered
    slice_seconds: float = SLICE_SECONDS  # [P] own time slice of a network call in a local node
    charge_seconds: float = 1.0  # [P] at most this long for the final token charge
    values_seconds: float = 2.0  # [P] own slice of the soft-filter value lookup in triage

    def limits_for(self, node: str) -> tuple[float, float]:
        """(timeout, minimum time left) of a node; a local node gets a small default."""
        return self.node_limits.get(node, (2.0, 0.0))
