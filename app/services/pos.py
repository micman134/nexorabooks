"""The till: selling over a counter, and counting the drawer afterwards.

A shop is not an office. Nobody raises an invoice, emails it, and waits thirty
days — somebody scans a bag of cement, takes two thousand naira, gives four
hundred back and calls the next customer. The screen has to keep up with that,
and the ledger has to end up telling the truth anyway.

So a till sale is not a special kind of transaction with its own private rules.
It is exactly what it looks like in the books of any business: **an invoice
that was paid the moment it was raised.** The same posting engine, the same
stock movement, the same VAT, the same receipt. What the till adds is the
speed, and one thing an invoice screen has no reason to do — a drawer that gets
counted.

That counting is the point of the session. Takings are only worth anything if
somebody compared what is in the drawer with what the till says should be
there, so:

  * nothing can be sold except inside an open session, which knows whose till
    it is and what float it started with;
  * every way a customer paid is recorded separately, because cash has to
    balance against a drawer and a card does not;
  * closing a session means entering what was actually counted. If that is less
    than expected, the difference is posted to an expense account with the
    word "short" on it. It is never quietly adjusted away — a till that is
    short by two thousand naira every Friday is exactly the thing this software
    exists to show its owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    DRAFT,
    EXPENSE,
    PAID,
    PART_PAID,
    POSTED,
    RECEIPT,
    STOCK_ITEM,
    TENDER_CASH,
    TILL_CLOSED,
    TILL_OPEN,
    Account,
    BankAccount,
    Contact,
    Invoice,
    InvoiceLine,
    Item,
    Payment,
    PaymentAllocation,
    TillSession,
    TillTender,
    User,
)
from . import cash as cash_service
from . import documents
from .posting import (
    EntryDraft,
    PostingError,
    audit,
    next_number,
    post_entry,
    sys_account,
)

#: The name of the customer a till sale is put against when nobody asks for a
#: receipt in their own name.
WALK_IN = "Walk-in customer"

#: How many results the search box shows. Enough to find it, few enough to read.
SEARCH_LIMIT = 24


class TillError(Exception):
    """Something about this sale or this session cannot be allowed."""


# --------------------------------------------------------------------------
# The pieces a sale is made of
# --------------------------------------------------------------------------


@dataclass
class Line:
    """One thing being sold."""

    item_id: int | None = None
    description: str = ""
    qty: int = 1000                       # milli-units
    unit_price: int = 0                   # minor units
    discount_pct: str = "0"
    account_id: int | None = None
    tax_code_id: int | None = None


@dataclass
class Tender:
    """One way this sale was paid for."""

    kind: str = TENDER_CASH
    amount: int = 0
    tendered: int = 0                     # what the customer handed over
    bank_account_id: int | None = None
    reference: str = ""

    @property
    def change(self) -> int:
        """What goes back across the counter. Never negative."""
        return max(0, self.tendered - self.amount) if self.kind == TENDER_CASH else 0


@dataclass
class Takings:
    """What one session has taken, by the way it was paid."""

    by_kind: dict[str, int] = field(default_factory=dict)
    sales: int = 0
    count: int = 0
    opening_float: int = 0

    @property
    def cash(self) -> int:
        return self.by_kind.get(TENDER_CASH, 0)

    @property
    def expected_cash(self) -> int:
        """What should be in the drawer: the float plus the cash taken."""
        return self.opening_float + self.cash

    @property
    def average_sale(self) -> int:
        return self.sales // self.count if self.count else 0


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def open_session(db: Session, user: User | None, cash_account: BankAccount,
                 name: str = "Till 1", opening_float: int = 0,
                 location_id: int | None = None) -> TillSession:
    """Start a shift. One open session per till, and it says whose it is."""
    if cash_account is None:
        raise TillError("Choose which cash account this till's drawer belongs to.")

    already = db.scalar(
        select(TillSession).where(TillSession.status == TILL_OPEN,
                                  TillSession.name == name))
    if already is not None:
        raise TillError(
            f"{name} is already open — {already.number}, opened "
            f"{already.opened_at:%d %b at %H:%M}"
            + (f" by {already.opened_by.display_name}." if already.opened_by else ".")
            + " Close it before opening another.")

    session = TillSession(
        number=next_number(db, "TILL"),
        name=(name or "Till 1")[:60],
        cash_account_id=cash_account.id,
        location_id=location_id,
        opened_by_id=user.id if user else None,
        opening_float=max(0, int(opening_float or 0)),
        status=TILL_OPEN,
    )
    db.add(session)
    db.flush()
    audit(db, user, "TILL_OPEN", "TillSession", session.id,
          detail=f"{session.name} with {session.opening_float} float")
    return session


def open_sessions(db: Session) -> list[TillSession]:
    return list(db.scalars(
        select(TillSession).where(TillSession.status == TILL_OPEN)
        .order_by(TillSession.opened_at)))


def session_for(db: Session, user: User | None) -> TillSession | None:
    """The session this person should be selling into, if there is one.

    Their own open session first; failing that, the only one there is. A shop
    with one till should never have to choose.
    """
    mine = [s for s in open_sessions(db)
            if user is not None and s.opened_by_id == user.id]
    if mine:
        return mine[0]
    everyone = open_sessions(db)
    return everyone[0] if len(everyone) == 1 else None


def takings(db: Session, session: TillSession) -> Takings:
    """What this session has taken so far, by tender."""
    out = Takings(opening_float=session.opening_float)
    rows = db.execute(
        select(TillTender.kind, func.coalesce(func.sum(TillTender.amount), 0))
        .where(TillTender.session_id == session.id)
        .group_by(TillTender.kind)).all()
    for kind, total in rows:
        out.by_kind[kind] = int(total)
        out.sales += int(total)
    out.count = int(db.scalar(
        select(func.count(func.distinct(TillTender.invoice_id)))
        .where(TillTender.session_id == session.id)) or 0)
    return out


def sales_of(db: Session, session: TillSession) -> list[Invoice]:
    """Every sale rung up on this session, newest first."""
    ids = list(db.scalars(
        select(TillTender.invoice_id).where(TillTender.session_id == session.id)))
    if not ids:
        return []
    return list(db.scalars(
        select(Invoice).where(Invoice.id.in_([i for i in ids if i]))
        .order_by(Invoice.id.desc())))


def _till_difference_account(db: Session) -> Account:
    """Where an over or a short goes. Made if the chart predates the till."""
    account = db.scalar(select(Account).where(Account.system_key == "TILL_DIFF"))
    if account is not None:
        return account
    account = db.scalar(select(Account).where(Account.code == "6960"))
    if account is None:
        account = Account(code="6960", name="Till Differences (Over and Short)",
                          type=EXPENSE, subtype="OPERATING_EXPENSE",
                          cashflow_class="OPERATING")
        db.add(account)
    account.system_key = "TILL_DIFF"
    account.is_system = True
    db.flush()
    return account


def close_session(db: Session, session: TillSession, counted: int,
                  user: User | None = None, banked: int = 0,
                  bank_account_id: int | None = None,
                  notes: str = "") -> TillSession:
    """Count the drawer, write down what was found, and say so in the ledger."""
    if not session.is_open:
        raise TillError(f"{session.number} was already closed.")

    counted = max(0, int(counted or 0))
    banked = max(0, int(banked or 0))
    figures = takings(db, session)

    session.expected_cash = figures.expected_cash
    session.counted_cash = counted
    session.difference = counted - figures.expected_cash
    session.notes = (notes or "")[:2000]
    session.closed_at = clock.now()
    session.closed_by_id = user.id if user else None
    session.status = TILL_CLOSED

    if session.difference:
        # An over is a small gain and a short is a small loss. Both go to the
        # same account so that a till which is short every week is one line on
        # the profit and loss, not a mystery spread across the ledger.
        drawer = db.get(BankAccount, session.cash_account_id)
        difference_account = _till_difference_account(db)
        short = session.difference < 0
        draft = EntryDraft(
            date=Date.today(),
            memo=(f"Till {session.name} {session.number} — drawer "
                  f"{'short' if short else 'over'} at close"),
            reference=session.number,
            source="TILL",
            source_id=session.id,
        )
        amount = abs(session.difference)
        if short:
            draft.debit(difference_account.id, amount, "Cash short")
            draft.credit(drawer.account_id, amount, f"{session.name} drawer")
        else:
            draft.debit(drawer.account_id, amount, f"{session.name} drawer")
            draft.credit(difference_account.id, amount, "Cash over")
        session.journal_entry_id = post_entry(db, draft, user=user).id

    if banked:
        if bank_account_id is None:
            raise TillError("Say which bank account the takings were paid into.")
        session.banked = banked
        session.bank_account_id = bank_account_id
        session.banking_entry_id = _bank_the_takings(
            db, session, banked, bank_account_id, user).id

    db.flush()
    audit(db, user, "TILL_CLOSE", "TillSession", session.id,
          detail=f"counted {counted}, expected {session.expected_cash}, "
                 f"difference {session.difference}")
    return session


def _bank_the_takings(db: Session, session: TillSession, amount: int,
                      bank_account_id: int, user: User | None):
    drawer = db.get(BankAccount, session.cash_account_id)
    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise TillError("That bank account no longer exists.")
    if bank.id == drawer.id:
        raise TillError("The takings cannot be paid into the same drawer.")

    draft = EntryDraft(
        date=Date.today(),
        memo=f"Takings from {session.name} {session.number} paid into {bank.name}",
        reference=session.number,
        source="TILL",
        source_id=session.id,
    )
    draft.debit(bank.account_id, amount, f"Takings from {session.name}")
    draft.credit(drawer.account_id, amount, f"{session.name} drawer")
    return post_entry(db, draft, user=user)


# --------------------------------------------------------------------------
# Selling
# --------------------------------------------------------------------------


def walk_in(db: Session) -> Contact:
    """The customer a counter sale is put against when there is no name."""
    found = db.scalar(select(Contact).where(Contact.name == WALK_IN))
    if found is None:
        found = Contact(code=next_number(db, "CONTACT"), name=WALK_IN,
                        is_customer=True, payment_terms_days=0,
                        notes="Created by the till for counter sales.")
        db.add(found)
        db.flush()
    return found


def search(db: Session, text: str) -> list[Item]:
    """Find something to sell, by barcode, code or name.

    A barcode that matches exactly comes back on its own: scanning is meant to
    put one thing in the basket, not open a list of near misses.
    """
    text = (text or "").strip()
    if not text:
        return []

    exact = db.scalar(select(Item).where(Item.barcode == text, Item.is_active.is_(True)))
    if exact is not None:
        return [exact]

    like = f"%{text}%"
    return list(db.scalars(
        select(Item)
        .where(Item.is_active.is_(True),
               or_(Item.name.ilike(like), Item.code.ilike(like),
                   Item.barcode.ilike(like)))
        .order_by(Item.name)
        .limit(SEARCH_LIMIT)))


def _line_from(db: Session, line: Line) -> InvoiceLine:
    item = db.get(Item, line.item_id) if line.item_id else None
    description = line.description or (item.name if item else "")
    if not description:
        raise TillError("Every line needs something on it.")

    price = line.unit_price
    if not price and item is not None:
        price = item.sale_price

    return InvoiceLine(
        line_no=1,
        item_id=item.id if item else None,
        description=description[:300],
        qty=int(line.qty or 0),
        unit_price=int(price or 0),
        discount_pct=str(line.discount_pct or "0"),
        account_id=line.account_id or (item.sales_account_id if item else None),
        tax_code_id=(line.tax_code_id
                     or (item.sale_tax_code_id if item else None)),
    )


def short_of_stock(db: Session, lines: list[Line]) -> list[str]:
    """What is being sold that the books say is not there.

    The sale still goes through — a shop cannot tell a customer holding the
    goods that the computer says they do not exist — but somebody is told, so
    the count can be corrected rather than drifting further.
    """
    wanted: dict[int, int] = {}
    for line in lines:
        if line.item_id:
            wanted[line.item_id] = wanted.get(line.item_id, 0) + int(line.qty or 0)

    warnings = []
    for item_id, qty in wanted.items():
        item = db.get(Item, item_id)
        if item is None or item.item_type != STOCK_ITEM or not item.track_stock:
            continue
        if qty > item.qty_on_hand:
            warnings.append(
                f"{item.name}: {item.qty_on_hand / 1000:g} {item.unit} on record, "
                f"{qty / 1000:g} being sold.")
    return warnings


def ring_up(db: Session, session: TillSession, lines: list[Line],
            tenders: list[Tender], user: User | None = None,
            contact_id: int | None = None, on: Date | None = None) -> Invoice:
    """Take a sale: raise the invoice, take the money, move the stock.

    All of it or none of it. The invoice and its payments are written in one
    transaction, because a sale that posted the goods out and lost the money is
    worse than a sale that failed.
    """
    if session is None or not session.is_open:
        raise TillError("Open a till before selling. Nothing can be rung up without one.")
    if not lines:
        raise TillError("There is nothing in this sale.")
    if not tenders:
        raise TillError("Say how the customer paid.")

    on = on or Date.today()
    contact = db.get(Contact, contact_id) if contact_id else None
    contact = contact or walk_in(db)

    invoice = Invoice(
        number=next_number(db, "INVOICE"),
        doc_type="INVOICE",
        contact_id=contact.id,
        date=on,
        due_date=on,
        status=DRAFT,
        location_id=session.location_id,
        reference=f"{session.name} · {session.number}",
        created_by_id=user.id if user else None,
    )
    db.add(invoice)
    db.flush()

    for number, line in enumerate(lines, start=1):
        row = _line_from(db, line)
        row.invoice_id = invoice.id
        row.line_no = number
        db.add(row)
    db.flush()
    db.refresh(invoice)

    documents.recalc_invoice(db, invoice)
    if invoice.total <= 0:
        raise TillError("A sale has to come to more than nothing.")

    paid = sum(max(0, int(t.amount or 0)) for t in tenders)
    if paid < invoice.total:
        raise TillError(
            "The payment is short of the sale. A counter sale is paid in full "
            "at the counter — for anything else, raise an invoice.")
    if paid > invoice.total:
        # More tendered than the sale: the extra is change, not income.
        raise TillError(
            "The payments add up to more than the sale. Cash handed over goes "
            "in the tendered box; what is recorded is what the sale came to.")

    documents.post_invoice(db, invoice, user=user)

    for tender in tenders:
        if tender.amount <= 0:
            continue
        _take(db, session, invoice, tender, contact, user, on)

    audit(db, user, "TILL_SALE", "Invoice", invoice.id,
          detail=f"{invoice.number} on {session.name}")
    db.flush()
    return invoice


def _take(db: Session, session: TillSession, invoice: Invoice, tender: Tender,
          contact: Contact, user: User | None, on: Date) -> Payment:
    """One tender: a real receipt in the ledger, and a row on the till."""
    account_id = tender.bank_account_id
    if tender.kind == TENDER_CASH or not account_id:
        account_id = session.cash_account_id
    if db.get(BankAccount, account_id) is None:
        raise TillError("That account for taking money no longer exists.")

    payment = Payment(
        number=next_number(db, "RECEIPT"),
        kind=RECEIPT,
        contact_id=contact.id,
        date=on,
        bank_account_id=account_id,
        method=dict(CASH="Cash", CARD="Card", TRANSFER="Bank transfer").get(
            tender.kind, tender.kind.title()),
        reference=(tender.reference or invoice.number)[:60],
        amount=int(tender.amount),
        memo=f"Counter sale {invoice.number} — {session.name}",
        status=POSTED,
        created_by_id=user.id if user else None,
    )
    db.add(payment)
    db.flush()
    db.add(PaymentAllocation(payment_id=payment.id, invoice_id=invoice.id,
                             amount=int(tender.amount)))
    db.flush()
    db.refresh(payment)
    cash_service.post_payment(db, payment, user=user)

    db.add(TillTender(
        session_id=session.id, invoice_id=invoice.id, payment_id=payment.id,
        kind=tender.kind, amount=int(tender.amount),
        tendered=int(tender.tendered or tender.amount), change=tender.change,
        reference=(tender.reference or "")[:60],
    ))
    db.flush()
    return payment


def refund(db: Session, session: TillSession, invoice: Invoice,
           user: User | None = None) -> Invoice:
    """Give a counter sale back: a credit note, and the cash out of the drawer.

    Nothing is edited and nothing is deleted. The sale stays on record and the
    refund stands beside it, which is the only version of this a shop owner can
    check afterwards.
    """
    if session is None or not session.is_open:
        raise TillError("Open a till before refunding anything.")
    if invoice.doc_type != "INVOICE" or invoice.status not in (POSTED, PART_PAID, PAID):
        raise TillError("Only a posted counter sale can be refunded.")

    already = db.scalar(select(Invoice).where(Invoice.credit_of_id == invoice.id))
    if already is not None:
        raise TillError(f"{invoice.number} was already refunded by {already.number}.")

    note = Invoice(
        number=next_number(db, "CREDIT_NOTE"),
        doc_type="CREDIT_NOTE",
        contact_id=invoice.contact_id,
        date=Date.today(),
        due_date=Date.today(),
        status=DRAFT,
        location_id=invoice.location_id,
        credit_of_id=invoice.id,
        reference=f"Refund of {invoice.number}",
        created_by_id=user.id if user else None,
    )
    db.add(note)
    db.flush()
    for number, line in enumerate(invoice.lines, start=1):
        db.add(InvoiceLine(
            invoice_id=note.id, line_no=number, item_id=line.item_id,
            description=line.description, qty=line.qty,
            unit_price=line.unit_price, discount_pct=line.discount_pct,
            account_id=line.account_id, tax_code_id=line.tax_code_id))
    db.flush()
    db.refresh(note)
    documents.recalc_invoice(db, note)
    documents.post_invoice(db, note, user=user)

    payment = Payment(
        number=next_number(db, "PAYMENT"),
        kind="PAYMENT",
        contact_id=invoice.contact_id,
        date=Date.today(),
        bank_account_id=session.cash_account_id,
        method="Cash",
        reference=note.number,
        amount=note.total,
        memo=f"Refund of counter sale {invoice.number} — {session.name}",
        status=POSTED,
        created_by_id=user.id if user else None,
    )
    db.add(payment)
    db.flush()
    db.add(PaymentAllocation(payment_id=payment.id, invoice_id=note.id,
                             amount=note.total))
    db.flush()
    db.refresh(payment)
    cash_service.post_payment(db, payment, user=user)

    db.add(TillTender(session_id=session.id, invoice_id=note.id,
                      payment_id=payment.id, kind=TENDER_CASH,
                      amount=-note.total, tendered=0, change=0,
                      reference=f"Refund of {invoice.number}"))
    audit(db, user, "TILL_REFUND", "Invoice", note.id,
          detail=f"{note.number} refunds {invoice.number}")
    db.flush()
    return note


def tenders_for(db: Session, invoice_id: int) -> list[TillTender]:
    return list(db.scalars(
        select(TillTender).where(TillTender.invoice_id == invoice_id)
        .order_by(TillTender.id)))


def till_accounts(db: Session) -> list[BankAccount]:
    """Accounts a drawer can be: cash first, because that is what a till is."""
    rows = list(db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                           .order_by(BankAccount.sort, BankAccount.name)))
    return sorted(rows, key=lambda b: (b.account_type != "CASH", b.sort, b.name))


__all__ = [
    "Line", "Tender", "Takings", "TillError", "close_session", "open_session",
    "open_sessions", "refund", "ring_up", "sales_of", "search", "session_for",
    "short_of_stock", "takings", "tenders_for", "till_accounts", "walk_in",
    "PostingError", "sys_account",
]
