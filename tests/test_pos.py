"""The till.

A counter sale is an invoice that was paid at once, so most of what is tested
here is that it really is one: the same ledger, the same stock movement, the
same VAT, the same receipt. The rest is the part a shop cares about and an
invoice screen has no reason to do — the drawer, and whether what is in it
matches what the till says should be.

The test that matters most is the one about a short drawer. Software that
quietly balances the till is worse than no software, because it hides the one
number the owner needs to see.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-pos-")

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    PAID,
    POSTED,
    STOCK_ITEM,
    TENDER_CARD,
    TENDER_CASH,
    TENDER_TRANSFER,
    Account,
    BankAccount,
    Contact,
    Invoice,
    Item,
    JournalLine,
    TillSession,
    TillTender,
    User,
)
from app.money import to_minor as M  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import costing, pos  # noqa: E402
from app.services.posting import next_number, sys_account  # noqa: E402


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-pos-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    with dbmod.session_scope_for(ref.slug) as session:
        yield session
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def drawer(db) -> BankAccount:
    from sqlalchemy import select
    return db.scalar(select(BankAccount).where(BankAccount.account_type == "CASH"))


def bank(db) -> BankAccount:
    from sqlalchemy import select
    return db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))


def user(db) -> User:
    from sqlalchemy import select
    return db.scalar(select(User))


def stock_item(db, name="Bag of cement", price=M("9,000"), qty=100,
               cost=M("6,500"), barcode="6001234567890") -> Item:
    item = Item(code=next_number(db, "ITEM"), name=name, item_type=STOCK_ITEM,
                barcode=barcode, sale_price=price, purchase_price=cost,
                track_stock=True, unit="bag",
                sales_account_id=sys_account(db, "SALES").id,
                inventory_account_id=sys_account(db, "INVENTORY").id,
                cogs_account_id=sys_account(db, "COGS").id)
    db.add(item)
    db.flush()
    if qty:
        costing.receive(db, item, qty * 1000, cost * qty, date.today(),
                        "OPENING", None, "OPEN", "Opening stock")
        db.flush()
    return item


def a_session(db, float_amount=M("20,000")) -> TillSession:
    return pos.open_session(db, user(db), drawer(db), "Till 1", float_amount)


def sell_one(db, session, item, qty=1000, tenders=None, contact_id=None):
    lines = [pos.Line(item_id=item.id, qty=qty)]
    if tenders is None:
        total = item.sale_price * qty // 1000
        tenders = [pos.Tender(kind=TENDER_CASH, amount=total, tendered=total)]
    return pos.ring_up(db, session, lines, tenders, user=user(db),
                       contact_id=contact_id)


def balance_of(db, account_id: int) -> int:
    from sqlalchemy import func, select

    from app.models import JournalEntry
    debit, credit = db.execute(
        select(func.coalesce(func.sum(JournalLine.debit), 0),
               func.coalesce(func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.account_id == account_id,
               JournalEntry.is_posted.is_(True))).one()
    return int(debit) - int(credit)


# --------------------------------------------------------------------------
# Opening a till
# --------------------------------------------------------------------------


def test_a_till_cannot_be_opened_twice(db):
    a_session(db)
    with pytest.raises(pos.TillError) as raised:
        a_session(db)
    assert "already open" in str(raised.value)


def test_two_different_tills_can_both_be_open(db):
    pos.open_session(db, user(db), drawer(db), "Till 1", M("20,000"))
    pos.open_session(db, user(db), drawer(db), "Till 2", M("10,000"))
    assert len(pos.open_sessions(db)) == 2


def test_nothing_can_be_sold_without_an_open_till(db):
    item = stock_item(db)
    with pytest.raises(pos.TillError) as raised:
        pos.ring_up(db, None, [pos.Line(item_id=item.id)],
                    [pos.Tender(amount=M("9,000"))])
    assert "Open a till" in str(raised.value)


def test_the_float_is_what_the_drawer_starts_with(db):
    session = a_session(db, M("20,000"))
    assert pos.takings(db, session).expected_cash == M("20,000")


# --------------------------------------------------------------------------
# Selling
# --------------------------------------------------------------------------


def test_a_counter_sale_is_an_invoice_that_is_already_paid(db):
    session = a_session(db)
    item = stock_item(db)
    invoice = sell_one(db, session, item)

    assert invoice.status == PAID
    assert invoice.total == M("9,000")
    assert invoice.amount_paid == invoice.total
    assert invoice.journal_entry_id


def test_the_money_lands_in_the_drawer(db):
    session = a_session(db)
    item = stock_item(db)
    before = balance_of(db, drawer(db).account_id)
    sell_one(db, session, item)
    assert balance_of(db, drawer(db).account_id) - before == M("9,000")


def test_the_stock_goes_out_and_the_cost_is_booked(db):
    session = a_session(db)
    item = stock_item(db, qty=100, cost=M("6,500"))
    sell_one(db, session, item, qty=2000)

    db.refresh(item)
    assert item.qty_on_hand == 98_000
    assert balance_of(db, sys_account(db, "COGS").id) == M("13,000")


def test_a_sale_can_be_paid_part_cash_and_part_card(db):
    session = a_session(db)
    item = stock_item(db)
    invoice = sell_one(db, session, item, tenders=[
        pos.Tender(kind=TENDER_CASH, amount=M("4,000"), tendered=M("5,000")),
        pos.Tender(kind=TENDER_CARD, amount=M("5,000"),
                   bank_account_id=bank(db).id, reference="4321"),
    ])

    assert invoice.amount_paid == M("9,000")
    kinds = {t.kind: t.amount for t in pos.tenders_for(db, invoice.id)}
    assert kinds == {TENDER_CASH: M("4,000"), TENDER_CARD: M("5,000")}
    assert balance_of(db, bank(db).account_id) == M("5,000")


def test_change_is_worked_out_and_recorded_but_never_posted(db):
    """The customer's change is not income and is not an expense — it simply
    never became the shop's money."""
    session = a_session(db)
    item = stock_item(db)
    invoice = sell_one(db, session, item, tenders=[
        pos.Tender(kind=TENDER_CASH, amount=M("9,000"), tendered=M("10,000"))])

    tender = pos.tenders_for(db, invoice.id)[0]
    assert tender.tendered == M("10,000")
    assert tender.change == M("1,000")
    assert tender.amount == M("9,000")
    assert balance_of(db, drawer(db).account_id) == M("9,000")


