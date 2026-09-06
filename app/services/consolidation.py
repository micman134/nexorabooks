"""Adding several companies up into one set of figures.

An owner with three companies has three sets of books that each balance and no
answer to "what did the group make?". This works that out, and it does it by
reading — no member company is written to while it runs, and there is no fourth
database holding a second version of the truth. Close the screen and nothing
has changed anywhere.

Three things have to happen, and each of them is a place where group accounts
usually go wrong:

  * **The columns have to line up.** Companies set up from the same chart share
    account codes, so the figures are combined on the code. Where two members
    use one code for different things, that is said out loud rather than
    silently added together.

  * **Money has to be translated.** A member keeping cedis cannot simply be
    added to a member keeping naira. Income and costs come across at the
    average rate for the period and balances at the closing rate, which is the
    ordinary treatment — and mixing two rates leaves a difference that has to
    go somewhere. It goes on its own line, named, in equity. It is never
    quietly forced into another figure to make the statement balance.

  * **The group cannot owe itself money.** What one member sells another is not
    group revenue, and what one owes another is not a group debt. Both sides
    are taken out — but only as far as they agree. If A says B owes it five
    million and B says it owes four, the four is eliminated and the missing
    million is reported as a difference for somebody to explain. Forcing it
    away would hide a real error: an invoice one side has posted and the other
    has not.

What this does **not** attempt: part-owned subsidiaries and minority interests,
goodwill on acquisition, or unrealised profit in stock bought from another
member. A group of wholly owned companies — which is nearly every group this
software will meet — is consolidated correctly. Anything else needs an
accountant, and the screen says so rather than producing a figure that looks
official and is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select

from .. import currency as currency_mod
from .. import db as dbmod
from ..group import Group, Member
from ..models import (
    EXPENSE,
    INCOME,
    Account,
    Company,
    Contact,
    JournalEntry,
    JournalLine,
)
from . import reports

#: Balance-sheet groupings, in the order a balance sheet is read.
ASSET_CURRENT = {"BANK", "CASH", "RECEIVABLE", "INVENTORY", "CURRENT_ASSET"}
ASSET_FIXED = {"FIXED_ASSET", "ACCUM_DEP", "OTHER_ASSET"}
LIABILITY_CURRENT = {"PAYABLE", "TAX_PAYABLE", "CURRENT_LIABILITY"}
LIABILITY_LONG = {"LOAN", "OTHER_LIABILITY"}
EQUITY_SUBS = {"CAPITAL", "DRAWINGS", "RESERVE", "RETAINED_EARNINGS"}

REVENUE_SUBS = {"SALES"}
COGS_SUBS = {"COGS"}
EXPENSE_SUBS = {"OPERATING_EXPENSE", "PAYROLL", "DEPRECIATION", "FINANCE_COST",
                "OTHER_EXPENSE"}
OTHER_INCOME_SUBS = {"OTHER_INCOME"}
TAX_SUBS = {"TAX_EXPENSE"}


# --------------------------------------------------------------------------
# Money across currencies
# --------------------------------------------------------------------------


def convert(amount: int, source, target, rate: Decimal) -> int:
    """One member's minor units into the group's, at the rate given.

    Both the number of minor units and the rate change, and getting either
    wrong is not a rounding error but a wrong set of accounts — a thousand
    naira and a thousand yen are not the same number of anything.
    """
    if source.code == target.code and rate == 1:
        return amount
    major = Decimal(amount) / Decimal(source.scale)
    return int((major * rate * Decimal(target.scale)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# What is read out of one member
# --------------------------------------------------------------------------


@dataclass
class Line:
    """One account, as one member reported it."""

    code: str
    name: str
    subtype: str
    kind: str                       # the account type: INCOME, EXPENSE, ...
    amount: int = 0                 # in that member's own currency


@dataclass
class Internal:
    """What one member had with another member of the group."""

    other: str                      # the other member's slug
    receivable: int = 0             # they owe us
    payable: int = 0                # we owe them
    sales: int = 0                  # we sold to them
    purchases: int = 0              # we bought from them


@dataclass
class Reading:
    """Everything taken out of one company, before translation."""

    slug: str
    name: str
    currency: object
    profit_lines: list[Line] = field(default_factory=list)
    balance_lines: list[Line] = field(default_factory=list)
    brought_forward: int = 0
    earnings: int = 0
    cash: int = 0
    internal: list[Internal] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _lines(db, bals, subs: set[str], kind: str | None = None) -> list[Line]:
    out: list[Line] = []
    for acc in db.scalars(select(Account).order_by(Account.code)):
        if acc.subtype not in subs:
            continue
        debit, credit = bals.get(acc.id, (0, 0))
        value = acc.signed(debit, credit)
        if value:
            out.append(Line(acc.code, acc.name, acc.subtype, kind or acc.type, value))
    return out


def _internal(db, member: Member, start: Date, end: Date) -> list[Internal]:
    """Balances and turnover with the other companies in the group.

    Balances are as at the end of the period; turnover is what happened during
    it. They are two different questions and asking them in one query is how
    they end up with the same wrong answer.
    """
    wanted = {int(k): v for k, v in member.internal.items() if str(k).isdigit()}
    if not wanted:
        return []

    found: dict[str, Internal] = {other: Internal(other=other)
                                  for other in set(wanted.values())}

    def totals(first: Date | None, last: Date):
        query = (
            select(JournalLine.contact_id, Account.type, Account.subtype,
                   func.coalesce(func.sum(JournalLine.debit), 0),
                   func.coalesce(func.sum(JournalLine.credit), 0))
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .join(Account, JournalLine.account_id == Account.id)
            .where(JournalEntry.is_posted.is_(True),
                   JournalEntry.date <= last,
                   JournalLine.contact_id.in_(list(wanted)))
            .group_by(JournalLine.contact_id, Account.type, Account.subtype))
        if first is not None:
            query = query.where(JournalEntry.date >= first)
        return db.execute(query).all()

    for contact_id, _kind, subtype, debit, credit in totals(None, end):
        record = found[wanted[contact_id]]
        if subtype == "RECEIVABLE":
            record.receivable += int(debit) - int(credit)
        elif subtype == "PAYABLE":
            record.payable += int(credit) - int(debit)

    for contact_id, kind, subtype, debit, credit in totals(start, end):
        record = found[wanted[contact_id]]
        if subtype in REVENUE_SUBS or (kind == INCOME and subtype in OTHER_INCOME_SUBS):
            record.sales += int(credit) - int(debit)
        elif kind == EXPENSE:
            record.purchases += int(debit) - int(credit)
    return list(found.values())


def read_member(member: Member, start: Date, end: Date) -> Reading:
    """Open one company, take its figures, close it again."""
    with dbmod.session_scope_for(member.slug) as db:
        company = db.get(Company, 1)
        spec = currency_mod.from_company(company)
        out = Reading(slug=member.slug,
                      name=company.name if company else member.name,
                      currency=spec)

        period = reports.balances(db, start, end)
        as_of = reports.balances(db, None, end)

        out.profit_lines = (
            _lines(db, period, REVENUE_SUBS) + _lines(db, period, COGS_SUBS)
            + _lines(db, period, EXPENSE_SUBS) + _lines(db, period, OTHER_INCOME_SUBS)
            + _lines(db, period, TAX_SUBS))
        out.balance_lines = (
            _lines(db, as_of, ASSET_CURRENT) + _lines(db, as_of, ASSET_FIXED)
            + _lines(db, as_of, LIABILITY_CURRENT) + _lines(db, as_of, LIABILITY_LONG)
            + _lines(db, as_of, EQUITY_SUBS))

        fy_start, _ = reports.fiscal_year_bounds(db, end)
        out.brought_forward = reports.net_profit_between(
            db, None, fy_start - timedelta(days=1))
        out.earnings = reports.net_profit_between(db, fy_start, end)

        flow = reports.cash_flow(db, start, end)
        out.cash = flow.closing_cash
        out.internal = _internal(db, member, start, end)
    return out


def suggest_internal(slug: str, others: list[Member]) -> dict[str, str]:
    """Which contacts in this company look like other companies in the group.

    Offered as a suggestion on the settings page, never applied on its own. Two
    companies in a group frequently have a customer record for each other under
    a slightly different name, and a wrong guess here would take real trade out
    of the group's revenue.
    """
    names = {}
    for member in others:
        if member.slug != slug:
            names[_plain(member.name)] = member.slug
    if not names:
        return {}

    found: dict[str, str] = {}
    with dbmod.session_scope_for(slug) as db:
        for contact in db.scalars(select(Contact)):
            match = names.get(_plain(contact.name))
            if match:
                found[str(contact.id)] = match
    return found


def _plain(name: str) -> str:
    """A company name with the noise taken off, for matching only."""
    text = (name or "").lower()
    for word in (" limited", " ltd", " plc", " nigeria", " enterprises",
                 " company", " co.", " inc", " llc", ".", ",", "'"):
        text = text.replace(word, " ")
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Putting them together
# --------------------------------------------------------------------------


@dataclass
class Row:
    """One line of a consolidated statement."""

    code: str
    name: str
    subtype: str
    kind: str
    parts: dict[str, int] = field(default_factory=dict)   # slug -> group money
    eliminated: int = 0

    @property
    def combined(self) -> int:
        return sum(self.parts.values())

    @property
    def total(self) -> int:
        return self.combined - self.eliminated


@dataclass
class Section:
    title: str
    rows: list[Row] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(row.total for row in self.rows)

    @property
    def combined(self) -> int:
        return sum(row.combined for row in self.rows)

    @property
    def eliminated(self) -> int:
        return sum(row.eliminated for row in self.rows)

    def of(self, slug: str) -> int:
        return sum(row.parts.get(slug, 0) for row in self.rows)


@dataclass
class Mismatch:
    """Two members who do not agree about what passed between them."""

    left: str
    right: str
    what: str
    left_says: int
    right_says: int
    #: True when the two keep their books in different currencies, where a
    #: difference is as likely to be the exchange rate as a missing document.
    across_currencies: bool = False

    @property
    def difference(self) -> int:
        return self.left_says - self.right_says


@dataclass
class Consolidated:
    """The group's figures, and everything a reader needs to trust them."""

    start: Date
    end: Date
    currency: object
    members: list[Reading] = field(default_factory=list)
    revenue: Section = field(default_factory=lambda: Section("Revenue"))
    cogs: Section = field(default_factory=lambda: Section("Cost of sales"))
    expenses: Section = field(default_factory=lambda: Section("Operating expenses"))
    other_income: Section = field(default_factory=lambda: Section("Other income"))
    tax: Section = field(default_factory=lambda: Section("Taxation"))
    current_assets: Section = field(default_factory=lambda: Section("Current assets"))
    fixed_assets: Section = field(default_factory=lambda: Section("Non-current assets"))
    current_liabilities: Section = field(
        default_factory=lambda: Section("Current liabilities"))
    long_liabilities: Section = field(
        default_factory=lambda: Section("Non-current liabilities"))
    equity: Section = field(default_factory=lambda: Section("Equity"))
    brought_forward: dict[str, int] = field(default_factory=dict)
    earnings: dict[str, int] = field(default_factory=dict)
    cash: dict[str, int] = field(default_factory=dict)
    translation: dict[str, int] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    # -- the profit and loss ------------------------------------------------
    @property
    def gross_profit(self) -> int:
        return self.revenue.total - self.cogs.total

    @property
    def operating_profit(self) -> int:
        return self.gross_profit - self.expenses.total

    @property
    def profit_before_tax(self) -> int:
        return self.operating_profit + self.other_income.total

    @property
    def net_profit(self) -> int:
        return self.profit_before_tax - self.tax.total

    # -- the balance sheet --------------------------------------------------
    @property
    def total_assets(self) -> int:
        return self.current_assets.total + self.fixed_assets.total

    @property
    def total_liabilities(self) -> int:
        return self.current_liabilities.total + self.long_liabilities.total

    @property
    def total_brought_forward(self) -> int:
        return sum(self.brought_forward.values())

    @property
    def total_earnings(self) -> int:
        return sum(self.earnings.values())

    @property
    def total_translation(self) -> int:
        return sum(self.translation.values())

    @property
    def total_equity(self) -> int:
        return (self.equity.total + self.total_brought_forward
                + self.total_earnings + self.total_translation)

    @property
    def difference(self) -> int:
        return self.total_assets - (self.total_liabilities + self.total_equity)

    @property
    def balances_ok(self) -> bool:
        return self.difference == 0

    @property
    def total_cash(self) -> int:
        return sum(self.cash.values())

    @property
    def eliminated_turnover(self) -> int:
        return self.revenue.eliminated

    @property
    def eliminated_balances(self) -> int:
        return self.current_assets.eliminated


