"""What the bank balance is going to do, and when it runs out.

Everything else in this software reports what happened. This module is the one
place that says what is *going to* happen, and that difference has to be
visible on the screen rather than buried here — so the vocabulary is fixed:

  **Committed** — a fact. An invoice you have raised. A bill you have entered.
  A pay run on its known cycle. The amount is certain; only the timing is not.

  **Expected** — a judgement, and only ever one kind: *when* a committed amount
  will actually move. It comes from that customer's own record of paying you,
  measured from your ledger. Nothing is invented, no revenue is assumed, and no
  cost appears that you have not already agreed to.

That is the whole model. It cannot know about a contract you have not entered
or a customer about to go under, and the screen says so.

The one number people want is the day the balance first goes below zero. It is
given plainly, with the three things that would push it furthest out, because
"you run out on 14 March" is only useful next to "and here is what to do".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    PAYMENT,
    RECEIPT,
    Bill,
    Contact,
    Invoice,
    RECUR_ACTIVE,
    Payment,
    PaymentAllocation,
    RecurringTemplate,
)
from . import reports

#: How far ahead the timeline runs by default.
HORIZON_DAYS = 90

#: A customer needs at least this many settled invoices before their own
#: payment record is used. Below it, one unusual invoice would set the pattern.
ENOUGH_HISTORY = 3

#: What is assumed when a customer has no record: they pay on the due date.
#: Deliberately not a pessimistic guess — inventing lateness nobody has shown
#: would make the forecast look prudent while being made up.
NO_HISTORY_LAG = 0

#: How far out an already-overdue bill is placed. Paying every overdue
#: supplier on the same morning is not what any business does, and modelling
#: it that way makes the balance dive on day one of every forecast.
OVERDUE_GRACE_DAYS = 7

#: Lateness beyond this is not projected. A customer 300 days late is not
#: paying in 300 days' time; they are a collection problem, and pretending to
#: know the date would put a large phantom receipt on the chart.
MAX_LAG_DAYS = 120


# --------------------------------------------------------------------------
# How each customer actually pays
# --------------------------------------------------------------------------


@dataclass
class Habit:
    """One customer's record of paying, measured rather than assumed."""

    contact_id: int
    name: str = ""
    settled: int = 0
    median_lag: int = 0          # days after the due date, negative = early
    worst_lag: int = 0
    on_time: int = 0             # how many of the settled ones were not late

    @property
    def has_record(self) -> bool:
        return self.settled >= ENOUGH_HISTORY

    @property
    def lag(self) -> int:
        """The lag actually used in the projection."""
        if not self.has_record:
            return NO_HISTORY_LAG
        return max(0, min(self.median_lag, MAX_LAG_DAYS))

    @property
    def reliability(self) -> float:
        """Share of settled invoices paid by the due date, 0 to 1."""
        return (self.on_time / self.settled) if self.settled else 0.0

    @property
    def verdict(self) -> str:
        if not self.has_record:
            return "No record yet — assumed to pay on the due date."
        if self.median_lag <= 0:
            return "Pays on time."
        if self.median_lag <= 7:
            return f"Usually about {self.median_lag} days late."
        if self.median_lag <= 30:
            return f"Typically {self.median_lag} days late."
        return f"Very slow — around {self.median_lag} days late."


def habits(db: Session, on: Date | None = None) -> dict[int, Habit]:
    """Every customer's payment record, worked out from settled invoices.

    Measured against the *due* date rather than the invoice date, because a
    customer on sixty-day terms who pays on day sixty is not late, and treating
    them as sixty days late would make every forecast wrong in the same
    direction.
    """
    on = on or Date.today()
    rows = db.execute(
        select(Payment.date, Invoice.due_date, Invoice.date, Invoice.contact_id)
        .join(PaymentAllocation, PaymentAllocation.payment_id == Payment.id)
        .join(Invoice, PaymentAllocation.invoice_id == Invoice.id)
        .where(Payment.kind == RECEIPT, Payment.date <= on)
    ).all()

    lags: dict[int, list[int]] = {}
    for paid_on, due, raised, contact_id in rows:
        if contact_id is None or paid_on is None:
            continue
        reference = due or raised
        if reference is None:
            continue
        lags.setdefault(contact_id, []).append((paid_on - reference).days)

    names = {c.id: c.name for c in db.scalars(select(Contact))}
    out: dict[int, Habit] = {}
    for contact_id, values in lags.items():
        out[contact_id] = Habit(
            contact_id=contact_id,
            name=names.get(contact_id, ""),
            settled=len(values),
            median_lag=int(round(median(values))),
            worst_lag=max(values),
            on_time=sum(1 for v in values if v <= 0),
        )
    return out


