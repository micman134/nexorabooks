"""Sales: quotations, invoices, credit notes and customer receipts."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import (
    DRAFT,
    POSTED,
    RECEIPT,
    VOID,
    Account,
    BankAccount,
    Contact,
    Invoice,
    InvoiceLine,
    Item,
    Location,
    JournalEntry,
    Payment,
    PaymentAllocation,
    TaxCode,
)
from ..money import fmt
from ..security import P_DELETE, P_ENTRY, P_VIEW, P_VOID
from ..services import attachments as A
from ..services import cash, deletion, documents
from ..services.posting import PostingError, audit, next_number, sys_account
from ..services.tax import vat_codes, wht_codes
from ..services import projects as PROJ
from ._common import (
    client_ip,
    db_of,
    need,
    parse_date,
    parse_id,
    parse_money,
    parse_qty,
    redirect,
    user_of,
)

router = APIRouter(prefix="/sales")


def _depth_fields(form, i: int) -> dict:
    """Batch number, expiry and serials typed on a line, if the item needs them."""
    def at(key):
        values = form.getlist(key)
        return values[i] if i < len(values) else None

    return dict(
        batch_no=(at("line_batch") or "").strip(),
        expiry_date=parse_date(at("line_expiry"), None) if at("line_expiry") else None,
        serials=(at("line_serials") or "").strip(),
    )


DOC_LABELS = {
    "INVOICE": ("Invoice", "invoices"),
    "QUOTE": ("Quotation", "quotes"),
    "CREDIT_NOTE": ("Credit note", "credit-notes"),
}
SLUG_TO_TYPE = {v[1]: k for k, v in DOC_LABELS.items()}


def _sales_accounts(db):
    return list(
        db.scalars(
            select(Account)
            .where(Account.type.in_(("INCOME",)), Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


def _form_context(request, db, inv):
    return dict(
        inv=inv,
        customers=list(
            db.scalars(
                select(Contact)
                .where(Contact.is_customer.is_(True), Contact.is_active.is_(True))
                .order_by(Contact.name)
            )
        ),
        items=list(
            db.scalars(select(Item).where(Item.is_active.is_(True)).order_by(Item.name))
        ),
        accounts=_sales_accounts(db),
        vat_codes=vat_codes(db),
        projects=PROJ.choices(db),
        wht_codes=wht_codes(db),
        locations=list(db.scalars(
            select(Location).where(Location.is_active.is_(True))
            .order_by(Location.sort, Location.name)
        )),
        needs_depth=any(
            i.track_batches or i.track_serials
            for i in db.scalars(select(Item).where(Item.is_active.is_(True)))
        ),
        label=DOC_LABELS[inv.doc_type][0],
        slug=DOC_LABELS[inv.doc_type][1],
    )


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


@router.get("/{slug}")
def index(request: Request, slug: str):
    from ..main import render

    if slug not in SLUG_TO_TYPE:
        return redirect("/sales/invoices")
    need(request, P_VIEW)
    db = db_of(request)
    doc_type = SLUG_TO_TYPE[slug]

    status = request.query_params.get("status", "")
    q = (request.query_params.get("q") or "").strip()
    stmt = select(Invoice).where(Invoice.doc_type == doc_type)
    if status:
        stmt = stmt.where(Invoice.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Contact, Invoice.contact_id == Contact.id).where(
            or_(Invoice.number.ilike(like), Contact.name.ilike(like),
                Invoice.reference.ilike(like), Invoice.po_number.ilike(like))
        )
    docs = list(db.scalars(stmt.order_by(Invoice.date.desc(), Invoice.id.desc()).limit(400)))

    totals = {
        "count": len(docs),
        "value": sum(d.total for d in docs if d.status != VOID),
        "outstanding": sum(d.balance_due for d in docs if d.status in (POSTED, "PART_PAID")),
    }
    label, _ = DOC_LABELS[doc_type]
    return render(
        request, "sales/index.html",
        docs=docs, slug=slug, label=label, status=status, q=q, totals=totals,
    )


# --------------------------------------------------------------------------
# Create / edit
# --------------------------------------------------------------------------


@router.get("/{slug}/new")
def new(request: Request, slug: str):
    from ..main import render

    if slug not in SLUG_TO_TYPE:
        return redirect("/sales/invoices")
    need(request, P_ENTRY)
    db = db_of(request)
    doc_type = SLUG_TO_TYPE[slug]
    company = request.state.company

    inv = Invoice(
        doc_type=doc_type,
        number="(assigned on save)",
        date=date.today(),
        status=DRAFT,
        terms=company.invoice_terms if company else "",
    )
    inv.lines = [InvoiceLine(line_no=i, qty=1000) for i in range(1, 4)]
    cid = parse_id(request.query_params.get("contact"))
    if cid:
        inv.contact_id = cid
    return render(request, "sales/form.html", is_new=True, **_form_context(request, db, inv))


@router.get("/{slug}/{doc_id}")
def detail(request: Request, slug: str, doc_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    if inv is None:
        return redirect(f"/sales/{slug}")
    entry = db.get(JournalEntry, inv.journal_entry_id) if inv.journal_entry_id else None
    allocs = list(
        db.scalars(select(PaymentAllocation).where(PaymentAllocation.invoice_id == inv.id))
    )
    from ..services import einvoice as ei

    ei_settings = ei.load(request.state.company_slug)
    return render(
        request, "sales/detail.html",
        inv=inv, entry=entry, allocations=allocs,
        files=A.list_for(db, inv.doc_type, inv.id),
        label=DOC_LABELS[inv.doc_type][0], slug=DOC_LABELS[inv.doc_type][1],
        delete_refused=deletion.why_not(db, inv),
        ei=ei.status_of(db, inv),
        ei_on=ei_settings.on,
        ei_wanted=ei.needs_clearance(inv, ei_settings),
    )


@router.get("/{slug}/{doc_id}/edit")
def edit(request: Request, slug: str, doc_id: int):
    from ..main import flash, render

    need(request, P_ENTRY)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    if inv is None:
        return redirect(f"/sales/{slug}")
    if inv.status not in (DRAFT, "SENT"):
        flash(request, f"{inv.number} has been posted. Void it and raise a new one, "
                       "or issue a credit note.", "warning")
        return redirect(f"/sales/{slug}/{doc_id}")
    while len(inv.lines) < 3:
        inv.lines.append(InvoiceLine(line_no=len(inv.lines) + 1, qty=1000))
    return render(request, "sales/form.html", is_new=False, **_form_context(request, db, inv))


@router.post("/{slug}/save")
async def save(request: Request, slug: str):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    doc_type = SLUG_TO_TYPE.get(slug, "INVOICE")
    doc_id = parse_id(form.get("id"))

    inv = db.get(Invoice, doc_id) if doc_id else None
    is_new = inv is None
    if not is_new and inv.status not in (DRAFT, "SENT"):
        flash(request, f"{inv.number} is already posted and cannot be edited.", "danger")
        return redirect(f"/sales/{slug}/{inv.id}")

    contact_id = parse_id(form.get("contact_id"))
    if not contact_id:
        flash(request, "Choose a customer.", "danger")
        return redirect(f"/sales/{slug}/new")

    # Look the customer up before the new row exists, so nothing half-built is
    # ever flushed to the database.
    contact = db.get(Contact, contact_id)
    if is_new:
        seq = {"INVOICE": "INVOICE", "QUOTE": "QUOTE", "CREDIT_NOTE": "CREDIT_NOTE"}[doc_type]
        inv = Invoice(number=next_number(db, seq), doc_type=doc_type, status=DRAFT,
                      created_by_id=user.id, contact_id=contact_id,
                      date=parse_date(form.get("date")))
        db.add(inv)

    inv.contact_id = contact_id
    inv.date = parse_date(form.get("date"))
    inv.due_date = (
        parse_date(form.get("due_date"), documents.due_date_for(db, inv.date, contact))
        if doc_type == "INVOICE"
        else parse_date(form.get("due_date"), inv.date)
    )
    inv.reference = (form.get("reference") or "").strip()
    inv.po_number = (form.get("po_number") or "").strip()
    inv.memo = form.get("memo") or ""
    inv.location_id = parse_id(form.get("location_id"))
    inv.terms = form.get("terms") or ""
    inv.wht_code_id = parse_id(form.get("wht_code_id")) or (
        contact.default_wht_code_id if contact else None
    )
    db.flush()

    # Rebuild the lines from the form
    for old in list(inv.lines):
        db.delete(old)
    db.flush()
    inv.lines = []

    descs = form.getlist("line_description")
    n = 0
    for i in range(len(descs)):
        item_id = parse_id(form.getlist("line_item_id")[i] if i < len(form.getlist("line_item_id")) else None)
        desc = (descs[i] or "").strip()
        qty = parse_qty(form.getlist("line_qty")[i] if i < len(form.getlist("line_qty")) else "0")
        price = parse_money(form.getlist("line_price")[i] if i < len(form.getlist("line_price")) else "0")
        if not desc and not item_id and not qty:
            continue
        if qty == 0 and price == 0:
            continue
        n += 1
        item = db.get(Item, item_id) if item_id else None
        if item and not desc:
            desc = item.name
        line = InvoiceLine(
            invoice_id=inv.id,
            line_no=n,
            item_id=item_id,
            description=desc,
            qty=qty,
            unit_price=price,
            discount_pct=(form.getlist("line_disc")[i] if i < len(form.getlist("line_disc")) else "0") or "0",
            account_id=parse_id(form.getlist("line_account")[i] if i < len(form.getlist("line_account")) else None)
            or (item.sales_account_id if item else None),
            tax_code_id=parse_id(form.getlist("line_tax")[i] if i < len(form.getlist("line_tax")) else None),
            project_id=parse_id(
                form.getlist("line_project")[i]
                if i < len(form.getlist("line_project")) else None
            ),
            **_depth_fields(form, i),
        )
        db.add(line)
        inv.lines.append(line)
    db.flush()

    if n == 0:
        flash(request, "Add at least one line with a quantity and a price.", "danger")
        db.commit()
        return redirect(f"/sales/{slug}/{inv.id}/edit")

    documents.recalc_invoice(db, inv)
    audit(db, user, "CREATE" if is_new else "UPDATE", "Invoice", inv.id,
          detail=f"{inv.number} {fmt(inv.total)}", ip=client_ip(request))

    action = form.get("action", "save")
    if action == "post" and doc_type != "QUOTE":
        try:
            documents.post_invoice(db, inv, user)
            audit(db, user, "POST", "Invoice", inv.id, detail=inv.number, ip=client_ip(request))
            db.commit()
            flash(request, f"{inv.number} posted — {fmt(inv.total)}.")
            _clear_if_wanted(request, db, inv)
            return redirect(f"/sales/{slug}/{inv.id}")
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect(f"/sales/{slug}/{doc_id or ''}".rstrip("/") + ("/edit" if doc_id else "/new"))

    db.commit()
    flash(request, f"{inv.number} saved as a draft.")
    return redirect(f"/sales/{slug}/{inv.id}")


def _clear_if_wanted(request, db, inv):
    """Send a freshly posted invoice for clearance, if this company files.

    Deliberately never raises. An invoice that is posted is posted — a Revenue
    Service that is unreachable must leave the books alone and the document in
    the queue, not roll back a sale.
    """
    from ..main import flash
    from ..services import einvoice as ei

    settings = ei.load(request.state.company_slug)
    if not settings.on or not settings.auto_submit:
        return
    if not ei.needs_clearance(inv, settings):
        return

    record = ei.submit(db, inv)
    db.commit()
    if record.status == ei.EI_CLEARED:
        flash(request,
              ("Rehearsal only — not filed. Reference " if record.was_a_rehearsal
               else "Cleared. Reference ") + record.irn,
              "info" if record.was_a_rehearsal else "success")
    elif record.status == ei.EI_REJECTED:
        flash(request, f"Not accepted: {record.last_error}", "danger")
    elif record.status == ei.EI_FAILED:
        flash(request, f"{record.last_error}", "warning")


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------


@router.post("/{slug}/{doc_id}/post")
def post_doc(request: Request, slug: str, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    try:
        documents.post_invoice(db, inv, user)
        audit(db, user, "POST", "Invoice", inv.id, detail=inv.number, ip=client_ip(request))
        db.commit()
        flash(request, f"{inv.number} posted — {fmt(inv.total)}.")
        _clear_if_wanted(request, db, inv)
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/sales/{slug}/{doc_id}")


@router.post("/{slug}/{doc_id}/void")
async def void_doc(request: Request, slug: str, doc_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    form = await request.form()
    inv = db.get(Invoice, doc_id)
    try:
        documents.void_invoice(db, inv, parse_date(form.get("void_date"), date.today()), user)
        audit(db, user, "VOID", "Invoice", inv.id,
              detail=f"{inv.number} — {form.get('reason', '')}", ip=client_ip(request))
        db.commit()
        flash(request, f"{inv.number} has been voided and reversed in the ledger.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/sales/{slug}/{doc_id}")


@router.post("/{slug}/{doc_id}/delete")
async def delete_doc(request: Request, slug: str, doc_id: int):
    """Delete a document outright. Super administrators only.

    Draft or posted, the result is the same: it is gone, and the ledger is left
    correct rather than merely lighter. See app/services/deletion.py for what
    that costs and what it refuses to do.
    """
    from ..main import flash

    user = need(request, P_DELETE)
    db = db_of(request)
    form = await request.form()
    inv = db.get(Invoice, doc_id)
    if inv is None:
        return redirect(f"/sales/{slug}")

    # Typing the number is the confirmation. A document is destroyed here, and
    # a dialog somebody dismisses by reflex is not consent to that.
    typed = (form.get("confirm") or "").strip().upper()
    if typed != (inv.number or "").upper():
        flash(request, f"Nothing was deleted. To delete {inv.number} you have to "
                       f"type its number exactly.", "danger")
        return redirect(f"/sales/{slug}/{doc_id}")

    number, was_draft = inv.number, inv.status == DRAFT
    try:
        gone = deletion.delete_invoice(db, inv, user)
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect(f"/sales/{slug}/{doc_id}")

    audit(db, user, "DELETE", "Invoice", doc_id,
          detail=f"{number} — deleted outright by a super administrator",
          ip=client_ip(request))
    db.commit()
    if was_draft:
        flash(request, f"Draft {number} deleted.", "warning")
    else:
        flash(request, f"{number} has been deleted. Its ledger entries went with "
                       f"it, so your trial balance still agrees.", "warning")
    return redirect(f"/sales/{slug}")


@router.post("/quotes/{doc_id}/convert")
def convert(request: Request, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    quote = db.get(Invoice, doc_id)
    try:
        inv = documents.convert_quote(db, quote, user)
        audit(db, user, "CONVERT", "Invoice", inv.id,
              detail=f"{quote.number} to {inv.number}", ip=client_ip(request))
        db.commit()
        flash(request, f"{quote.number} converted to draft invoice {inv.number}.")
        return redirect(f"/sales/invoices/{inv.id}/edit")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect(f"/sales/quotes/{doc_id}")


@router.post("/invoices/{doc_id}/credit-note")
def make_credit_note(request: Request, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    cn = documents.credit_note_from(db, inv, user)
    audit(db, user, "CREATE", "Invoice", cn.id,
          detail=f"Credit note {cn.number} for {inv.number}", ip=client_ip(request))
    db.commit()
    flash(request, f"Draft credit note {cn.number} created — adjust the lines and post it.")
    return redirect(f"/sales/credit-notes/{cn.id}/edit")


@router.get("/{slug}/{doc_id}/pdf")
def invoice_pdf(request: Request, slug: str, doc_id: int):
    """The document as a PDF, for filing, emailing or sending on."""
    from fastapi.responses import Response

    from ..services import pdfdocs

    need(request, P_VIEW)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    if inv is None:
        return redirect(f"/sales/{slug}")
    data = pdfdocs.invoice_pdf(db, inv, slug=request.state.company_slug)
    name = f"{pdfdocs.LABELS.get(inv.doc_type, 'Invoice')} {inv.number}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.get("/{slug}/{doc_id}/print")
def print_doc(request: Request, slug: str, doc_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    if inv is None:
        return redirect(f"/sales/{slug}")
    return render(request, "sales/print.html", inv=inv, label=DOC_LABELS[inv.doc_type][0])


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------

receipts = APIRouter(prefix="/receipts")


@receipts.get("")
def receipt_index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    stmt = select(Payment).where(Payment.kind == RECEIPT)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Contact, Payment.contact_id == Contact.id).where(
            or_(Payment.number.ilike(like), Contact.name.ilike(like), Payment.reference.ilike(like))
        )
    pays = list(db.scalars(stmt.order_by(Payment.date.desc(), Payment.id.desc()).limit(300)))
    total = sum(p.amount for p in pays if p.status != VOID)
    return render(request, "sales/receipts.html", payments=pays, q=q, total=total)


@receipts.get("/new")
def receipt_new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    contact_id = parse_id(request.query_params.get("contact"))
    invoice_id = parse_id(request.query_params.get("invoice"))
    if invoice_id and not contact_id:
        inv = db.get(Invoice, invoice_id)
        contact_id = inv.contact_id if inv else None

    open_invoices = []
    if contact_id:
        open_invoices = list(
            db.scalars(
                select(Invoice)
                .where(
                    Invoice.contact_id == contact_id,
                    Invoice.status.in_((POSTED, "PART_PAID")),
                )
                .order_by(Invoice.date)
            )
        )
    return render(
        request, "sales/receipt_form.html",
        customers=list(
            db.scalars(
                select(Contact).where(Contact.is_customer.is_(True), Contact.is_active.is_(True))
                .order_by(Contact.name)
            )
        ),
        banks=list(
            db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.sort))
        ),
        contact_id=contact_id,
        invoice_id=invoice_id,
        open_invoices=open_invoices,
        today=date.today(),
    )


@receipts.post("/save")
async def receipt_save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    contact_id = parse_id(form.get("contact_id"))
    bank_id = parse_id(form.get("bank_account_id"))
    if not contact_id or not bank_id:
        flash(request, "Choose a customer and the account the money went into.", "danger")
        return redirect("/receipts/new")

    pay = Payment(
        number=next_number(db, "RECEIPT"),
        kind=RECEIPT,
        contact_id=contact_id,
        date=parse_date(form.get("date")),
        bank_account_id=bank_id,
        method=form.get("method") or "Bank transfer",
        reference=(form.get("reference") or "").strip(),
        amount=parse_money(form.get("amount")),
        wht_amount=parse_money(form.get("wht_amount")),
        vat_withheld=parse_money(form.get("vat_withheld")),
        discount_amount=parse_money(form.get("discount_amount")),
        bank_charge=parse_money(form.get("bank_charge")),
        memo=form.get("memo") or "",
        created_by_id=user.id,
    )
    db.add(pay)
    db.flush()

    # Explicit allocations, else oldest-first
    ids = form.getlist("alloc_invoice_id")
    amounts = form.getlist("alloc_amount")
    any_alloc = False
    for i, raw_id in enumerate(ids):
        inv_id = parse_id(raw_id)
        amt = parse_money(amounts[i] if i < len(amounts) else "0")
        if inv_id and amt:
            db.add(PaymentAllocation(payment_id=pay.id, invoice_id=inv_id, amount=amt))
            any_alloc = True
    db.flush()
    if any_alloc:
        db.refresh(pay)
        # Spread WHT and discount across the allocated invoices proportionally
        _spread_credits(pay)
    else:
        cash.auto_allocate(db, pay)

    try:
        cash.post_payment(db, pay, user)
        audit(db, user, "POST", "Payment", pay.id,
              detail=f"{pay.number} {fmt(pay.amount)}", ip=client_ip(request))
        db.commit()
        flash(request, f"Receipt {pay.number} recorded — {fmt(pay.amount)} received.")
        return redirect(f"/receipts/{pay.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/receipts/new")


def _spread_credits(pay: Payment) -> None:
    """Attach WHT, VAT withheld at source and discount to the allocated documents."""
    from ..money import allocate

    if not pay.allocations:
        return
    weights = [a.amount for a in pay.allocations]
    if sum(weights) == 0:
        return
    for field_name, total in (("wht_amount", pay.wht_amount),
                              ("vat_withheld", pay.vat_withheld),
                              ("discount", pay.discount_amount)):
        if not total:
            continue
        parts = allocate(total, weights)
        for alloc, part in zip(pay.allocations, parts):
            setattr(alloc, field_name, part)
            alloc.amount = max(alloc.amount - part, 0)


@receipts.get("/{pay_id}")
def receipt_detail(request: Request, pay_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    pay = db.get(Payment, pay_id)
    if pay is None:
        return redirect("/receipts")
    entry = db.get(JournalEntry, pay.journal_entry_id) if pay.journal_entry_id else None
    return render(request, "sales/receipt_detail.html", pay=pay, entry=entry,
                  files=A.list_for(db, "RECEIPT", pay.id))


@receipts.post("/{pay_id}/void")
async def receipt_void(request: Request, pay_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    form = await request.form()
    pay = db.get(Payment, pay_id)
    try:
        cash.void_payment(db, pay, parse_date(form.get("void_date"), date.today()), user)
        audit(db, user, "VOID", "Payment", pay.id, detail=pay.number, ip=client_ip(request))
        db.commit()
        flash(request, f"{pay.number} voided and reversed.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/receipts/{pay_id}")


@receipts.get("/{pay_id}/pdf")
def receipt_pdf(request: Request, pay_id: int):
    from fastapi.responses import Response

    from ..services import pdfdocs

    need(request, P_VIEW)
    db = db_of(request)
    pay = db.get(Payment, pay_id)
    if pay is None:
        return redirect("/receipts")
    data = pdfdocs.receipt_pdf(db, pay, slug=request.state.company_slug)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="Receipt {pay.number}.pdf"'})


@receipts.get("/{pay_id}/print")
def receipt_print(request: Request, pay_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    pay = db.get(Payment, pay_id)
    return render(request, "sales/receipt_print.html", pay=pay)


# ``receipts`` is mounted at the top level (/receipts) by app.main.


# --------------------------------------------------------------------------
# Electronic invoicing
# --------------------------------------------------------------------------


@router.post("/{slug}/{doc_id}/clear")
def clear_doc(request: Request, slug: str, doc_id: int):
    """Send this one invoice for clearance, by hand."""
    from ..main import flash
    from ..services import einvoice as ei

    need(request, P_ENTRY)
    db = db_of(request)
    inv = db.get(Invoice, doc_id)
    if inv is None:
        flash(request, "That document could not be found.", "danger")
        return redirect(f"/sales/{slug}")

    settings = ei.load(request.state.company_slug)
    if not settings.on:
        flash(request, "E-invoicing is switched off for this company. "
                       "Turn it on under Settings, Electronic invoicing.", "warning")
        return redirect(f"/sales/{slug}/{doc_id}")

    record = ei.submit(db, inv, force=True)
    db.commit()
    if record.status == ei.EI_CLEARED:
        flash(request,
              ("Rehearsal only — this invoice has not been filed. Reference "
               if record.was_a_rehearsal else "Cleared. Reference ") + record.irn,
              "info" if record.was_a_rehearsal else "success")
    elif record.status == ei.EI_REJECTED:
        flash(request, f"Not accepted: {record.last_error}", "danger")
    elif record.status == ei.EI_NOT_REQUIRED:
        flash(request, record.last_error or "This document is not one that is filed.", "info")
    else:
        flash(request, record.last_error or "It did not get through — it is queued.",
              "warning")
    return redirect(f"/sales/{slug}/{doc_id}")