def _add(section: Section, line: Line, slug: str, amount: int,
         seen: dict[str, Row], problems: list[str]) -> None:
    row = seen.get(line.code)
    if row is None:
        row = Row(code=line.code, name=line.name, subtype=line.subtype,
                  kind=line.kind)
        seen[line.code] = row
        section.rows.append(row)
    elif row.name.strip().lower() != line.name.strip().lower():
        note = (f"Account {line.code} is “{row.name}” in one company "
                f"and “{line.name}” in another. They have been added "
                "together on the code.")
        if note not in problems:
            problems.append(note)
    row.parts[slug] = row.parts.get(slug, 0) + amount


def _section_for(subtype: str, out: Consolidated) -> Section | None:
    if subtype in REVENUE_SUBS:
        return out.revenue
    if subtype in COGS_SUBS:
        return out.cogs
    if subtype in EXPENSE_SUBS:
        return out.expenses
    if subtype in OTHER_INCOME_SUBS:
        return out.other_income
    if subtype in TAX_SUBS:
        return out.tax
    if subtype in ASSET_CURRENT:
        return out.current_assets
    if subtype in ASSET_FIXED:
        return out.fixed_assets
    if subtype in LIABILITY_CURRENT:
        return out.current_liabilities
    if subtype in LIABILITY_LONG:
        return out.long_liabilities
    if subtype in EQUITY_SUBS:
        return out.equity
    return None


