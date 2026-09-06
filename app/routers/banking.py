"""Bank and cash accounts, transfers and bank reconciliation."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import func, select

from .. import clock
from ..models import (
    ASSET,
    Account,
    BankAccount,
    JournalEntry,
    JournalLine,
    Reconciliation,
)
from ..money import fmt
from ..security import P_ENTRY, P_JOURNAL, P_VIEW
from ..services import cash
from ..services.posting import PostingError, account_net, audit
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

router = APIRouter(prefix="/banking")


def _balance(db, bank: BankAccount, on: date | None = None) -> int:
    return account_net(db, bank.account_id, None, on or date.today())


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    banks = list(db.scalars(select(BankAccount).order_by(BankAccount.sort, BankAccount.name)))
    balances = {b.id: _balance(db, b) for b in banks}
    uncleared = {}
    for b in banks:
        n = db.scalar(
            select(func.count(JournalLine.id))
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                JournalLine.account_id == b.account_id,
                JournalLine.cleared.is_(False),
                JournalEntry.is_void.is_(False),
            )
        )
        uncleared[b.id] = int(n or 0)
    return render(
        request, "banking/index.html",
        banks=banks, balances=balances, uncleared=uncleared,
        total=sum(balances.values()),
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    free_accounts = [
        a for a in db.scalars(
            select(Account).where(Account.type == ASSET, Account.is_active.is_(True))
            .order_by(Account.code)
        )
        if not db.scalar(select(BankAccount).where(BankAccount.account_id == a.id))
    ]
    return render(request, "banking/form.html", bank=BankAccount(name=""),
                  accounts=free_accounts, is_new=True)


@router.get("/transfer")
def transfer_form(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    banks = list(
        db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.sort))
    )
    return render(request, "banking/transfer.html", banks=banks,
                  balances={b.id: _balance(db, b) for b in banks}, today=date.today())


@router.post("/transfer")
async def transfer(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    src = db.get(BankAccount, parse_id(form.get("from_id")))
    dst = db.get(BankAccount, parse_id(form.get("to_id")))
    amount = parse_money(form.get("amount"))
    if not src or not dst:
        flash(request, "Choose both accounts.", "danger")
        return redirect("/banking/transfer")
    try:
        entry = cash.post_transfer(
            db, parse_date(form.get("date")), src, dst, amount,
            parse_money(form.get("charge")), (form.get("reference") or "").strip(),
            form.get("memo") or "", user,
        )
        audit(db, user, "TRANSFER", "JournalEntry", entry.id,
              detail=f"{fmt(amount)} {src.name} to {dst.name}", ip=client_ip(request))
        db.commit()
        flash(request, f"{fmt(amount)} transferred from {src.name} to {dst.name}.")
        return redirect(f"/banking/{dst.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/banking/transfer")


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    bid = parse_id(form.get("id"))
    bank = db.get(BankAccount, bid) if bid else None
    is_new = bank is None

    if is_new:
        account_id = parse_id(form.get("account_id"))
        if not account_id:
            flash(request, "Choose the ledger account this bank account posts to.", "danger")
            return redirect("/banking/new")
        bank = BankAccount(account_id=account_id)
        db.add(bank)

    bank.name = (form.get("name") or "").strip() or "Bank account"
    bank.bank_name = (form.get("bank_name") or "").strip()
    bank.account_number = (form.get("account_number") or "").strip()
    bank.account_type = form.get("account_type") or "CURRENT"
    bank.branch = (form.get("branch") or "").strip()
    bank.currency_code = (form.get("currency_code") or "NGN").strip()
    bank.is_active = parse_bool(form.get("is_active"))
    bank.account_name = (form.get("account_name") or "").strip()
    bank.sort_code = (form.get("sort_code") or "").strip()
    bank.swift = (form.get("swift") or "").strip().upper()
    bank.iban = (form.get("iban") or "").strip().upper().replace(" ", "")
    bank.show_on_invoices = parse_bool(form.get("show_on_invoices"))
    if parse_bool(form.get("is_default")):
        for other in db.scalars(select(BankAccount)):
            other.is_default = False
        bank.is_default = True
    db.flush()

    acc = db.get(Account, bank.account_id)
    if acc:
        acc.is_bank = True
    audit(db, user, "CREATE" if is_new else "UPDATE", "BankAccount", bank.id,
          detail=bank.name, ip=client_ip(request))
    db.commit()
    if bank.show_on_invoices and not bank.can_be_shown:
        flash(request, f"{bank.name} saved, but it will not appear on invoices yet — "
                       "a bank name and an account number are the least a customer "
                       "needs in order to pay you.", "warning")
    elif bank.show_on_invoices:
        flash(request, f"{bank.name} saved. Its details now appear on every invoice "
                       "you send, under \u201cHow to pay\u201d.")
    else:
        flash(request, f"{bank.name} saved.")
    return redirect(f"/banking/{bank.id}")


@router.get("/{bank_id}")
def detail(request: Request, bank_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        return redirect("/banking")

    end = parse_date(request.query_params.get("end"), date.today())
    start = parse_date(request.query_params.get("start"), end - timedelta(days=90))
    from ..services.reports import general_ledger

    acc, opening, rows, closing = general_ledger(db, bank.account_id, start, end)
    return render(
        request, "banking/detail.html",
        bank=bank, account=acc, opening=opening, rows=rows, closing=closing,
        start=start, end=end,
    )


@router.get("/{bank_id}/edit")
def edit(request: Request, bank_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    return render(request, "banking/form.html", bank=bank, accounts=[], is_new=False)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


@router.get("/{bank_id}/reconcile")
def reconcile_form(request: Request, bank_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        return redirect("/banking")

    statement_date = parse_date(request.query_params.get("statement_date"), date.today())
    statement_balance = parse_money(request.query_params.get("statement_balance"))

    rows = db.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == bank.account_id,
            JournalEntry.is_void.is_(False),
            JournalEntry.date <= statement_date,
        )
        .order_by(JournalEntry.date, JournalEntry.id)
    ).all()

    unc = [(l, e) for l, e in rows if not l.cleared]
    cleared_total = sum(l.debit - l.credit for l, _ in rows if l.cleared)
    book_balance = sum(l.debit - l.credit for l, _ in rows)
    difference = statement_balance - cleared_total if statement_balance else 0

    history = list(
        db.scalars(
            select(Reconciliation)
            .where(Reconciliation.bank_account_id == bank_id)
            .order_by(Reconciliation.statement_date.desc()).limit(12)
        )
    )
    return render(
        request, "banking/reconcile.html",
        bank=bank, rows=rows, uncleared=unc, statement_date=statement_date,
        statement_balance=statement_balance, cleared_total=cleared_total,
        book_balance=book_balance, difference=difference, history=history,
    )


@router.post("/{bank_id}/reconcile")
async def reconcile(request: Request, bank_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    bank = db.get(BankAccount, bank_id)
    statement_date = parse_date(form.get("statement_date"))
    statement_balance = parse_money(form.get("statement_balance"))

    checked = {parse_id(v) for v in form.getlist("cleared")}
    lines = db.scalars(
        select(JournalLine)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalLine.account_id == bank.account_id,
            JournalEntry.is_void.is_(False),
            JournalEntry.date <= statement_date,
        )
    )
    cleared_total = 0
    for line in lines:
        line.cleared = line.id in checked
        line.cleared_date = statement_date if line.cleared else None
        if line.cleared:
            cleared_total += line.debit - line.credit
    db.flush()

    difference = statement_balance - cleared_total
    if form.get("action") == "finish":
        if difference != 0:
            db.commit()
            flash(
                request,
                f"Not reconciled yet — the statement is {fmt(abs(difference))} "
                f"{'above' if difference > 0 else 'below'} the cleared items. "
                "Tick or untick items until the difference is zero.",
                "warning",
            )
            return redirect(f"/banking/{bank_id}/reconcile?statement_date={statement_date}"
                            f"&statement_balance={statement_balance / 100}")
        rec = Reconciliation(
            bank_account_id=bank_id,
            statement_date=statement_date,
            statement_balance=statement_balance,
            cleared_total=cleared_total,
            difference=0,
            is_closed=True,
            closed_at=clock.now(),
            closed_by_id=user.id,
        )
        db.add(rec)
        db.flush()
        for line in db.scalars(
            select(JournalLine).where(
                JournalLine.account_id == bank.account_id,
                JournalLine.cleared.is_(True),
                JournalLine.reconciliation_id.is_(None),
            )
        ):
            line.reconciliation_id = rec.id
        audit(db, user, "RECONCILE", "BankAccount", bank_id,
              detail=f"{bank.name} to {statement_date:%d %b %Y} at {fmt(statement_balance)}",
              ip=client_ip(request))
        db.commit()
        flash(request, f"{bank.name} reconciled to {statement_date:%d %b %Y}. "
                       f"Statement balance {fmt(statement_balance)} agrees to the ledger.")
        return redirect(f"/banking/{bank_id}")

    db.commit()
    flash(request, f"Progress saved — difference is {fmt(difference)}.",
          "success" if difference == 0 else "warning")
    return redirect(f"/banking/{bank_id}/reconcile?statement_date={statement_date}"
                    f"&statement_balance={statement_balance / 100}")
