"""The one small exception that means "this could not be scored, and here is why".

It lives alone, with no import, so the adapters of the evaluation libraries (which run in a
throw-away environment) and the judge can share it. The reason is a fixed word
(`no_judge_model`, `providers_failed`, `judge_same_family`, `model_unknown` ...): it is kept in
the `decider` field of the stored result as `not_scored:<reason>`.
"""

from __future__ import annotations


class NotScored(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def decider_for(reason: str) -> str:
    return f"not_scored:{reason}" if reason else "not_scored"
