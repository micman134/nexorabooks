"""What a group of companies is, and how its figures are put together.

Several companies under one owner is the ordinary shape of a Nigerian business
once it has grown: a trading company, a logistics company, a property company.
Each keeps its own books — which is right, because each is its own legal
person — and the owner still has to answer one question at the end of the year:
what did the whole thing make?

This file holds the settings that answer it, and nothing else. The arithmetic
is in ``app/services/consolidation.py``.

The settings live in ``group.json`` in the data folder rather than inside any
one company's database, because a group belongs to none of its members. They
are:

  * which companies are in the group;
  * the currency the group reports in, and the rate for each member that does
    not keep its books in that currency;
  * which customer or supplier record in each company stands for another
    company in the group, so that what the group owes itself can be taken out.

Nothing here changes a single company's books. Consolidation is a way of
looking at them, not a transaction, and this software will not write to a
member company while it is doing it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from . import companies as registry
from . import config

FILE = "group.json"


def _one() -> Decimal:
    return Decimal("1")


def rate_of(text) -> Decimal:
    """A rate as the customer typed it, or one if it makes no sense.

    A rate of nought would silently wipe a member company out of the group
    accounts, so it is refused in the same breath as the letters and the empty
    box.
    """
    try:
        value = Decimal(str(text).strip().replace(",", ""))
    except (InvalidOperation, AttributeError, ValueError):
        return _one()
    return value if value > 0 else _one()


@dataclass
class Member:
    """One company in the group, and how its figures come across."""

    slug: str
    include: bool = True
    #: Group currency units per one unit of this company's currency.
    closing_rate: str = "1"
    average_rate: str = "1"
    #: Contact id in this company -> the slug of the group company it is.
    internal: dict[str, str] = field(default_factory=dict)

    @property
    def closing(self) -> Decimal:
        return rate_of(self.closing_rate)

    @property
    def average(self) -> Decimal:
        return rate_of(self.average_rate)

    @property
    def name(self) -> str:
        ref = registry.get(self.slug)
        return ref.name if ref else self.slug

    @property
    def exists(self) -> bool:
        ref = registry.get(self.slug)
        return bool(ref and ref.exists)


@dataclass
class Group:
    """The group as a whole."""

    name: str = ""
    currency: str = ""
    members: list[Member] = field(default_factory=list)

    @property
    def chosen(self) -> list[Member]:
        return [m for m in self.members if m.include and m.exists]

    @property
    def is_set_up(self) -> bool:
        return len(self.chosen) >= 2

    def member(self, slug: str) -> Member | None:
        return next((m for m in self.members if m.slug == slug), None)

    def internal_slugs(self) -> set[str]:
        return {m.slug for m in self.chosen}


def _file():
    return config.data_dir() / FILE


def load() -> Group:
    """The saved group, with any company that has since been added folded in.

    A company created after the group was set up appears in the list, switched
    off. That way a new company is never quietly consolidated because somebody
    forgot the settings page existed, and never invisible either.
    """
    raw = {}
    path = _file()
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            raw = {}

    group = Group(name=raw.get("name", ""), currency=raw.get("currency", ""))
    saved = {row.get("slug"): row for row in raw.get("members", [])
             if isinstance(row, dict) and row.get("slug")}

    for ref in registry.all_companies():
        row = saved.get(ref.slug, {})
        group.members.append(Member(
            slug=ref.slug,
            include=bool(row.get("include", False)),
            closing_rate=str(row.get("closing_rate", "1")),
            average_rate=str(row.get("average_rate", "1")),
            internal={str(k): str(v) for k, v in (row.get("internal") or {}).items()},
        ))
    return group


def save(group: Group) -> None:
    _file().write_text(json.dumps({
        "name": group.name,
        "currency": group.currency,
        "members": [
            {"slug": m.slug, "include": m.include,
             "closing_rate": m.closing_rate, "average_rate": m.average_rate,
             "internal": m.internal}
            for m in group.members
        ],
    }, indent=2), encoding="utf-8")
