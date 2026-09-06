"""The till screens: opening it, selling from it, and counting it at the end."""
from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from ..models import (
    TENDER_CASH,
    TENDER_KINDS,
    TILL_CLOSED,
    BankAccount,
    Contact,
    Invoice,
    Location,
    TillSession,
)
from ..money import to_major
from ..security import P_ENTRY, P_VIEW
from ..services import pos
from ..services.posting import PostingError
from ._common import (
    client_ip,
    db_of,
    need,
    parse_id,
    parse_money,
    redirect,
    user_of,
)

router = APIRouter(prefix="/pos")

#: Notes people actually hold, for counting the drawer. The first list that
#: matches the currency is used; anything else gets the last one.
NOTES = {
    "NGN": [1000, 500, 200, 100, 50, 20, 10, 5],
    "GHS": [200, 100, 50, 20, 10, 5, 2, 1],
    "KES": [1000, 500, 200, 100, 50, 20, 10, 5],
    "ZAR": [200, 100, 50, 20, 10, 5, 2, 1],
    "USD": [100, 50, 20, 10, 5, 1],
    "EUR": [200, 100, 50, 20, 10, 5, 2, 1],
    "GBP": [50, 20, 10, 5, 2, 1],
    "*": [1000, 500, 200, 100, 50, 20, 10, 5, 1],
}


def _where_card_money_goes(db, session) -> list[BankAccount]:
    """Accounts a card or transfer can land in, the likeliest one first.

    A card payment does not go into the drawer, so the bank comes first here —
    the opposite of the list used for choosing which drawer a till is.
    """
    rows = [b for b in pos.till_accounts(db) if b.id != session.cash_account_id]
    return sorted(rows, key=lambda b: (b.account_type == "CASH",
                                       not b.is_default, b.sort, b.name))


def _notes_for() -> list[int]:
    from .. import currency as currency_mod
    return NOTES.get(currency_mod.active().code, NOTES["*"])


# --------------------------------------------------------------------------
# The till
# --------------------------------------------------------------------------


@router.get("")
def till(request: Request):
    from ..main import render

    user = need(request, P_VIEW)
    db = db_of(request)
    session = pos.session_for(db, user)

    if session is None:
        return render(
            request, "pos/open.html",
            accounts=pos.till_accounts(db),
            others=pos.open_sessions(db),
            locations=list(db.scalars(select(Location)
                                      .where(Location.is_active.is_(True))
                                      .order_by(Location.sort, Location.name))),
        )

    sold = parse_id(request.query_params.get("sold"))
    return render(
        request, "pos/till.html",
        session=session,
        figures=pos.takings(db, session),
        accounts=_where_card_money_goes(db, session),
        tender_kinds=TENDER_KINDS,
        customers=list(db.scalars(select(Contact)
                                  .where(Contact.is_customer.is_(True),
                                         Contact.is_active.is_(True))
                                  .order_by(Contact.name))),
        recent=pos.sales_of(db, session)[:8],
        just_sold=db.get(Invoice, sold) if sold else None,
    )


