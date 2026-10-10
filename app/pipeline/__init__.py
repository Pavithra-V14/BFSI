"""The query pipeline (spec 4, 7, 10.1-10.2, 11.1, 11.4 writes, 14 spans): Phase 8.

`pipeline.Pipeline(deps).answer(request, db)` runs one question through the 17 stages of the
workflow diagram as a LangGraph graph and returns a `Result`. The chat API (Phase 10) calls it; it
has no web routes of its own.

Rules this package keeps (each has a test):
- Every model call goes through the gateway module. Nothing here calls a provider.
- The generator gets static instructions, the labelled chunks and the question, never history.
- The raw question lives only in the request state, in memory, hidden from `repr`. Models get the
  pass-1 copy (`ModelCopy`); everything stored, logged, queued or put in a review row is the pass-2
  copy (`mask_for_storage`). The cache key is a hash of the pass-1 text (spec 10.1: pass-1 is itself
  masked), and no question text is ever stored in the cache.
- The graph is compiled with no checkpointer and LangChain / LangSmith tracing is switched off in
  code (`safety.py`), because the graph state holds the raw question.
- A failed masker means a refused question, never an unmasked one.
- A failed dependency is a failed stage with a structured error, and the answer to the user is a
  fixed text ("temporarily unavailable"), never a guess.
"""
