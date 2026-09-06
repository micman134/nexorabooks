"""Which customer's books a request is allowed to touch.

There are two ways Nexora Books runs, and they need opposite answers.

**Installed.** One business, one computer, one set of people who all work for
that business. Somebody with several companies switches between them freely,
because they own all of them. The choice lives in the session cookie, which is
fine: the worst a person can do by tampering with it is open their own second
company.

**Hosted.** Many unrelated businesses on one server. Here the session cookie is
the wrong place to keep the answer, because a cookie is something the visitor
holds. Anything the visitor holds is something the visitor can change, and a
changed value that selects a database is not an inconvenience — it is a way
into somebody else's books.

So in hosted mode the boundary is the hostname:

    acme.nexorabooks.com   ->  the company at slug "acme", and nothing else
    beta.nexorabooks.com   ->  the company at slug "beta",  and nothing else

The hostname is set by DNS and checked here against a strict pattern. It is not
taken from a header the browser is free to invent — ``Host`` is, but a wrong
one resolves to a slug that either does not exist (404) or is not the tenant's
(and then it is that tenant's own subdomain, which tells the visitor nothing
they did not already know and gives them a login screen they cannot pass).

Two rules make this hold, and both are enforced rather than documented:

1. In hosted mode the company is resolved **only** from the host. There is no
   fallback to "the first company on the box" — a fallback is exactly how a
   stranger arriving at an unknown name would be handed somebody's books.
2. In hosted mode the screens that list, switch, create, rename or archive
   companies are refused outright. A screen that lists companies is a screen
   that leaks the customer list, even if every link on it is dead.
"""
from __future__ import annotations

import os
import re

#: Labels that must never resolve to a customer, because they are either
#: already in use for the service itself or would be mistaken for it.
RESERVED = frozenset({
    "www", "app", "apps", "api", "admin", "administrator", "root",
    "mail", "smtp", "imap", "pop", "webmail", "email",
    "static", "assets", "cdn", "media", "files", "download", "downloads",
    "help", "docs", "doc", "support", "status", "blog", "news",
    "billing", "pay", "payments", "checkout", "account", "accounts",
    "login", "signup", "register", "auth", "sso", "id",
    "test", "testing", "staging", "stage", "dev", "demo", "sandbox",
    "ns", "ns1", "ns2", "dns", "mx", "ftp", "vpn", "git", "ci",
    "nexora", "nexorabooks", "tavo", "tavonetworks",
})

#: A tenant label. Deliberately narrower than DNS allows: lowercase letters,
#: digits and inner hyphens only. ``company_dir`` builds a filesystem path from
#: this, so anything that could be a path — a dot, a slash, a backslash, a
#: colon, a percent escape, a leading dot-dot — must never get through.
LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

#: The longest a slug may be. ``companies.slugify`` truncates to 40, so a label
#: longer than that could never match a real company anyway.
MAX_LABEL = 40


def hosted() -> bool:
    """True when this process is serving many customers from one server.

    Off unless somebody deliberately turned it on. An installed copy on a desk
    in Lekki must never wake up one morning behaving like a shared server
    because of a stray environment variable in a batch file.
    """
    return os.environ.get("NEXORA_HOSTED", "").strip().lower() in ("1", "true", "yes", "on")


def base_domain() -> str:
    """The domain customers' subdomains hang off, e.g. ``nexorabooks.com``."""
    return os.environ.get("NEXORA_BASE_DOMAIN", "").strip().lower().strip(".")


def _strip_port(host: str) -> str:
    """``acme.example.com:8756`` -> ``acme.example.com``.

    IPv6 literals arrive bracketed (``[::1]:8756``); they can never carry a
    tenant label, but they must not crash the parser on their colons either.
    """
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host                      # an address, never a name
    return host.split(":", 1)[0]


def slug_from_host(host: str | None) -> str | None:
    """The tenant a hostname names, or ``None`` if it names no tenant.

    ``None`` is the safe answer and the common one. It means: serve this
    visitor nothing. It must never be turned into "well, serve them the first
    company then".
    """
    base = base_domain()
    if not base:
        return None                      # not configured; refuse everything

    name = _strip_port(host or "")
    if not name or name == base:
        return None                      # the bare domain belongs to nobody

    suffix = "." + base
    if not name.endswith(suffix):
        return None                      # a name from somewhere else entirely

    label = name[: -len(suffix)]
    if "." in label:
        return None                      # a.b.example.com is not a tenant
    if len(label) > MAX_LABEL:
        return None
    if label in RESERVED:
        return None
    if not LABEL.match(label):
        return None
    return label


def resolve(host: str | None) -> str | None:
    """The company slug this request may open, or ``None`` for none at all.

    In installed mode this returns ``None`` too — not because the request is
    refused, but because the host has no say in it there and the caller should
    go on using the session.
    """
    if not hosted():
        return None
    return slug_from_host(host)


class NotOurs(Exception):
    """Raised when a hosted request names no tenant we serve.

    Carries no detail on purpose. Telling a visitor whether a subdomain exists
    is telling them your customer list one guess at a time.
    """


def guard_installed_only(what: str = "This") -> None:
    """Refuse an operation that only makes sense on an installed copy.

    Listing, switching, creating, renaming and archiving companies are all
    fine on somebody's own computer and all wrong on a shared server. Rather
    than hide the buttons and hope, the routes themselves call this.
    """
    if hosted():
        from fastapi import HTTPException

        raise HTTPException(
            404,
            f"{what} is not available on the hosted service. Each business has "
            "its own address and its own books.",
        )
