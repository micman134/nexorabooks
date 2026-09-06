"""Variance decomposition — the engine behind "why did that change?".

The idea is simple and the discipline around it matters more than the
arithmetic. Given two periods, we take the movement in profit and break it into
smaller and smaller pieces until the pieces are individual journal entries.
At every level the children add back to the parent exactly, so a person can
follow a number from "profit is down 8%" all the way to the transaction that
caused it and never once be asked to take our word for something.

Three rules this module keeps:

  1. **Nothing is estimated.** Every figure is a sum of posted journal lines.
     There is no model, no fitting and no smoothing anywhere in here. If a
     number appears on the screen it is in the ledger.

  2. **It ties to the profit and loss.** The section totals are computed from
     the same subtype groupings ``reports.profit_and_loss`` uses, so the
     Time Machine and the P&L can never disagree. There is a test that proves
     it, and if somebody changes the P&L without changing this, it fails.

  3. **Sign is about profit, not about bookkeeping.** ``delta`` is the movement
     in the account's own natural direction — revenue up is positive, cost up
     is positive. ``profit_effect`` is what that movement did to the bottom
     line, so a cost going up is negative. The screens rank by profit effect,
     because "what hurt me most" is the question being asked.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DEBIT_TYPES,
    EXPENSE,
    INCOME,
    Account,
    Contact,
    Item,
    JournalEntry,
    JournalLine,
)

# --------------------------------------------------------------------------
# The sections, which deliberately mirror the profit and loss
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SectionDef:
    key: str
    label: str
    subtypes: frozenset[str]
    type_: str

    @property
    def sign(self) -> int:
        """+1 if more of this is good for profit, -1 if more of it is bad."""
        return 1 if self.type_ == INCOME else -1


SECTIONS: tuple[SectionDef, ...] = (
    SectionDef("revenue", "Revenue", frozenset({"SALES"}), INCOME),
    SectionDef("other_income", "Other income", frozenset({"OTHER_INCOME"}), INCOME),
    SectionDef("cogs", "Cost of sales", frozenset({"COGS"}), EXPENSE),
    SectionDef(
        "expenses",
        "Operating expenses",
        frozenset(
            {"OPERATING_EXPENSE", "PAYROLL", "DEPRECIATION", "FINANCE_COST", "OTHER_EXPENSE"}
        ),
        EXPENSE,
    ),
    SectionDef("tax", "Taxation", frozenset({"TAX_EXPENSE"}), EXPENSE),
)

SECTION_BY_KEY = {s.key: s for s in SECTIONS}

# How far down a branch can be opened, in order.
DIMENSIONS = ("section", "account", "contact", "item", "entry")

DIMENSION_LABELS = {
    "section": "Where the movement came from",
    "account": "Which account",
    "contact": "Which customer or supplier",
    "item": "Which product or service",
    "entry": "The transactions behind it",
}

COLUMN_LABELS = {
    "section": "Part of the accounts",
    "account": "Account",
    "contact": "Customer or supplier",
    "item": "Product or service",
    "entry": "Transaction",
}


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Period:
    label: str
    start: Date
    end: Date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def _month_start(d: Date) -> Date:
    return d.replace(day=1)


def _month_end(d: Date) -> Date:
    return (d.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _add_months(d: Date, months: int) -> Date:
    total = (d.year * 12 + d.month - 1) + months
    return Date(total // 12, total % 12 + 1, 1)


def _quarter_start(d: Date) -> Date:
    return Date(d.year, ((d.month - 1) // 3) * 3 + 1, 1)


def _shift(period: Period, months: int, label: str) -> Period:
    """Move a whole-month period back by N months, keeping its shape."""
    start = _add_months(period.start, months)
    end = _month_end(_add_months(period.end.replace(day=1), months))
    return Period(label, start, end)


def compare_choices(on: Date | None = None) -> list[tuple[str, Period, Period]]:
    """The ready-made comparisons offered on the screen.

    Each is ``(key, current, prior)``. They are all whole calendar periods
    because comparing a part-month against a full one is the single easiest way
    to frighten somebody with a variance that is not real.
    """
    on = on or Date.today()
    last_full_month = _month_end(_add_months(_month_start(on), -1))
    lm = Period("Last month", _month_start(last_full_month), last_full_month)
    pm = _shift(lm, -1, "The month before")
    lm_yr = _shift(lm, -12, "Same month last year")

    q_start = _quarter_start(on)
    lq_start = _add_months(q_start, -3)
    lq = Period("Last quarter", lq_start, _month_end(_add_months(lq_start, 2)))
    pq = _shift(lq, -3, "The quarter before")
    lq_yr = _shift(lq, -12, "Same quarter last year")

    ytd = Period("This year to date", Date(on.year, 1, 1), on)
    ytd_prior = Period(
        "Same time last year", Date(on.year - 1, 1, 1), Date(on.year - 1, on.month, on.day)
    )

    return [
        ("month_prev", lm, pm),
        ("month_year", lm, lm_yr),
        ("quarter_prev", lq, pq),
        ("quarter_year", lq, lq_yr),
        ("ytd_year", ytd, ytd_prior),
    ]


def choice(key: str, on: Date | None = None) -> tuple[Period, Period] | None:
    for k, cur, prior in compare_choices(on):
        if k == key:
            return cur, prior
    return None


# --------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------


@dataclass
class Node:
    """One line in the drill-down, at whatever depth."""

    key: str
    label: str
    dim: str
    current: int
    prior: int
    sign: int = 1
    subtitle: str = ""
    can_open: bool = True
    ref: str = ""  # where "see the transactions" should go, on a leaf
    section: str = ""  # which part of the P&L this came from, for linking back

    @property
    def delta(self) -> int:
        """Movement in the account's own direction: revenue up is positive."""
        return self.current - self.prior

    @property
    def profit_effect(self) -> int:
        """What that movement did to the bottom line."""
        return self.delta * self.sign

    @property
    def helped(self) -> bool:
        return self.profit_effect > 0

    @property
    def pct_change(self) -> float | None:
        """None when there is nothing to compare against, which is not zero."""
        if self.prior == 0:
            return None
        return (self.current - self.prior) * 100.0 / abs(self.prior)

    @property
    def is_new(self) -> bool:
        return self.prior == 0 and self.current != 0

    @property
    def is_gone(self) -> bool:
        return self.current == 0 and self.prior != 0

    def share_of(self, total: int) -> float:
        """This node's share of a movement, as a percentage of its size.

        Deliberately measured against the *absolute* total, so that when
        children pull in opposite directions the shares stay meaningful
        instead of one of them reading as 400%.
        """
        if not total:
            return 0.0
        return self.profit_effect * 100.0 / abs(total)


