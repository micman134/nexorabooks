"""Customers and suppliers."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from sqlalchemy import func, or_, select

from ..models import Bill, Contact, Invoice, Payment
from ..security import P_ENTRY, P_VIEW
from ..services import reports
from ..services.posting import audit, next_number
from ..services.tax import wht_codes
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_int,
    parse_money,
    redirect,
    user_of,
)

router = APIRouter(prefix="/contacts")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    kind = request.query_params.get("kind", "customer")
    show_inactive = parse_bool(request.query_params.get("inactive"))

    stmt = select(Contact)
    if kind == "customer":
        stmt = stmt.where(Contact.is_customer.is_(True))
    elif kind == "vendor":
        stmt = stmt.where(Contact.is_vendor.is_(True))
    if not show_inactive:
        stmt = stmt.where(Contact.is_active.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Contact.name.ilike(like), Contact.code.ilike(like),
                Contact.phone.ilike(like), Contact.email.ilike(like), Contact.tin.ilike(like))
        )
    contacts = list(db.scalars(stmt.order_by(Contact.name)))

    # Outstanding balance per contact
    ar_rows, _, _ = reports.aging(db, date.today(), receivable=True)
    ap_rows, _, _ = reports.aging(db, date.today(), receivable=False)
    ar = {r.contact.id: r.total for r in ar_rows}
    ap = {r.contact.id: r.total for r in ap_rows}

    return render(
        request, "contacts/index.html",
        contacts=contacts, q=q, kind=kind, show_inactive=show_inactive, ar=ar, ap=ap,
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    kind = request.query_params.get("kind", "customer")
    contact = Contact(
        code="",
        name="",
        is_customer=(kind != "vendor"),
        is_vendor=(kind == "vendor"),
        payment_terms_days=request.state.company.default_payment_terms_days
        if request.state.company else 30,
    )
    return render(request, "contacts/form.html", contact=contact,
                  wht_codes=wht_codes(db), is_new=True)


@router.get("/{contact_id}")
def detail(request: Request, contact_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    contact = db.get(Contact, contact_id)
    if contact is None:
        return redirect("/contacts")

    invoices = list(
        db.scalars(
            select(Invoice).where(Invoice.contact_id == contact_id)
            .order_by(Invoice.date.desc(), Invoice.id.desc()).limit(50)
        )
    )
    bills = list(
        db.scalars(
            select(Bill).where(Bill.contact_id == contact_id)
            .order_by(Bill.date.desc(), Bill.id.desc()).limit(50)
        )
    )
    payments = list(
        db.scalars(
            select(Payment).where(Payment.contact_id == contact_id)
            .order_by(Payment.date.desc(), Payment.id.desc()).limit(50)
        )
    )
    outstanding_ar = sum(i.balance_due for i in invoices if i.status in ("POSTED", "PART_PAID"))
    outstanding_ap = sum(b.balance_due for b in bills if b.status in ("POSTED", "PART_PAID"))

    from ..services import attachments as A

    return render(
        request, "contacts/detail.html",
        contact=contact, invoices=invoices, bills=bills, payments=payments,
        files=A.list_for(db, "CONTACT", contact.id),
        outstanding_ar=outstanding_ar, outstanding_ap=outstanding_ap,
    )


@router.get("/{contact_id}/edit")
def edit(request: Request, contact_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    contact = db.get(Contact, contact_id)
    if contact is None:
        return redirect("/contacts")
    return render(request, "contacts/form.html", contact=contact,
                  wht_codes=wht_codes(db), is_new=False)


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    cid = parse_id(form.get("id"))

    contact = db.get(Contact, cid) if cid else None
    is_new = contact is None
    if is_new:
        contact = Contact(code=form.get("code") or next_number(db, "CONTACT"))
        db.add(contact)

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "A name is required.", "danger")
        return redirect("/contacts/new")

    if form.get("code"):
        contact.code = form.get("code").strip()
    contact.name = name
    contact.contact_type = form.get("contact_type") or "COMPANY"
    contact.is_customer = parse_bool(form.get("is_customer"))
    contact.is_vendor = parse_bool(form.get("is_vendor"))
    if not contact.is_customer and not contact.is_vendor:
        contact.is_customer = True
    contact.tin = (form.get("tin") or "").strip()
    contact.rc_number = (form.get("rc_number") or "").strip()
    contact.contact_person = (form.get("contact_person") or "").strip()
    contact.email = (form.get("email") or "").strip()
    contact.phone = (form.get("phone") or "").strip()
    contact.address = (form.get("address") or "").strip()
    contact.city = (form.get("city") or "").strip()
    contact.state = (form.get("state") or "").strip()
    contact.payment_terms_days = parse_int(form.get("payment_terms_days"), 30)
    contact.credit_limit = parse_money(form.get("credit_limit"))
    contact.default_wht_code_id = parse_id(form.get("default_wht_code_id"))
    contact.is_small_company = parse_bool(form.get("is_small_company"))
    contact.withholds_vat = parse_bool(form.get("withholds_vat"))
    contact.bank_name = (form.get("bank_name") or "").strip()
    contact.bank_account_no = (form.get("bank_account_no") or "").strip()
    contact.notes = form.get("notes") or ""
    contact.is_active = parse_bool(form.get("is_active"))

    db.flush()
    audit(db, user, "CREATE" if is_new else "UPDATE", "Contact", contact.id,
          detail=contact.name, ip=client_ip(request))
    db.commit()
    flash(request, f"{contact.name} saved.")
    return redirect(f"/contacts/{contact.id}")


@router.post("/{contact_id}/archive")
def archive(request: Request, contact_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    contact = db.get(Contact, contact_id)
    if contact:
        contact.is_active = not contact.is_active
        audit(db, user, "ARCHIVE" if not contact.is_active else "RESTORE",
              "Contact", contact.id, detail=contact.name, ip=client_ip(request))
        db.commit()
        flash(request, f"{contact.name} {'archived' if not contact.is_active else 'restored'}.")
    return redirect(f"/contacts/{contact_id}")


@router.get("/{contact_id}/statement/pdf")
def contact_statement_pdf(request: Request, contact_id: int):
    from fastapi.responses import Response

    from ..services import pdfdocs

    need(request, P_VIEW)
    db = db_of(request)
    # The same window the statement screen used, so the PDF matches what was
    # on screen when the button was pressed.
    end = parse_date(request.query_params.get("end"), date.today())
    start = parse_date(request.query_params.get("start"), end - timedelta(days=90))
    contact, opening, rows, closing = reports.statement(db, contact_id, start, end)
    if contact is None:
        return redirect("/contacts")
    data = pdfdocs.statement_pdf(db, contact, rows, opening, closing, start, end,
                                 slug=request.state.company_slug)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="Statement {contact.name}.pdf"'})


@router.get("/{contact_id}/statement")
def contact_statement(request: Request, contact_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    end = parse_date(request.query_params.get("end"), date.today())
    start = parse_date(request.query_params.get("start"), end - timedelta(days=90))
    contact, opening, rows, closing = reports.statement(db, contact_id, start, end)
    return render(
        request, "contacts/statement.html",
        contact=contact, opening=opening, rows=rows, closing=closing, start=start, end=end,
    )