@router.post("/open")
async def open_till(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    account = db.get(BankAccount, parse_id(form.get("cash_account_id")) or 0)
    try:
        session = pos.open_session(
            db, user, account,
            name=(form.get("name") or "Till 1").strip(),
            opening_float=parse_money(form.get("opening_float")),
            location_id=parse_id(form.get("location_id")),
        )
    except (pos.TillError, PostingError) as exc:
        flash(request, str(exc), "danger")
        return redirect("/pos")

    db.commit()
    flash(request, f"{session.name} is open. Opening float "
                   f"{_money(session.opening_float)}.")
    return redirect("/pos")


def _money(minor: int) -> str:
    from ..money import fmt
    return fmt(minor)


@router.get("/search")
def search(request: Request):
    """What the till screen calls as somebody types or scans."""
    need(request, P_VIEW)
    db = db_of(request)
    found = pos.search(db, request.query_params.get("q", ""))
    return JSONResponse({
        "items": [
            {
                "id": item.id,
                "code": item.code,
                "name": item.name,
                "unit": item.unit,
                "barcode": item.barcode or "",
                "price": item.sale_price,
                "price_major": str(to_major(item.sale_price)),
                "on_hand": item.qty_on_hand / 1000,
                "tracked": bool(item.track_stock),
            }
            for item in found
        ]
    })


@router.post("/sell")
async def sell(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    session = pos.session_for(db, user)
    form = await request.form()

    try:
        lines = _lines_from(form.get("lines"))
        tenders = _tenders_from(form.get("tenders"))
        invoice = pos.ring_up(db, session, lines, tenders, user=user,
                              contact_id=parse_id(form.get("contact_id")))
    except (pos.TillError, PostingError, ValueError) as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return redirect("/pos")

    warnings = pos.short_of_stock(db, lines)
    db.commit()
    if warnings:
        flash(request, "Sold, but the stock record disagrees — " +
              " ".join(warnings) + " Count these and correct the records.",
              "warning")
    return redirect(f"/pos?sold={invoice.id}")


def _lines_from(raw) -> list[pos.Line]:
    rows = json.loads(raw or "[]")
    lines = []
    for row in rows:
        qty = int(round(float(row.get("qty") or 0) * 1000))
        if qty <= 0:
            continue
        lines.append(pos.Line(
            item_id=int(row["item_id"]) if row.get("item_id") else None,
            description=str(row.get("description") or "")[:300],
            qty=qty,
            unit_price=parse_money(str(row.get("price") or "0")),
            discount_pct=str(row.get("discount") or "0"),
        ))
    if not lines:
        raise pos.TillError("There is nothing in this sale.")
    return lines


def _tenders_from(raw) -> list[pos.Tender]:
    rows = json.loads(raw or "[]")
    tenders = []
    for row in rows:
        amount = parse_money(str(row.get("amount") or "0"))
        if amount <= 0:
            continue
        tenders.append(pos.Tender(
            kind=str(row.get("kind") or TENDER_CASH).upper()[:12],
            amount=amount,
            tendered=parse_money(str(row.get("tendered") or "0")) or amount,
            bank_account_id=parse_id(row.get("account_id")),
            reference=str(row.get("reference") or "")[:60],
        ))
    if not tenders:
        raise pos.TillError("Say how the customer paid.")
    return tenders


# --------------------------------------------------------------------------
# The receipt
# --------------------------------------------------------------------------


@router.get("/sale/{invoice_id}/receipt")
def receipt(request: Request, invoice_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return redirect("/pos")
    return render(request, "pos/receipt.html", inv=invoice,
                  tenders=pos.tenders_for(db, invoice.id))


@router.get("/sale/{invoice_id}/receipt.pdf")
def receipt_pdf(request: Request, invoice_id: int):
    need(request, P_VIEW)
    db = db_of(request)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return redirect("/pos")

    from ..services import posreceipt
    body = posreceipt.render(db, invoice, pos.tenders_for(db, invoice.id))
    return Response(body, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="receipt-{invoice.number}.pdf"'})


@router.post("/sale/{invoice_id}/refund")
def refund(request: Request, invoice_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    session = pos.session_for(db, user)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        return redirect("/pos")

    try:
        note = pos.refund(db, session, invoice, user=user)
    except (pos.TillError, PostingError) as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return redirect("/pos")

    db.commit()
    flash(request, f"{invoice.number} refunded by {note.number}. "
                   f"{_money(note.total)} out of the drawer.")
    return redirect("/pos")


# --------------------------------------------------------------------------
# Counting the drawer
# --------------------------------------------------------------------------


@router.get("/close")
def close_form(request: Request):
    from ..main import render

    user = need(request, P_ENTRY)
    db = db_of(request)
    session = pos.session_for(db, user)
    if session is None:
        return redirect("/pos")

    return render(
        request, "pos/close.html",
        session=session,
        figures=pos.takings(db, session),
        notes=_notes_for(),
        banks=[b for b in pos.till_accounts(db) if b.id != session.cash_account_id],
        sales=pos.sales_of(db, session),
    )


@router.post("/close")
async def close(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    session = pos.session_for(db, user)
    if session is None:
        return redirect("/pos")
    form = await request.form()

    try:
        pos.close_session(
            db, session,
            counted=parse_money(form.get("counted")),
            user=user,
            banked=parse_money(form.get("banked")),
            bank_account_id=parse_id(form.get("bank_account_id")),
            notes=(form.get("notes") or "").strip(),
        )
    except (pos.TillError, PostingError) as exc:
        db.rollback()
        flash(request, str(exc), "danger")
        return redirect("/pos/close")

    db.commit()
    if session.difference:
        flash(request,
              f"{session.name} closed. The drawer is "
              f"{_money(abs(session.difference))} "
              f"{'short' if session.is_short else 'over'} — that difference is "
              "now on the profit and loss where it can be seen.",
              "warning")
    else:
        flash(request, f"{session.name} closed and the drawer balances exactly.")
    return redirect(f"/pos/sessions/{session.id}")


@router.get("/sessions")
def sessions(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    rows = list(db.scalars(select(TillSession).order_by(TillSession.id.desc())
                           .limit(200)))
    return render(request, "pos/sessions.html", rows=rows,
                  short=sum(1 for r in rows if r.status == TILL_CLOSED and r.is_short))


@router.get("/sessions/{session_id}")
def one_session(request: Request, session_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    session = db.get(TillSession, session_id)
    if session is None:
        return redirect("/pos/sessions")

    return render(request, "pos/session.html", session=session,
                  figures=pos.takings(db, session),
                  sales=pos.sales_of(db, session),
                  tender_kinds=TENDER_KINDS,
                  today=date.today(), ip=client_ip(request))
