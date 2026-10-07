"""Guardrails (spec section 9) and prompts (9.6): the local checks around every query.

Everything here is plain functions over text, so each rule can be tested on its own and the
pipeline (Phase 8) only has to call them in the spec's order. Importing this package loads
nothing heavy: the Presidio/spaCy masker is imported only inside `masking.py`, and this package is
the ONLY runtime code that may import spaCy or Presidio (a test checks it). It never imports
`app.ingest`.
"""
