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
    "generate": (12.0, 4.0),
    "persist": (3.0, 0.3),
}


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

    def limits_for(self, node: str) -> tuple[float, float]:
        """(timeout, minimum time left) of a node; a local node gets a small default."""
        return self.node_limits.get(node, (2.0, 0.0))
