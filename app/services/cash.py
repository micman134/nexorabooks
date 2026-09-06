"""Money in and money out: receipts, supplier payments and bank transfers."""
from __future__ import annotations

from datetime import date as Date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    PAYMENT,
    RECEIPT,
    VOID,
    BankAccount,
    Bill,
    Invoice,
    JournalEntry,
    Payment,
    PaymentAllocation,
    User,
)
from .documents import refresh_bill_status, refresh_invoice_status
from .posting import EntryDraft, PostingError, next_number, post_entry, reverse_entry, sys_account


def settled_total(pay: Payment) -> int:
    """The amount of debt this payment clears.

    Cash, plus anything the customer kept back and accounted for on your behalf:
    withholding tax, VAT withheld at source, and any settlement discount taken.
    All four together clear the invoice.
    """
    return pay.amount + pay.wht_amount + pay.vat_withheld + pay.discount_amount


def post_payment(db: Session, pay: Payment, user: User | None = None) -> JournalEntry:
    """Post a customer receipt or a supplier payment and apply its allocations."""
    if pay.journal_entry_id:
        raise PostingError(f"{pay.number} has already been posted.")

    total = settled_total(pay)
    if total <= 0 and pay.bank_charge <= 0:
        raise PostingError("Enter an amount before posting.")

    bank = db.get(BankAccount, pay.bank_account_id)
    if bank is None:
        raise PostingError("Choose the bank or cash account this money moved through.")

    allocated = sum(
        a.amount + a.wht_amount + a.vat_withheld + a.discount for a in pay.allocations
    )
    if allocated > total:
        from ..money import fmt

        raise PostingError(
            f"You have allocated {fmt(allocated)} but the payment is only {fmt(total)}."
        )
    pay.unallocated = total - allocated

    is_receipt = pay.kind == RECEIPT
    ar_ap = sys_account(db, "AR" if is_receipt else "AP")
    label = "Receipt" if is_receipt else "Payment"

    draft = EntryDraft(
        date=pay.date,
        memo=f"{label} {pay.number} — {pay.contact.name}",
        reference=pay.reference or pay.number,
        source=RECEIPT if is_receipt else PAYMENT,
        source_id=pay.id,
    )

    if is_receipt:
        draft.debit(bank.account_id, pay.amount - pay.bank_charge,
                    f"{label} {pay.number} — {pay.contact.name}")
        if pay.bank_charge:
            draft.debit(sys_account(db, "BANK_CHARGES"), pay.bank_charge,
                        f"Bank charge on {pay.number}")
        if pay.wht_amount:
            draft.debit(sys_account(db, "WHT_RECEIVABLE"), pay.wht_amount,
                        f"WHT credit deducted by {pay.contact.name}",
                        contact_id=pay.contact_id)
        if pay.vat_withheld:
            # A government or oil-and-gas customer keeps the VAT back and pays
            # it to the NRS itself. It is not lost — it comes off what you owe
            # on your VAT return, against the credit note they issue.
            draft.debit(sys_account(db, "VAT_WITHHELD"), pay.vat_withheld,
                        f"VAT withheld at source by {pay.contact.name}",
                        contact_id=pay.contact_id)
        if pay.discount_amount:
            draft.debit(sys_account(db, "DISCOUNT_ALLOWED"), pay.discount_amount,
                        f"Settlement discount — {pay.number}", contact_id=pay.contact_id)
        draft.credit(ar_ap, total, f"{label} {pay.number}", contact_id=pay.contact_id)
    else:
        draft.debit(ar_ap, total, f"{label} {pay.number}", contact_id=pay.contact_id)
        draft.credit(bank.account_id, pay.amount + pay.bank_charge,
                     f"{label} {pay.number} — {pay.contact.name}")
        if pay.bank_charge:
            draft.debit(sys_account(db, "BANK_CHARGES"), pay.bank_charge,
                        f"Bank charge on {pay.number}")
        if pay.wht_amount:
            draft.credit(sys_account(db, "WHT_PAYABLE"), pay.wht_amount,
                         f"WHT withheld from {pay.contact.name} — due to NRS by the 21st",
                         contact_id=pay.contact_id)
        if pay.discount_amount:
            draft.credit(sys_account(db, "DISCOUNT_RECEIVED"), pay.discount_amount,
                         f"Settlement discount — {pay.number}", contact_id=pay.contact_id)

    entry = post_entry(db, draft, user=user)
    pay.journal_entry_id = entry.id

    for alloc in pay.allocations:
        applied = alloc.amount + alloc.wht_amount + alloc.vat_withheld + alloc.discount
        if alloc.invoice_id:
            inv = db.get(Invoice, alloc.invoice_id)
            inv.amount_paid += applied
            refresh_invoice_status(inv)
        elif alloc.bill_id:
            bill = db.get(Bill, alloc.bill_id)
            bill.amount_paid += applied
            refresh_bill_status(bill)

    db.flush()
    return entry


