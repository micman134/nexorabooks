"""Budgets, and comparing them with what actually happened.

A budget is a figure per account per month. Nothing here writes to the ledger;
the whole point of the module is to read the ledger and say how far off the
plan the business is.

Signs follow the account's natural direction, so a revenue budget of ₦10m and
an expense budget of ₦4m are both positive numbers. That means "over budget"
is good news on revenue and bad news on costs, which is why the variance is
reported with a favourable/adverse flag rather than left for the reader to
work out from a minus sign.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EXPENSE, INCOME, Account, Budget, BudgetLine
from .posting import PostingError
from .reports import balances


class BudgetError(PostingError):
    """Safe to show the user."""


BUDGETABLE = (INCOME, EXPENSE)


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def period_of(on: date) -> int:
    return on.year * 100 + on.month


def period_start(period: int) -> date:
    y, m = divmod(period, 100)
    return date(y, m, 1)


def period_end(period: int) -> date:
    y, m = divmod(period, 100)
    return date(y, m, monthrange(y, m)[1])


def periods_in(start: date, end: date) -> list[int]:
    """Every month a budget covers, from its start to its end."""
    out: list[int] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(y * 100 + m)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def month_label(period: int) -> str:
    from ..models import MONTH_NAMES

    y, m = divmod(period, 100)
    return f"{MONTH_NAMES[m - 1][:3]} {str(y)[-2:]}"


# --------------------------------------------------------------------------
# Building a budget
# --------------------------------------------------------------------------


def budgetable_accounts(db: Session) -> list[Account]:
    return list(
        db.scalars(
            select(Account)
            .where(Account.type.in_(BUDGETABLE), Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


def grid(db: Session, budget: Budget) -> dict[tuple[int, int], int]:
    """``{(account_id, period): amount}`` — how the edit screen sees a budget.

    Read from the table rather than ``budget.lines``: rows written earlier in
    the same transaction are not on the loaded relationship yet, and a stale
    grid here would silently drop half a budget.
    """
    rows = db.scalars(select(BudgetLine).where(BudgetLine.budget_id == budget.id))
    return {(l.account_id, l.period): l.amount for l in rows}


def set_line(db: Session, budget: Budget, account_id: int, period: int, amount: int) -> None:
    """Write one cell. A zero removes the row rather than storing a zero."""
    existing = db.scalar(
        select(BudgetLine).where(
            BudgetLine.budget_id == budget.id,
            BudgetLine.account_id == account_id,
            BudgetLine.period == period,
        )
    )
    if amount == 0:
        if existing is not None:
            db.delete(existing)
        return
    if existing is None:
        db.add(BudgetLine(budget_id=budget.id, account_id=account_id,
                          period=period, amount=amount))
    else:
        existing.amount = amount


def spread(annual: int, periods: list[int]) -> dict[int, int]:
    """Split an annual figure evenly across months, losing nothing to rounding.

    The remainder goes on the first months rather than being dropped, so the
    twelve monthly figures always add back to the annual one.
    """
    if not periods:
        return {}
    n = len(periods)
    base, remainder = divmod(abs(annual), n)
    sign = -1 if annual < 0 else 1
    out = {}
    for i, period in enumerate(periods):
        out[period] = sign * (base + (1 if i < remainder else 0))
    return out


def fill_from_actuals(
    db: Session,
    budget: Budget,
    source_start: date,
    source_end: date,
    uplift_pct: str = "0",
    only_empty: bool = True,
) -> int:
    """Start a budget from what actually happened, with an optional uplift.

    This is how most small businesses build a budget: last year plus ten per
    cent. Doing it by hand across twelve months and forty accounts is where
    mistakes creep in.
    """
    periods = periods_in(budget.start_date, budget.end_date)
    source = periods_in(source_start, source_end)
    if not periods or not source:
        return 0

    factor = Decimal(1) + (Decimal(str(uplift_pct or 0)) / 100)
    existing = grid(db, budget)
    written = 0

    for i, period in enumerate(periods):
        # Line each budget month up with the matching source month, wrapping
        # if the source period is shorter.
        src = source[i % len(source)]
        actual = actuals_for(db, period_start(src), period_end(src))
        for account_id, amount in actual.items():
            if only_empty and (account_id, period) in existing:
                continue
            planned = int((Decimal(amount) * factor).to_integral_value())
            set_line(db, budget, account_id, period, planned)
            written += 1
    db.flush()
    return written


def actuals_for(db: Session, start: date, end: date) -> dict[int, int]:
    """``{account_id: amount}`` in the account's natural direction."""
    bals = balances(db, start, end)
    out: dict[int, int] = {}
    for account in db.scalars(select(Account).where(Account.type.in_(BUDGETABLE))):
        debit, credit = bals.get(account.id, (0, 0))
        value = account.signed(debit, credit)
        if value:
            out[account.id] = value
    return out


# --------------------------------------------------------------------------
# Variance
# --------------------------------------------------------------------------


