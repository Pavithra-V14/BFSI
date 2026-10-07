"""The curated web domains: the regulators' and the company's own (spec 6.8, 9.5).

A contact found in a document is allow-listed as a helpline only when its domain is curated, and a
model answer may keep a web address as plain text only when its domain is curated. One list serves
both: the built-in regulator domains plus the company's own (`COMPANY_DOMAINS` [T]).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

# Regulator domains (a name matches the domain itself and any sub-domain of it).
SEED_DOMAINS = (
    "irdai.gov.in",
    "sebi.gov.in",
    "rbi.org.in",
    "npci.org.in",
    "amfiindia.com",
    "scores.gov.in",
)
# The company's own domain for the sample documents (spec 20); the real ones: COMPANY_DOMAINS [T].
DEFAULT_COMPANY_DOMAINS = ("auravance.aura",)


def curated_domains(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The regulator domains plus the company's (COMPANY_DOMAINS, comma-separated, or the sample
    company's domain when it is not set)."""
    listed = ()
    if env is not None:
        listed = tuple(d.strip().lower() for d in (env.get("COMPANY_DOMAINS") or "").split(","))
    company = tuple(d for d in listed if d) or DEFAULT_COMPANY_DOMAINS
    return (*SEED_DOMAINS, *company)


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")
_AUTHORITY_END = re.compile(r"[/?#\\\s]")  # a backslash ends it too: browsers treat "\" as "/"
_HOST = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")


def host_of(contact: str) -> str:
    """The host of an email address or web address, lower-cased, without "www.". Empty when it is
    not a plain host name.

    Only the AUTHORITY (the text before the first "/", "?", "#" or "\\") is looked at, and "user@"
    is taken from there alone: in `https://evil.com/login?next=a@irdai.gov.in` the host is evil.com,
    not what follows an "@" further on."""
    text = _SCHEME.sub("", contact.strip().lower())
    authority = _AUTHORITY_END.split(text, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1].split(":", 1)[0].removeprefix("www.").strip(".")
    return host if _HOST.fullmatch(host) else ""


def domain_is_seeded(contact: str, domains: Iterable[str]) -> bool:
    """True when an email address or web address belongs to one of the curated domains (the
    domain itself or a sub-domain of it)."""
    host = host_of(contact)
    return bool(host) and any(host == d or host.endswith("." + d) for d in domains)