def test_a_card_payment_never_touches_the_drawer(db):
    session = a_session(db)
    item = stock_item(db)
    sell_one(db, session, item, tenders=[
        pos.Tender(kind=TENDER_CARD, amount=M("9,000"),
                   bank_account_id=bank(db).id)])
    assert balance_of(db, drawer(db).account_id) == 0
    assert pos.takings(db, session).expected_cash == M("20,000")   # just the float


def test_a_sale_paid_short_is_refused(db):
    session = a_session(db)
    item = stock_item(db)
    with pytest.raises(pos.TillError) as raised:
        sell_one(db, session, item, tenders=[
            pos.Tender(kind=TENDER_CASH, amount=M("5,000"))])
    assert "short of the sale" in str(raised.value)


def test_paying_more_than_the_sale_is_refused_rather_than_becoming_income(db):
    session = a_session(db)
    item = stock_item(db)
    with pytest.raises(pos.TillError):
        sell_one(db, session, item, tenders=[
            pos.Tender(kind=TENDER_CASH, amount=M("12,000"),
                       tendered=M("12,000"))])


def test_an_empty_sale_is_refused(db):
    session = a_session(db)
    with pytest.raises(pos.TillError):
        pos.ring_up(db, session, [], [pos.Tender(amount=100)])


def test_a_sale_with_no_payment_is_refused(db):
    session = a_session(db)
    item = stock_item(db)
    with pytest.raises(pos.TillError):
        pos.ring_up(db, session, [pos.Line(item_id=item.id)], [])


def test_a_counter_sale_goes_to_the_walk_in_customer_by_default(db):
    session = a_session(db)
    invoice = sell_one(db, session, stock_item(db))
    assert invoice.contact.name == pos.WALK_IN