@dataclass
class VarianceRow:
    account: Account
    budget: int = 0
    actual: int = 0

    @property
    def variance(self) -> int:
        """Actual less budget, in the account's natural direction."""
        return self.actual - self.budget

    @property
    def is_favourable(self) -> bool:
        """More revenue is good; more cost is not."""
        if self.variance == 0:
            return True
        return self.variance > 0 if self.account.type == INCOME else self.variance < 0

    @property
    def is_material(self) -> bool:
        """Worth flagging at all.

        A budget spread over twelve months leaves kobo-sized rounding on some
        accounts. Calling a two-kobo difference "adverse" trains people to
        ignore the column, so anything under a naira is left unmarked.
        """
        return abs(self.variance) >= 100

    @property
    def variance_pct(self) -> str:
        if not self.budget:
            return "—" if not self.actual else "n/a"
        pct = Decimal(self.variance) * 100 / Decimal(abs(self.budget))
        return f"{pct.quantize(Decimal('0.1'))}%"


@dataclass
class VarianceSection:
    title: str
    rows: list[VarianceRow] = field(default_factory=list)

    @property
    def budget(self) -> int:
        return sum(r.budget for r in self.rows)

    @property
    def actual(self) -> int:
        return sum(r.actual for r in self.rows)

    @property
    def variance(self) -> int:
        return self.actual - self.budget


@dataclass
class VarianceReport:
    budget: Budget
    start: date
    end: date
    sections: list[VarianceSection]
    revenue: VarianceSection
    cost_of_sales: VarianceSection
    expenses: VarianceSection

    @property
    def budget_profit(self) -> int:
        return self.revenue.budget - self.cost_of_sales.budget - self.expenses.budget

    @property
    def actual_profit(self) -> int:
        return self.revenue.actual - self.cost_of_sales.actual - self.expenses.actual

    @property
    def profit_variance(self) -> int:
        return self.actual_profit - self.budget_profit


def variance(db: Session, budget: Budget, start: date, end: date) -> VarianceReport:
    """Budget against actual for a window inside the budget's year."""
    periods = set(periods_in(start, end))
    planned: dict[int, int] = {}
    for line in db.scalars(select(BudgetLine).where(BudgetLine.budget_id == budget.id)):
        if line.period in periods:
            planned[line.account_id] = planned.get(line.account_id, 0) + line.amount

    actual = actuals_for(db, start, end)

    accounts = {a.id: a for a in db.scalars(select(Account).where(Account.type.in_(BUDGETABLE)))}
    rows: dict[int, VarianceRow] = {}
    for account_id in set(planned) | set(actual):
        account = accounts.get(account_id)
        if account is None:
            continue
        rows[account_id] = VarianceRow(
            account=account,
            budget=planned.get(account_id, 0),
            actual=actual.get(account_id, 0),
        )

    def section(title, subtypes):
        picked = [r for r in rows.values() if r.account.subtype in subtypes]
        picked.sort(key=lambda r: r.account.code)
        return VarianceSection(title=title, rows=picked)

    revenue = section("Revenue", ("SALES", "OTHER_INCOME"))
    cogs = section("Cost of sales", ("COGS",))
    expenses = section(
        "Operating expenses",
        ("OPERATING_EXPENSE", "PAYROLL", "DEPRECIATION", "FINANCE_COST",
         "OTHER_EXPENSE", "TAX_EXPENSE"),
    )
    return VarianceReport(
        budget=budget, start=start, end=end,
        sections=[revenue, cogs, expenses],
        revenue=revenue, cost_of_sales=cogs, expenses=expenses,
    )


def monthly_totals(db: Session, budget: Budget) -> list[tuple[int, int, int]]:
    """``(period, budgeted profit, actual profit)`` for each month — the chart."""
    out = []
    by_period: dict[int, int] = {}
    accounts = {a.id: a for a in db.scalars(select(Account).where(Account.type.in_(BUDGETABLE)))}
    for line in db.scalars(select(BudgetLine).where(BudgetLine.budget_id == budget.id)):
        account = accounts.get(line.account_id)
        if account is None:
            continue
        sign = 1 if account.type == INCOME else -1
        by_period[line.period] = by_period.get(line.period, 0) + sign * line.amount

    for period in periods_in(budget.start_date, budget.end_date):
        actual = 0
        for account_id, amount in actuals_for(
            db, period_start(period), period_end(period)
        ).items():
            account = accounts.get(account_id)
            if account is None:
                continue
            actual += amount if account.type == INCOME else -amount
        out.append((period, by_period.get(period, 0), actual))
    return out


def create(db: Session, name: str, start: date, end: date, user=None) -> Budget:
    if end < start:
        raise BudgetError("A budget cannot end before it starts.")
    if db.scalar(select(Budget).where(Budget.name == name, Budget.start_date == start)):
        raise BudgetError(f"A budget called '{name}' already starts on {start:%d %b %Y}.")
    budget = Budget(name=name, start_date=start, end_date=end,
                    created_by_id=user.id if user else None)
    db.add(budget)
    db.flush()
    return budget