def _eliminate(out: Consolidated, readings: dict[str, Reading],
               group: Group) -> None:
    """Take out what the group owes and sells to itself.

    Only the agreed part is removed. Where the two sides differ, the smaller
    figure is eliminated and the difference is reported: an invoice posted in
    one company and not the other is a real error, and hiding it inside a
    consolidation is how it survives to the year end.
    """
    done: set[tuple[str, str, str]] = set()

    def rate(slug: str, closing: bool) -> Decimal:
        member = group.member(slug)
        if member is None:
            return Decimal("1")
        return member.closing if closing else member.average

    def to_group(slug: str, amount: int, closing: bool) -> int:
        return convert(amount, readings[slug].currency, out.currency,
                       rate(slug, closing))

    def take(section: Section, subtypes: set[str], slug: str, amount: int) -> int:
        """Remove ``amount`` from this member's share of these accounts."""
        left = amount
        for row in section.rows:
            if left <= 0:
                break
            if row.subtype not in subtypes:
                continue
            available = row.parts.get(slug, 0) - row.eliminated
            if available <= 0:
                continue
            taken = min(left, available)
            row.eliminated += taken
            left -= taken
        return amount - left

    for reading in out.members:
        for link in reading.internal:
            other = readings.get(link.other)
            if other is None:
                continue
            mirror = next((i for i in other.internal if i.other == reading.slug), None)

            pair = tuple(sorted((reading.slug, link.other)))
            mixed = reading.currency.code != other.currency.code

            # What we say they owe us, against what they say they owe us.
            ours = to_group(reading.slug, link.receivable, True)
            theirs = to_group(link.other, mirror.payable, True) if mirror else 0
            if (ours > 0 or theirs > 0) and (pair + ("owed",)) not in done:
                done.add(pair + ("owed",))
                agreed = min(ours, theirs)
                if agreed > 0:
                    take(out.current_assets, {"RECEIVABLE"}, reading.slug, agreed)
                    take(out.current_liabilities, {"PAYABLE"}, link.other, agreed)
                if ours != theirs:
                    out.mismatches.append(Mismatch(
                        left=reading.slug, right=link.other,
                        what="owed between them", left_says=ours,
                        right_says=theirs, across_currencies=mixed))

            # What we sold them, against what they say they bought.
            sold = to_group(reading.slug, link.sales, False)
            bought = to_group(link.other, mirror.purchases, False) if mirror else 0
            if (sold > 0 or bought > 0) and (pair + ("traded",)) not in done:
                done.add(pair + ("traded",))
                agreed = min(sold, bought)
                if agreed > 0:
                    take(out.revenue, REVENUE_SUBS, reading.slug, agreed)
                    # The other side booked it as stock or as an expense; take
                    # it from cost of sales first and the rest from expenses.
                    removed = take(out.cogs, COGS_SUBS, link.other, agreed)
                    if removed < agreed:
                        take(out.expenses, EXPENSE_SUBS, link.other,
                             agreed - removed)
                if sold != bought:
                    out.mismatches.append(Mismatch(
                        left=reading.slug, right=link.other,
                        what="traded between them", left_says=sold,
                        right_says=bought, across_currencies=mixed))


