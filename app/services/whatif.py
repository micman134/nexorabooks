"""What if — the last twelve months re-run against different assumptions.

This is not a model. It takes what actually happened over a real period, applies
the changes a person types in, and shows the arithmetic. Nothing is fitted,
nothing is extrapolated, and the base case is always the real figures, shown
beside the changed ones so the difference is visible rather than asserted.

The one place judgement enters is the link between price and volume: put your
prices up 10% and you may lose customers. The software does not know how many,
and will not pretend to — **you type that in**, and the screen says plainly that
it is your assumption rather than its finding. What it does instead is show the
break-even: how much volume you could lose before the increase stops being
worth having. That number *is* arithmetic, and it is the useful one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from decimal import Decimal

from sqlalchemy.orm import Session

from . import reports


def _pct(value: str | float | None) -> Decimal:
    """A percentage typed by a person, as a multiplier. '10' becomes 1.10."""
    try:
        return Decimal(1) + Decimal(str(value or 0)) / Decimal(100)
    except Exception:
        return Decimal(1)


@dataclass
class Assumptions:
    """What the person changed. Everything is a percentage except headcount."""

    price_change: str = "0"        # what you charge
    volume_change: str = "0"       # how much you sell
    cost_change: str = "0"         # what your suppliers charge
    overhead_change: str = "0"     # rent, salaries, everything fixed
    extra_fixed_cost: int = 0      # a new hire, a new van, in minor units
    collection_days: str = "0"     # days sooner (-) or later (+) you are paid

    @property
    def anything_changed(self) -> bool:
        return any([
            _pct(self.price_change) != 1, _pct(self.volume_change) != 1,
            _pct(self.cost_change) != 1, _pct(self.overhead_change) != 1,
            self.extra_fixed_cost, str(self.collection_days).strip() not in ("", "0"),
        ])


@dataclass
class Outcome:
    """One version of the year: the real one, or a changed one."""

    label: str
    revenue: int = 0
    cost_of_sales: int = 0
    overheads: int = 0

    @property
    def gross_profit(self) -> int:
        return self.revenue - self.cost_of_sales

    @property
    def net_profit(self) -> int:
        return self.gross_profit - self.overheads

    @property
    def gross_margin(self) -> float:
        return (self.gross_profit * 100.0 / self.revenue) if self.revenue else 0.0

    @property
    def net_margin(self) -> float:
        return (self.net_profit * 100.0 / self.revenue) if self.revenue else 0.0


@dataclass
class Result:
    start: Date
    end: Date
    base: Outcome
    changed: Outcome
    assumptions: Assumptions
    notes: list[str] = field(default_factory=list)
    #: How far volume could fall before a price rise stops paying for itself.
    breakeven_volume_drop: float | None = None
    working_capital: int = 0

    @property
    def profit_change(self) -> int:
        return self.changed.net_profit - self.base.net_profit

    @property
    def better(self) -> bool:
        return self.profit_change > 0

    @property
    def revenue_change(self) -> int:
        return self.changed.revenue - self.base.revenue


def _base(db: Session, start: Date, end: Date) -> Outcome:
    pl = reports.profit_and_loss(db, start, end)
    return Outcome(
        label="As it actually was",
        revenue=pl.revenue.total + pl.other_income.total,
        cost_of_sales=pl.cogs.total,
        overheads=pl.expenses.total + pl.tax.total,
    )


def run(db: Session, start: Date, end: Date, assumptions: Assumptions) -> Result:
    """Re-run the period with the assumptions applied. Pure arithmetic."""
    base = _base(db, start, end)

    price = _pct(assumptions.price_change)
    volume = _pct(assumptions.volume_change)
    cost = _pct(assumptions.cost_change)
    overhead = _pct(assumptions.overhead_change)

    # Revenue moves with both price and volume. Cost of sales moves with volume
    # and with what suppliers charge — but NOT with price, which is the whole
    # reason a price rise is worth more than it first looks.
    changed = Outcome(
        label="With your changes",
        revenue=int(Decimal(base.revenue) * price * volume),
        cost_of_sales=int(Decimal(base.cost_of_sales) * volume * cost),
        overheads=int(Decimal(base.overheads) * overhead) + int(assumptions.extra_fixed_cost),
    )

    result = Result(start=start, end=end, base=base, changed=changed,
                    assumptions=assumptions)

    if price != 1 and base.revenue and base.cost_of_sales:
        result.breakeven_volume_drop = _breakeven(base, price, cost)
        if result.breakeven_volume_drop is not None:
            result.notes.append(
                f"At the new price you could sell "
                f"{result.breakeven_volume_drop:.1f}% less and still make the same "
                "gross profit. Below that the increase costs you money."
            )

    if assumptions.extra_fixed_cost and base.gross_profit and base.revenue:
        margin = base.gross_profit / base.revenue
        if margin > 0:
            needed = int(assumptions.extra_fixed_cost / margin)
            result.notes.append(
                f"A cost of that size needs about {needed / 100:,.0f} more sales "
                "over the same period to pay for itself, at your current margin."
            )

    days = _days(assumptions.collection_days)
    if days:
        daily = base.revenue / max(1, (end - start).days + 1)
        result.working_capital = int(-daily * days)
        direction = "tie up" if days > 0 else "release"
        result.notes.append(
            f"Being paid {abs(days)} days {'later' if days > 0 else 'sooner'} would "
            f"{direction} about {abs(result.working_capital) / 100:,.0f} in working "
            "capital. It does not change profit — only when the money is there."
        )
    return result


def _days(value) -> int:
    try:
        return int(str(value or 0).strip() or 0)
    except ValueError:
        return 0


def _breakeven(base: Outcome, price: Decimal, cost: Decimal) -> float | None:
    """How far volume can fall before the price rise stops being worth it.

    Solve for the volume multiplier v where the new gross profit equals the
    old one:  R·p·v − C·c·v = R − C  →  v = (R − C) / (R·p − C·c)
    """
    denominator = Decimal(base.revenue) * price - Decimal(base.cost_of_sales) * cost
    if denominator <= 0:
        return None
    multiplier = Decimal(base.gross_profit) / denominator
    return float((Decimal(1) - multiplier) * 100)


# --------------------------------------------------------------------------
# The ready-made questions
# --------------------------------------------------------------------------

PRESETS: list[tuple[str, str, Assumptions]] = [
    ("prices_up", "Put prices up 10%",
     Assumptions(price_change="10")),
    ("prices_up_lose", "Put prices up 10% and lose 15% of the volume",
     Assumptions(price_change="10", volume_change="-15")),
    ("costs_up", "Suppliers put their prices up 10%",
     Assumptions(cost_change="10")),
    ("quiet_year", "Sales fall 20%",
     Assumptions(volume_change="-20")),
    ("paid_later", "Everybody pays 30 days later",
     Assumptions(collection_days="30")),
]


def preset(key: str) -> Assumptions | None:
    for name, _label, assumptions in PRESETS:
        if name == key:
            return assumptions
    return None
