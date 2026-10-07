"""The fixed texts the guardrails return (spec 4.1, 4.2, 9.4, 9.5). [P] wording.

They are constants, never model output, so a greeting, a refusal or a warning costs no model call
and reads the same every time. Nothing here repeats the user's words.
"""

from __future__ import annotations

# 4.2: only a whole-message greeting or farewell gets one of these (no model, no retrieval).
GREETING_REPLY = (
    "Hello! I can answer questions about our banking, financial services and insurance "
    "documents. What would you like to know?"
)
FAREWELL_REPLY = "Goodbye! Come back any time you have a question about our documents."
THANKS_REPLY = "You're welcome! Ask me anything else about our documents."

EMPTY_QUERY = "Please type a question about our documents."
TOO_SHORT = (
    "Please add a little more detail to your first question (at least {minimum} characters)."
)
TOO_LONG = (
    "Your question is too long. Please shorten it to about {limit} tokens or fewer and ask again."
)

# 4.1 step 4: a strong injection signal is refused; a mixed signal asks for a rewording.
BLOCKED = "I can't help with that request."
REWORD = (
    "I couldn't process that request as written. Please reword your question about our documents."
)

# 4.1 step 11: every retrieved chunk was dropped by the context guard.
NO_RELIABLE_CONTEXT = (
    "I couldn't find reliable information in our documents to answer that. "
    "Please try rewording your question."
)

# 9.5: shown only when relevant (advice-like questions, answers with financial figures). [T]
DISCLAIMER = (
    "This is general information drawn from our documents, not personal financial, legal or tax "
    "advice. For decisions about your own situation, please speak to a qualified adviser or "
    "contact customer care."
)

# 9.4: the warning that goes with an answer whose checks failed.
WARNING_NUMBERS = (
    "Some figures in this answer could not be found in the cited documents: {figures}. "
    "Please check them against the source before relying on them."
)
WARNING_CITATIONS = (
    "Some sources named in this answer were not among the documents retrieved for your question."
)
WARNING_GROUNDING = (
    "Parts of this answer may not be supported by the cited documents. "
    "Please check the source before relying on it."
)
WARNING_NO_CITATIONS = "This answer does not cite a source document. Please treat it with care."
