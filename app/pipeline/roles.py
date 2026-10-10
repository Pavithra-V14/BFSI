"""Which `access_role` values a user's role may see (spec 7 "access_role from the logged-in user").

[P] Owner decision in Phase 8: a customer sees `customer`; staff see `customer` and `staff`; an
admin sees all three. The isolation must hold on every path (a test per role). An unknown role sees
nothing and the pipeline refuses the request.
"""

from __future__ import annotations

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
