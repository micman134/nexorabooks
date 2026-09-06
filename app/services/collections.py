"""Who to chase, in what order, and what to say.

Built on one idea: the right way to chase a customer depends on how *that*
customer has actually behaved, and your ledger already knows. Somebody who has
paid forty invoices on time and is a week late has forgotten. Somebody ninety
days overdue who has never paid on time is a different conversation, and
sending them the same polite note wastes a fortnight.

So each customer gets: what they owe, how overdue it is, what they are worth to
you, how they have paid in the past, and a suggested approach with a message
already written.

**It drafts. It never sends.** The wording is a starting point in your voice to
edit, not an email the software puts out over your name — a chasing letter to
the wrong customer costs more than the invoice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Company, Contact, Invoice
from . import reports
from .cashtimeline import Habit, habits

#: What to do about it. In escalating order.
WAIT, REMIND, CALL, LETTER, STOP = "WAIT", "REMIND", "CALL", "LETTER", "STOP"

APPROACHES = {
    WAIT: "Leave it — not due yet",
    REMIND: "A friendly reminder",
    CALL: "Telephone them",
    LETTER: "A formal letter",
    STOP: "Stop supplying, and decide",
}


@dataclass
class Owing:
    """One customer, what they owe, and what to do about it."""

    contact: Contact
    total: int = 0
    overdue: int = 0
    oldest_days: int = 0
    invoices: list = field(default_factory=list)
    habit: Habit | None = None
    approach: str = WAIT
    reasons: list[str] = field(default_factory=list)
    #: Turnover with this customer over the last year — what losing them costs.
    worth: int = 0

    @property
    def name(self) -> str:
        return self.contact.name

    @property
    def approach_label(self) -> str:
        return APPROACHES.get(self.approach, self.approach)

    @property
    def urgent(self) -> bool:
        return self.approach in (LETTER, STOP)

    @property
    def is_late(self) -> bool:
        return self.overdue > 0

    @property
    def history(self) -> str:
        return self.habit.verdict if self.habit else "No record yet."


def _decide(row: Owing) -> None:
    """Choose the approach, and record why in words a person can argue with."""
    habit, days = row.habit, row.oldest_days

    if row.overdue <= 0:
        row.approach = WAIT
        row.reasons.append("Nothing of theirs is past its due date yet.")
        return

    reliable = bool(habit and habit.has_record and habit.reliability >= 0.7)
    slow = bool(habit and habit.has_record and habit.median_lag > 21)

    if days <= 14:
        # A fortnight is a nudge, whoever it is. Telephoning somebody five days
        # late because they happen to be new is how you lose a new customer.
        row.approach = REMIND
        if reliable:
            row.reasons.append(
                f"They have paid on time {habit.on_time} of {habit.settled} times. "
                "This is almost certainly an oversight."
            )
        else:
            row.reasons.append("Only a few days late — a nudge is enough.")
    elif days <= 30:
        row.approach = REMIND if reliable else CALL
        row.reasons.append(
            "Under a month late, and they usually pay." if reliable
            else "Under a month late, but they have not earned the benefit of the doubt."
        )
    elif days <= 60:
        row.approach = CALL
        row.reasons.append("Over a month late. A reminder has not worked; telephone them.")
    elif days <= 90:
        row.approach = LETTER
        row.reasons.append("Two months late. Put it in writing so there is a record.")
    else:
        row.approach = STOP
        row.reasons.append(
            "Over ninety days. Decide whether this is collectable at all — and "
            "stop supplying on credit until it is settled."
        )

    if slow and row.approach in (REMIND, CALL):
        row.reasons.append(
            f"They are habitually about {habit.median_lag} days late, so this is "
            "how they always are rather than anything new."
        )
    if row.worth and row.total and row.worth > row.total * 6:
        row.reasons.append(
            "They are a large customer — worth a telephone call rather than a "
            "solicitor's letter."
        )
        if row.approach == STOP:
            row.approach = LETTER


def review(db: Session, on: Date | None = None) -> list[Owing]:
    """Every customer who owes anything, worst first."""
    on = on or Date.today()
    known = habits(db, on)
    year_start = on - timedelta(days=365)

    rows, _buckets, _total = reports.aging(db, on, receivable=True)
    out: list[Owing] = []
    for age in rows:
        if age.total <= 0:
            continue
        contact = age.contact
        owing = Owing(contact=contact, total=age.total,
                      overdue=sum(age.buckets[1:]),
                      habit=known.get(contact.id))

        for doc, _bucket, amount in age.docs:
            due = doc.due_date or doc.date
            late = max(0, (on - due).days)
            owing.oldest_days = max(owing.oldest_days, late)
            owing.invoices.append((doc, late, amount))
        owing.invoices.sort(key=lambda item: -item[1])

        owing.worth = int(db.scalar(
            select(reports.func.coalesce(reports.func.sum(Invoice.total), 0)).where(
                Invoice.contact_id == contact.id,
                Invoice.doc_type == "INVOICE",
                Invoice.status.in_(("POSTED", "PART_PAID", "PAID")),
                Invoice.date >= year_start,
            )
        ) or 0)

        _decide(owing)
        out.append(owing)

    order = {STOP: 0, LETTER: 1, CALL: 2, REMIND: 3, WAIT: 4}
    out.sort(key=lambda r: (order.get(r.approach, 9), -r.overdue, -r.total))
    return out


# --------------------------------------------------------------------------
# Should you offer a discount to be paid sooner?
# --------------------------------------------------------------------------


@dataclass
class DiscountCheck:
    offer_pct: str
    days_sooner: int
    cost: int = 0
    worth_it: bool = False
    annualised: float = 0.0
    verdict: str = ""


def discount_check(amount: int, offer_pct: str, days_sooner: int,
                   borrowing_rate_pct: str = "24") -> DiscountCheck:
    """Is 2% off to be paid 30 days sooner a good deal?

    The comparison people get wrong: 2% for 30 days is not 2% a year, it is
    roughly 24% a year. Set beside what borrowing actually costs you, the
    answer is usually obvious — and usually the opposite of what it looks like.
    """
    check = DiscountCheck(offer_pct=str(offer_pct), days_sooner=int(days_sooner or 0))
    try:
        pct = Decimal(str(offer_pct or 0))
        rate = Decimal(str(borrowing_rate_pct or 0))
    except Exception:
        return check
    if pct <= 0 or check.days_sooner <= 0:
        check.verdict = "Enter a discount and how many days sooner it would be paid."
        return check

    check.cost = int(Decimal(amount) * pct / Decimal(100))
    check.annualised = float(pct * Decimal(365) / Decimal(check.days_sooner))
    check.worth_it = Decimal(check.annualised) < rate
    if check.worth_it:
        check.verdict = (
            f"Worth offering. Giving up {pct}% to be paid {check.days_sooner} days "
            f"sooner works out at about {check.annualised:.0f}% a year, which is "
            f"less than the {rate}% it costs you to borrow."
        )
    else:
        check.verdict = (
            f"Do not offer it. {pct}% for {check.days_sooner} days is about "
            f"{check.annualised:.0f}% a year — far dearer than the {rate}% you "
            "would pay to borrow the same money. Chase them instead."
        )
    return check


# --------------------------------------------------------------------------
# What to say
# --------------------------------------------------------------------------


def draft(db: Session, owing: Owing, on: Date | None = None) -> dict:
    """A message to edit and send yourself. Never sent by the software."""
    on = on or Date.today()
    company = db.get(Company, 1)
    us = company.name if company else "us"
    from ..money import fmt

    late = [(doc, days, amount) for doc, days, amount in owing.invoices if days > 0]
    listed = "\n".join(
        f"  {doc.number}   {doc.date:%d %b %Y}   {fmt(amount)}"
        + (f"   {days} days overdue" if days > 0 else "")
        for doc, days, amount in owing.invoices[:12]
    )
    person = owing.contact.contact_person or "Sir or Madam"

    if owing.approach == REMIND:
        subject = f"Reminder: {fmt(owing.overdue)} outstanding"
        body = (
            f"Dear {person},\n\n"
            f"I hope you are well. Our records show the following is outstanding "
            f"with {us}:\n\n{listed}\n\n"
            f"If it has already been paid, please ignore this and accept my "
            f"apologies — and if you could let me know the date, I will find it "
            f"at our end.\n\nThank you,\n"
        )
    elif owing.approach == CALL:
        subject = f"Payment of {fmt(owing.overdue)} — may I call?"
        body = (
            f"Dear {person},\n\n"
            f"The following has now been outstanding for some time:\n\n{listed}\n\n"
            f"I would rather sort this out over the telephone than by email. "
            f"Could you let me know a good time to call, or ring me on the number "
            f"below?\n\nThank you,\n"
        )
    elif owing.approach == LETTER:
        subject = f"Overdue account — {fmt(owing.overdue)}"
        body = (
            f"Dear {person},\n\n"
            f"Despite our earlier requests, the following remains unpaid:\n\n"
            f"{listed}\n\n"
            f"The oldest of these is now {owing.oldest_days} days past its due "
            f"date. Please arrange payment within seven days, or contact me to "
            f"agree a schedule.\n\n"
            f"I would much rather agree something than take this further.\n\n"
            f"Yours sincerely,\n"
        )
    elif owing.approach == STOP:
        subject = f"Account on hold — {fmt(owing.overdue)} overdue"
        body = (
            f"Dear {person},\n\n"
            f"The following has been outstanding for more than ninety days:\n\n"
            f"{listed}\n\n"
            f"I have had to put your account on hold, so we cannot supply further "
            f"goods on credit until it is settled.\n\n"
            f"Please telephone me this week so we can agree how to clear it. If "
            f"there is a difficulty, I would far rather know about it.\n\n"
            f"Yours sincerely,\n"
        )
    else:
        subject = f"Your account with {us}"
        body = (
            f"Dear {person},\n\n"
            f"A summary of your account as it stands:\n\n{listed}\n\n"
            f"Nothing is overdue — this is for your records.\n\nThank you,\n"
        )

    return {
        "to": owing.contact.email or "",
        "subject": subject,
        "body": body,
        "late_count": len(late),
    }