@dataclass
class Level:
    """A node together with the breakdown one level below it."""

    node: Node
    children: list[Node] = field(default_factory=list)
    crumbs: list[Node] = field(default_factory=list)
    child_dim: str = ""
    unexplained: int = 0  # children minus parent; must be zero, and is checked

    @property
    def helped_by(self) -> list[Node]:
        return sorted(
            (c for c in self.children if c.profit_effect > 0),
            key=lambda c: -c.profit_effect,
        )

    @property
    def hurt_by(self) -> list[Node]:
        return sorted(
            (c for c in self.children if c.profit_effect < 0),
            key=lambda c: c.profit_effect,
        )

    @property
    def ranked(self) -> list[Node]:
        return sorted(self.children, key=lambda c: -abs(c.profit_effect))

    @property
    def largest(self) -> int:
        """The biggest single effect among the children.

        Bars are drawn against this rather than against the parent's movement.
        When children pull in opposite directions — revenue up nineteen million,
        costs up sixteen — the parent's net movement is small and every bar
        would peg at full width, which tells the reader nothing. Scaling to the
        largest sibling keeps the picture readable and still honest, because
        the exact figures are in the column beside it.
        """
        return max((abs(c.profit_effect) for c in self.children), default=0)

    @property
    def offsetting(self) -> bool:
        """True when the net movement is much smaller than what made it up.

        The threshold is deliberately not "any child bigger than the net". A
        movement of nineteen million made up of a twenty-one million rise and a
        one-and-a-half million fall is not really two things cancelling out, and
        describing it that way would be misleading. Half again is the point at
        which the net figure genuinely stops representing what happened.
        """
        return self.largest > abs(self.node.profit_effect) * 1.5

    @property
    def opposed(self) -> list[Node]:
        """Children pulling the opposite way to the movement overall."""
        move = self.node.profit_effect
        if not move:
            return []
        return [c for c in self.ranked if c.profit_effect and
                (c.profit_effect > 0) != (move > 0)]