def void_payment(db: Session, pay: Payment, on: Date | None = None, user: User | None = None) -> None:
    if pay.status == VOID:
        raise PostingError(f"{pay.number} is already void.")
    on = on or pay.date
    if pay.journal_entry_id:
        entry = db.get(JournalEntry, pay.journal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, on=on, user=user, memo=f"Void of {pay.number}")

    for alloc in pay.allocations:
        applied = alloc.amount + alloc.wht_amount + alloc.vat_withheld + alloc.discount
        if alloc.invoice_id:
            inv = db.get(Invoice, alloc.invoice_id)
            inv.amount_paid -= applied
            refresh_invoice_status(inv)
        elif alloc.bill_id:
            bill = db.get(Bill, alloc.bill_id)
            bill.amount_paid -= applied
            refresh_bill_status(bill)

    pay.status = VOID
    db.flush()


def auto_allocate(db: Session, pay: Payment) -> None:
    """Apply a payment to the contact's oldest outstanding documents first."""
    total = settled_total(pay) - sum(
        a.amount + a.wht_amount + a.vat_withheld + a.discount for a in pay.allocations
    )
    if total <= 0:
        return

    if pay.kind == RECEIPT:
        docs = db.scalars(
            select(Invoice)
            .where(
                Invoice.contact_id == pay.contact_id,
                Invoice.status.in_(("POSTED", "PART_PAID")),
                Invoice.doc_type == "INVOICE",
            )
            .order_by(Invoice.date, Invoice.id)
        )
    else:
        docs = db.scalars(
            select(Bill)
            .where(
                Bill.contact_id == pay.contact_id,
                Bill.status.in_(("POSTED", "PART_PAID")),
                Bill.doc_type == "BILL",
            )
            .order_by(Bill.date, Bill.id)
        )

    # The withheld and discount portions attach to the first documents they cover.
    wht_left, vat_left, disc_left = pay.wht_amount, pay.vat_withheld, pay.discount_amount
    for doc in docs:
        if total <= 0:
            break
        outstanding = doc.balance_due
        if outstanding <= 0:
            continue
        apply = min(outstanding, total)
        w = min(wht_left, apply)
        v = min(vat_left, apply - w)
        d = min(disc_left, apply - w - v)
        cash = apply - w - v - d
        alloc = PaymentAllocation(
            payment_id=pay.id, amount=cash, wht_amount=w, vat_withheld=v, discount=d
        )
        if pay.kind == RECEIPT:
            alloc.invoice_id = doc.id
        else:
            alloc.bill_id = doc.id
        pay.allocations.append(alloc)
        wht_left -= w
        vat_left -= v
        disc_left -= d
        total -= apply
    db.flush()


# --------------------------------------------------------------------------
# Bank transfers
# --------------------------------------------------------------------------


def post_transfer(
    db: Session,
    on: Date,
    from_bank: BankAccount,
    to_bank: BankAccount,
    amount: int,
    charge: int = 0,
    reference: str = "",
    memo: str = "",
    user: User | None = None,
) -> JournalEntry:
    if from_bank.id == to_bank.id:
        raise PostingError("Choose two different accounts for a transfer.")
    if amount <= 0:
        raise PostingError("A transfer must be for a positive amount.")

    number = next_number(db, "TRANSFER")
    draft = EntryDraft(
        date=on,
        memo=memo or f"Transfer {number}: {from_bank.name} to {to_bank.name}",
        reference=reference or number,
        source="TRANSFER",
    )
    draft.debit(to_bank.account_id, amount, f"Transfer in from {from_bank.name}")
    draft.credit(from_bank.account_id, amount + charge, f"Transfer out to {to_bank.name}")
    if charge:
        draft.debit(sys_account(db, "BANK_CHARGES"), charge, f"Transfer charge — {number}")
    return post_entry(db, draft, user=user)