def test_the_walk_in_customer_is_only_created_once(db):
    session = a_session(db)
    item = stock_item(db)
    sell_one(db, session, item)
    sell_one(db, session, item)
    from sqlalchemy import func, select
    assert db.scalar(select(func.count(Contact.id))
                     .where(Contact.name == pos.WALK_IN)) == 1


def test_a_named_customer_can_be_put_on_the_sale(db):
    session = a_session(db)
    contact = Contact(code=next_number(db, "CONTACT"), name="Mrs Adebayo",
                      is_customer=True)
    db.add(contact)
    db.flush()
    invoice = sell_one(db, session, stock_item(db), contact_id=contact.id)
    assert invoice.contact_id == contact.id


def test_selling_more_than_is_on_record_warns_but_still_sells(db):
    """A shop cannot tell a customer holding the goods that they do not exist."""
    session = a_session(db)
    item = stock_item(db, qty=2)
    lines = [pos.Line(item_id=item.id, qty=5000)]

    warnings = pos.short_of_stock(db, lines)
    assert warnings and "Bag of cement" in warnings[0]

    invoice = pos.ring_up(db, session, lines, [
        pos.Tender(kind=TENDER_CASH, amount=M("45,000"), tendered=M("45,000"))],
        user=user(db))
    assert invoice.status == PAID
    db.refresh(item)
    assert item.qty_on_hand == -3000


# --------------------------------------------------------------------------
# Finding something to sell
# --------------------------------------------------------------------------


def test_a_barcode_that_matches_exactly_comes_back_on_its_own(db):
    wanted = stock_item(db, "Bag of cement", barcode="6001234567890")
    stock_item(db, "Bag of cement 50kg", barcode="6009999999999", qty=0)
    found = pos.search(db, "6001234567890")
    assert [i.id for i in found] == [wanted.id]


def test_a_name_finds_everything_that_looks_like_it(db):
    stock_item(db, "Bag of cement", barcode="1", qty=0)
    stock_item(db, "Cement mixer", barcode="2", qty=0)
    stock_item(db, "Roofing sheet", barcode="3", qty=0)
    names = {i.name for i in pos.search(db, "cement")}
    assert names == {"Bag of cement", "Cement mixer"}


def test_searching_for_nothing_returns_nothing(db):
    stock_item(db)
    assert pos.search(db, "") == []
    assert pos.search(db, "   ") == []


# --------------------------------------------------------------------------
# Counting the drawer
# --------------------------------------------------------------------------


def test_the_expected_cash_is_the_float_plus_the_cash_taken(db):
    session = a_session(db, M("20,000"))
    item = stock_item(db)
    sell_one(db, session, item)                                   # 9,000 cash
    sell_one(db, session, item, tenders=[
        pos.Tender(kind=TENDER_CARD, amount=M("9,000"),
                   bank_account_id=bank(db).id)])                 # not cash

    figures = pos.takings(db, session)
    assert figures.cash == M("9,000")
    assert figures.expected_cash == M("29,000")
    assert figures.sales == M("18,000")
    assert figures.count == 2


def test_a_drawer_that_balances_posts_nothing_extra(db):
    session = a_session(db, M("20,000"))
    sell_one(db, session, stock_item(db))
    pos.close_session(db, session, counted=M("29,000"), user=user(db))

    assert session.difference == 0
    assert session.journal_entry_id is None
    assert session.status == "CLOSED"


def test_a_short_drawer_is_written_into_the_ledger_not_hidden(db):
    """The one number the owner needs to see."""
    session = a_session(db, M("20,000"))
    sell_one(db, session, stock_item(db))
    pos.close_session(db, session, counted=M("27,000"), user=user(db))

    assert session.expected_cash == M("29,000")
    assert session.difference == -M("2,000")
    assert session.is_short
    assert session.journal_entry_id

    from sqlalchemy import select
    shortfall = db.scalar(select(Account).where(Account.system_key == "TILL_DIFF"))
    assert balance_of(db, shortfall.id) == M("2,000")
    assert balance_of(db, drawer(db).account_id) == M("7,000")