# --------------------------------------------------------------------------
# What is going to move, and when
# --------------------------------------------------------------------------

#: What kind of thing an entry on the timeline is.
IN_INVOICE, OUT_BILL, OUT_PAYROLL, OUT_RECURRING, OUT_TAX = (
    "INVOICE", "BILL", "PAYROLL", "RECURRING", "TAX"
)


@dataclass
class Event:
    """One expected movement of money."""

    when: Date
    amount: int                  # positive in, negative out
    kind: str
    label: str
    detail: str = ""
    certain: bool = True         # is the *timing* certain, not the amount
    overdue_days: int = 0
    contact_id: int | None = None
    link: str = ""

    @property
    def is_in(self) -> bool:
        return self.amount > 0


@dataclass
class Day:
    when: Date
    money_in: int = 0
    money_out: int = 0
    closing: int = 0
    events: list[Event] = field(default_factory=list)

    @property
    def net(self) -> int:
        return self.money_in - self.money_out


@dataclass
class Timeline:
    start: Date
    end: Date
    opening: int = 0
    days: list[Day] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    #: The first day the balance is below nothing, if there is one.
    runs_out_on: Date | None = None
    lowest: int = 0
    lowest_on: Date | None = None
    #: Money owed to you that is too far gone to put on the chart.
    excluded_overdue: int = 0
    excluded_count: int = 0
    #: What was already past its due date on the day the timeline starts. Held
    #: separately because it is not a forecast — it is a fact about today, and
    #: mixing the two makes the headline meaningless.
    overdue_in: int = 0
    overdue_out: int = 0

    @property
    def already_short(self) -> bool:
        """Owing more, right now, than there is in the bank to pay it."""
        return self.overdue_out > self.opening + self.overdue_in

    @property
    def closing(self) -> int:
        return self.days[-1].closing if self.days else self.opening

    @property
    def total_in(self) -> int:
        return sum(d.money_in for d in self.days)

    @property
    def total_out(self) -> int:
        return sum(d.money_out for d in self.days)

    @property
    def days_left(self) -> int | None:
        if self.runs_out_on is None:
            return None
        return (self.runs_out_on - self.start).days

    @property
    def survives(self) -> bool:
        return self.runs_out_on is None


def _expected_receipts(db: Session, start: Date, end: Date,
                       known: dict[int, Habit]) -> tuple[list[Event], int, int]:
    """Unpaid invoices, dated by when that customer actually pays."""
    events: list[Event] = []
    excluded_value = excluded_count = 0

    docs = db.scalars(
        select(Invoice).where(
            Invoice.status.in_(("POSTED", "PART_PAID")),
            Invoice.doc_type == "INVOICE",
        ).order_by(Invoice.due_date)
    )
    for doc in docs:
        outstanding = doc.balance_due
        if outstanding <= 0:
            continue
        due = doc.due_date or doc.date
        habit = known.get(doc.contact_id)
        lag = habit.lag if habit else NO_HISTORY_LAG
        when = due + timedelta(days=lag)
        overdue = max(0, (start - due).days)

        if when < start:
            # Already past the date this customer usually pays on. It is not
            # arriving "yesterday", so put it a few days out rather than
            # dropping it — unless it is so old that any date would be fiction.
            if overdue > MAX_LAG_DAYS:
                excluded_value += outstanding
                excluded_count += 1
                continue
            when = start + timedelta(days=3)
        if when > end:
            continue

        events.append(Event(
            when=when,
            amount=outstanding,
            kind=IN_INVOICE,
            label=doc.contact.name if doc.contact else "A customer",
            detail=(f"{doc.number}, due {due:%d %b}"
                    + (f" — {overdue} days overdue" if overdue else "")),
            certain=bool(habit and habit.has_record),
            overdue_days=overdue,
            contact_id=doc.contact_id,
            link=f"/sales/invoices/{doc.id}",
        ))
    return events, excluded_value, excluded_count


