"""Manual journal entries, opening balances and the chart of accounts."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import (
    ACCOUNT_TYPES,
    SUBTYPES,
    Account,
    Contact,
    JournalEntry,
    JournalLine,
)
from ..money import fmt
from ..security import P_ADMIN, P_JOURNAL, P_VIEW
from ..services import reports
from ..services.posting import (
    EntryDraft,
    PostingError,
    audit,
    post_entry,
    reverse_entry,
    sys_account,
)
from ..services.tax import vat_codes
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_money,
    period_from_query,
    redirect,
)

router = APIRouter(prefix="/journals")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    q = (request.query_params.get("q") or "").strip()
    source = request.query_params.get("source", "")

    stmt = select(JournalEntry).where(JournalEntry.date >= start, JournalEntry.date <= end)
    if source:
        stmt = stmt.where(JournalEntry.source == source)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(JournalEntry.number.ilike(like), JournalEntry.memo.ilike(like),
                JournalEntry.reference.ilike(like))
        )
    entries = list(
        db.scalars(stmt.order_by(JournalEntry.date.desc(), JournalEntry.id.desc()).limit(500))
    )
    return render(
        request, "journals/index.html",
        entries=entries, start=start, end=end, preset=preset, q=q, source=source,
        total=sum(e.total_debit for e in entries if not e.is_void),
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    return render(
        request, "journals/form.html",
        accounts=list(
            db.scalars(select(Account).where(Account.is_active.is_(True)).order_by(Account.code))
        ),
        contacts=list(
            db.scalars(select(Contact).where(Contact.is_active.is_(True)).order_by(Contact.name))
        ),
        vat_codes=vat_codes(db),
        today=date.today(),
        rows=range(6),
    )


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()

    draft = EntryDraft(
        date=parse_date(form.get("date")),
        memo=(form.get("memo") or "").strip(),
        reference=(form.get("reference") or "").strip(),
        source="MANUAL",
    )
    get = lambda k, i: (form.getlist(k)[i] if i < len(form.getlist(k)) else None)  # noqa: E731
    accounts = form.getlist("line_account")
    for i in range(len(accounts)):
        acc_id = parse_id(accounts[i])
        if not acc_id:
            continue
        dr = parse_money(get("line_debit", i))
        cr = parse_money(get("line_credit", i))
        memo = (get("line_memo", i) or "").strip()
        contact_id = parse_id(get("line_contact", i))
        tax_id = parse_id(get("line_tax", i))
        base = dr if dr else -cr
        if dr:
            draft.debit(acc_id, dr, memo, contact_id=contact_id,
                        tax_code_id=tax_id, tax_base=base if tax_id else 0)
        elif cr:
            draft.credit(acc_id, cr, memo, contact_id=contact_id,
                         tax_code_id=tax_id, tax_base=base if tax_id else 0)

    try:
        entry = post_entry(db, draft, user=user)
        audit(db, user, "POST", "JournalEntry", entry.id,
              detail=f"{entry.number} {fmt(entry.total_debit)}", ip=client_ip(request))
        db.commit()
        flash(request, f"Journal {entry.number} posted — {fmt(entry.total_debit)}.")
        return redirect(f"/journals/{entry.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/journals/new")


@router.get("/opening-balances")
def opening_form(request: Request):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    accounts = list(
        db.scalars(
            select(Account)
            .where(Account.is_active.is_(True), Account.subtype != "RETAINED_EARNINGS")
            .order_by(Account.code)
        )
    )
    existing = {}
    opening_entry = db.scalar(select(JournalEntry).where(JournalEntry.source == "OPENING"))
    if opening_entry:
        for line in opening_entry.lines:
            existing[line.account_id] = line.debit - line.credit
    return render(
        request, "journals/opening.html",
        accounts=accounts, existing=existing, opening_entry=opening_entry,
        today=date.today(),
    )


@router.post("/opening-balances")
async def opening_save(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    on = parse_date(form.get("date"))

    # Replace any previous opening entry so balances are never double counted
    prev = db.scalar(select(JournalEntry).where(JournalEntry.source == "OPENING",
                                                JournalEntry.is_void.is_(False)))
    if prev:
        try:
            reverse_entry(db, prev, on=on, user=user, memo="Replaced opening balances")
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect("/journals/opening-balances")

    draft = EntryDraft(date=on, memo="Opening balances", source="OPENING",
                       reference="OPENING")
    ids = form.getlist("account_id")
    debits = form.getlist("debit")
    credits = form.getlist("credit")
    for i, raw in enumerate(ids):
        acc_id = parse_id(raw)
        if not acc_id:
            continue
        dr = parse_money(debits[i] if i < len(debits) else "0")
        cr = parse_money(credits[i] if i < len(credits) else "0")
        if dr:
            draft.debit(acc_id, dr, "Opening balance")
        elif cr:
            draft.credit(acc_id, cr, "Opening balance")

    # The difference goes to Opening Balance Equity so the entry always balances
    diff = draft.total_debit - draft.total_credit
    if diff:
        obe = sys_account(db, "OPENING_EQUITY")
        draft.signed(obe, -diff, "Opening balance equity — difference on conversion")

    if not draft.lines:
        flash(request, "Enter at least one opening balance.", "danger")
        return redirect("/journals/opening-balances")

    try:
        entry = post_entry(db, draft, user=user)
        audit(db, user, "OPENING", "JournalEntry", entry.id,
              detail=f"{fmt(entry.total_debit)}", ip=client_ip(request))
        db.commit()
        msg = f"Opening balances posted as {entry.number}."
        if diff:
            msg += (f" A difference of {fmt(abs(diff))} was posted to Opening Balance Equity — "
                    "clear it once all balances are entered.")
        flash(request, msg)
        return redirect(f"/journals/{entry.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/journals/opening-balances")


@router.get("/{entry_id}")
def detail(request: Request, entry_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    entry = db.get(JournalEntry, entry_id)
    if entry is None:
        return redirect("/journals")
    reversal = db.scalar(select(JournalEntry).where(JournalEntry.reverses_id == entry.id))
    from ..services import attachments as A

    return render(request, "journals/detail.html", entry=entry, reversal=reversal,
                  files=A.list_for(db, "JOURNAL", entry.id), today=date.today())


@router.post("/{entry_id}/reverse")
async def reverse(request: Request, entry_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    entry = db.get(JournalEntry, entry_id)
    try:
        rev = reverse_entry(db, entry, parse_date(form.get("date"), date.today()), user,
                            memo=form.get("memo") or "")
        audit(db, user, "REVERSE", "JournalEntry", entry.id,
              detail=f"{entry.number} reversed by {rev.number}", ip=client_ip(request))
        db.commit()
        flash(request, f"{entry.number} reversed by {rev.number}.", "warning")
        return redirect(f"/journals/{rev.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect(f"/journals/{entry_id}")


# --------------------------------------------------------------------------
# Chart of accounts
# --------------------------------------------------------------------------

coa = APIRouter(prefix="/accounts")


@coa.get("")
def accounts_index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    show_inactive = parse_bool(request.query_params.get("inactive"))
    q = (request.query_params.get("q") or "").strip()
    stmt = select(Account)
    if not show_inactive:
        stmt = stmt.where(Account.is_active.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Account.code.ilike(like), Account.name.ilike(like)))
    accounts = list(db.scalars(stmt.order_by(Account.code)))
    bals = reports.balances(db, None, date.today())
    values = {}
    for a in accounts:
        d, c = bals.get(a.id, (0, 0))
        values[a.id] = a.signed(d, c)
    return render(
        request, "accounts/index.html",
        accounts=accounts, values=values, q=q, show_inactive=show_inactive,
        types=ACCOUNT_TYPES,
    )


@coa.get("/new")
def account_new(request: Request):
    from ..main import render

    need(request, P_JOURNAL)
    return render(request, "accounts/form.html", account=Account(code="", name="", type="EXPENSE"),
                  is_new=True, types=ACCOUNT_TYPES, subtypes=SUBTYPES)


@coa.post("/save")
async def account_save(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    aid = parse_id(form.get("id"))
    acc = db.get(Account, aid) if aid else None
    is_new = acc is None

    code = (form.get("code") or "").strip()
    name = (form.get("name") or "").strip()
    if not code or not name:
        flash(request, "An account needs both a code and a name.", "danger")
        return redirect("/accounts/new")

    clash = db.scalar(select(Account).where(Account.code == code))
    if clash and (is_new or clash.id != acc.id):
        flash(request, f"Account code {code} is already used by {clash.name}.", "danger")
        return redirect("/accounts/new" if is_new else f"/accounts/{aid}/edit")

    if is_new:
        acc = Account(code=code, name=name)
        db.add(acc)
    else:
        acc.code = code
        acc.name = name

    if not acc.is_system:
        acc.type = form.get("type") or "EXPENSE"
        acc.subtype = form.get("subtype") or ""
    acc.description = form.get("description") or ""
    acc.cashflow_class = form.get("cashflow_class") or "OPERATING"
    acc.is_active = parse_bool(form.get("is_active"))
    db.flush()
    audit(db, user, "CREATE" if is_new else "UPDATE", "Account", acc.id,
          detail=f"{acc.code} {acc.name}", ip=client_ip(request))
    db.commit()
    flash(request, f"{acc.code} — {acc.name} saved.")
    return redirect("/accounts")


@coa.get("/{account_id}/edit")
def account_edit(request: Request, account_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    acc = db.get(Account, account_id)
    if acc is None:
        return redirect("/accounts")
    return render(request, "accounts/form.html", account=acc, is_new=False,
                  types=ACCOUNT_TYPES, subtypes=SUBTYPES)


@coa.post("/{account_id}/archive")
def account_archive(request: Request, account_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    acc = db.get(Account, account_id)
    if acc is None:
        return redirect("/accounts")
    if acc.is_system and acc.is_active:
        flash(request, f"{acc.code} — {acc.name} is a system account and cannot be archived.", "danger")
        return redirect("/accounts")
    acc.is_active = not acc.is_active
    audit(db, user, "ARCHIVE" if not acc.is_active else "RESTORE", "Account", acc.id,
          detail=acc.name, ip=client_ip(request))
    db.commit()
    flash(request, f"{acc.name} {'archived' if not acc.is_active else 'restored'}.")
    return redirect("/accounts")


@coa.post("/restore-defaults")
def restore_defaults(request: Request):
    from ..main import flash
    from ..seed import seed_accounts

    user = need(request, P_ADMIN)
    db = db_of(request)
    created = seed_accounts(db)
    audit(db, user, "RESTORE_COA", "Account", detail=f"{created} accounts added",
          ip=client_ip(request))
    db.commit()
    flash(request, f"{created} missing standard account(s) restored."
          if created else "Every standard account is already present.")
    return redirect("/accounts")


# ``coa`` is mounted at the top level (/accounts) by app.main.
