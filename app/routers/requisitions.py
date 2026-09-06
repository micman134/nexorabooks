"""Requisitions: raising them, approving them, paying them, retiring them."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import (
    OPEN_REQUISITIONS,
    REQ_PAID,
    REQ_REJECTED,
    REQ_RETIRED,
    REQUISITION_STATUSES,
    Account,
    BankAccount,
    Contact,
    JournalEntry,
    Requisition,
    RequisitionLine,
    User,
)
from ..money import fmt, fmt_plain
from ..security import P_ADMIN, P_VIEW, can
from ..services import attachments as A
from ..services import requisitions as R
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
    user_of,
)

router = APIRouter(prefix="/requisitions")


def _expense_accounts(db):
    """What a requisition can be charged to — running costs, not the ledger at large."""
    return list(
        db.scalars(
            select(Account)
            .where(Account.is_active.is_(True),
                   Account.type.in_(("EXPENSE", "ASSET")))
            .order_by(Account.code)
        )
    )


def _may_see(db, req, user) -> bool:
    if user is None:
        return False
    if can(user, P_ADMIN) or user.pays_requisitions or user.approves_large_requisitions:
        return True
    if req.raised_by_id == user.id or req.manager_id == user.id:
        return True
    raiser = db.get(User, req.raised_by_id)
    return bool(raiser and raiser.manager_id == user.id)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------


@router.get("")
def index(request: Request):
    from ..main import render

    user = need(request, P_VIEW)
    db = db_of(request)
    view = request.query_params.get("view", "waiting")
    status = request.query_params.get("status", "")

    waiting = R.waiting_for(db, user)
    sent_back = R.sent_back_to(db, user)
    everything = R.visible_to(db, user)
    mine = [r for r in everything if r.raised_by_id == user.id]

    if view == "mine":
        rows = mine
    elif view == "all":
        rows = everything
    elif view == "unretired":
        rows = [r for r in everything if r.status == REQ_PAID]
    else:
        rows = waiting
    if status:
        rows = [r for r in rows if r.status == status]

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        return csv_response(
            f"requisitions-{date.today()}.csv",
            ["Number", "Date", "Raised by", "Purpose", "Amount", "Status",
             "With", "Paid", "Spent", "To return"],
            [[r.number, r.date, r.raised_by.display_name, r.purpose[:200],
              fmt_plain(r.total), r.status_label, r.awaiting,
              fmt_plain(r.paid_amount), fmt_plain(r.amount_spent),
              fmt_plain(r.balance_to_return) if r.status == REQ_RETIRED else ""]
             for r in rows],
        )

    return render(
        request, "requisitions/index.html",
        rows=rows, view=view, status=status,
        statuses=REQUISITION_STATUSES,
        waiting=waiting, sent_back=sent_back,
        counts={
            "waiting": len(waiting),
            "mine": len(mine),
            "all": len(everything),
            "unretired": len([r for r in everything if r.status == REQ_PAID]),
        },
        summary=R.summary(db, user),
        limit=R.limit(db),
    )


@router.get("/outstanding")
def outstanding(request: Request):
    from ..main import render

    user = need(request, P_VIEW)
    db = db_of(request)
    if not (can(user, P_ADMIN) or user.pays_requisitions):
        return redirect("/requisitions?view=unretired")
    rows = R.outstanding_by_person(db)
    return render(
        request, "requisitions/outstanding.html",
        rows=rows, total=sum(r[2] for r in rows),
        items=R.unretired(db),
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    user = need(request, P_VIEW)
    db = db_of(request)
    req = Requisition(number="(assigned on save)", date=date.today(),
                      raised_by_id=user.id, manager_id=user.manager_id,
                      department=user.department or "")
    req.lines = [RequisitionLine(line_no=i, qty=1000) for i in range(1, 4)]
    return render(
        request, "requisitions/form.html", req=req, is_new=True,
        accounts=_expense_accounts(db),
        vendors=list(db.scalars(
            select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
            .order_by(Contact.name)
        )),
        manager=db.get(User, user.manager_id) if user.manager_id else None,
        limit=R.limit(db),
    )


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_VIEW)
    db = db_of(request)
    form = await request.form()
    rid = parse_id(form.get("id"))
    req = db.get(Requisition, rid) if rid else None
    is_new = req is None

    if is_new:
        req = R.create(db, user, parse_date(form.get("date")))
    else:
        if req.raised_by_id != user.id and not can(user, P_ADMIN):
            flash(request, "You can only change a requisition you raised.", "danger")
            return redirect(f"/requisitions/{req.id}")
        if not req.is_editable:
            flash(request, f"{req.number} has been sent for approval and cannot be "
                           "changed. Ask whoever has it to send it back.", "danger")
            return redirect(f"/requisitions/{req.id}")
        req.date = parse_date(form.get("date"), req.date)

    req.purpose = (form.get("purpose") or "").strip()
    req.department = (form.get("department") or "").strip()
    req.needed_by = parse_date(form.get("needed_by"), None) if form.get("needed_by") else None
    db.flush()

    for old in list(req.lines):
        db.delete(old)
    db.flush()

    get = lambda key, i: (form.getlist(key)[i] if i < len(form.getlist(key)) else None)  # noqa: E731
    descs = form.getlist("line_description")
    n = 0
    for i in range(len(descs)):
        desc = (descs[i] or "").strip()
        qty = parse_qty(get("line_qty", i), 0)
        price = parse_money(get("line_price", i))
        if not desc and not price:
            continue
        n += 1
        db.add(RequisitionLine(
            requisition_id=req.id, line_no=n, description=desc,
            account_id=parse_id(get("line_account", i)),
            vendor_id=parse_id(get("line_vendor", i)),
            qty=qty or 1000, unit_price=price,
        ))
    db.flush()
    R.recalc(db, req)

    audit(db, user, "CREATE" if is_new else "UPDATE", "Requisition", req.id,
          detail=f"{req.number} {fmt(req.total)}", ip=client_ip(request))

    # "Save and send" in one step, so nothing sits forgotten in draft
    if form.get("action") == "submit":
        try:
            R.submit(db, req, user)
            db.commit()
            flash(request, f"{req.number} sent to "
                           f"{req.manager.display_name if req.manager else 'your manager'} "
                           "for approval.")
            return redirect(f"/requisitions/{req.id}")
        except PostingError as e:
            db.commit()          # keep the draft, report the problem
            flash(request, str(e), "danger")
            return redirect(f"/requisitions/{req.id}")

    db.commit()
    flash(request, f"{req.number} saved as a draft. It has not been sent yet.")
    return redirect(f"/requisitions/{req.id}")


@router.get("/{req_id}")
def detail(request: Request, req_id: int):
    from ..main import render

    user = need(request, P_VIEW)
    db = db_of(request)
    req = db.get(Requisition, req_id)
    if req is None:
        return redirect("/requisitions")
    if not _may_see(db, req, user):
        from ..main import flash

        flash(request, "That requisition is not yours to see.", "danger")
        return redirect("/requisitions")

    return render(
        request, "requisitions/detail.html",
        req=req,
        files=A.list_for(db, "REQUISITION", req.id),
        can_manager=R.can_approve_as_manager(db, req, user),
        can_director=R.can_approve_as_director(db, req, user),
        can_pay=R.can_pay(db, req, user),
        can_retire=R.can_retire(db, req, user),
        can_edit=req.is_editable and (req.raised_by_id == user.id or can(user, P_ADMIN)),
        paid_shares=R.paid_shares(req),
        banks=list(db.scalars(
            select(BankAccount).where(BankAccount.is_active.is_(True))
            .order_by(BankAccount.sort)
        )),
        payment_entry=db.get(JournalEntry, req.payment_entry_id)
        if req.payment_entry_id else None,
        retirement_entry=db.get(JournalEntry, req.retirement_entry_id)
        if req.retirement_entry_id else None,
        limit=R.limit(db),
    )


@router.get("/{req_id}/edit")
def edit(request: Request, req_id: int):
    from ..main import flash, render

    user = need(request, P_VIEW)
    db = db_of(request)
    req = db.get(Requisition, req_id)
    if req is None:
        return redirect("/requisitions")
    if req.raised_by_id != user.id and not can(user, P_ADMIN):
        flash(request, "You can only change a requisition you raised.", "danger")
        return redirect(f"/requisitions/{req_id}")
    if not req.is_editable:
        flash(request, f"{req.number} is with {req.awaiting} and cannot be changed.",
              "warning")
        return redirect(f"/requisitions/{req_id}")
    while len(req.lines) < 3:
        req.lines.append(RequisitionLine(line_no=len(req.lines) + 1, qty=1000))
    return render(
        request, "requisitions/form.html", req=req, is_new=False,
        accounts=_expense_accounts(db),
        vendors=list(db.scalars(
            select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
            .order_by(Contact.name)
        )),
        manager=db.get(User, req.manager_id) if req.manager_id else None,
        limit=R.limit(db),
    )


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def _act(request, req_id, work, success):
    """Shared shape for every workflow action: do it, or say plainly why not."""
    from ..main import flash

    user = need(request, P_VIEW)
    db = db_of(request)
    req = db.get(Requisition, req_id)
    if req is None:
        return redirect("/requisitions")
    try:
        result = work(db, req, user)
        db.commit()
        flash(request, success(req, result))
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/requisitions/{req_id}")


@router.post("/{req_id}/submit")
def submit(request: Request, req_id: int):
    return _act(
        request, req_id,
        lambda db, req, user: R.submit(db, req, user),
        lambda req, _r: f"{req.number} sent to "
                        f"{req.manager.display_name if req.manager else 'your manager'} "
                        "for approval.",
    )


@router.post("/{req_id}/approve")
async def approve(request: Request, req_id: int):
    form = await request.form()
    note = (form.get("note") or "").strip()
    return _act(
        request, req_id,
        lambda db, req, user: R.approve(db, req, user, note),
        lambda req, _r: f"{req.number} approved — it is now with {req.awaiting}.",
    )


@router.post("/{req_id}/reject")
async def reject(request: Request, req_id: int):
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    return _act(
        request, req_id,
        lambda db, req, user: R.reject(db, req, user, reason),
        lambda req, _r: f"{req.number} sent back to {req.raised_by.display_name} "
                        "with your reason.",
    )


@router.post("/{req_id}/withdraw")
async def withdraw(request: Request, req_id: int):
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    return _act(
        request, req_id,
        lambda db, req, user: R.withdraw(db, req, user, reason),
        lambda req, _r: f"{req.number} withdrawn.",
    )


@router.post("/{req_id}/pay")
async def pay(request: Request, req_id: int):
    form = await request.form()
    bank_id = parse_id(form.get("bank_account_id"))
    on = parse_date(form.get("date"))
    amount = parse_money(form.get("amount")) or None
    reference = (form.get("reference") or "").strip()

    return _act(
        request, req_id,
        lambda db, req, user: R.pay(db, req, user, bank_account_id=bank_id or 0,
                                    on=on, amount=amount, reference=reference),
        lambda req, _r: f"{fmt(req.paid_amount)} sent to "
                        f"{req.raised_by.display_name}. "
                        f"{req.raised_by.display_name.split()[0]} now has to retire it "
                        "with receipts.",
    )


@router.post("/{req_id}/retire")
async def retire(request: Request, req_id: int):
    from ..main import flash

    user = need(request, P_VIEW)
    db = db_of(request)
    req = db.get(Requisition, req_id)
    if req is None:
        return redirect("/requisitions")

    form = await request.form()
    spent: dict[int, int] = {}
    for key, value in form.multi_items():
        if key.startswith("spent_"):
            line_id = parse_id(key.split("_", 1)[1])
            if line_id:
                spent[line_id] = parse_money(value)

    try:
        R.retire(
            db, req, user,
            spent=spent,
            on=parse_date(form.get("date")),
            note=(form.get("note") or "").strip(),
            settle_bank_account_id=parse_id(form.get("bank_account_id")),
        )
        db.commit()
        balance = req.balance_to_return
        if balance > 0:
            flash(request, f"{req.number} retired. {fmt(balance)} came back into the "
                           "bank and the expense has been reduced to match.")
        elif balance < 0:
            flash(request, f"{req.number} retired. {fmt(-balance)} was paid back out "
                           "to cover the overspend.")
        else:
            flash(request, f"{req.number} retired in full.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/requisitions/{req_id}")


@router.post("/{req_id}/reopen")
def reopen(request: Request, req_id: int):
    return _act(
        request, req_id,
        lambda db, req, user: R.unretire(db, req, user),
        lambda req, _r: f"{req.number} reopened. The correcting entry has been "
                        "reversed — file the retirement again.",
    )


@router.post("/{req_id}/delete")
def delete(request: Request, req_id: int):
    from ..main import flash

    user = need(request, P_VIEW)
    db = db_of(request)
    req = db.get(Requisition, req_id)
    if req is None:
        return redirect("/requisitions")
    if req.status != "DRAFT":
        flash(request, "Only a draft can be deleted. Withdraw it instead.", "danger")
        return redirect(f"/requisitions/{req_id}")
    if req.raised_by_id != user.id and not can(user, P_ADMIN):
        flash(request, "You can only delete a requisition you raised.", "danger")
        return redirect(f"/requisitions/{req_id}")
    number = req.number
    A.delete_all_for(db, request.state.company_slug, "REQUISITION", req.id)
    audit(db, user, "DELETE", "Requisition", req.id, detail=number, ip=client_ip(request))
    db.delete(req)
    db.commit()
    flash(request, f"Draft {number} deleted.", "warning")
    return redirect("/requisitions")