def _expected_payments(db: Session, start: Date, end: Date) -> list[Event]:
    """Bills you have entered, on their due dates. You choose when to pay."""
    events = []
    for doc in db.scalars(
        select(Bill).where(
            Bill.status.in_(("POSTED", "PART_PAID")), Bill.doc_type == "BILL"
        ).order_by(Bill.due_date)
    ):
        outstanding = doc.balance_due
        if outstanding <= 0:
            continue
        due = doc.due_date or doc.date
        # A bill already past its due date is not paid on the morning the
        # timeline starts — nobody clears every overdue supplier at once.
        # Placing them a week out reflects what actually happens, and the
        # screen shows the overdue total separately so nothing is hidden.
        when = start + timedelta(days=OVERDUE_GRACE_DAYS) if due < start else due
        if when > end:
            continue
        events.append(Event(
            when=when,
            amount=-outstanding,
            kind=OUT_BILL,
            label=doc.contact.name if doc.contact else "A supplier",
            detail=(f"{doc.number}, due {due:%d %b}"
                    + (" — already overdue" if due < start else "")),
            overdue_days=max(0, (start - due).days),
            contact_id=doc.contact_id,
            link=f"/purchases/bills/{doc.id}",
        ))
    return events


def _expected_payroll(db: Session, start: Date, end: Date) -> list[Event]:
    """The next pay runs, at the cost the last one actually came to.

    The *amount* is the last run's net pay plus its deductions, which is a real
    figure out of the ledger rather than an estimate. What is assumed is only
    that the run happens again on its cycle, which is the safest assumption
    there is about payroll.
    """
    from ..models import PayrollRun

    last = db.scalar(
        select(PayrollRun)
        .where(PayrollRun.status.in_(("POSTED", "PAID")))
        .order_by(PayrollRun.period_end.desc())
    )
    if last is None:
        return []
    # What the run actually costs the bank: everybody's net pay plus the
    # deductions that have to be remitted. ``employer_cost_total`` already
    # carries both where it has been worked out.
    cost = last.employer_cost_total or (last.gross_total + last.pension_employer_total)
    if cost <= 0:
        return []

    step = {"MONTHLY": 30, "FORTNIGHTLY": 14, "WEEKLY": 7}.get(
        getattr(last, "frequency", "MONTHLY") or "MONTHLY", 30
    )
    events = []
    when = (last.pay_date or last.period_end) + timedelta(days=step)
    while when <= end:
        if when >= start:
            events.append(Event(
                when=when,
                amount=-cost,
                kind=OUT_PAYROLL,
                label="Payroll",
                detail=f"On the same cycle as {last.period_end:%B}, at the same cost",
                link="/payroll",
            ))
        when += timedelta(days=step)
    return events


def _expected_recurring(db: Session, start: Date, end: Date) -> list[Event]:
    """Costs and income the customer has already set up to repeat."""
    from . import recurring as REC

    events = []
    for template in db.scalars(
        select(RecurringTemplate).where(RecurringTemplate.status == RECUR_ACTIVE)
    ):
        try:
            dates = REC.occurrences_between(template, start, end)
        except Exception:  # pragma: no cover - a broken template is not fatal
            continue
        amount = getattr(template, "estimated_total", 0) or 0
        if amount <= 0:
            continue
        outgoing = (template.doc_type or "INVOICE").upper() in ("BILL", "DEBIT_NOTE")
        for when in dates:
            if not (start <= when <= end):
                continue
            events.append(Event(
                when=when,
                amount=-amount if outgoing else amount,
                kind=OUT_RECURRING,
                label=template.name,
                detail="A repeating " + ("cost" if outgoing else "invoice")
                       + " you set up",
                link="/recurring",
            ))
    return events


def _expected_tax(db: Session, start: Date, end: Date) -> list[Event]:
    """Tax already collected and owed, on its filing date."""
    from .. import config

    period_end = start.replace(day=1) - timedelta(days=1)
    try:
        ret = reports.vat_return(db, period_end.replace(day=1), period_end)
    except Exception:  # pragma: no cover
        return []
    if ret.net_payable <= 0 or ret.due_date is None:
        return []
    if not (start <= ret.due_date <= end):
        return []
    return [Event(
        when=ret.due_date,
        amount=-ret.net_payable,
        kind=OUT_TAX,
        label="Tax due",
        detail=f"On {period_end:%B}, payable by {ret.due_date:%d %B}",
        link="/reports/vat",
    )]


