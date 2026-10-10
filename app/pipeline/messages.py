"""Fixed texts of the pipeline. [P] Wording is a draft for the owner to confirm.

Constants, never model output; they repeat nothing the user wrote. The guardrails' own fixed texts
(greeting, too long, blocked, no reliable context, the disclaimer ...) stay in
`app.guardrails.messages`, and the unclear-turn ending in `app.memory.messages`.
"""

from __future__ import annotations

OUT_OF_DOMAIN = (
    "I can only help with questions about our banking, financial services and insurance "
    "documents. Please ask about one of those topics."
)
UNAVAILABLE = "This service is temporarily unavailable. Please try again in a few minutes."
STARTING_UP = "The service is starting up. Please try again in a minute."
BUSY = (
    "Your previous message is still being answered. Please wait for the answer and then ask again."
)
RATE_LIMITED = "You are sending questions too quickly. Please wait a moment and try again."
OVER_BUDGET = "You have reached today's usage limit. Please try again tomorrow."
SESSION_NOT_FOUND = "That conversation could not be found."
LOW_CONFIDENCE = (
    "I'm not fully sure this answers your question, and it may be incorrect. "
    "Could you rephrase it or add more detail?"
)
CLARIFY = "{understood}{question}"
DEGRADED_RERANK = "Some search steps ran in a reduced mode, so this answer may be less precise."
DEGRADED_MOCK = "This is an offline reply made without our usual systems; please do not rely on it."
