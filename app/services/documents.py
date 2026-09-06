"""Business documents: invoices, credit notes, bills, debit notes.

Each function here does two things and nothing else — work out the numbers, and
hand a balanced draft to the posting engine.  All ledger writes go through
``posting.post_entry``.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    DRAFT,
    PAID,
    PART_PAID,
    POSTED,
    STOCK_ITEM,
    VOID,
    Account,
    Bill,
    BillLine,
    Company,
    Invoice,
    InvoiceLine,
    Item,
    JournalEntry,
    StockMove,
    User,
)
from . import costing, tax
from .posting import EntryDraft, PostingError, next_number, post_entry, reverse_entry, sys_account


def _r(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def line_net(qty_milli: int, unit_price: int, discount_pct: str = "0") -> int:
    """VAT-exclusive net value of a document line, in kobo."""
    gross = Decimal(qty_milli) * Decimal(unit_price) / Decimal(1000)
    disc = Decimal(str(discount_pct or "0"))
    if disc:
        gross = gross * (Decimal(100) - disc) / Decimal(100)
    return _r(gross)


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------


def recalc_invoice(db: Session, inv: Invoice) -> Invoice:
    subtotal = vat_total = gross_before_disc = 0
    for line in inv.lines:
        gross = _r(Decimal(line.qty) * Decimal(line.unit_price) / Decimal(1000))
        line.net = line_net(line.qty, line.unit_price, line.discount_pct)
        line.vat_amount = tax.vat_on(line.net, line.tax_code)
        gross_before_disc += gross
        subtotal += line.net
        vat_total += line.vat_amount

    inv.subtotal = subtotal
    inv.discount_total = gross_before_disc - subtotal
    inv.vat_total = vat_total
    inv.total = subtotal + vat_total

    # Expected WHT the customer will deduct — informational on the invoice,
    # and used to prefill the receipt when payment arrives.
    wht, _ = tax.wht_on(subtotal, inv.wht_code, inv.contact)
    inv.wht_total = wht
    return inv


def recalc_bill(db: Session, bill: Bill) -> Bill:
    subtotal = vat_total = gross_before_disc = 0
    for line in bill.lines:
        gross = _r(Decimal(line.qty) * Decimal(line.unit_price) / Decimal(1000))
        line.net = line_net(line.qty, line.unit_price, line.discount_pct)
        line.vat_amount = tax.vat_on(line.net, line.tax_code)
        gross_before_disc += gross
        subtotal += line.net
        vat_total += line.vat_amount

    bill.subtotal = subtotal
    bill.discount_total = gross_before_disc - subtotal
    bill.vat_total = vat_total
    bill.total = subtotal + vat_total

    # WHT we are required to deduct when we pay this supplier.
    wht, _ = tax.wht_on(subtotal, bill.wht_code, bill.contact)
    bill.wht_total = wht
    return bill


def due_date_for(db: Session, on: Date, contact) -> Date:
    days = contact.payment_terms_days if contact else None
    if days is None:
        company = db.get(Company, 1)
        days = company.default_payment_terms_days if company else 30
    return on + timedelta(days=days or 0)


# --------------------------------------------------------------------------
# Posting sales documents
# --------------------------------------------------------------------------



def _serials(line) -> list[str]:
    """Serial numbers typed on a document line, one per line or comma separated."""
    raw = (getattr(line, "serials", "") or "").replace(",", "\n")
    return [s.strip() for s in raw.split("\n") if s.strip()]


def _batch_id(db: Session, item, line):
    """The batch a line names, if it names one. Otherwise let the engine choose."""
    from ..models import Batch

    batch_no = (getattr(line, "batch_no", "") or "").strip()
    if not batch_no or not item.track_batches:
        return None
    return db.scalar(
        select(Batch.id).where(Batch.item_id == item.id, Batch.batch_no == batch_no)
    )


def post_invoice(db: Session, inv: Invoice, user: User | None = None) -> JournalEntry:
    """Post an invoice or credit note to the ledger and move stock."""
    if inv.status not in (DRAFT,):
        raise PostingError(f"{inv.number} is already {inv.status.lower()} — it cannot be posted again.")
    if not inv.lines:
        raise PostingError("Add at least one line before posting.")

    recalc_invoice(db, inv)
    if inv.total == 0:
        raise PostingError("This document totals zero — there is nothing to post.")

    is_credit = inv.doc_type == "CREDIT_NOTE"
    sign = -1 if is_credit else 1

    ar = sys_account(db, "AR")
    vat_out = sys_account(db, "VAT_OUTPUT")
    default_sales = sys_account(db, "SALES")
    label = "Credit note" if is_credit else "Invoice"

    draft = EntryDraft(
        date=inv.date,
        memo=f"{label} {inv.number} — {inv.contact.name}",
        reference=inv.number,
        source="INVOICE",
        source_id=inv.id,
    )

    draft.signed(ar, sign * inv.total, f"{label} {inv.number}", contact_id=inv.contact_id)

    for line in inv.lines:
        acc_id = line.account_id
        if not acc_id and line.item_id:
            item = db.get(Item, line.item_id)
            acc_id = item.sales_account_id if item else None
        acc_id = acc_id or default_sales.id
        draft.signed(
            acc_id,
            -sign * line.net,
            line.description[:255],
            contact_id=inv.contact_id,
            item_id=line.item_id,
            project_id=line.project_id,
            tax_code_id=line.tax_code_id,
            tax_base=sign * line.net,
        )

    if inv.vat_total:
        draft.signed(vat_out, -sign * inv.vat_total, f"Output VAT — {inv.number}",
                     contact_id=inv.contact_id, tax_base=sign * inv.subtotal)

    # Cost of sales
    cogs_total = 0
    for line in inv.lines:
        if not line.item_id:
            continue
        item = db.get(Item, line.item_id)
        if item is None or item.item_type != STOCK_ITEM or not item.track_stock:
            continue
        inv_acc = item.inventory_account_id or sys_account(db, "INVENTORY").id
        cogs_acc = item.cogs_account_id or sys_account(db, "COGS").id
        if is_credit:
            # Goods coming back in, at the current average cost
            cost = _r(Decimal(costing.unit_cost(item)) * Decimal(line.qty) / 1000)
            costing.receive(db, item, line.qty, cost, inv.date, "CREDIT_NOTE",
                            inv.id, inv.number, f"Return — {inv.number}",
                            location=inv.location_id, batch_no=line.batch_no,
                            expiry_date=line.expiry_date,
                            serials=_serials(line), supplier_id=inv.contact_id)
            draft.debit(inv_acc, cost, f"Stock returned — {item.name}", item_id=item.id)
            draft.credit(cogs_acc, cost, f"Cost of return — {item.name}", item_id=item.id)
        else:
            _move, cost = costing.issue(db, item, line.qty, inv.date, "INVOICE",
                                        inv.id, inv.number, f"Sale — {inv.number}",
                                        location=inv.location_id,
                                        batch=_batch_id(db, item, line),
                                        serials=_serials(line),
                                        customer_id=inv.contact_id)
            draft.debit(cogs_acc, cost, f"Cost of sale — {item.name}", item_id=item.id)
            draft.credit(inv_acc, cost, f"Stock issued — {item.name}", item_id=item.id)
        line.cogs_amount = cost
        cogs_total += cost

    inv.cogs_total = cogs_total
    entry = post_entry(db, draft, user=user)
    inv.journal_entry_id = entry.id
    inv.status = POSTED
    inv.posted_at = clock.now()
    db.flush()

    # Put it in the e-invoicing queue if this company files. Only queued here,
    # never sent: posting happens in bulk imports, in recurring runs and at a
    # till, and none of those may be made to wait on somebody else's server.
    # Sending is the caller's business — interactively from the invoice screen,
    # or from the queue.
    from . import einvoice

    einvoice.enqueue(db, inv)
    return entry


def void_invoice(db: Session, inv: Invoice, on: Date | None = None, user: User | None = None) -> None:
    if inv.status == VOID:
        raise PostingError(f"{inv.number} is already void.")
    if inv.amount_paid:
        raise PostingError(
            f"{inv.number} has payments allocated to it. "
            "Remove or void those receipts first, then void the invoice."
        )
    on = on or inv.date
    if inv.journal_entry_id:
        entry = db.get(JournalEntry, inv.journal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, on=on, user=user, memo=f"Void of {inv.number}")
    # Put any stock back the way it was. The list is materialised first so the
    # reversing moves this creates are not themselves reversed.
    originals = [
        m
        for m in db.scalars(
            select(StockMove).where(
                StockMove.doc_type.in_(("INVOICE", "CREDIT_NOTE")),
                StockMove.doc_id == inv.id,
            )
        ).all()
        if not m.memo.startswith("Reversal")
    ]
    for move in originals:
        costing.reverse_move(db, move, on, f"Reversal — void of {inv.number}")
    inv.status = VOID
    db.flush()


# --------------------------------------------------------------------------
# Posting purchase documents
# --------------------------------------------------------------------------


def post_bill(db: Session, bill: Bill, user: User | None = None) -> JournalEntry:
    if bill.status not in (DRAFT,):
        raise PostingError(f"{bill.number} is already {bill.status.lower()} — it cannot be posted again.")
    if not bill.lines:
        raise PostingError("Add at least one line before posting.")

    recalc_bill(db, bill)
    if bill.total == 0:
        raise PostingError("This document totals zero — there is nothing to post.")

    is_debit_note = bill.doc_type == "DEBIT_NOTE"
    sign = -1 if is_debit_note else 1

    ap = sys_account(db, "AP")
    vat_in = sys_account(db, "VAT_INPUT")
    default_exp = sys_account(db, "PURCHASES")
    label = "Debit note" if is_debit_note else "Bill"

    draft = EntryDraft(
        date=bill.date,
        memo=f"{label} {bill.number} — {bill.contact.name}",
        reference=bill.vendor_invoice_no or bill.number,
        source="BILL",
        source_id=bill.id,
    )

    draft.signed(ap, -sign * bill.total, f"{label} {bill.number}", contact_id=bill.contact_id)

    for line in bill.lines:
        item = db.get(Item, line.item_id) if line.item_id else None
        if item is not None and item.item_type == STOCK_ITEM and item.track_stock:
            # Stock always capitalises into inventory — it is an asset until it
            # is sold. This overrides whatever account is on the line: putting
            # a stock purchase in Purchases instead would leave the stock
            # records and the inventory account permanently apart, and the
            # stock valuation report would never tie again.
            acc_id = item.inventory_account_id or sys_account(db, "INVENTORY").id
        else:
            acc_id = line.account_id or (item.purchase_account_id if item else None) \
                or default_exp.id
        draft.signed(
            acc_id,
            sign * line.net,
            line.description[:255],
            contact_id=bill.contact_id,
            item_id=line.item_id,
            project_id=line.project_id,
            tax_code_id=line.tax_code_id,
            tax_base=sign * line.net,
        )

        # Stock movement at cost
        if item is not None and item.item_type == STOCK_ITEM and item.track_stock and line.qty:
            if is_debit_note:
                _m, _c = costing.issue(db, item, line.qty, bill.date, "DEBIT_NOTE",
                                       bill.id, bill.number,
                                       f"Return to supplier — {bill.number}",
                                       location=bill.location_id,
                                       batch=_batch_id(db, item, line),
                                       serials=_serials(line))
            else:
                costing.receive(db, item, line.qty, line.net, bill.date, "BILL",
                                bill.id, bill.number, f"Purchase — {bill.number}",
                                location=bill.location_id, batch_no=line.batch_no,
                                expiry_date=line.expiry_date, serials=_serials(line),
                                supplier_id=bill.contact_id)

    if bill.vat_total:
        company = db.get(Company, 1)
        # A business that is not VAT registered cannot recover input VAT, so it
        # is expensed rather than carried as an asset.
        target = vat_in if (company and company.is_vat_registered) else sys_account(db, "VAT_IRRECOVERABLE")
        draft.signed(target, sign * bill.vat_total, f"Input VAT — {bill.number}",
                     contact_id=bill.contact_id, tax_base=sign * bill.subtotal)

    entry = post_entry(db, draft, user=user)
    bill.journal_entry_id = entry.id
    bill.status = POSTED
    bill.posted_at = clock.now()
    db.flush()
    return entry


def void_bill(db: Session, bill: Bill, on: Date | None = None, user: User | None = None) -> None:
    if bill.status == VOID:
        raise PostingError(f"{bill.number} is already void.")
    if bill.amount_paid:
        raise PostingError(
            f"{bill.number} has payments allocated to it. "
            "Remove or void those payments first, then void the bill."
        )
    on = on or bill.date
    if bill.journal_entry_id:
        entry = db.get(JournalEntry, bill.journal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, on=on, user=user, memo=f"Void of {bill.number}")
    originals = [
        m
        for m in db.scalars(
            select(StockMove).where(
                StockMove.doc_type.in_(("BILL", "DEBIT_NOTE")),
                StockMove.doc_id == bill.id,
            )
        ).all()
        if not m.memo.startswith("Reversal")
    ]
    for move in originals:
        costing.reverse_move(db, move, on, f"Reversal — void of {bill.number}")
    bill.status = VOID
    db.flush()


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------


def refresh_invoice_status(inv: Invoice) -> None:
    if inv.status in (DRAFT, VOID):
        return
    if inv.amount_paid <= 0:
        inv.status = POSTED
    elif inv.amount_paid >= inv.total:
        inv.status = PAID
    else:
        inv.status = PART_PAID


def refresh_bill_status(bill: Bill) -> None:
    if bill.status in (DRAFT, VOID):
        return
    if bill.amount_paid <= 0:
        bill.status = POSTED
    elif bill.amount_paid >= bill.total:
        bill.status = PAID
    else:
        bill.status = PART_PAID


def convert_quote(db: Session, quote: Invoice, user: User | None = None) -> Invoice:
    """Turn an accepted quotation into a draft invoice."""
    if quote.doc_type != "QUOTE":
        raise PostingError("Only a quotation can be converted to an invoice.")
    inv = Invoice(
        number=next_number(db, "INVOICE"),
        doc_type="INVOICE",
        contact_id=quote.contact_id,
        date=Date.today(),
        due_date=due_date_for(db, Date.today(), quote.contact),
        reference=quote.number,
        po_number=quote.po_number,
        status=DRAFT,
        wht_code_id=quote.wht_code_id,
        memo=quote.memo,
        terms=quote.terms,
        converted_from_id=quote.id,
        created_by_id=user.id if user else None,
    )
    db.add(inv)
    db.flush()
    for line in quote.lines:
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                line_no=line.line_no,
                item_id=line.item_id,
                description=line.description,
                qty=line.qty,
                unit_price=line.unit_price,
                discount_pct=line.discount_pct,
                account_id=line.account_id,
                tax_code_id=line.tax_code_id,
                batch_no=line.batch_no,
                expiry_date=line.expiry_date,
                serials=line.serials,
            )
        )
    db.flush()
    db.refresh(inv)
    recalc_invoice(db, inv)
    quote.status = "CONVERTED"
    db.flush()
    return inv


def credit_note_from(db: Session, inv: Invoice, user: User | None = None) -> Invoice:
    """Create a draft credit note mirroring an existing invoice."""
    cn = Invoice(
        number=next_number(db, "CREDIT_NOTE"),
        doc_type="CREDIT_NOTE",
        contact_id=inv.contact_id,
        date=Date.today(),
        reference=inv.number,
        status=DRAFT,
        wht_code_id=inv.wht_code_id,
        memo=f"Credit note against invoice {inv.number}",
        credit_of_id=inv.id,
        created_by_id=user.id if user else None,
    )
    db.add(cn)
    db.flush()
    for line in inv.lines:
        db.add(
            InvoiceLine(
                invoice_id=cn.id,
                line_no=line.line_no,
                item_id=line.item_id,
                description=line.description,
                qty=line.qty,
                unit_price=line.unit_price,
                discount_pct=line.discount_pct,
                account_id=line.account_id,
                tax_code_id=line.tax_code_id,
                batch_no=line.batch_no,
                expiry_date=line.expiry_date,
                serials=line.serials,
            )
        )
    db.flush()
    db.refresh(cn)
    recalc_invoice(db, cn)
    return cn


def debit_note_from(db: Session, bill: Bill, user: User | None = None) -> Bill:
    dn = Bill(
        number=next_number(db, "DEBIT_NOTE"),
        doc_type="DEBIT_NOTE",
        contact_id=bill.contact_id,
        date=Date.today(),
        reference=bill.number,
        status=DRAFT,
        wht_code_id=bill.wht_code_id,
        memo=f"Debit note against bill {bill.number}",
        debit_of_id=bill.id,
        created_by_id=user.id if user else None,
    )
    db.add(dn)
    db.flush()
    for line in bill.lines:
        db.add(
            BillLine(
                bill_id=dn.id,
                line_no=line.line_no,
                item_id=line.item_id,
                description=line.description,
                qty=line.qty,
                unit_price=line.unit_price,
                discount_pct=line.discount_pct,
                account_id=line.account_id,
                tax_code_id=line.tax_code_id,
                batch_no=line.batch_no,
                expiry_date=line.expiry_date,
                serials=line.serials,
            )
        )
    db.flush()
    db.refresh(dn)
    recalc_bill(db, dn)
    return dn
