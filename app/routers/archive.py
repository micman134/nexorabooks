"""The filing cabinet: paperwork from before these books, and paperwork beside them.

Two different things are being asked of one screen, and keeping them apart is
most of the design.

Filing is the common case. A drawer of old invoices and receipts gets scanned,
tagged with who it was with and for how much, and becomes searchable. Nothing
touches the ledger, because the money on those documents has usually already
moved — entering a paid 2024 invoice as revenue in 2026 would not be tidying up,
it would be inventing income.

Entering is the other case, and it is a decision made one document at a time.
An old invoice that is *still owed* belongs in the accounts, because otherwise
the business is understating what it is due. Tick the box and the filed document
also becomes a real posted invoice, permanently linked to its scan, so nobody
can later wonder whether the two are the same thing.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import DRAFT, Contact, FiledDocument, Invoice, InvoiceLine
from ..security import P_DELETE, P_ENTRY, P_VIEW, can
from ..services import attachments as A
from ..services import documents
from ..services.posting import PostingError, audit, next_number, sys_account
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_money,
    redirect,
)

router = APIRouter(prefix="/archive")

KINDS = [
    ("INVOICE", "Invoice — something you billed a customer"),
    ("RECEIPT", "Receipt — proof that money moved"),
    ("BILL", "Bill — something a supplier billed you"),
    ("STATEMENT", "Statement or letter"),
    ("OTHER", "Anything else worth keeping"),
]

#: Only an unpaid sales invoice can be turned into a ledger entry from here.
#: A receipt records money that has already moved and a supplier bill belongs
#: in Purchases, where it can be matched to a payment properly.
CAN_GO_IN_THE_BOOKS = ("INVOICE",)


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    search = (request.query_params.get("q") or "").strip()
    kind = (request.query_params.get("kind") or "").strip().upper()

    query = select(FiledDocument).order_by(FiledDocument.doc_date.desc(),
                                           FiledDocument.id.desc())
    if kind:
        query = query.where(FiledDocument.kind == kind)
    if search:
        like = f"%{search}%"
        query = query.where(or_(FiledDocument.party.ilike(like),
                                FiledDocument.reference.ilike(like),
                                FiledDocument.note.ilike(like)))
    rows = list(db.scalars(query.limit(400)))
    files = {r.id: A.list_for(db, "FILED", r.id) for r in rows}
    return render(request, "archive/index.html", rows=rows, files=files,
                  kinds=KINDS, q=search, kind=kind)


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    return render(request, "archive/form.html", kinds=KINDS,
                  today=date.today(), can_post=CAN_GO_IN_THE_BOOKS)


@router.post("/new")
async def create(request: Request):
    from ..main import flash, render

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    kind = (form.get("kind") or "OTHER").strip().upper()
    party = (form.get("party") or "").strip()
    when = parse_date(form.get("doc_date"), None)
    reference = (form.get("reference") or "").strip()
    amount = parse_money(form.get("amount"))
    note = (form.get("note") or "").strip()
    into_books = parse_bool(form.get("into_books"))
    upload = form.get("file")

    if not party:
        flash(request, "Say who the document is with — a customer or a supplier. "
                       "Without a name it cannot be found again.", "danger")
        return redirect("/archive/new")
    if upload is None or not getattr(upload, "filename", ""):
        flash(request, "Choose the scan or photo to file.", "danger")
        return redirect("/archive/new")

    filed = FiledDocument(
        kind=kind, party=party, doc_date=when, reference=reference,
        amount=amount, note=note,
        filed_by_id=user.id if user else None,
        filed_by_name=(user.display_name if user else "") or "",
    )
    db.add(filed)
    db.flush()

    try:
        await A.save_upload(db, request.state.company_slug, "FILED", filed.id,
                            upload, user=user, note=reference)
    except A.AttachmentError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/archive/new")

    made = ""
    if into_books and kind in CAN_GO_IN_THE_BOOKS:
        if amount <= 0:
            db.rollback()
            flash(request, "An amount is needed before this can go into the books — "
                           "otherwise there is nothing to owe.", "danger")
            return redirect("/archive/new")
        try:
            invoice = _into_the_books(db, filed, user)
        except PostingError as e:
            db.rollback()
            flash(request, f"Filed nothing. {e}", "danger")
            return redirect("/archive/new")
        filed.invoice_id = invoice.id
        made = (f" It is also in your books as {invoice.number}, showing as money "
                f"owed to you.")

    audit(db, user, "CREATE", "FiledDocument", filed.id,
          detail=f"{filed.kind_label} — {party} {reference}".strip(),
          ip=client_ip(request))
    db.commit()
    flash(request, f"Filed.{made}")
    return redirect("/archive")


def _into_the_books(db, filed: FiledDocument, user) -> Invoice:
    """Turn a filed invoice into a real, posted one still owed by the customer."""
    name = filed.party.strip()
    contact = db.scalar(select(Contact).where(Contact.name == name))
    if contact is None:
        contact = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True)
        db.add(contact)
        db.flush()
    elif not contact.is_customer:
        contact.is_customer = True

    when = filed.doc_date or date.today()
    invoice = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=contact.id, date=when, due_date=when,
                      status=DRAFT,
                      reference=filed.reference or "",
                      memo=f"Entered from filed paperwork on {date.today():%d %b %Y}.")
    db.add(invoice)
    db.flush()
    db.add(InvoiceLine(
        invoice_id=invoice.id, line_no=1,
        description=(filed.note or filed.reference or "Brought forward from earlier records")[:255],
        qty=1000, unit_price=filed.amount,
        account_id=sys_account(db, "SALES").id))
    db.flush()
    db.refresh(invoice)
    documents.recalc_invoice(db, invoice)
    documents.post_invoice(db, invoice, user)
    return invoice


@router.post("/{filed_id}/delete")
def remove(request: Request, filed_id: int):
    """Take a filed document out. Super administrators only, like every delete."""
    from ..main import flash

    user = need(request, P_DELETE)
    db = db_of(request)
    filed = db.get(FiledDocument, filed_id)
    if filed is None:
        return redirect("/archive")
    if filed.invoice_id:
        flash(request, "This one is also in your books. Delete the invoice it made "
                       "first, so the two never disagree.", "danger")
        return redirect("/archive")

    what = f"{filed.kind_label} — {filed.party} {filed.reference}".strip()
    A.delete_all_for(db, request.state.company_slug, "FILED", filed.id)
    db.delete(filed)
    audit(db, user, "DELETE", "FiledDocument", filed_id, detail=what,
          ip=client_ip(request))
    db.commit()
    flash(request, "Taken out of the archive.", "warning")
    return redirect("/archive")
