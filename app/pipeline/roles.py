"""Which `access_role` values a user's role may see (spec 7 "access_role from the logged-in user").

[P] Owner decision in Phase 8: a customer sees `customer`; staff see `customer` and `staff`; an
admin sees all three. The isolation must hold on every path (a test per role). An unknown role sees
nothing and the pipeline refuses the request.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

VISIBLE_ROLES: dict[str, tuple[str, ...]] = {
    "customer": ("customer",),
    "staff": ("customer", "staff"),
    "admin": ("customer", "staff", "admin"),
}


class UnknownRole(ValueError):
    pass


def visible_roles(role: str) -> tuple[str, ...]:
    try:
        return VISIBLE_ROLES[role]
    except (KeyError, TypeError):
        raise UnknownRole("unknown role") from None


def role_map_version(mapping: Mapping[str, tuple[str, ...]] | None = None) -> str:
    """A short hash of the whole role-to-visible-roles map. It is part of every exact-cache key
    (spec 10.3 item 7, [P] Phase 9): if the map ever changes, every earlier entry becomes
    unreachable at once, whatever the corpus version says. Computed from the live map on each call,
    so a change takes effect without a restart."""
    current = VISIBLE_ROLES if mapping is None else mapping
    canonical = json.dumps(
        {role: list(roles) for role, roles in sorted(current.items())}, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