def test_a_drawer_with_too_much_in_it_is_recorded_the_same_way(db):
    session = a_session(db, M("20,000"))
    sell_one(db, session, stock_item(db))
    pos.close_session(db, session, counted=M("29,500"), user=user(db))

    assert session.difference == M("500")
    from sqlalchemy import select
    account = db.scalar(select(Account).where(Account.system_key == "TILL_DIFF"))
    assert balance_of(db, account.id) == -M("500")


def test_the_takings_can_be_sent_to_the_bank_at_close(db):
    session = a_session(db, M("20,000"))
    sell_one(db, session, stock_item(db))
    pos.close_session(db, session, counted=M("29,000"), user=user(db),
                      banked=M("25,000"), bank_account_id=bank(db).id)

    assert session.banked == M("25,000")
    assert session.banking_entry_id
    assert balance_of(db, bank(db).account_id) == M("25,000")
    assert balance_of(db, drawer(db).account_id) == M("9,000") - M("25,000")


def test_takings_cannot_be_banked_into_the_drawer_they_came_from(db):
    session = a_session(db)
    sell_one(db, session, stock_item(db))
    with pytest.raises(pos.TillError):
        pos.close_session(db, session, counted=M("29,000"), user=user(db),
                          banked=M("5,000"), bank_account_id=drawer(db).id)


def test_a_closed_till_cannot_be_closed_again(db):
    session = a_session(db)
    pos.close_session(db, session, counted=M("20,000"), user=user(db))
    with pytest.raises(pos.TillError):
        pos.close_session(db, session, counted=M("20,000"), user=user(db))


def test_a_closed_till_cannot_be_sold_from(db):
    session = a_session(db)
    item = stock_item(db)
    pos.close_session(db, session, counted=M("20,000"), user=user(db))
    with pytest.raises(pos.TillError):
        sell_one(db, session, item)


# --------------------------------------------------------------------------
# Giving it back
# --------------------------------------------------------------------------


def test_a_refund_is_a_credit_note_and_cash_out_of_the_drawer(db):
    session = a_session(db)
    item = stock_item(db)
    sale = sell_one(db, session, item)
    note = pos.refund(db, session, sale, user=user(db))

    assert note.doc_type == "CREDIT_NOTE"
    assert note.credit_of_id == sale.id
    assert note.total == sale.total
    assert balance_of(db, drawer(db).account_id) == 0        # in, then out again
    db.refresh(item)
    assert item.qty_on_hand == 100_000                        # and back on the shelf


def test_the_original_sale_is_left_exactly_as_it_was(db):
    """Nothing is edited and nothing is deleted, so both sides stay checkable."""
    session = a_session(db)
    sale = sell_one(db, session, stock_item(db))
    number, total, entry = sale.number, sale.total, sale.journal_entry_id
    pos.refund(db, session, sale, user=user(db))

    db.refresh(sale)
    assert (sale.number, sale.total, sale.journal_entry_id) == (number, total, entry)
    assert sale.status == PAID


def test_the_same_sale_cannot_be_refunded_twice(db):
    session = a_session(db)
    sale = sell_one(db, session, stock_item(db))
    pos.refund(db, session, sale, user=user(db))
    with pytest.raises(pos.TillError) as raised:
        pos.refund(db, session, sale, user=user(db))
    assert "already refunded" in str(raised.value)


def test_a_refund_reduces_what_the_drawer_should_hold(db):
    session = a_session(db, M("20,000"))
    sale = sell_one(db, session, stock_item(db))
    pos.refund(db, session, sale, user=user(db))
    assert pos.takings(db, session).expected_cash == M("20,000")


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    """Signed in, with something on the shelf to sell."""
    import json as _json

    from fastapi.testclient import TestClient

    from app.main import app

    stock_item(db)
    db.commit()
    dbmod.reset_all()
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123",
                                   "next": "/"}, follow_redirects=True)
        assert r.status_code == 200
        c.json = _json
        yield c


def open_till(c, float_amount="20,000"):
    return c.post("/pos/open", data={"name": "Till 1",
                                     "opening_float": float_amount,
                                     "cash_account_id": "2"},
                  follow_redirects=True)


