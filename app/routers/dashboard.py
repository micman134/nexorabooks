"""Home dashboard and global search."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select

from ..models import Bill, Contact, Invoice, Item, JournalEntry, Payment
from ..services import reports
from ._common import db_of, redirect, user_of

router = APIRouter()


@router.get("/")
def home(request: Request):
    from ..main import render

    db = db_of(request)
    company = request.state.company
    if company and not company.setup_complete:
        # Only an administrator can finish setting the company up. Sending
        # anybody else to that screen earns them a bare "not allowed", which
        # is a miserable first thing to see on the day you are given an
        # account — so they get told what is actually going on instead.
        from ..security import P_ADMIN, can

        if can(user_of(request), P_ADMIN):
            return redirect("/settings/company?first_run=1")
        return render(request, "dashboard_waiting.html", company=company)

    data = reports.dashboard(db, date.today())

    recent_invoices = list(
        db.scalars(
            select(Invoice)
            .where(Invoice.doc_type == "INVOICE")
            .order_by(Invoice.id.desc())
            .limit(8)
        )
    )
    recent_bills = list(
        db.scalars(
            select(Bill).where(Bill.doc_type == "BILL").order_by(Bill.id.desc()).limit(8)
        )
    )
    recent_payments = list(db.scalars(select(Payment).order_by(Payment.id.desc()).limit(8)))

    return render(
        request,
        "dashboard.html",
        d=data,
        recent_invoices=recent_invoices,
        recent_bills=recent_bills,
        recent_payments=recent_payments,
        **_waiting(db, request.state.user),
    )


def _waiting(db, user) -> dict:
    """The two jobs that quietly go undone: recurring billing and depreciation."""
    from ..models import ASSET_ACTIVE, DepreciationRun, FixedAsset, POSTED
    from ..services import assets as FA
    from ..services import recurring as REC

    today = date.today()
    recurring_due = REC.due_count(db)

    last = db.scalar(
        select(func.max(DepreciationRun.period)).where(DepreciationRun.status == POSTED)
    )
    period = FA.next_period(int(last)) if last else FA.period_of(today)
    depreciation_due = 0
    if period <= FA.period_of(today):
        for asset in db.scalars(select(FixedAsset).where(FixedAsset.status == ASSET_ACTIVE)):
            charge = FA.charge_for(asset, period)
            if charge:
                depreciation_due += charge.amount

    from ..services import requisitions as REQ

    return {
        "requisitions": REQ.summary(db, user),
        "recurring_due": recurring_due,
        "depreciation_due": depreciation_due,
        "depreciation_period": period,
        "depreciation_label": FA.period_end(period).strftime("%B %Y"),
    }


@router.get("/search")
def search(request: Request):
    from ..main import render

    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    # NOTE: the key is "products", not "items" — in a template ``results.items``
    # would resolve to the dict's own .items() method.
    results = {"contacts": [], "invoices": [], "bills": [], "products": [], "journals": []}
    if len(q) >= 2:
        like = f"%{q}%"
        results["contacts"] = list(
            db.scalars(
                select(Contact)
                .where(or_(Contact.name.ilike(like), Contact.code.ilike(like),
                           Contact.phone.ilike(like), Contact.email.ilike(like)))
                .limit(15)
            )
        )
        results["invoices"] = list(
            db.scalars(
                select(Invoice)
                .where(or_(Invoice.number.ilike(like), Invoice.reference.ilike(like),
                           Invoice.po_number.ilike(like), Invoice.memo.ilike(like)))
                .order_by(Invoice.id.desc())
                .limit(15)
            )
        )
        results["bills"] = list(
            db.scalars(
                select(Bill)
                .where(or_(Bill.number.ilike(like), Bill.vendor_invoice_no.ilike(like),
                           Bill.memo.ilike(like)))
                .order_by(Bill.id.desc())
                .limit(15)
            )
        )
        results["products"] = list(
            db.scalars(
                select(Item)
                .where(or_(Item.name.ilike(like), Item.code.ilike(like),
                           Item.barcode.ilike(like)))
                .limit(15)
            )
        )
        results["journals"] = list(
            db.scalars(
                select(JournalEntry)
                .where(or_(JournalEntry.number.ilike(like), JournalEntry.memo.ilike(like),
                           JournalEntry.reference.ilike(like)))
                .order_by(JournalEntry.id.desc())
                .limit(15)
            )
        )
    return render(request, "search.html", q=q, results=results)
