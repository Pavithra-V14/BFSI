"""The gateway package (spec section 5): every LLM, embedding and rerank call goes here."""

import os

# Use the model map that ships inside LiteLLM instead of downloading one at import time.
# This keeps start-up free of network calls and makes the map change only with the pinned
# version. It must be set before LiteLLM is imported anywhere.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
# LiteLLM's own log level: errors only (backend.py also switches its verbose output off).
os.environ.setdefault("LITELLM_LOG", "ERROR")
# In its default "DEV" mode LiteLLM reads a `.env` file from the working directory the moment it
# is imported, and puts everything in it (keys, database and cache addresses) into the process
# environment. This project reads settings only from the shell (CLAUDE.md), so "PRODUCTION" mode
# is set: LiteLLM then never opens a `.env` file.
os.environ.setdefault("LITELLM_MODE", "PRODUCTION")

from app.gateway import questions  # noqa: E402
from app.gateway.config import GatewayConfig  # noqa: E402
from app.gateway.decisions import (  # noqa: E402
    ChoiceAnswer,
    ChoiceQuestion,
    DecisionResult,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
)
from app.gateway.errors import (  # noqa: E402
    AllProvidersFailed,
    ContextWindowExceeded,
    EmbeddingUnavailable,
    GatewayError,
)
from app.gateway.gateway import CompletionResult, EmbeddingResult, Gateway  # noqa: E402
from app.gateway.rerank import RerankResult  # noqa: E402

__all__ = [
    "AllProvidersFailed",
    "ChoiceAnswer",
    "ChoiceQuestion",
    "CompletionResult",
    "ContextWindowExceeded",
    "DecisionResult",
    "EmbeddingResult",
    "EmbeddingUnavailable",
    "Gateway",
    "GatewayConfig",
    "GatewayError",
    "NoulAnswer",
    "NoulQuestion",
    "RerankResult",
    "ScoreAnswer",
    "ScoreQuestion",
    "questions",
]