def test_the_till_screen_asks_you_to_open_one_first(client):
    r = client.get("/pos", follow_redirects=True)
    assert r.status_code == 200
    assert "Open the till" in r.text


def test_opening_a_till_leads_to_the_selling_screen(client):
    r = open_till(client)
    assert r.status_code == 200
    assert "Scan a barcode" in r.text
    assert "Take the money" in r.text


def test_the_search_the_till_calls_returns_what_it_needs(client):
    open_till(client)
    r = client.get("/pos/search?q=cement")
    assert r.status_code == 200
    found = r.json()["items"]
    assert found and found[0]["name"] == "Bag of cement"
    assert found[0]["price"] == 900_000
    assert "on_hand" in found[0] and "barcode" in found[0]


def test_a_barcode_returns_exactly_one_thing_to_put_in_the_basket(client):
    open_till(client)
    found = client.get("/pos/search?q=6001234567890").json()["items"]
    assert len(found) == 1


def test_a_sale_can_be_taken_from_the_screen(client):
    import json

    open_till(client)
    r = client.post("/pos/sell", data={
        "lines": json.dumps([{"item_id": 1, "qty": 2, "price": "9,000"}]),
        "tenders": json.dumps([{"kind": "CASH", "amount": "18,000",
                                "tendered": "20,000"}]),
        "contact_id": "",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "18,000.00 taken" in r.text
    assert "Print the receipt" in r.text


def test_a_sale_that_does_not_add_up_is_refused_with_a_reason(client):
    import json

    open_till(client)
    r = client.post("/pos/sell", data={
        "lines": json.dumps([{"item_id": 1, "qty": 1, "price": "9,000"}]),
        "tenders": json.dumps([{"kind": "CASH", "amount": "5,000"}]),
    }, follow_redirects=True)
    assert "short of the sale" in r.text


def test_selling_below_the_stock_record_says_so_but_keeps_the_sale(client):
    import json

    open_till(client)
    r = client.post("/pos/sell", data={
        "lines": json.dumps([{"item_id": 1, "qty": 500, "price": "9,000"}]),
        "tenders": json.dumps([{"kind": "CASH", "amount": "4,500,000",
                                "tendered": "4,500,000"}]),
    }, follow_redirects=True)
    assert "the stock record disagrees" in r.text
    assert "taken" in r.text


def test_the_receipt_can_be_printed_on_screen_and_as_a_pdf(client):
    import json

    open_till(client)
    client.post("/pos/sell", data={
        "lines": json.dumps([{"item_id": 1, "qty": 1, "price": "9,000"}]),
        "tenders": json.dumps([{"kind": "CASH", "amount": "9,000",
                                "tendered": "10,000"}]),
    }, follow_redirects=True)

    page = client.get("/pos/sale/1/receipt", follow_redirects=True)
    assert page.status_code == 200
    assert "Bag of cement" in page.text
    assert "Change" in page.text

    pdf = client.get("/pos/sale/1/receipt.pdf", follow_redirects=True)
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF")
    assert b"%%EOF" in pdf.content


def test_closing_the_till_from_the_screen_records_the_difference(client):
    import json

    open_till(client)
    client.post("/pos/sell", data={
        "lines": json.dumps([{"item_id": 1, "qty": 1, "price": "9,000"}]),
        "tenders": json.dumps([{"kind": "CASH", "amount": "9,000",
                                "tendered": "9,000"}]),
    }, follow_redirects=True)

    form = client.get("/pos/close", follow_redirects=True)
    assert "Count the drawer" in form.text
    assert "29,000.00" in form.text

    r = client.post("/pos/close", data={"counted": "27,500", "banked": "0",
                                        "notes": "Two customers, one mistake"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "short" in r.text
    assert "1,500.00" in r.text


def test_the_session_list_shows_what_happened(client):
    open_till(client)
    client.post("/pos/close", data={"counted": "20,000"}, follow_redirects=True)
    r = client.get("/pos/sessions", follow_redirects=True)
    assert "TILL-0001" in r.text
    assert "balanced" in r.text
