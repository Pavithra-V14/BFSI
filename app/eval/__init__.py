"""Evaluation (spec section 15, Phase 13).

Everything that scores the system lives here. It runs on the admin machine, with the evaluation
keys, against a separate test collection and a separate evaluation database (spec 15.1). Two
parts are light and safe inside the hosted API: `sampling` and `checks` (the 5 % production sample
with its cheap code checks, written off the request path). The heavy parts (`runner`, `judge`,
`ragas_adapter`, `deepeval_adapter`) are never imported by the API process.
"""
