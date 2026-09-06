"""Landed cost — spreading freight, duty and clearing over a consignment."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import (
    DRAFT,
    LANDED_BASES,
    POSTED,
    Bill,
    Contact,
    JournalEntry,
    LandedCost,
    LandedCostCharge,
    LandedCostLine,
)
from ..money import fmt_plain
from ..security import P_JOURNAL, P_VIEW, P_VOID
from ..services import landed as L
from ..services.posting import PostingError, audit
from ._common import (
    client_ip,
    db_of,
    need,
    parse_date,
    parse_id,
    parse_money,
    parse_qty,
    redirect,
)

router = APIRouter(prefix="/landed-costs")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    rows = list(db.scalars(select(LandedCost).order_by(LandedCost.date.desc(),
                                                       LandedCost.id.desc())))
    return render(
        request, "landed/index.html",
        rows=rows, bases=LANDED_BASES, today=date.today(),
        posted_total=sum(r.total_charges for r in rows if r.status == POSTED),
    )


@router.post("/create")
async def create(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    lc = L.create(db, parse_date(form.get("date")),
                  basis=form.get("basis") or "VALUE", user=user)
    lc.reference = (form.get("reference") or "").strip()
    lc.note = form.get("note") or ""

    bill_id = parse_id(form.get("bill_id"))
    if bill_id:
        bill = db.get(Bill, bill_id)
        try:
            L.add_bill(db, lc, bill)
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect("/landed-costs")

    audit(db, user, "CREATE", "LandedCost", lc.id, detail=lc.number, ip=client_ip(request))
    db.commit()
    flash(request, f"{lc.number} started. Add the charges to spread.")
    return redirect(f"/landed-costs/{lc.id}")


@router.get("/{lc_id}")
def detail(request: Request, lc_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        return csv_response(
            f"landed-cost-{lc.number}.csv",
            ["Item", "Purchase", "Quantity", "Invoiced", "Share of charges",
             "New unit cost", "Uplift"],
            [[l.description, l.bill.number if l.bill else "", f"{l.qty / 1000:g}",
              fmt_plain(l.value), fmt_plain(l.allocated),
              fmt_plain(l.new_unit_cost), l.uplift_pct] for l in lc.lines],
        )

    return render(
        request, "landed/detail.html",
        lc=lc, bases=LANDED_BASES,
        entry=db.get(JournalEntry, lc.journal_entry_id) if lc.journal_entry_id else None,
        purchases=L.recent_purchases(db),
        charge_accounts=L.charge_accounts(db),
        suppliers=list(db.scalars(
            select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
            .order_by(Contact.name)
        )),
    )


@router.post("/{lc_id}/add-bill")
async def add_bill(request: Request, lc_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    form = await request.form()
    bill = db.get(Bill, parse_id(form.get("bill_id")))
    if bill is None:
        flash(request, "Choose the purchase the charges belong to.", "danger")
        return redirect(f"/landed-costs/{lc_id}")
    try:
        added = L.add_bill(db, lc, bill)
        db.commit()
        flash(request, f"{added} line{'s' if added != 1 else ''} from {bill.number} added.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/landed-costs/{lc_id}")


@router.post("/{lc_id}/add-charge")
async def add_charge(request: Request, lc_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    form = await request.form()
    try:
        L.add_charge(
            db, lc,
            (form.get("description") or "Freight").strip(),
            parse_money(form.get("amount")),
            parse_id(form.get("account_id")),
            contact_id=parse_id(form.get("contact_id")),
        )
        db.commit()
        flash(request, "Charge added and spread over the consignment.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/landed-costs/{lc_id}")


@router.post("/{lc_id}/update")
async def update(request: Request, lc_id: int):
    """Change the basis, the weights, or remove a charge — then re-spread."""
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    if lc.status != DRAFT:
        flash(request, f"{lc.number} has been posted and cannot be changed.", "danger")
        return redirect(f"/landed-costs/{lc_id}")

    form = await request.form()
    lc.basis = form.get("basis") or lc.basis
    lc.reference = (form.get("reference") or "").strip()
    lc.note = form.get("note") or ""
    lc.date = parse_date(form.get("date"), lc.date)

    for key, value in form.multi_items():
        if key.startswith("weight_"):
            line = db.get(LandedCostLine, parse_id(key.split("_", 1)[1]) or 0)
            if line and line.landed_cost_id == lc.id:
                line.weight = parse_qty(value, 0)
    for raw in form.getlist("remove_charge"):
        charge = db.get(LandedCostCharge, parse_id(raw) or 0)
        if charge and charge.landed_cost_id == lc.id:
            db.delete(charge)
    for raw in form.getlist("remove_line"):
        line = db.get(LandedCostLine, parse_id(raw) or 0)
        if line and line.landed_cost_id == lc.id:
            db.delete(line)
    db.flush()

    L.recalc(db, lc)
    audit(db, user, "UPDATE", "LandedCost", lc.id, detail=lc.number, ip=client_ip(request))
    db.commit()
    flash(request, "Spread again on the new basis.")
    return redirect(f"/landed-costs/{lc_id}")


@router.post("/{lc_id}/post")
def post_it(request: Request, lc_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    try:
        L.post(db, lc, user=user)
        db.commit()
        flash(request, f"{lc.number} posted — the charges are now part of the stock.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/landed-costs/{lc_id}")


@router.post("/{lc_id}/void")
def void_it(request: Request, lc_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    try:
        L.void(db, lc, user=user)
        db.commit()
        flash(request, f"{lc.number} reversed. The cost is back off the stock.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/landed-costs/{lc_id}")


@router.post("/{lc_id}/delete")
def delete(request: Request, lc_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    lc = db.get(LandedCost, lc_id)
    if lc is None:
        return redirect("/landed-costs")
    if lc.status != DRAFT:
        flash(request, "Only a draft can be deleted. A posted one is reversed.", "danger")
        return redirect(f"/landed-costs/{lc_id}")
    number = lc.number
    audit(db, user, "DELETE", "LandedCost", lc.id, detail=number, ip=client_ip(request))
    db.delete(lc)
    db.commit()
    flash(request, f"Draft {number} deleted.", "warning")
    return redirect("/landed-costs")
