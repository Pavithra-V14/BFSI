"""The Streamlit frontend (spec 24, Phase 11).

A separate service. It talks to the API over HTTP only and imports nothing from `app/`, so none of
the API's packages (torch, spaCy, Presidio, the ingestion package) ever reach this service.
"""
