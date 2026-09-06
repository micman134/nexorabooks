"""Destroying a record, for the one person allowed to.

Everything else in this software is built on the opposite principle. A posted
document is voided, not edited; a mistake is corrected by a reversal that sits
beside the original so an auditor can follow what happened. That is what
double-entry bookkeeping is for and it is the reason the reports can be trusted.

This module is the deliberate exception, and it exists because the owner of a
business asked for it and it is their ledger. A company entering years of old
paperwork will make duplicates. A demonstration leaves test invoices behind.
There has to be a way to take those out that does not leave "VOID — test" down
the middle of every report for the next decade.

So: a super administrator can delete an invoice, and what is left afterwards is
nothing. No document, no journal entry, no stock movement, no attachment.

Three things this will not do, and none of them is a matter of taste:

  * **It will not leave the books unbalanced.** Deleting an invoice deletes its
    ledger entries too. If only the document went, the trial balance would
    still carry its revenue and its debtor, and the owner would have a set of
    accounts that disagrees with the invoice list for a reason nobody could
    find. Delete means the money is unwound as well.

  * **It will not silently change a period that is closed.** If the books are
    locked up to a date, or the invoice sits in a fiscal year that has been
    closed, the deletion is refused. Those figures have been filed with
    somebody — a tax authority, a bank, a board — and altering them afterwards
    without noticing is how a small tidy-up becomes a serious problem.

  * **It will not leave a payment pointing at nothing.** An invoice with money
    allocated to it is refused, and says which receipt to deal with first.
    Otherwise the receipt survives, still allocated, to an invoice that is gone.

The result: what is deleted is genuinely gone, and what remains is still a
coherent set of books. Those two things are compatible. "Gone" and "correct"
are not the same requirement, and this module is careful to satisfy both.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..models import (
    DRAFT,
    Attachment,
    Company,
    FiscalYear,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    StockMove,
    User,
)
from . import attachments
from .posting import PostingError


@dataclass(frozen=True)
class Removed:
    """What was taken out, so the person can be told exactly what happened."""

    number: str
    contact: str
    date: str
    total: int
    entries: int
    stock_moves: int
    attachments: int


def _year_is_closed(db: Session, on) -> bool:
    year = db.scalar(
        select(FiscalYear).where(FiscalYear.start_date <= on, FiscalYear.end_date >= on))
    return bool(year is not None and getattr(year, "is_closed", False))


def why_not(db: Session, inv: Invoice) -> str:
    """The reason this invoice cannot be deleted, or '' when it can be.

    Separate from ``delete_invoice`` so a screen can grey the button out and
    say why, rather than offering something that will be refused after the
    person has typed a confirmation.
    """
    if inv is None:
        return "That document is not here any more."
    if inv.status == DRAFT:
        # A draft has never touched the ledger, so none of the checks below can
        # apply to it. It is a piece of typing, and it may always be thrown away.
        return ""
    if inv.amount_paid:
        return (
            f"{inv.number} has money allocated to it. Remove or void the receipt "
            "that paid it first — otherwise the receipt would be left pointing "
            "at an invoice that no longer exists.")
    company = db.get(Company, 1)
    if company is not None and company.lock_date and inv.date <= company.lock_date:
        return (
            f"The books are locked up to {company.lock_date:%d %b %Y} and "
            f"{inv.number} is dated {inv.date:%d %b %Y}. Those figures have "
            "already been reported. Move the lock date first if you are certain.")
    if _year_is_closed(db, inv.date):
        return (
            f"{inv.number} is in a financial year that has been closed. Deleting "
            "it would change accounts that are already signed off.")
    return ""


def delete_invoice(db: Session, inv: Invoice, user: User | None = None) -> Removed:
    """Remove an invoice and everything the ledger knows about it.

    Raises ``PostingError`` with something a person can act on when it must not
    happen. Returns a description of what went, for the message afterwards.
    """
    from ..security import can, P_DELETE

    if not can(user, P_DELETE):
        raise PostingError(
            "Only a super administrator can delete a document. An administrator "
            "can void it instead, which reverses it and leaves it visible.")

    refused = why_not(db, inv)
    if refused:
        raise PostingError(refused)

    number = inv.number
    contact = getattr(inv.contact, "name", "") or ""
    when = inv.date
    total = inv.total

    # --- the ledger ------------------------------------------------------
    # Both the original entry and anything that reversed it: a voided invoice
    # has two, and leaving either behind would unbalance the accounts.
    entries = list(db.scalars(
        select(JournalEntry).where(
            JournalEntry.source.in_(("INVOICE", "CREDIT_NOTE", "REVERSAL")),
            JournalEntry.source_id == inv.id)).all())
    if inv.journal_entry_id:
        direct = db.get(JournalEntry, inv.journal_entry_id)
        if direct is not None and direct not in entries:
            entries.append(direct)
    # A reversal points back at what it reversed; take those too.
    for entry in list(entries):
        for reversal in db.scalars(
            select(JournalEntry).where(JournalEntry.reverses_id == entry.id)).all():
            if reversal not in entries:
                entries.append(reversal)

    # Everything pointing AT those entries has to let go before any of them can
    # be removed, or SQLite refuses the delete on a foreign key. The invoice
    # holds one; a reversal holds another; a closed year could hold a third.
    ids = {e.id for e in entries}
    inv.journal_entry_id = None
    for entry in entries:
        if entry.reverses_id in ids:
            entry.reverses_id = None
    for year in db.scalars(select(FiscalYear).where(
            FiscalYear.closing_entry_id.in_(ids))).all():
        year.closing_entry_id = None
    db.flush()

    entry_count = 0
    with db.no_autoflush:
        for entry in entries:
            for line in db.scalars(
                    select(JournalLine).where(JournalLine.entry_id == entry.id)).all():
                db.delete(line)
            db.delete(entry)
            entry_count += 1
    db.flush()

    # --- stock --------------------------------------------------------------
    moves = list(db.scalars(select(StockMove).where(
        StockMove.doc_type.in_(("INVOICE", "CREDIT_NOTE")),
        StockMove.doc_id == inv.id)).all())
    for move in moves:
        db.delete(move)

    # --- the paperwork ------------------------------------------------------
    files = list(db.scalars(select(Attachment).where(
        Attachment.doc_type.in_(("INVOICE", "QUOTE", "CREDIT_NOTE")),
        Attachment.doc_id == inv.id)).all())
    for attachment in files:
        try:
            path = attachments.path_for(dbmod.current_slug(), attachment)
            if path.exists():
                path.unlink()
        except Exception:       # noqa: BLE001 — a missing file must not block it
            pass
        db.delete(attachment)

    # --- the document itself ------------------------------------------------
    for line in db.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)).all():
        db.delete(line)
    db.delete(inv)
    db.flush()

    return Removed(number=number, contact=contact,
                   date=when.isoformat() if when else "", total=total,
                   entries=entry_count, stock_moves=len(moves),
                   attachments=len(files))
