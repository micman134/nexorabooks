"""Turning a read statement into a batch, and a confirmed line into a posting.

The rule this module exists to enforce is that **nothing reaches the ledger
until a person confirms that particular line**. The reading finds the
transactions, the matcher proposes what they are, and this module carries out
whatever a human then agreed to — one line at a time, each producing an
ordinary posting that can be looked at, printed and reversed like any other.

There is no bulk "import everything" that posts unseen. The nearest thing is
"confirm the strong matches", which still applies them one by one, still
records who did it, and still leaves everything it touched visible in the
audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ACTION_CLEAR,
    ACTION_IGNORE,
    ACTION_PAYMENT,
    ACTION_POST,
    ACTION_RECEIPT,
    CONFIRMED,
    IGNORED,
    PAYMENT,
    RECEIPT,
    SUGGESTED,
    UNMATCHED,
    Account,
    BankAccount,
    BankImport,
    BankImportLine,
    Bill,
    Invoice,
    JournalLine,
    Payment,
    PaymentAllocation,
    User,
)
from . import cash, matching
from .posting import EntryDraft, PostingError, next_number, post_entry


class ImportProblem(PostingError):
    """Something about this line cannot be carried out."""


# --------------------------------------------------------------------------
# Creating the batch
# --------------------------------------------------------------------------


@dataclass
class Outcome:
    batch: BankImport
    added: int = 0
    duplicates: int = 0


def existing_fingerprints(db: Session, bank_account_id: int) -> set[str]:
    """Everything already imported for this account, so nothing lands twice."""
    rows = db.execute(
        select(BankImportLine.fingerprint)
        .join(BankImport, BankImportLine.batch_id == BankImport.id)
        .where(BankImport.bank_account_id == bank_account_id)
    )
    return {row[0] for row in rows if row[0]}


def create(
    db: Session,
    bank_account_id: int,
    reading,
    filename: str = "",
    user: User | None = None,
) -> Outcome:
    """Save the read statement as a batch, skipping anything seen before."""
    seen = existing_fingerprints(db, bank_account_id)

    batch = BankImport(
        bank_account_id=bank_account_id,
        filename=filename[:200],
        file_format=reading.format,
        imported_by_id=user.id if user else None,
        first_date=reading.first_date,
        last_date=reading.last_date,
        opening_balance=reading.opening_balance,
        closing_balance=reading.closing_balance,
    )
    db.add(batch)
    db.flush()

    added = duplicates = 0
    for line in reading.lines:
        mark = line.fingerprint
        if mark in seen:
            duplicates += 1
            continue
        seen.add(mark)
        db.add(BankImportLine(
            batch_id=batch.id,
            row_no=line.row,
            date=line.date,
            description=line.description[:1000],
            reference=(line.reference or "")[:120],
            payee=(line.payee or "")[:200],
            amount=line.amount,
            balance=line.balance,
            fingerprint=mark[:200],
        ))
        added += 1

    batch.line_count = added
    batch.duplicate_count = duplicates
    db.flush()

    rows = list(db.scalars(
        select(BankImportLine).where(BankImportLine.batch_id == batch.id)
    ))
    batch.total_in = sum(r.amount for r in rows if r.amount > 0)
    batch.total_out = sum(-r.amount for r in rows if r.amount < 0)
    return Outcome(batch=batch, added=added, duplicates=duplicates)


def run_matching(db: Session, batch: BankImport) -> None:
    """Work out what each line probably is. Writes suggestions, posts nothing."""
    rows = list(db.scalars(
        select(BankImportLine)
        .where(BankImportLine.batch_id == batch.id, BankImportLine.status == UNMATCHED)
        .order_by(BankImportLine.date, BankImportLine.id)
    ))
    if not rows:
        return
    for row, suggestion in zip(rows, matching.suggest_all(db, batch.bank_account_id, rows)):
        row.action = suggestion.action
        row.score = suggestion.score
        row.reason = suggestion.why
        row.contact_id = suggestion.contact_id
        row.account_id = suggestion.account_id
        row.journal_line_id = suggestion.journal_line_id
        row.documents = suggestion.document_ids
        row.status = SUGGESTED if suggestion.action else UNMATCHED


# --------------------------------------------------------------------------
# Carrying out one line
# --------------------------------------------------------------------------


def apply(
    db: Session,
    line: BankImportLine,
    action: str,
    *,
    account_id: int | None = None,
    contact_id: int | None = None,
    document_ids: list[int] | None = None,
    journal_line_id: int | None = None,
    user: User | None = None,
    learn: bool = True,
) -> BankImportLine:
    """Do what a person decided about one statement line."""
    if line.status == CONFIRMED:
        raise ImportProblem("That line has already been dealt with.")

    batch = db.get(BankImport, line.batch_id)
    bank = db.get(BankAccount, batch.bank_account_id)
    if bank is None:
        raise ImportProblem("That bank account no longer exists.")

    if action == ACTION_IGNORE:
        line.status = IGNORED
        line.action = ACTION_IGNORE
        return line

    if action == ACTION_CLEAR:
        _clear(db, line, journal_line_id or line.journal_line_id)
    elif action in (ACTION_RECEIPT, ACTION_PAYMENT):
        _settle(db, line, bank, action, contact_id or line.contact_id,
                document_ids if document_ids is not None else line.documents, user)
    elif action == ACTION_POST:
        _post_direct(db, line, bank, account_id or line.account_id,
                     contact_id or line.contact_id, user)
        if learn:
            matching.remember(db, line.description, line.amount,
                              account_id or line.account_id,
                              contact_id or line.contact_id, line.date)
    else:
        raise ImportProblem("Choose what this line was before confirming it.")

    line.status = CONFIRMED
    line.action = action
    return line


def _clear(db: Session, line: BankImportLine, journal_line_id: int | None) -> None:
    """It was already in the books: tick it off rather than record it again."""
    if not journal_line_id:
        raise ImportProblem("Choose which entry in your books this line is.")
    target = db.get(JournalLine, journal_line_id)
    if target is None:
        raise ImportProblem("That entry no longer exists.")
    if target.cleared:
        raise ImportProblem("That entry has already been ticked off another line.")
    if target.debit - target.credit != line.amount:
        raise ImportProblem(
            "That entry is not for the same amount as the statement line, so it "
            "cannot be the same transaction."
        )
    target.cleared = True
    target.cleared_date = line.date
    line.journal_line_id = target.id
    line.entry_id = target.entry_id


def _settle(
    db: Session,
    line: BankImportLine,
    bank: BankAccount,
    action: str,
    contact_id: int | None,
    document_ids: list[int],
    user: User | None,
) -> None:
    """Money in or out against a customer or supplier, allocated to documents."""
    if not contact_id:
        raise ImportProblem("Choose the customer or supplier this was with.")

    is_receipt = action == ACTION_RECEIPT
    if is_receipt and line.amount <= 0:
        raise ImportProblem("That line is money going out, so it cannot be a receipt.")
    if not is_receipt and line.amount >= 0:
        raise ImportProblem("That line is money coming in, so it cannot be a payment.")

    pay = Payment(
        number=next_number(db, "RECEIPT" if is_receipt else "PAYMENT"),
        kind=RECEIPT if is_receipt else PAYMENT,
        contact_id=contact_id,
        date=line.date,
        bank_account_id=bank.id,
        amount=abs(line.amount),
        reference=(line.reference or "")[:60],
        memo=f"From the bank statement: {line.description}"[:1000],
    )
    db.add(pay)
    db.flush()

    if document_ids:
        _allocate_to(db, pay, document_ids, is_receipt)
    else:
        # No particular document named: oldest first, which is what a person
        # does by hand and what every accounting convention assumes.
        cash.auto_allocate(db, pay)

    entry = cash.post_payment(db, pay, user)
    line.payment_id = pay.id
    line.entry_id = entry.id
    line.contact_id = contact_id
    line.documents = document_ids or []
    _tick_off(db, bank, entry, line)


def _allocate_to(db: Session, pay: Payment, document_ids: list[int], is_receipt: bool) -> None:
    """Spread the money over the documents the person chose, oldest first."""
    model = Invoice if is_receipt else Bill
    docs = [db.get(model, doc_id) for doc_id in document_ids]
    docs = [doc for doc in docs if doc is not None and doc.balance_due > 0]
    if not docs:
        raise ImportProblem(
            "None of the documents chosen are still outstanding — somebody may have "
            "settled them since this statement was imported."
        )
    docs.sort(key=lambda doc: (doc.date, doc.id))

    left = pay.amount
    for doc in docs:
        if left <= 0:
            break
        share = min(left, doc.balance_due)
        allocation = PaymentAllocation(payment_id=pay.id, amount=share)
        if is_receipt:
            allocation.invoice_id = doc.id
        else:
            allocation.bill_id = doc.id
        db.add(allocation)
        left -= share
    db.flush()
    db.refresh(pay)


def _post_direct(
    db: Session,
    line: BankImportLine,
    bank: BankAccount,
    account_id: int | None,
    contact_id: int | None,
    user: User | None,
) -> None:
    """Neither a receipt nor a bill payment: straight to an account."""
    if not account_id:
        raise ImportProblem("Choose which account this belongs to.")
    account = db.get(Account, account_id)
    if account is None:
        raise ImportProblem("That account no longer exists.")
    if account.id == bank.account_id:
        raise ImportProblem(
            "That is the bank account itself. Money cannot move from an account "
            "to the same account — choose what it was spent on or received for."
        )

    memo = line.description[:250] or "From the bank statement"
    draft = EntryDraft(
        date=line.date,
        memo=memo,
        reference=(line.reference or "")[:60],
        source="STATEMENT",
    )
    value = abs(line.amount)
    if line.amount > 0:
        draft.debit(bank.account_id, value, memo)
        draft.credit(account, value, memo, contact_id=contact_id)
    else:
        draft.debit(account, value, memo, contact_id=contact_id)
        draft.credit(bank.account_id, value, memo)

    entry = post_entry(db, draft, user)
    line.entry_id = entry.id
    line.account_id = account_id
    line.contact_id = contact_id
    _tick_off(db, bank, entry, line)


def _tick_off(db: Session, bank: BankAccount, entry, line: BankImportLine) -> None:
    """Mark the bank side of a newly created entry as cleared.

    It came off the statement, so it has by definition cleared the bank. Not
    doing this would leave every imported transaction sitting on the next
    reconciliation as though it were outstanding, which would make the import
    worse than useless.
    """
    for row in db.scalars(select(JournalLine).where(JournalLine.entry_id == entry.id)):
        if row.account_id == bank.account_id:
            row.cleared = True
            row.cleared_date = line.date
            line.journal_line_id = row.id


# --------------------------------------------------------------------------
# The bulk action
# --------------------------------------------------------------------------


def confirm_strong(db: Session, batch: BankImport, user: User | None = None) -> dict:
    """Carry out every suggestion the matcher was sure about.

    Applied one at a time, each through the same path a person clicking the
    button uses, so nothing takes a shortcut past the checks. A line that fails
    is left alone with the reason attached rather than stopping the run.
    """
    done = skipped = failed = 0
    problems: list[str] = []
    rows = list(db.scalars(
        select(BankImportLine)
        .where(BankImportLine.batch_id == batch.id, BankImportLine.status == SUGGESTED)
        .order_by(BankImportLine.date, BankImportLine.id)
    ))
    for row in rows:
        if row.score < matching.STRONG or not row.action:
            skipped += 1
            continue
        try:
            # Not learned from: a rule written from the software's own guess
            # would turn one wrong match into next month's suggestion.
            apply(db, row, row.action, user=user, learn=False)
            db.flush()
            done += 1
        except (ImportProblem, PostingError) as exc:
            db.rollback()
            failed += 1
            problems.append(f"{row.date:%d %b}: {exc}")
    return {"done": done, "skipped": skipped, "failed": failed, "problems": problems}


# --------------------------------------------------------------------------
# What the review screen needs to know
# --------------------------------------------------------------------------


def choices_for(db: Session, line: BankImportLine) -> dict:
    """The documents and entries a person might pick for this line."""
    from datetime import timedelta

    money_in = line.amount > 0
    model = Invoice if money_in else Bill
    doc_type = "INVOICE" if money_in else "BILL"
    query = select(model).where(
        model.status.in_(("POSTED", "PART_PAID")),
        model.doc_type == doc_type,
        model.date <= line.date + timedelta(days=matching.NEARBY_DAYS),
    )
    if line.contact_id:
        query = query.where(model.contact_id == line.contact_id)
    docs = [d for d in db.scalars(query.order_by(model.date.desc()).limit(60))
            if d.balance_due > 0]

    bank = db.get(BankAccount, db.get(BankImport, line.batch_id).bank_account_id)
    nearby = []
    if bank is not None:
        from ..models import JournalEntry

        rows = db.execute(
            select(JournalLine, JournalEntry)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalEntry.is_posted.is_(True),
                JournalLine.account_id == bank.account_id,
                JournalLine.cleared.is_(False),
                JournalEntry.date >= line.date - timedelta(days=matching.NEARBY_DAYS * 3),
                JournalEntry.date <= line.date + timedelta(days=matching.NEARBY_DAYS * 3),
            )
            .order_by(JournalEntry.date)
        ).all()
        nearby = [
            (row, entry) for row, entry in rows
            if (row.debit - row.credit) == line.amount
        ]
    return {"documents": docs, "nearby": nearby}
