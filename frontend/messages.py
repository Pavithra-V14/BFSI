"""The frontend's own fixed texts. [P] Drafts: the owner confirms the wording before release.

Constants only: nothing here repeats what a user typed or what a model wrote. The answers and the
API's own fixed messages are shown as they come (escaped), never rewritten here.
"""

from __future__ import annotations

APP_NAME = "BFSI Assistant"
TAGLINE = "Answers from our policy and regulatory documents, with the clauses they come from."

WAKING_TITLE = "The service is waking up"
WAKING_TEXT = (
    "This can take up to a minute the first time after a quiet period. "
    "You do not need to do anything: this page will carry on by itself."
)
WAKING_GAVE_UP = "The service is still not ready. Please try again in a minute."
TIMEOUT_TEXT = (
    "That took longer than expected. Your question may still have been answered: "
    "open the chat from the list on the left before asking it again."
)
GENERIC_ERROR = "Something went wrong. Please try again in a moment."
SIGNED_OUT = "Please sign in again."
CONNECTION_ERROR = (
    "The connection to the service was interrupted. Your question may have been received: "
    "open your chats on the left before asking it again."
)
INSECURE_API_TITLE = "Not a secure connection"
INSECURE_API_TEXT = (
    "The service address does not start with https, so passwords and your sign-in would travel "
    "unprotected. Do not sign in here. Tell the person who runs this service."
)
CONFIG_MISSING_TITLE = "Safety settings may not be active"
CONFIG_MISSING_TEXT = (
    "This page was started from a different folder than the project folder, so its settings "
    "file was not found. Tell the person who runs this service."
)
INTERRUPTED_TITLE = "Question not sent"
INTERRUPTED_TEXT = (
    "You sent another question while an answer was still on its way, so the page stopped "
    "waiting for the first one. It may still be answered: open it from My chats on the left. "
    "Please ask your new question again once the answer has arrived."
)
LOGOUT_FAILED_TITLE = "Signed out on this page only"
LOGOUT_FAILED_TEXT = (
    "The service could not be reached to end your sign-in, so it stays valid there until it "
    "expires. If this is a shared computer, sign in again later and sign out."
)

# Stages shown while an answer is on its way. They are generic and time-based: the service does
# not report its progress, so these do not claim which step is running.
PROGRESS_STAGES: tuple[tuple[float, str], ...] = (
    (0.0, "Checking your question"),
    (4.0, "Searching the documents"),
    (12.0, "Writing the answer"),
    (30.0, "Still working. This one is taking longer than usual"),
)

NO_FEEDBACK_YET = "Feedback is not available for this answer yet."
FEEDBACK_THANKS = "Thank you, your feedback was noted."

LOW_CONFIDENCE_TITLE = "Check this answer"
SOURCE_REMOVED = "A document this answer relied on has been withdrawn since."
UNVERIFIED_TITLE = "Not fully verified"
UNVERIFIED_TEXT = "This answer could not be fully verified when it was given."
TEST_PERIOD_TITLE = "Test period"
EMPTY_CHAT = "Ask a question about a policy, a product or a regulation."

# What each reduced-mode code means, in plain words (unknown codes get the last text).
DEGRADED_TEXT = {
    "lexical_rerank": (
        "Some search steps ran in a reduced mode, so this answer may be less precise."
    ),
    "mock_answer": (
        "This is an offline reply made without our usual systems. Please do not rely on it."
    ),
    "dev_embedding": "This answer was made in a test set-up. Please do not rely on it.",
}
DEGRADED_DEFAULT = "Some of our systems ran in a reduced mode, so this answer may be less precise."

# Friendly text for the API's error codes where the API's own message is not enough.
ERROR_TEXT = {
    "not_signed_in": SIGNED_OUT,
    "too_many_requests": "You are sending requests too quickly. Please wait a moment.",
    "too_many_attempts": "Too many attempts. Please wait a while and try again.",
    "over_budget": "You have reached today's usage limit. Please try again tomorrow.",
    "busy": "Your previous message is still being answered. Please wait for it to finish.",
    "starting_up": "The service is starting up. Please try again in a minute.",
    "connection_error": CONNECTION_ERROR,
    "service_unavailable": "This service is temporarily unavailable. Please try again shortly.",
    "forbidden": "You do not have access to this.",
    "not_found": "That could not be found.",
    "account_exists": "That username or email cannot be used.",
    "invalid_username": (
        "Use 3 to 32 letters, digits, dots, dashes or underscores, starting with a letter or digit."
    ),
    "invalid_email": "That does not look like an email address.",
    "invalid_password": (
        "Choose a longer, less guessable password (10 to 128 characters, not your name, "
        "not a very common password)."
    ),
    "request_too_large": "That message is too long.",
    "entity_review_disabled": "Entity review is not switched on on this machine.",
    "admin_machine_only": "This is done on the admin machine, not on the hosted service.",
    "single_word_needs_confirmation": "A one-word name is approved only when you confirm it.",
}

REVIEW_LABELS = (
    "correct_refusal",
    "should_have_answered",
    "wrong_answer",
    "bad_question",
    "other",
)
LABEL_TEXT = {
    "correct_refusal": "Correct refusal",
    "should_have_answered": "Should have answered",
    "wrong_answer": "Wrong answer",
    "bad_question": "Bad question",
    "other": "Other",
}
# What happened when an item was added to the evaluation set (the service's one-word answer).
CANDIDATE_TEXT = {
    "added": "It waits as a candidate for the next evaluation set version.",
    "already_added": "It was already a candidate.",
    "not_added": "The label was saved; a bad question is not added to the set.",
    "no_evaluation_database": "The label was saved; no evaluation database is set up here.",
    "failed": "The label was saved; the candidate could not be written.",
}
STAGE_TEXT = {
    "shadow": "Shadow: only admins see answers",
    "staff_beta": "Staff beta: admins and staff see answers",
    "open": "Open: everyone sees answers",
}
ROLE_TEXT = {"customer": "Customer", "staff": "Staff", "admin": "Admin"}
