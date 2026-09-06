"""The three cash screens: what is coming, what if, and who to chase.

These are the only screens in the software that look forward, and each of them
says so on its face. Everything they project is built from commitments already
in the books — invoices raised, bills entered, a pay run on its known cycle —
timed by what each customer has actually done. Nothing here posts anything, and
the collections screen drafts messages without ever sending one.
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request

from .. import prefs
from ..models import Contact
from ..security import P_VIEW
from ..services import cashtimeline as CT
from ..services import charts, collections, reports, whatif
from ._common import db_of, need, parse_date, parse_int, parse_money, redirect

router = APIRouter(prefix="/cash")

HORIZONS = [(30, "30 days"), (60, "60 days"), (90, "90 days"), (180, "6 months")]


# --------------------------------------------------------------------------
# What is coming
# --------------------------------------------------------------------------


@router.get("")
@router.get("/timeline")
def timeline(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    days = parse_int(request.query_params.get("days"), CT.HORIZON_DAYS) or CT.HORIZON_DAYS
    days = min(max(days, 14), 365)
    on = parse_date(request.query_params.get("on"), date.today())

    line = CT.build(db, on, days)
    return render(
        request,
        "cash/timeline.html",
        t=line,
        days=days,
        horizons=HORIZONS,
        levers=CT.levers(db, line),
        chart=_balance_chart(line),
        weeks=_weeks(line),
        upcoming=[e for e in line.events][:60],
    )


def _weeks(line: CT.Timeline) -> list[charts.Bucket]:
    """The timeline grouped into columns the chart can label."""
    buckets: list[charts.Bucket] = []
    step = 1 if len(line.days) <= 31 else 7
    for start in range(0, len(line.days), step):
        chunk = line.days[start:start + step]
        if not chunk:
            continue
        buckets.append(charts.Bucket(
            label=prefs.strftime(chunk[0].when, "%-d %b"),
            start=chunk[0].when,
            end=chunk[-1].when,
            money_in=sum(d.money_in for d in chunk),
            money_out=sum(d.money_out for d in chunk),
            closing=chunk[-1].closing,
        ))
    return buckets


def _balance_chart(line: CT.Timeline) -> str:
    return charts.cash_chart(_weeks(line))


# --------------------------------------------------------------------------
# What if
# --------------------------------------------------------------------------


@router.get("/what-if")
def what_if(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = request.query_params

    end = parse_date(q.get("end"), date.today())
    start = parse_date(q.get("start"), end - timedelta(days=364))
    if start > end:
        start, end = end, start

    chosen = (q.get("preset") or "").strip()
    assumptions = whatif.preset(chosen) if chosen else None
    if assumptions is None:
        assumptions = whatif.Assumptions(
            price_change=q.get("price_change", "0"),
            volume_change=q.get("volume_change", "0"),
            cost_change=q.get("cost_change", "0"),
            overhead_change=q.get("overhead_change", "0"),
            extra_fixed_cost=parse_money(q.get("extra_fixed_cost")),
            collection_days=q.get("collection_days", "0"),
        )

    return render(
        request,
        "cash/what_if.html",
        result=whatif.run(db, start, end, assumptions),
        a=assumptions,
        start=start,
        end=end,
        presets=whatif.PRESETS,
        chosen=chosen,
    )


# --------------------------------------------------------------------------
# Who to chase
# --------------------------------------------------------------------------


@router.get("/collections")
def collections_list(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    on = parse_date(request.query_params.get("on"), date.today())
    rows = collections.review(db, on)

    return render(
        request,
        "cash/collections.html",
        rows=rows,
        on=on,
        owed=sum(r.total for r in rows),
        overdue=sum(r.overdue for r in rows),
        urgent=[r for r in rows if r.urgent],
    )


@router.get("/collections/{contact_id}")
def collection_detail(request: Request, contact_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    on = parse_date(request.query_params.get("on"), date.today())

    rows = collections.review(db, on)
    owing = next((r for r in rows if r.contact.id == contact_id), None)
    if owing is None:
        contact = db.get(Contact, contact_id)
        if contact is None:
            return redirect("/cash/collections")
        return render(request, "cash/collection.html", owing=None, contact=contact, on=on)

    check = None
    offer = request.query_params.get("offer")
    if offer:
        check = collections.discount_check(
            owing.overdue or owing.total, offer,
            parse_int(request.query_params.get("sooner"), 30) or 30,
            request.query_params.get("borrowing", "24"),
        )

    return render(
        request,
        "cash/collection.html",
        owing=owing,
        contact=owing.contact,
        on=on,
        message=collections.draft(db, owing, on),
        check=check,
        offer=offer or "",
        sooner=request.query_params.get("sooner", "30"),
        borrowing=request.query_params.get("borrowing", "24"),
    )