def build(group: Group, start: Date, end: Date) -> Consolidated:
    """The group's figures for a period. Reads only; writes nothing anywhere."""
    chosen = group.chosen
    spec = (currency_mod.preset(group.currency)
            or (currency_mod.from_company(None) if not chosen else None))

    readings: dict[str, Reading] = {}
    for member in chosen:
        readings[member.slug] = read_member(member, start, end)

    if spec is None:
        spec = (readings[chosen[0].slug].currency if chosen
                else currency_mod.DEFAULT)

    out = Consolidated(start=start, end=end, currency=spec)
    out.members = [readings[m.slug] for m in chosen]

    seen_profit: dict[str, Row] = {}
    seen_balance: dict[str, Row] = {}
    for member in chosen:
        reading = readings[member.slug]
        for line in reading.profit_lines:
            section = _section_for(line.subtype, out)
            if section is not None:
                _add(section, line, member.slug,
                     convert(line.amount, reading.currency, spec, member.average),
                     seen_profit, out.problems)
        for line in reading.balance_lines:
            section = _section_for(line.subtype, out)
            if section is not None:
                _add(section, line, member.slug,
                     convert(line.amount, reading.currency, spec, member.closing),
                     seen_balance, out.problems)

        out.brought_forward[member.slug] = convert(
            reading.brought_forward, reading.currency, spec, member.closing)
        out.earnings[member.slug] = convert(
            reading.earnings, reading.currency, spec, member.average)
        out.cash[member.slug] = convert(
            reading.cash, reading.currency, spec, member.closing)

    _eliminate(out, readings, group)

    # Whatever the two rates leave behind is a translation difference. It is
    # named, put in equity, and never buried in another figure.
    for member in chosen:
        reading = readings[member.slug]
        if reading.currency.code == spec.code and member.closing == 1:
            out.translation[member.slug] = 0
            continue
        assets = (out.current_assets.of(member.slug)
                  + out.fixed_assets.of(member.slug))
        liabilities = (out.current_liabilities.of(member.slug)
                       + out.long_liabilities.of(member.slug))
        equity = (out.equity.of(member.slug) + out.brought_forward[member.slug]
                  + out.earnings[member.slug])
        out.translation[member.slug] = assets - liabilities - equity

    out.notes = _notes(out, group, chosen)
    return out


