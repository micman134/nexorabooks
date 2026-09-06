"""Purchases: orders, bills, debit notes, supplier payments and quick expenses."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import (
    DRAFT,
    PAYMENT,
    POSTED,
    VOID,
    Account,
    BankAccount,
    Bill,
    BillLine,
    Contact,
    Item,
    Location,
    JournalEntry,
    Payment,
    PaymentAllocation,
    TaxCode,
)
from ..money import fmt
from ..security import P_ENTRY, P_VIEW, P_VOID
from ..services import attachments as A
from ..services import cash, documents, tax
from ..services.posting import EntryDraft, PostingError, audit, next_number, post_entry, sys_account
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
)

router = APIRouter(prefix="/purchases")


def _default_line_account(db, item):
    """Where a purchase line lands when the user leaves the account on Default.

    Stock capitalises into inventory — it is an asset until it is sold, and
    putting it in Purchases instead leaves the stock records and the inventory
    account permanently apart. Everything else goes to the item's own expense
    account.
    """
    from ..models import STOCK_ITEM

    if item is None:
        return None
    if item.item_type == STOCK_ITEM and item.track_stock:
        return item.inventory_account_id or sys_account(db, "INVENTORY").id
    return item.purchase_account_id


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
    "BILL": ("Bill", "bills"),
    "PO": ("Purchase order", "orders"),
    "DEBIT_NOTE": ("Debit note", "debit-notes"),
}
SLUG_TO_TYPE = {v[1]: k for k, v in DOC_LABELS.items()}


def _expense_accounts(db):
    return list(
        db.scalars(
            select(Account)
            .where(Account.type.in_(("EXPENSE", "ASSET")), Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


def _form_context(request, db, bill):
    return dict(
        bill=bill,
        vendors=list(
            db.scalars(
                select(Contact)
                .where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
                .order_by(Contact.name)
            )
        ),
        items=list(db.scalars(select(Item).where(Item.is_active.is_(True)).order_by(Item.name))),
        accounts=_expense_accounts(db),
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
        label=DOC_LABELS[bill.doc_type][0],
        slug=DOC_LABELS[bill.doc_type][1],
    )


@router.get("/{slug}")
def index(request: Request, slug: str):
    from ..main import render

    if slug not in SLUG_TO_TYPE:
        return redirect("/purchases/bills")
    need(request, P_VIEW)
    db = db_of(request)
    doc_type = SLUG_TO_TYPE[slug]

    status = request.query_params.get("status", "")
    q = (request.query_params.get("q") or "").strip()
    stmt = select(Bill).where(Bill.doc_type == doc_type)
    if status:
        stmt = stmt.where(Bill.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Contact, Bill.contact_id == Contact.id).where(
            or_(Bill.number.ilike(like), Contact.name.ilike(like),
                Bill.vendor_invoice_no.ilike(like))
        )
    docs = list(db.scalars(stmt.order_by(Bill.date.desc(), Bill.id.desc()).limit(400)))
    totals = {
        "count": len(docs),
        "value": sum(d.total for d in docs if d.status != VOID),
        "outstanding": sum(d.balance_due for d in docs if d.status in (POSTED, "PART_PAID")),
        "wht": sum(d.wht_total for d in docs if d.status in (POSTED, "PART_PAID")),
    }
    return render(
        request, "purchases/index.html",
        docs=docs, slug=slug, label=DOC_LABELS[doc_type][0], status=status, q=q, totals=totals,
    )


@router.get("/{slug}/new")
def new(request: Request, slug: str):
    from ..main import render

    if slug not in SLUG_TO_TYPE:
        return redirect("/purchases/bills")
    need(request, P_ENTRY)
    db = db_of(request)
    bill = Bill(
        doc_type=SLUG_TO_TYPE[slug],
        number="(assigned on save)",
        date=date.today(),
        status=DRAFT,
    )
    bill.lines = [BillLine(line_no=i, qty=1000) for i in range(1, 4)]
    cid = parse_id(request.query_params.get("contact"))
    if cid:
        bill.contact_id = cid
    return render(request, "purchases/form.html", is_new=True, **_form_context(request, db, bill))


@router.get("/{slug}/{doc_id}")
def detail(request: Request, slug: str, doc_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    if bill is None:
        return redirect(f"/purchases/{slug}")
    entry = db.get(JournalEntry, bill.journal_entry_id) if bill.journal_entry_id else None
    allocs = list(db.scalars(select(PaymentAllocation).where(PaymentAllocation.bill_id == bill.id)))
    wht_note = ""
    if bill.wht_code_id:
        _amt, wht_note = tax.wht_on(bill.subtotal, bill.wht_code, bill.contact)
    return render(
        request, "purchases/detail.html",
        bill=bill, entry=entry, allocations=allocs, wht_note=wht_note,
        files=A.list_for(db, bill.doc_type, bill.id),
        label=DOC_LABELS[bill.doc_type][0], slug=DOC_LABELS[bill.doc_type][1],
    )


@router.get("/{slug}/{doc_id}/edit")
def edit(request: Request, slug: str, doc_id: int):
    from ..main import flash, render

    need(request, P_ENTRY)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    if bill is None:
        return redirect(f"/purchases/{slug}")
    if bill.status != DRAFT:
        flash(request, f"{bill.number} has been posted. Void it or raise a debit note.", "warning")
        return redirect(f"/purchases/{slug}/{doc_id}")
    while len(bill.lines) < 3:
        bill.lines.append(BillLine(line_no=len(bill.lines) + 1, qty=1000))
    return render(request, "purchases/form.html", is_new=False, **_form_context(request, db, bill))


@router.post("/{slug}/save")
async def save(request: Request, slug: str):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    doc_type = SLUG_TO_TYPE.get(slug, "BILL")
    doc_id = parse_id(form.get("id"))

    bill = db.get(Bill, doc_id) if doc_id else None
    is_new = bill is None
    if not is_new and bill.status != DRAFT:
        flash(request, f"{bill.number} is already posted and cannot be edited.", "danger")
        return redirect(f"/purchases/{slug}/{bill.id}")

    contact_id = parse_id(form.get("contact_id"))
    if not contact_id:
        flash(request, "Choose a supplier.", "danger")
        return redirect(f"/purchases/{slug}/new")

    # Look the supplier up before the new row exists, so nothing half-built is
    # ever flushed to the database.
    contact = db.get(Contact, contact_id)
    if is_new:
        bill = Bill(number=next_number(db, doc_type), doc_type=doc_type,
                    status=DRAFT, created_by_id=user.id,
                    contact_id=contact_id, date=parse_date(form.get("date")))
        db.add(bill)

    bill.contact_id = contact_id
    bill.date = parse_date(form.get("date"))
    bill.due_date = parse_date(form.get("due_date"), documents.due_date_for(db, bill.date, contact))
    bill.vendor_invoice_no = (form.get("vendor_invoice_no") or "").strip()
    bill.reference = (form.get("reference") or "").strip()
    bill.memo = form.get("memo") or ""
    bill.location_id = parse_id(form.get("location_id"))
    bill.wht_code_id = parse_id(form.get("wht_code_id")) or (
        contact.default_wht_code_id if contact else None
    )
    db.flush()

    for old in list(bill.lines):
        db.delete(old)
    db.flush()
    bill.lines = []

    descs = form.getlist("line_description")
    get = lambda key, i: (form.getlist(key)[i] if i < len(form.getlist(key)) else None)  # noqa: E731
    n = 0
    for i in range(len(descs)):
        item_id = parse_id(get("line_item_id", i))
        desc = (descs[i] or "").strip()
        qty = parse_qty(get("line_qty", i) or "0")
        price = parse_money(get("line_price", i) or "0")
        if not desc and not item_id and not qty:
            continue
        if qty == 0 and price == 0:
            continue
        n += 1
        item = db.get(Item, item_id) if item_id else None
        if item and not desc:
            desc = item.name
        line = BillLine(
            bill_id=bill.id,
            line_no=n,
            item_id=item_id,
            description=desc,
            qty=qty,
            unit_price=price,
            discount_pct=(get("line_disc", i) or "0"),
            account_id=parse_id(get("line_account", i)) or _default_line_account(db, item),
            tax_code_id=parse_id(get("line_tax", i)),
            project_id=parse_id(get("line_project", i)),
            **_depth_fields(form, i),
        )
        db.add(line)
        bill.lines.append(line)
    db.flush()

    if n == 0:
        flash(request, "Add at least one line with a quantity and a price.", "danger")
        db.commit()
        return redirect(f"/purchases/{slug}/{bill.id}/edit")

    documents.recalc_bill(db, bill)
    audit(db, user, "CREATE" if is_new else "UPDATE", "Bill", bill.id,
          detail=f"{bill.number} {fmt(bill.total)}", ip=client_ip(request))

    if form.get("action") == "post" and doc_type != "PO":
        try:
            documents.post_bill(db, bill, user)
            audit(db, user, "POST", "Bill", bill.id, detail=bill.number, ip=client_ip(request))
            db.commit()
            msg = f"{bill.number} posted — {fmt(bill.total)}."
            if bill.wht_total:
                msg += f" Withhold {fmt(bill.wht_total)} when you pay."
            flash(request, msg)
            return redirect(f"/purchases/{slug}/{bill.id}")
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect(f"/purchases/{slug}/new")

    db.commit()
    flash(request, f"{bill.number} saved as a draft.")
    return redirect(f"/purchases/{slug}/{bill.id}")


@router.post("/{slug}/{doc_id}/post")
def post_doc(request: Request, slug: str, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    try:
        documents.post_bill(db, bill, user)
        audit(db, user, "POST", "Bill", bill.id, detail=bill.number, ip=client_ip(request))
        db.commit()
        flash(request, f"{bill.number} posted — {fmt(bill.total)}.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/purchases/{slug}/{doc_id}")


@router.post("/{slug}/{doc_id}/void")
async def void_doc(request: Request, slug: str, doc_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    form = await request.form()
    bill = db.get(Bill, doc_id)
    try:
        documents.void_bill(db, bill, parse_date(form.get("void_date"), date.today()), user)
        audit(db, user, "VOID", "Bill", bill.id, detail=bill.number, ip=client_ip(request))
        db.commit()
        flash(request, f"{bill.number} has been voided and reversed in the ledger.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/purchases/{slug}/{doc_id}")


@router.post("/{slug}/{doc_id}/delete")
def delete_draft(request: Request, slug: str, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    if bill and bill.status == DRAFT:
        number = bill.number
        audit(db, user, "DELETE", "Bill", bill.id, detail=number, ip=client_ip(request))
        # The files go with it, otherwise they sit on disk with nothing pointing at them.
        A.delete_all_for(db, request.state.company_slug, bill.doc_type, bill.id)
        db.delete(bill)
        db.commit()
        flash(request, f"Draft {number} deleted.", "warning")
    else:
        flash(request, "Only a draft can be deleted. Posted documents are voided.", "danger")
    return redirect(f"/purchases/{slug}")


@router.post("/bills/{doc_id}/debit-note")
def make_debit_note(request: Request, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    dn = documents.debit_note_from(db, bill, user)
    db.commit()
    flash(request, f"Draft debit note {dn.number} created — adjust the lines and post it.")
    return redirect(f"/purchases/debit-notes/{dn.id}/edit")


@router.get("/{slug}/{doc_id}/print")
def print_doc(request: Request, slug: str, doc_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    bill = db.get(Bill, doc_id)
    return render(request, "purchases/print.html", bill=bill,
                  label=DOC_LABELS[bill.doc_type][0])


# --------------------------------------------------------------------------
# Quick expense — a cash purchase with no supplier account involved
# --------------------------------------------------------------------------


@router.get("/expense/new")
def expense_new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    return render(
        request, "purchases/expense.html",
        vendors=list(
            db.scalars(select(Contact).where(Contact.is_active.is_(True)).order_by(Contact.name))
        ),
        accounts=list(
            db.scalars(
                select(Account).where(Account.type == "EXPENSE", Account.is_active.is_(True))
                .order_by(Account.code)
            )
        ),
        banks=list(
            db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.sort))
        ),
        vat_codes=vat_codes(db),
        projects=PROJ.choices(db),
        wht_codes=wht_codes(db),
        today=date.today(),
    )


@router.post("/expense/save")
async def expense_save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    on = parse_date(form.get("date"))
    net = parse_money(form.get("amount"))
    bank_id = parse_id(form.get("bank_account_id"))
    account_id = parse_id(form.get("account_id"))
    if not net or not bank_id or not account_id:
        flash(request, "Enter an amount, an expense account and the account it was paid from.", "danger")
        return redirect("/purchases/expense/new")

    bank = db.get(BankAccount, bank_id)
    contact_id = parse_id(form.get("contact_id"))
    contact = db.get(Contact, contact_id) if contact_id else None
    vat_id = parse_id(form.get("tax_code_id"))
    wht_id = parse_id(form.get("wht_code_id"))
    vat_code = db.get(TaxCode, vat_id) if vat_id else None
    wht_code = db.get(TaxCode, wht_id) if wht_id else None

    vat_amount = tax.vat_on(net, vat_code)
    wht_amount, wht_note = tax.wht_on(net, wht_code, contact)
    payee = contact.name if contact else (form.get("payee") or "Cash expense")
    memo = form.get("memo") or f"Expense — {payee}"

    number = next_number(db, "PAYMENT")
    draft = EntryDraft(date=on, memo=f"{memo} ({number})",
                       reference=(form.get("reference") or "").strip() or number,
                       source="PAYMENT")
    draft.debit(account_id, net, memo, contact_id=contact_id,
                tax_code_id=vat_code.id if vat_code else None, tax_base=net)
    if vat_amount:
        company = request.state.company
        target = sys_account(db, "VAT_INPUT" if company.is_vat_registered else "VAT_IRRECOVERABLE")
        draft.debit(target, vat_amount, f"Input VAT — {memo}", tax_base=net)
    if wht_amount:
        draft.credit(sys_account(db, "WHT_PAYABLE"), wht_amount,
                     f"WHT withheld from {payee}", contact_id=contact_id)
    draft.credit(bank.account_id, net + vat_amount - wht_amount, memo)

    try:
        entry = post_entry(db, draft, user=user)
        audit(db, user, "EXPENSE", "JournalEntry", entry.id,
              detail=f"{memo} {fmt(net + vat_amount)}", ip=client_ip(request))
        db.commit()
        msg = f"Expense recorded — {fmt(net + vat_amount - wht_amount)} paid from {bank.name}."
        if wht_amount:
            msg += f" {fmt(wht_amount)} withheld ({wht_note})."
        flash(request, msg)
        return redirect(f"/journals/{entry.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/purchases/expense/new")


# --------------------------------------------------------------------------
# Supplier payments
# --------------------------------------------------------------------------

payments = APIRouter(prefix="/payments")


@payments.get("")
def payment_index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    stmt = select(Payment).where(Payment.kind == PAYMENT)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Contact, Payment.contact_id == Contact.id).where(
            or_(Payment.number.ilike(like), Contact.name.ilike(like), Payment.reference.ilike(like))
        )
    pays = list(db.scalars(stmt.order_by(Payment.date.desc(), Payment.id.desc()).limit(300)))
    return render(
        request, "purchases/payments.html", payments=pays, q=q,
        total=sum(p.amount for p in pays if p.status != VOID),
        wht_total=sum(p.wht_amount for p in pays if p.status != VOID),
    )


@payments.get("/new")
def payment_new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    contact_id = parse_id(request.query_params.get("contact"))
    bill_id = parse_id(request.query_params.get("bill"))
    if bill_id and not contact_id:
        b = db.get(Bill, bill_id)
        contact_id = b.contact_id if b else None

    open_bills = []
    if contact_id:
        open_bills = list(
            db.scalars(
                select(Bill)
                .where(Bill.contact_id == contact_id, Bill.status.in_((POSTED, "PART_PAID")))
                .order_by(Bill.date)
            )
        )
    return render(
        request, "purchases/payment_form.html",
        vendors=list(
            db.scalars(
                select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
                .order_by(Contact.name)
            )
        ),
        banks=list(
            db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True)).order_by(BankAccount.sort))
        ),
        contact_id=contact_id, bill_id=bill_id, open_bills=open_bills, today=date.today(),
    )


@payments.post("/save")
async def payment_save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    contact_id = parse_id(form.get("contact_id"))
    bank_id = parse_id(form.get("bank_account_id"))
    if not contact_id or not bank_id:
        flash(request, "Choose a supplier and the account the money came from.", "danger")
        return redirect("/payments/new")

    pay = Payment(
        number=next_number(db, "PAYMENT"),
        kind=PAYMENT,
        contact_id=contact_id,
        date=parse_date(form.get("date")),
        bank_account_id=bank_id,
        method=form.get("method") or "Bank transfer",
        reference=(form.get("reference") or "").strip(),
        amount=parse_money(form.get("amount")),
        wht_amount=parse_money(form.get("wht_amount")),
        discount_amount=parse_money(form.get("discount_amount")),
        bank_charge=parse_money(form.get("bank_charge")),
        memo=form.get("memo") or "",
        created_by_id=user.id,
    )
    db.add(pay)
    db.flush()

    ids = form.getlist("alloc_bill_id")
    amounts = form.getlist("alloc_amount")
    any_alloc = False
    for i, raw in enumerate(ids):
        bid = parse_id(raw)
        amt = parse_money(amounts[i] if i < len(amounts) else "0")
        if bid and amt:
            db.add(PaymentAllocation(payment_id=pay.id, bill_id=bid, amount=amt))
            any_alloc = True
    db.flush()
    if any_alloc:
        db.refresh(pay)
        from .sales import _spread_credits

        _spread_credits(pay)
    else:
        cash.auto_allocate(db, pay)

    try:
        cash.post_payment(db, pay, user)
        audit(db, user, "POST", "Payment", pay.id,
              detail=f"{pay.number} {fmt(pay.amount)}", ip=client_ip(request))
        db.commit()
        msg = f"Payment {pay.number} recorded — {fmt(pay.amount)} paid."
        if pay.wht_amount:
            msg += f" {fmt(pay.wht_amount)} withheld and due to the NRS by the 21st of next month."
        flash(request, msg)
        return redirect(f"/payments/{pay.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/payments/new")


@payments.get("/{pay_id}")
def payment_detail(request: Request, pay_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    pay = db.get(Payment, pay_id)
    if pay is None:
        return redirect("/payments")
    entry = db.get(JournalEntry, pay.journal_entry_id) if pay.journal_entry_id else None
    return render(request, "purchases/payment_detail.html", pay=pay, entry=entry,
                  files=A.list_for(db, "PAYMENT", pay.id))


@payments.post("/{pay_id}/void")
async def payment_void(request: Request, pay_id: int):
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
    return redirect(f"/payments/{pay_id}")
