"""Observability (spec 14, 5.3, 5.4, 13.6, 15.6, 18): structured logs, alerts, the numbers behind
the dashboards, the removal of traces of a deleted chat, and the dev-only stage printer.

Everything here handles NAMES, COUNTS and TIMES. None of it ever sees a question, an answer, a
username, an address or a key: the structures that enter this package carry no text, and the
places where a string could slip in (log messages, alert fields) are filtered by an allow-list.
"""