def _notes(out: Consolidated, group: Group, chosen: list[Member]) -> list[str]:
    """What a reader has to be told for the figures to mean anything."""
    from ..money import fmt as _fmt

    def fmt(value: int) -> str:
        """In the group's currency, whatever company's books happen to be open."""
        return _fmt(value, cur=out.currency)

    notes = [
        f"These figures combine {len(chosen)} companies: "
        + ", ".join(reading.name for reading in out.members) + ".",
        "Every company is treated as wholly owned. There is no minority "
        "interest, no goodwill and no adjustment for profit still sitting in "
        "stock bought from another member.",
    ]

    foreign = [r for r in out.members if r.currency.code != out.currency.code]
    if foreign:
        notes.append(
            "Translated into " + out.currency.code + ": "
            + "; ".join(
                f"{r.name} keeps {r.currency.code} at "
                f"{group.member(r.slug).average} for the period and "
                f"{group.member(r.slug).closing} at the date"
                for r in foreign)
            + ". Income and costs come across at the average rate and balances "
              "at the closing rate, so the two leave a difference, shown in "
              "equity as a translation difference.")

    if out.revenue.eliminated:
        notes.append(
            f"{fmt(out.revenue.eliminated)} of sales between group companies "
            "has been taken out of revenue and the same amount out of costs.")
    if out.current_assets.eliminated:
        notes.append(
            f"{fmt(out.current_assets.eliminated)} owed between group companies "
            "has been taken out of both what the group is owed and what it owes.")
    if not any(m.internal for m in chosen):
        notes.append(
            "No customer or supplier has been marked as another company in the "
            "group, so nothing has been eliminated. If these companies trade "
            "with each other, the figures below count that trade twice.")
    if out.mismatches:
        notes.append(
            "Some members do not agree about what passed between them; the "
            "agreed part has been eliminated and the difference is listed.")
    return notes
