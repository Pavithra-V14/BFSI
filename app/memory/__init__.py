"""Short-term memory and sessions (spec 8, 4.3, 4.4, 12.1, 13.6): Phase 7.

Functions the pipeline (Phase 8) and the chat API (Phase 10) will call; nothing here is a route
and nothing calls a provider except `summary.fold_summary`, which goes through the gateway.

- `store`: sessions and messages in the main database. Every function takes the user id and a
  session that belongs to someone else looks exactly like a missing one. Only the masked types of
  `app.guardrails.scan` (`StoredQuery`, `StoredAnswer`) are accepted as message text.
- `buffer`: the history for the rewrite step (a summary plus a verbatim buffer of recent turns).
- `summary`: folds turns that no longer fit the buffer into the running summary (one decide-tier
  call), from masked text only.
- `history`: ties the two together for one turn.
- `facts`: `session_facts` (product line, topic, approved entities), without a model call.
- `unclear`: the count of consecutive unclear turns and its fixed ending (spec 4.3).
- `retention`: the 90-day sweep. `documents`: clearing summaries when a document is deleted.

The generator never sees any of this (spec 8.2): it gets no history.
"""