# --------------------------------------------------------------------------
# The sums
# --------------------------------------------------------------------------


_UNASSIGNED = "-"
"""The key standing for "this line had no customer / no product on it".

It has to be a real key rather than ``None`` because it travels through a URL,
and it has to be distinguishable from "do not filter on this at all" — those
two mean very different things and confusing them silently mixes every
customer's figures into one row.
"""


def _narrow(q, column, key: str | None):
    """Add one filter, where ``_UNASSIGNED`` means the column is NULL."""
    if key is None:
        return q
    if key == _UNASSIGNED:
        return q.where(column.is_(None))
    value = _as_int(key)
    return q.where(column == value) if value is not None else q


def _grouped(
    db: Session,
    start: Date,
    end: Date,
    group_col,
    subtypes: frozenset[str] | None,
    account: str | None = None,
    contact: str | None = None,
    item: str | None = None,
) -> dict:
    """``{group value: signed amount}`` for posted lines in the range.

    The sign is applied per account, so a section made of several accounts
    still adds up correctly even if somebody has put a contra account in it.
    """
    # Grouped by account *type* as well as by the requested column, because the
    # natural direction of a balance depends on the type and the sum has to be
    # signed before rows from different types are added together.
    q = (
        select(
            group_col,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.is_posted.is_(True),
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .group_by(group_col, Account.type)
    )
    if subtypes is not None:
        q = q.where(Account.subtype.in_(tuple(subtypes)))
    q = _narrow(q, JournalLine.account_id, account)
    q = _narrow(q, JournalLine.contact_id, contact)
    q = _narrow(q, JournalLine.item_id, item)

    out: dict = {}
    for key, acc_type, debit, credit in db.execute(q):
        debit, credit = int(debit), int(credit)
        value = (debit - credit) if acc_type in DEBIT_TYPES else (credit - debit)
        out[key] = out.get(key, 0) + value
    return out


def _pair(cur: dict, prior: dict) -> list:
    """Every key present in either period, so a thing that stopped still shows."""
    return sorted(set(cur) | set(prior), key=lambda k: (k is None, str(k)))


def _real(nodes: list[Node]) -> list[Node]:
    """Drop rows that are nil in both periods.

    An account can appear in the grouping with equal debits and credits — a
    posting and its reversal, say — and a row reading zero, zero, zero invites
    somebody to click it and find nothing. Removing them cannot affect any
    total, because they are worth nothing.
    """
    return [n for n in nodes if n.current or n.prior]


# --------------------------------------------------------------------------
# Walking the tree
# --------------------------------------------------------------------------


def _root(db: Session, cur: Period, prior: Period) -> tuple[Node, list[Node]]:
    """Net profit, broken into the five parts of the profit and loss."""
    children: list[Node] = []
    total_now = total_was = 0
    for sec in SECTIONS:
        c = _grouped(db, cur.start, cur.end, Account.subtype, sec.subtypes)
        p = _grouped(db, prior.start, prior.end, Account.subtype, sec.subtypes)
        now, was = sum(c.values()), sum(p.values())
        total_now += now * sec.sign
        total_was += was * sec.sign
        if now or was:
            children.append(Node(sec.key, sec.label, "section", now, was, sec.sign))
    root = Node("", "Net profit", "root", total_now, total_was, 1)
    return root, children


def _accounts(db: Session, sec: SectionDef, cur: Period, prior: Period) -> list[Node]:
    c = _grouped(db, cur.start, cur.end, JournalLine.account_id, sec.subtypes)
    p = _grouped(db, prior.start, prior.end, JournalLine.account_id, sec.subtypes)
    ids = [k for k in _pair(c, p) if k is not None]
    accounts = (
        {a.id: a for a in db.scalars(select(Account).where(Account.id.in_(ids)))} if ids else {}
    )
    out = []
    for aid in ids:
        acc = accounts.get(aid)
        if acc is None:
            continue
        out.append(
            Node(str(aid), acc.name, "account", c.get(aid, 0), p.get(aid, 0), sec.sign,
                 subtitle=acc.code, section=sec.key)
        )
    return _real(out)


def _contacts(db: Session, sec: SectionDef, account: str, cur: Period, prior: Period) -> list[Node]:
    c = _grouped(db, cur.start, cur.end, JournalLine.contact_id, sec.subtypes, account=account)
    p = _grouped(db, prior.start, prior.end, JournalLine.contact_id, sec.subtypes, account=account)
    keys = _pair(c, p)
    ids = [k for k in keys if k is not None]
    names = (
        {x.id: x.name for x in db.scalars(select(Contact).where(Contact.id.in_(ids)))}
        if ids
        else {}
    )
    out = []
    for k in keys:
        node = Node(
            str(k) if k is not None else _UNASSIGNED,
            names.get(k, "Not linked to anyone") if k is not None else "Not linked to anyone",
            "contact",
            c.get(k, 0),
            p.get(k, 0),
            sec.sign,
            section=sec.key,
        )
        if k is None:
            node.subtitle = "entries with no customer or supplier on them"
        out.append(node)
    return _real(out)


def _items(
    db: Session,
    sec: SectionDef,
    account: str,
    contact: str | None,
    cur: Period,
    prior: Period,
) -> list[Node]:
    c = _grouped(db, cur.start, cur.end, JournalLine.item_id, sec.subtypes,
                 account=account, contact=contact)
    p = _grouped(db, prior.start, prior.end, JournalLine.item_id, sec.subtypes,
                 account=account, contact=contact)
    keys = _pair(c, p)
    ids = [k for k in keys if k is not None]
    names = {x.id: x.name for x in db.scalars(select(Item).where(Item.id.in_(ids)))} if ids else {}
    out = []
    for k in keys:
        out.append(
            Node(
                str(k) if k is not None else _UNASSIGNED,
                names.get(k, "No product or service") if k is not None else "No product or service",
                "item",
                c.get(k, 0),
                p.get(k, 0),
                sec.sign,
                section=sec.key,
            )
        )
    return _real(out)


def _entries(
    db: Session,
    sec: SectionDef,
    account: str,
    contact: str | None,
    item: str | None,
    cur: Period,
    prior: Period,
) -> list[Node]:
    """The bottom of the tree: individual transactions in the current period.

    Prior-period transactions are different documents, not earlier versions of
    these ones, so there is nothing honest to compare a single entry against.
    Each row shows what it was, and links to it.
    """
    c = _grouped(db, cur.start, cur.end, JournalLine.entry_id, sec.subtypes,
                 account=account, contact=contact, item=item)
    ids = [k for k in c if k is not None]
    entries = (
        {e.id: e for e in db.scalars(select(JournalEntry).where(JournalEntry.id.in_(ids)))}
        if ids
        else {}
    )
    out = []
    for eid, value in sorted(c.items(), key=lambda kv: -abs(kv[1])):
        entry = entries.get(eid)
        if entry is None:
            continue
        node = Node(
            str(eid),
            entry.memo or entry.reference or entry.number,
            "entry",
            value,
            0,
            sec.sign,
            subtitle=f"{entry.number} · {entry.date:%d %b %Y}",
            can_open=False,
            ref=f"/journals/{eid}",
            section=sec.key,
        )
        out.append(node)
    return out


def explore(db: Session, cur: Period, prior: Period, path: list[str] | None = None) -> Level:
    """Open the tree at ``path`` and return that node with its children.

    ``path`` is the keys taken so far: ``["revenue", "12", "45"]`` means the
    revenue section, account 12, contact 45.
    """
    path = [p for p in (path or []) if p != ""]

    root, sections = _root(db, cur, prior)
    if not path or path[0] not in SECTION_BY_KEY:
        return _finish(Level(root, sections, [], "section"))

    sec = SECTION_BY_KEY[path[0]]
    crumbs: list[Node] = [root]
    node = next(
        (s for s in sections if s.key == sec.key),
        Node(sec.key, sec.label, "section", 0, 0, sec.sign, section=sec.key),
    )

    # --- level 1: which account within the section -----------------------
    accounts = _accounts(db, sec, cur, prior)
    if len(path) == 1:
        return _finish(Level(node, accounts, crumbs, "account"))

    crumbs.append(node)
    account = path[1]
    node = next((a for a in accounts if a.key == account), None)
    if node is None:  # a stale or hand-typed link: show the level above it
        return _finish(Level(crumbs[-1], accounts, crumbs[:-1], "account"))

    # --- level 2: which customer or supplier -----------------------------
    contacts = _contacts(db, sec, account, cur, prior)
    if len(path) == 2:
        return _finish(Level(node, contacts, crumbs, "contact"))

    crumbs.append(node)
    contact = path[2]
    node = next((x for x in contacts if x.key == contact), None)
    if node is None:
        return _finish(Level(crumbs[-1], contacts, crumbs[:-1], "contact"))

    # --- level 3: which product or service -------------------------------
    if len(path) == 3:
        kids = _items(db, sec, account, contact, cur, prior)
        # A lone "no product" row explains nothing, so skip to the transactions.
        if len(kids) <= 1:
            return _finish(
                Level(node, _entries(db, sec, account, contact, None, cur, prior), crumbs, "entry")
            )
        return _finish(Level(node, kids, crumbs, "item"))

    crumbs.append(node)
    item = path[3]
    items = _items(db, sec, account, contact, cur, prior)
    node = next((x for x in items if x.key == item), None)
    if node is None:
        return _finish(Level(crumbs[-1], items, crumbs[:-1], "item"))

    # --- level 4: the transactions themselves ----------------------------
    return _finish(
        Level(node, _entries(db, sec, account, contact, item, cur, prior), crumbs, "entry")
    )


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finish(level: Level) -> Level:
    """Prove the children add back to the parent before anybody sees them.

    At the transaction level they will not, because that level only lists the
    current period, so the check is skipped there rather than reported as an
    inconsistency.
    """
    if level.child_dim != "entry" and level.children:
        kids = sum(c.profit_effect for c in level.children)
        level.unexplained = level.node.profit_effect - kids
    level.children = level.ranked
    return level


# --------------------------------------------------------------------------
# Saying it in words
# --------------------------------------------------------------------------


def narrate(level: Level, fmt) -> list[str]:
    """Two or three plain sentences about what this level shows.

    ``fmt`` is the money formatter, passed in so this module never has to know
    which currency the company keeps its books in. Every sentence is assembled
    from real figures — there is no generated prose anywhere in this file.
    """
    node, out = level.node, []
    move = node.profit_effect
    name = "Profit" if node.dim == "root" else node.label

    if move == 0 and node.current == node.prior:
        return [f"{name} did not move at all between the two periods."]

    if node.dim == "root":
        direction = "up" if move > 0 else "down"
        pct = node.pct_change
        tail = f" ({abs(pct):.1f}%)" if pct is not None else ""
        out.append(f"Profit is {direction} {fmt(abs(move))}{tail}.")
    else:
        verb = "rose" if node.delta > 0 else "fell"
        pct = node.pct_change
        tail = f", {abs(pct):.1f}%" if pct is not None else ""
        effect = "which helped profit" if move > 0 else "which cost you profit"
        out.append(f"{name} {verb} by {fmt(abs(node.delta))}{tail} — {effect}.")

    ranked = level.ranked
    if ranked:
        first = ranked[0]
        share = abs(first.share_of(move)) if move else 0
        word = "added" if first.profit_effect > 0 else "cost"
        opposed = level.opposed
        if level.offsetting and len(ranked) >= 2:
            # The net figure hides two larger movements pulling against each
            # other. Saying "X is 561% of the movement" is arithmetically true
            # and useless; what the reader needs is that the two nearly
            # cancelled out.
            second = ranked[1]
            against = ("gave most of that back" if second.profit_effect * first.profit_effect < 0
                       else "moved with it")
            out.append(
                f"That net figure hides two much bigger movements. "
                f"{first.label} {word} {fmt(abs(first.profit_effect))}, and "
                f"{second.label} {against} at {fmt(abs(second.profit_effect))}."
            )
        elif share > 100 and opposed:
            # One thing did more than the whole movement, and something else
            # took part of it back. Quoting a share over 100% is correct and
            # unreadable; naming both sides is neither.
            other = opposed[0]
            out.append(
                f"It is mostly {first.label}, which {word} {fmt(abs(first.profit_effect))} "
                f"— more than the net movement, because {other.label} went the "
                f"other way by {fmt(abs(other.profit_effect))}."
            )
        elif share >= 40:
            out.append(
                f"Most of it is one thing: {first.label}, which {word} "
                f"{fmt(abs(first.profit_effect))} — {share:.0f}% of the movement."
            )
        elif len(ranked) >= 2:
            second = ranked[1]
            out.append(
                f"The two biggest pieces are {first.label} "
                f"({fmt(abs(first.profit_effect))}) and {second.label} "
                f"({fmt(abs(second.profit_effect))})."
            )
        else:
            out.append(f"It comes down to {first.label}, {fmt(abs(first.profit_effect))}.")

    # At transaction level every row is "new" by construction, because the prior
    # period contains different documents rather than earlier versions of these
    # ones. Saying so would be true of every row and useful about none.
    if level.child_dim == "entry":
        return out

    new = [c for c in ranked if c.is_new]
    gone = [c for c in ranked if c.is_gone]
    if new:
        out.append(
            f"New this period: {', '.join(c.label for c in new[:3])}"
            + (f" and {len(new) - 3} more." if len(new) > 3 else ".")
        )
    if gone:
        out.append(
            f"Nothing at all this period from: {', '.join(c.label for c in gone[:3])}"
            + (f" and {len(gone) - 3} more." if len(gone) > 3 else ".")
        )
    return out


# --------------------------------------------------------------------------
# What the brief and the board pack ask for
# --------------------------------------------------------------------------


def top_movers(db: Session, cur: Period, prior: Period, limit: int = 6) -> list[Node]:
    """The biggest movers at account level, across every section.

    Used by the daily brief and the board pack. Account level is the right
    depth for a summary: section level is too coarse to act on and contact
    level is too long to read.
    """
    movers: list[Node] = []
    for sec in SECTIONS:
        movers.extend(_accounts(db, sec, cur, prior))
    movers = [m for m in movers if m.profit_effect != 0]
    movers.sort(key=lambda m: -abs(m.profit_effect))
    return movers[:limit]


def summary(db: Session, cur: Period, prior: Period) -> Level:
    """The top of the tree, ready for a headline."""
    return explore(db, cur, prior, [])
