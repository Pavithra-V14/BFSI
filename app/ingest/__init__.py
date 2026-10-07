"""Ingestion (spec section 6): the admin machine only, never on Render.

Nothing in `app/api/` may import this package (a test checks it), and the packages it needs are
in `requirements-ingest.txt`, not in `requirements.txt`. Real parsing, captioning, embedding and
indexing are started by the owner with `python -m app.ingest`; the tests use fakes, Qdrant's
in-memory mode, fakeredis and SQLite.
"""