# --------------------------------------------------------------------------
# Building the timeline
# --------------------------------------------------------------------------


def build(db: Session, start: Date | None = None, days: int = HORIZON_DAYS) -> Timeline:
    """The projected bank balance, day by day."""
    start = start or Date.today()
    end = start + timedelta(days=days)

    bank_ids = reports._bank_account_ids(db)
    opening = reports._cash_balance(db, bank_ids, start)
    timeline = Timeline(start=start, end=end, opening=opening)

    known = habits(db, start)
    receipts, excluded, excluded_count = _expected_receipts(db, start, end, known)
    timeline.excluded_overdue = excluded
    timeline.excluded_count = excluded_count

    events = receipts
    events += _expected_payments(db, start, end)
    for source in (_expected_payroll, _expected_recurring, _expected_tax):
        try:
            events += source(db, start, end)
        except Exception:  # pragma: no cover - one missing module is not fatal
            continue

    events.sort(key=lambda e: (e.when, -e.amount))
    timeline.events = events
    timeline.overdue_in = sum(e.amount for e in events if e.is_in and e.overdue_days)
    timeline.overdue_out = sum(-e.amount for e in events
                               if not e.is_in and e.overdue_days)

    by_day: dict[Date, Day] = {}
    cursor = start
    while cursor <= end:
        by_day[cursor] = Day(when=cursor)
        cursor += timedelta(days=1)
    for event in events:
        day = by_day.get(event.when)
        if day is None:
            continue
        if event.amount > 0:
            day.money_in += event.amount
        else:
            day.money_out += -event.amount
        day.events.append(event)

    running = opening
    timeline.lowest, timeline.lowest_on = opening, start
    for cursor in sorted(by_day):
        day = by_day[cursor]
        running += day.net
        day.closing = running
        if running < timeline.lowest:
            timeline.lowest, timeline.lowest_on = running, cursor
        if running < 0 and timeline.runs_out_on is None:
            timeline.runs_out_on = cursor
        timeline.days.append(day)
    return timeline


# --------------------------------------------------------------------------
# What would help
# --------------------------------------------------------------------------


@dataclass
class Lever:
    """Something that could be done, and what it is worth."""

    title: str
    detail: str
    worth: int
    moves_to: Date | None = None
    link: str = ""
    link_label: str = "Look at it"

    @property
    def gains_days(self) -> int:
        return 0


def levers(db: Session, timeline: Timeline, limit: int = 3) -> list[Lever]:
    """The few things that would push the date out furthest.

    Each one is worked out by re-running the same timeline with that one thing
    changed, so the number of days it buys is arithmetic rather than a guess.
    """
    out: list[Lever] = []

    overdue = [e for e in timeline.events if e.is_in and e.overdue_days > 0]
    overdue.sort(key=lambda e: -e.amount)
    for event in overdue[:2]:
        out.append(Lever(
            title=f"Chase {event.label}",
            detail=f"{event.detail}. Collecting this is the single biggest thing "
                   "you can do this week.",
            worth=event.amount,
            link=f"/contacts/{event.contact_id}" if event.contact_id else "",
            link_label="Open the customer",
        ))

    if timeline.runs_out_on is not None:
        bills_before = [
            e for e in timeline.events
            if not e.is_in and e.kind == OUT_BILL and e.when <= timeline.runs_out_on
        ]
        bills_before.sort(key=lambda e: e.amount)
        if bills_before:
            worst = bills_before[0]
            out.append(Lever(
                title=f"Talk to {worst.label} about timing",
                detail=f"{worst.detail}. Agreeing to pay this a fortnight later "
                       "would carry you past the difficult day.",
                worth=-worst.amount,
                link=worst.link,
                link_label="Open the bill",
            ))

    if timeline.excluded_count:
        out.append(Lever(
            title=f"{timeline.excluded_count} very old invoices are not on this chart",
            detail="They are too far overdue to guess a date for. They are either "
                   "collectable — in which case chase them — or they are not, in "
                   "which case they should not be in your figures at all.",
            worth=timeline.excluded_overdue,
            link="/reports/aging?kind=ar",
            link_label="See them",
        ))

    return out[:limit]
