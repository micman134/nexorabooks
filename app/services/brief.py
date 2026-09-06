"""Today's brief, and the board pack.

Both answer the same question at different lengths: *of everything in these
books, what actually deserves attention right now?*

The mechanism is deliberately dull. Every check below looks for one specific,
verifiable condition — an invoice past its due date, an account that moved, a
draft nobody posted — and when it finds one it produces a ``Point`` carrying
the money at stake and a link to the evidence. The points are then sorted by
that amount. There is no scoring model, no weighting anybody has to trust, and
nothing that could put a figure on the screen which is not in the ledger.

Ranking by money at stake is the one judgement this module makes, and it is
the right one: a person with ten minutes should spend them on the largest
thing, not the newest.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    DRAFT,
    Account,
    Bill,
    Invoice,
    JournalEntry,
)
from . import reports
from . import variance as V

# What a point is about. Used only to choose an icon and a colour.
URGENT, WATCH, GOOD, INFO = "urgent", "watch", "good", "info"


@dataclass
class Point:
    """One thing worth knowing, with the evidence attached."""

    kind: str
    title: str
    detail: str = ""
    amount: int = 0
    link: str = ""
    link_label: str = "Look at it"
    at_stake: int = 0  # what this is ranked by; defaults to abs(amount)

    def __post_init__(self) -> None:
        if not self.at_stake:
            self.at_stake = abs(self.amount)


@dataclass
class Brief:
    on: Date
    cash: int = 0
    cash_link: str = "/banking"
    profit_month: int = 0
    profit_prior: int = 0
    revenue_month: int = 0
    receivables: int = 0
    payables: int = 0
    headline: str = ""
    needs_you: list[Point] = field(default_factory=list)
    moved: list[V.Node] = field(default_factory=list)
    parts: list[V.Node] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    profit_last_full: int = 0
    profit_before: int = 0
    cur: V.Period | None = None
    prior: V.Period | None = None

    @property
    def quiet(self) -> bool:
        return not self.needs_you


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------


def _overdue_receivables(db: Session, on: Date) -> list[Point]:
    rows, buckets, _total = reports.aging(db, on, receivable=True)
    overdue = sum(buckets[1:])
    if overdue <= 0:
        return []
    late_rows = [r for r in rows if sum(r.buckets[1:]) > 0]
    points = [
        Point(
            URGENT,
            "Money owed to you that is past its due date",
            f"{len(late_rows)} customer{'s are' if len(late_rows) != 1 else ' is'} "
            "late paying you.",
            overdue,
            "/reports/aging?kind=ar",
            "See who owes what",
        )
    ]
    # The worst single debtor gets its own line, because "chase this one" is a
    # far more useful instruction than "chase somebody".
    if late_rows:
        worst = max(late_rows, key=lambda r: sum(r.buckets[1:]))
        late = sum(worst.buckets[1:])
        oldest = max(i for i in range(1, 5) if worst.buckets[i])
        points.append(
            Point(
                URGENT,
                f"{worst.contact.name} is the one to chase",
                f"The oldest of it is {reports.AGE_BUCKETS[oldest].lower()} overdue.",
                late,
                f"/contacts/{worst.contact.id}",
                "Open the customer",
            )
        )
    return points


def _overdue_payables(db: Session, on: Date) -> list[Point]:
    rows, buckets, _ = reports.aging(db, on, receivable=False)
    overdue = sum(buckets[1:])
    if overdue <= 0:
        return []
    return [
        Point(
            WATCH,
            "Bills you are late paying",
            "Late payment is how suppliers stop giving credit.",
            overdue,
            "/reports/aging?kind=ap",
            "See what is owed",
        )
    ]


def _due_this_week(db: Session, on: Date) -> list[Point]:
    horizon = on + timedelta(days=7)
    total = db.scalar(
        select(func.coalesce(func.sum(Bill.total - Bill.amount_paid), 0)).where(
            Bill.status.in_(("POSTED", "PART_PAID")),
            Bill.doc_type == "BILL",
            Bill.due_date >= on,
            Bill.due_date <= horizon,
        )
    )
    total = int(total or 0)
    if total <= 0:
        return []
    return [
        Point(
            WATCH,
            "Bills falling due in the next seven days",
            "Make sure the cash is there for them.",
            total,
            "/purchases/bills",
            "See the bills",
        )
    ]


def _unposted_drafts(db: Session) -> list[Point]:
    out = []
    inv = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.status == DRAFT, Invoice.doc_type == "INVOICE"
        )
    ).one()
    if inv[0]:
        out.append(
            Point(
                URGENT,
                f"{inv[0]} invoice{'s' if inv[0] != 1 else ''} still in draft",
                "A draft invoice is not in your books and nobody has been asked to pay it.",
                int(inv[1]),
                "/sales/invoices?status=DRAFT",
                "Post them",
            )
        )
    bill = db.execute(
        select(func.count(Bill.id), func.coalesce(func.sum(Bill.total), 0)).where(
            Bill.status == DRAFT, Bill.doc_type == "BILL"
        )
    ).one()
    if bill[0]:
        out.append(
            Point(
                WATCH,
                f"{bill[0]} bill{'s' if bill[0] != 1 else ''} still in draft",
                "Your costs are understated until these are posted.",
                int(bill[1]),
                "/purchases/bills?status=DRAFT",
                "Post them",
            )
        )
    return out


def _recurring_waiting(db: Session, on: Date) -> list[Point]:
    from . import recurring as REC

    try:
        due = REC.due(db, on)
    except Exception:  # pragma: no cover - a broken template must not kill the brief
        return []
    if not due:
        return []
    count = sum(d.count for d in due)
    total = sum(d.value for d in due)
    return [
        Point(
            URGENT,
            f"{count} recurring invoice{'s' if count != 1 else ''} waiting to go out",
            "These are already agreed with the customer. Until they are raised, "
            "nobody has been billed.",
            total,
            "/recurring",
            "Raise them",
        )
    ]


def _requisitions_waiting(db: Session, user) -> list[Point]:
    from . import requisitions as REQ

    if user is None:
        return []
    try:
        s = REQ.summary(db, user)
    except Exception:  # pragma: no cover
        return []
    out = []
    if s.waiting_for_me:
        n = s.waiting_for_me
        out.append(
            Point(
                URGENT,
                f"{n} requisition{'s' if n != 1 else ''} waiting for you",
                "Somebody cannot get on with their work until you decide.",
                s.waiting_value,
                "/requisitions",
                "Review them",
            )
        )
    if s.sent_back_to_me:
        n = s.sent_back_to_me
        out.append(
            Point(
                WATCH,
                f"{n} requisition{'s' if n != 1 else ''} came back to you",
                "Rejected with a reason. Nothing happens until you look at it.",
                0,
                "/requisitions",
                "See why",
            )
        )
    if s.all_unretired:
        out.append(
            Point(
                WATCH,
                f"{s.all_unretired} cash advance"
                f"{'s' if s.all_unretired != 1 else ''} not yet accounted for",
                "Money paid out that nobody has produced receipts or change for.",
                s.all_unretired_value,
                "/requisitions",
                "See them",
            )
        )
    return out


def _low_stock(db: Session) -> list[Point]:
    items = reports.low_stock(db)
    if not items:
        return []
    names = ", ".join(i.name for i in items[:3])
    if len(items) > 3:
        names += f" and {len(items) - 3} more"
    return [
        Point(
            WATCH,
            f"{len(items)} item{'s' if len(items) != 1 else ''} at or below the reorder level",
            names,
            sum(i.stock_value for i in items),
            "/inventory?filter=low",
            "See the items",
        )
    ]


def _tax_due(db: Session, on: Date) -> list[Point]:
    """Only ever raised for a month that has actually finished.

    Warning about a return before its period has closed produces a figure that
    is going to change, which teaches people to ignore the warning.
    """
    period_end = on.replace(day=1) - timedelta(days=1)
    try:
        ret = reports.vat_return(db, period_end.replace(day=1), period_end)
    except Exception:  # pragma: no cover
        return []
    if ret.net_payable <= 0 or ret.due_date is None:
        return []
    days = (ret.due_date - on).days
    if days < 0 or days > 21:
        return []
    return [
        Point(
            URGENT if days <= 7 else WATCH,
            f"Tax on {period_end:%B} is due in {days} day{'s' if days != 1 else ''}",
            f"Due {ret.due_date:%d %B}.",
            ret.net_payable,
            "/reports/vat",
            "Check the return",
        )
    ]


def _bank_overdrawn(db: Session, on: Date) -> list[Point]:
    """A bank account in the red is the one thing nobody should have to hunt for."""
    from ..models import BankAccount

    out = []
    bals = reports.balances(db, None, on)
    for bank in db.scalars(select(BankAccount)):
        acc = db.get(Account, bank.account_id)
        if acc is None:
            continue
        d, c = bals.get(acc.id, (0, 0))
        value = acc.signed(d, c)
        if value < 0:
            out.append(
                Point(
                    URGENT,
                    f"{bank.name} is overdrawn",
                    "Either money has gone out that should not have, or something "
                    "has not been recorded.",
                    value,
                    "/banking",
                    "Open banking",
                )
            )
    return out


def _unbalanced_books(db: Session, on: Date) -> list[Point]:
    """A cheap integrity check that costs nothing and would matter enormously."""
    bs = reports.balance_sheet(db, on)
    if bs.difference == 0:
        return []
    return [  # pragma: no cover - should be unreachable, and is checked anyway
        Point(
            URGENT,
            "The balance sheet does not balance",
            "This should never happen. Take a backup and get in touch before "
            "entering anything else.",
            bs.difference,
            "/reports/balance-sheet",
            "See it",
        )
    ]


CHECKS_NO_USER = (
    _overdue_receivables,
    _overdue_payables,
    _due_this_week,
    _tax_due,
    _bank_overdrawn,
    _unbalanced_books,
)


# --------------------------------------------------------------------------
# Putting the brief together
# --------------------------------------------------------------------------


def build(db: Session, on: Date | None = None, user=None) -> Brief:
    on = on or Date.today()
    b = Brief(on=on)

    bank_ids = reports._bank_account_ids(db)
    b.cash = reports._cash_balance(db, bank_ids, on)

    month_start = on.replace(day=1)
    prior_end = month_start - timedelta(days=1)
    prior_start = prior_end.replace(day=1)
    b.cur = V.Period("This month", month_start, on)
    b.prior = V.Period("Last month", prior_start, prior_end)

    pl = reports.profit_and_loss(db, month_start, on)
    b.profit_month = pl.net_profit
    b.revenue_month = pl.revenue.total
    b.profit_prior = reports.profit_and_loss(db, prior_start, prior_end).net_profit

    _, _, b.receivables = reports.aging(db, on, receivable=True)
    _, _, b.payables = reports.aging(db, on, receivable=False)

    points: list[Point] = []
    for check in CHECKS_NO_USER:
        try:
            points.extend(check(db, on))
        except Exception:  # pragma: no cover - one broken check must not blank the page
            continue
    for check in (_unposted_drafts, _low_stock):
        try:
            points.extend(check(db))
        except Exception:  # pragma: no cover
            continue
    try:
        points.extend(_recurring_waiting(db, on))
    except Exception:  # pragma: no cover
        pass
    points.extend(_requisitions_waiting(db, user))

    order = {URGENT: 0, WATCH: 1, INFO: 2, GOOD: 3}
    points.sort(key=lambda p: (order.get(p.kind, 9), -p.at_stake))
    b.needs_you = points

    # Last complete month against the one before it, which is the only
    # comparison that is fair this early in a month.
    last_full = V.Period("Last month", prior_start, prior_end)
    before = V.Period(
        "The month before",
        (prior_start - timedelta(days=1)).replace(day=1),
        prior_start - timedelta(days=1),
    )
    level = V.explore(db, last_full, before)
    b.moved = V.top_movers(db, last_full, before, limit=5)
    b.story = V.narrate(level, _fmt)
    b.parts = level.children
    b.profit_last_full = level.node.current
    b.profit_before = level.node.prior
    b.headline = _headline(b)
    return b


def _fmt(value: int) -> str:
    from ..money import fmt

    return fmt(value)


def _headline(b: Brief) -> str:
    """A count the reader can check against the list underneath it.

    An earlier version counted only the urgent points, which read as a lie
    whenever the list below it was longer. It now accounts for every row.
    """
    total = len(b.needs_you)
    urgent = sum(1 for p in b.needs_you if p.kind == URGENT)
    if not total:
        return "Nothing needs a decision today."
    rest = total - urgent
    if urgent and rest:
        return (
            f"{urgent} thing{'s' if urgent != 1 else ''} need"
            f"{'' if urgent != 1 else 's'} a decision, and {rest} more worth a look."
        )
    if urgent:
        return (
            f"{urgent} thing{'s' if urgent != 1 else ''} need"
            f"{'' if urgent != 1 else 's'} a decision today."
        )
    return f"{total} thing{'s' if total != 1 else ''} worth a look."


# --------------------------------------------------------------------------
# The board pack
# --------------------------------------------------------------------------


@dataclass
class Question:
    """Something a board member will ask, with the answer already worked out."""

    question: str
    answer: str
    link: str = ""


@dataclass
class BoardPack:
    cur: V.Period
    prior: V.Period
    pl: object = None
    pl_prior: object = None
    bs: object = None
    cf: object = None
    kpis: list[tuple[str, str, str]] = field(default_factory=list)
    movers: list[V.Node] = field(default_factory=list)
    story: list[str] = field(default_factory=list)
    risks: list[Point] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    budget: object = None
    budget_name: str = ""


def _kpis(pl, pl_prior, bs, cash, receivables, payables, fmt) -> list[tuple[str, str, str]]:
    """Name, value, and how it compares. All plain arithmetic on real totals."""

    def margin(p):
        return (p.gross_profit * 100.0 / p.revenue.total) if p.revenue.total else 0.0

    def net_margin(p):
        return (p.net_profit * 100.0 / p.revenue.total) if p.revenue.total else 0.0

    def moved(now, was, unit=""):
        if was == 0:
            return "no figure to compare with"
        diff = now - was
        way = "up" if diff > 0 else "down" if diff < 0 else "level"
        if way == "level":
            return "unchanged"
        return f"{way} {abs(diff):.1f}{unit or ' points'}"

    rows = [
        ("Revenue", fmt(pl.revenue.total),
         moved_money(pl.revenue.total, pl_prior.revenue.total)),
        ("Gross margin", f"{margin(pl):.1f}%", moved(margin(pl), margin(pl_prior), "%")),
        ("Operating expenses", fmt(pl.expenses.total),
         moved_money(pl.expenses.total, pl_prior.expenses.total)),
        ("Net profit", fmt(pl.net_profit),
         moved_money(pl.net_profit, pl_prior.net_profit)),
        ("Net margin", f"{net_margin(pl):.1f}%",
         moved(net_margin(pl), net_margin(pl_prior), "%")),
        ("Cash at bank", fmt(cash), ""),
        ("Owed to us", fmt(receivables), ""),
        ("We owe", fmt(payables), ""),
    ]
    if bs is not None:
        rows.append(("Total assets", fmt(bs.total_assets), ""))
    return rows


def moved_money(now: int, was: int) -> str:
    if was == 0:
        return "no figure to compare with"
    pct = (now - was) * 100.0 / abs(was)
    way = "up" if pct > 0 else "down" if pct < 0 else "level"
    if way == "level":
        return "unchanged"
    return f"{way} {abs(pct):.1f}%"


def _questions(db: Session, pack: BoardPack, fmt) -> list[Question]:
    """The questions that actually get asked, answered from the ledger.

    Each one is only included when the books contain the answer, so the pack
    never carries a heading with nothing under it.
    """
    out: list[Question] = []
    pl, prior = pack.pl, pack.pl_prior

    if pack.movers:
        worst = min(pack.movers, key=lambda m: m.profit_effect)
        if worst.profit_effect < 0:
            out.append(
                Question(
                    "What hurt us most this period?",
                    f"{worst.label}: {fmt(abs(worst.profit_effect))} worse than last "
                    f"period ({fmt(worst.prior)} then, {fmt(worst.current)} now).",
                    f"/insights/why?p={worst.section}&p={worst.key}",
                )
            )
        best = max(pack.movers, key=lambda m: m.profit_effect)
        if best.profit_effect > 0:
            out.append(
                Question(
                    "And what helped?",
                    f"{best.label}: {fmt(best.profit_effect)} better than last period.",
                    f"/insights/why?p={best.section}&p={best.key}",
                )
            )

    if pl.revenue.total:
        gm_now = pl.gross_profit * 100.0 / pl.revenue.total
        gm_was = (
            prior.gross_profit * 100.0 / prior.revenue.total if prior.revenue.total else None
        )
        if gm_was is None:
            answer = f"{gm_now:.1f}%. There is no prior period to compare it with."
        else:
            diff = gm_now - gm_was
            way = "held" if abs(diff) < 0.05 else ("improved" if diff > 0 else "slipped")
            answer = f"{gm_now:.1f}%, against {gm_was:.1f}% last period — it {way}."
        out.append(Question("Is the margin holding?", answer, "/reports/profit-and-loss"))

    rows, buckets, total = reports.aging(db, pack.cur.end, receivable=True)
    if total:
        overdue = sum(buckets[1:])
        share = overdue * 100.0 / total if total else 0
        out.append(
            Question(
                "How much of what we are owed is late?",
                f"{fmt(overdue)} of {fmt(total)} — {share:.0f}%. "
                + (
                    f"The largest is {rows[0].contact.name} at {fmt(rows[0].total)}."
                    if rows
                    else ""
                ),
                "/reports/aging?kind=ar",
            )
        )

    if pack.cf is not None:
        cf = pack.cf
        if cf.net_movement or cf.operating_total:
            direction = "improved" if cf.net_movement > 0 else "went backwards"
            answer = (
                f"Trading brought in {fmt(cf.operating_total)}. "
                f"Cash {direction} by {fmt(abs(cf.net_movement))} over the period, "
                f"from {fmt(cf.opening_cash)} to {fmt(cf.closing_cash)}."
            )
            rest = cf.investing_total + cf.financing_total
            if rest:
                answer += (
                    f" The other {fmt(abs(rest))} is investing and financing rather "
                    "than trading."
                )
            else:
                answer += " All of the movement came from trading."
            out.append(Question("Did the profit turn into cash?", answer,
                                "/reports/cash-flow"))

    if pack.budget is not None:
        # "Worst" means furthest the wrong way, which is not simply the most
        # negative number: an expense over budget is adverse while revenue over
        # budget is good news. VarianceRow already knows the difference.
        adverse = [
            row
            for sec in pack.budget.sections
            for row in sec.rows
            if row.is_material and not row.is_favourable
        ]
        if adverse:
            worst = max(adverse, key=lambda r: abs(r.variance))
            out.append(
                Question(
                    "Where are we furthest from the plan?",
                    f"{worst.account.name}: {fmt(abs(worst.variance))} adverse against "
                    f"a budget of {fmt(worst.budget)}.",
                    "/budgets/variance",
                )
            )
    return out


def board_pack(db: Session, cur: V.Period, prior: V.Period, fmt=None) -> BoardPack:
    """Build the pack.

    ``fmt`` is how money becomes text. It is a parameter because the screen and
    the PDF cannot use the same one: the built-in PDF fonts have no glyph for
    every currency symbol, so the PDF writes "NGN 1,000.00" where the screen
    writes the symbol. Passing the formatter in keeps both renderers reading
    from one set of figures instead of each computing its own.
    """
    fmt = fmt or _fmt
    pack = BoardPack(cur=cur, prior=prior)
    pack.pl = reports.profit_and_loss(
        db, cur.start, cur.end, prior.start, prior.end, prior.label
    )
    pack.pl_prior = reports.profit_and_loss(db, prior.start, prior.end)
    pack.bs = reports.balance_sheet(db, cur.end)
    pack.cf = reports.cash_flow(db, cur.start, cur.end)

    bank_ids = reports._bank_account_ids(db)
    cash = reports._cash_balance(db, bank_ids, cur.end)
    _, _, receivables = reports.aging(db, cur.end, receivable=True)
    _, _, payables = reports.aging(db, cur.end, receivable=False)

    pack.kpis = _kpis(pack.pl, pack.pl_prior, pack.bs, cash, receivables, payables, fmt)
    pack.movers = V.top_movers(db, cur, prior, limit=8)
    pack.story = V.narrate(V.explore(db, cur, prior), fmt)

    # Risks are the brief's own checks, taken at the end of the period so the
    # pack says what was true on the reporting date rather than today.
    risks: list[Point] = []
    for check in CHECKS_NO_USER:
        try:
            risks.extend(check(db, cur.end))
        except Exception:  # pragma: no cover
            continue
    for check in (_unposted_drafts, _low_stock):
        try:
            risks.extend(check(db))
        except Exception:  # pragma: no cover
            continue
    risks.sort(key=lambda p: -p.at_stake)
    pack.risks = risks[:6]

    pack.budget = _budget_for(db, cur)
    if pack.budget is not None:
        pack.budget_name = pack.budget.budget.name

    pack.questions = _questions(db, pack, fmt)
    return pack


def _budget_for(db: Session, cur: V.Period):
    """The budget covering this period, if the customer keeps one."""
    from ..models import Budget
    from . import budgets as B

    try:
        budget = db.scalar(
            select(Budget)
            .where(Budget.start_date <= cur.end, Budget.end_date >= cur.start)
            .order_by(Budget.id.desc())
        )
        if budget is None:
            return None
        return B.variance(db, budget, cur.start, cur.end)
    except Exception:  # pragma: no cover - a budget is optional, never load-bearing
        return None
