"""Invoices and bills that repeat.

A template is not a document. It sits outside the books until it generates
something, and what it generates is a draft unless you have said otherwise —
so a rent invoice with the wrong figure on it can be fixed before a customer
ever sees it.

The date arithmetic is the part that matters. A monthly template set to the
31st has to land on 30 April and go back to 31 May, not walk backwards down
the calendar a day at a time until it is billing on the 28th all year. That is
what ``anchor_day`` is for.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    DRAFT,
    FORTNIGHTLY_R,
    HALF_YEARLY_R,
    MONTHLY_R,
    QUARTERLY_R,
    RECUR_ACTIVE,
    RECUR_FINISHED,
    RECUR_PAUSED,
    WEEKLY_R,
    YEARLY_R,
    Bill,
    BillLine,
    Contact,
    Invoice,
    InvoiceLine,
    RecurringDocument,
    RecurringTemplate,
    User,
)
from .documents import post_bill, post_invoice, recalc_bill, recalc_invoice
from .posting import PostingError, audit, next_number

MONTH_STEPS = {
    MONTHLY_R: 1,
    QUARTERLY_R: 3,
    HALF_YEARLY_R: 6,
    YEARLY_R: 12,
}
DAY_STEPS = {
    WEEKLY_R: 7,
    FORTNIGHTLY_R: 14,
}


class RecurringError(PostingError):
    """Safe to show the user."""


# --------------------------------------------------------------------------
# When does it fall next?
# --------------------------------------------------------------------------


def add_months(on: date, months: int, anchor_day: int | None = None) -> date:
    """Move ``on`` forward by whole months, keeping the anchor day.

    The anchor is clamped, never carried: a template anchored on the 31st lands
    on 30 April and is back on 31 May.
    """
    day = anchor_day or on.day
    total = (on.year * 12 + on.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day, monthrange(year, month)[1]))


def next_after(on: date, frequency: str, anchor_day: int | None = None) -> date:
    if frequency in DAY_STEPS:
        return on + timedelta(days=DAY_STEPS[frequency])
    return add_months(on, MONTH_STEPS.get(frequency, 1), anchor_day)


def occurrences_between(template: RecurringTemplate, start: date, end: date) -> list[date]:
    """Every date this template would fall on in a window. Read-only."""
    out: list[date] = []
    on = template.next_date
    guard = 0
    while on <= end and guard < 500:
        guard += 1
        if on >= start and not _finished_at(template, on, len(out)):
            out.append(on)
        elif _finished_at(template, on, len(out)):
            break
        on = next_after(on, template.frequency, template.anchor_day)
    return out


def _finished_at(template: RecurringTemplate, on: date, extra: int) -> bool:
    if template.end_date and on > template.end_date:
        return True
    if template.max_occurrences and template.occurrences + extra >= template.max_occurrences:
        return True
    return False


def is_finished(template: RecurringTemplate) -> bool:
    if template.end_date and template.next_date > template.end_date:
        return True
    if template.max_occurrences and template.occurrences >= template.max_occurrences:
        return True
    return False


# --------------------------------------------------------------------------
# What is waiting
# --------------------------------------------------------------------------


@dataclass
class Due:
    template: RecurringTemplate
    dates: list[date]

    @property
    def count(self) -> int:
        return len(self.dates)

    @property
    def value(self) -> int:
        return self.template.estimated_total * self.count


def due(db: Session, upto: date | None = None) -> list[Due]:
    """Templates with at least one document owed on or before ``upto``.

    Missed dates are not skipped. A template left alone for three months owes
    three documents, and the list says so.
    """
    upto = upto or date.today()
    out: list[Due] = []
    for template in db.scalars(
        select(RecurringTemplate)
        .where(RecurringTemplate.status == RECUR_ACTIVE)
        .order_by(RecurringTemplate.next_date)
    ):
        dates = occurrences_between(template, date.min, upto)
        if dates:
            out.append(Due(template=template, dates=dates))
    return out


def due_count(db: Session, upto: date | None = None) -> int:
    return sum(d.count for d in due(db, upto))


# --------------------------------------------------------------------------
# Generating
# --------------------------------------------------------------------------


def generate_one(
    db: Session,
    template: RecurringTemplate,
    on: date | None = None,
    user: User | None = None,
) -> Invoice | Bill:
    """Produce one document from a template and move the template forward."""
    if template.status == RECUR_FINISHED:
        raise RecurringError(f"'{template.name}' has finished and will not generate again.")
    if not template.lines:
        raise RecurringError(f"'{template.name}' has no lines, so there is nothing to bill.")

    on = on or template.next_date
    contact = db.get(Contact, template.contact_id)
    if contact is None:
        raise RecurringError(f"'{template.name}' points at a customer who no longer exists.")

    doc = _build(db, template, contact, on, user)
    db.flush()
    db.refresh(doc)
    if template.is_sales:
        recalc_invoice(db, doc)
    else:
        recalc_bill(db, doc)

    posted = False
    if template.auto_post:
        try:
            if template.is_sales:
                post_invoice(db, doc, user=user)
            else:
                post_bill(db, doc, user=user)
            posted = True
        except PostingError:
            # A locked period or a missing account must not lose the document;
            # it stays a draft and somebody deals with it.
            posted = False

    db.add(
        RecurringDocument(
            template_id=template.id,
            doc_type=template.doc_type,
            doc_id=doc.id,
            doc_number=doc.number,
            date=on,
            total=doc.total,
            was_posted=posted,
        )
    )

    template.occurrences += 1
    template.last_generated = on
    template.next_date = next_after(on, template.frequency, template.anchor_day)
    if is_finished(template):
        template.status = RECUR_FINISHED
    db.flush()
    audit(db, user, "GENERATE", "RecurringTemplate", template.id,
          detail=f"{template.name} → {doc.number}")
    return doc


def _build(db, template, contact, on, user):
    due_date = on + timedelta(days=template.payment_terms_days or 0)
    if template.is_sales:
        doc = Invoice(
            number=next_number(db, template.doc_type),
            doc_type=template.doc_type,
            contact_id=contact.id,
            date=on,
            due_date=due_date,
            status=DRAFT,
            reference=template.reference,
            memo=template.memo,
            terms=template.terms,
            wht_code_id=template.wht_code_id,
            created_by_id=user.id if user else None,
        )
        db.add(doc)
        db.flush()
        for line in template.lines:
            db.add(InvoiceLine(
                invoice_id=doc.id, line_no=line.line_no, item_id=line.item_id,
                description=line.description, qty=line.qty, unit_price=line.unit_price,
                discount_pct=line.discount_pct, account_id=line.account_id,
                tax_code_id=line.tax_code_id,
            ))
        return doc

    doc = Bill(
        number=next_number(db, template.doc_type),
        doc_type=template.doc_type,
        contact_id=contact.id,
        date=on,
        due_date=due_date,
        status=DRAFT,
        reference=template.reference,
        memo=template.memo,
        wht_code_id=template.wht_code_id,
        created_by_id=user.id if user else None,
    )
    db.add(doc)
    db.flush()
    for line in template.lines:
        db.add(BillLine(
            bill_id=doc.id, line_no=line.line_no, item_id=line.item_id,
            description=line.description, qty=line.qty, unit_price=line.unit_price,
            discount_pct=line.discount_pct, account_id=line.account_id,
            tax_code_id=line.tax_code_id,
        ))
    return doc


def catch_up(
    db: Session,
    template: RecurringTemplate,
    upto: date | None = None,
    user: User | None = None,
) -> list:
    """Generate every document this template owes up to ``upto``."""
    upto = upto or date.today()
    made = []
    guard = 0
    while (
        template.status == RECUR_ACTIVE
        and template.next_date <= upto
        and not is_finished(template)
        and guard < 500
    ):
        guard += 1
        made.append(generate_one(db, template, template.next_date, user=user))
    return made


def run_all(db: Session, upto: date | None = None, user: User | None = None) -> dict:
    """Generate everything owed across every active template."""
    upto = upto or date.today()
    made, failed = [], []
    for template in list(
        db.scalars(select(RecurringTemplate).where(RecurringTemplate.status == RECUR_ACTIVE))
    ):
        try:
            made.extend(catch_up(db, template, upto, user=user))
        except RecurringError as e:
            failed.append((template, str(e)))
    return {"made": made, "failed": failed}


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def pause(db: Session, template: RecurringTemplate, user: User | None = None) -> None:
    template.status = RECUR_PAUSED if template.status == RECUR_ACTIVE else RECUR_ACTIVE
    if template.status == RECUR_ACTIVE and is_finished(template):
        raise RecurringError(
            f"'{template.name}' has reached its end date or its limit. "
            "Change one of those before starting it again."
        )
    db.flush()
    audit(db, user, "PAUSE" if template.status == RECUR_PAUSED else "RESUME",
          "RecurringTemplate", template.id, detail=template.name)


def skip_next(db: Session, template: RecurringTemplate, user: User | None = None) -> date:
    """Miss one — the customer is away, the retainer is on hold for a month."""
    skipped = template.next_date
    template.next_date = next_after(skipped, template.frequency, template.anchor_day)
    template.occurrences += 1
    if is_finished(template):
        template.status = RECUR_FINISHED
    db.flush()
    audit(db, user, "SKIP", "RecurringTemplate", template.id,
          detail=f"{template.name} — skipped {skipped:%d %b %Y}")
    return template.next_date
