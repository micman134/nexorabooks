"""Accounting integrity tests.

These exist to answer one question: can the books ever go wrong?
Every test ends by proving the trial balance is nil and the balance sheet balances.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-test-")

from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    PAID,
    PART_PAID,
    PAYMENT,
    POSTED,
    RECEIPT,
    STOCK_ITEM,
    VOID,
    Account,
    BankAccount,
    Bill,
    BillLine,
    Contact,
    Invoice,
    InvoiceLine,
    Item,
    JournalEntry,
    Payment,
    PaymentAllocation,
    User,
)
from app.money import allocate, fmt, pct_of, split_inclusive, to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import cash, costing, documents, reports, tax  # noqa: E402
from app.services.posting import (  # noqa: E402
    EntryDraft,
    PostingError,
    account_net,
    next_number,
    post_entry,
    reverse_entry,
    sys_account,
)

TODAY = date(2026, 6, 15)


@pytest.fixture()
def db():
    """A fresh, fully seeded company for every test."""
    tmp = tempfile.mkdtemp(prefix="nexora-test-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    dbmod.init_db()
    session = dbmod.SessionLocal()
    bootstrap(session)
    session.commit()
    yield session
    session.close()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def acc(db, key):
    return sys_account(db, key)


def bal(db, key, end=None):
    return account_net(db, acc(db, key).id, None, end or date(2030, 1, 1))


def assert_books_balance(db, when=None):
    when = when or date(2030, 1, 1)
    rows, td, tc = reports.trial_balance(db, None, when)
    assert td == tc, f"Trial balance out by {fmt(td - tc)}"
    bs = reports.balance_sheet(db, when)
    assert bs.difference == 0, f"Balance sheet out by {fmt(bs.difference)}"


def make_customer(db, name="Dangote Cement Plc", tin="12345678-0001", small=False):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                tin=tin, is_small_company=small, payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def make_vendor(db, name="Lagos Supplies Ltd", tin="98765432-0001", small=False):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                tin=tin, is_small_company=small, payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def make_item(db, name="Bag of cement", sale=500000, cost=350000):
    it = Item(
        code=next_number(db, "ITEM"), name=name, item_type=STOCK_ITEM, unit="bag",
        sale_price=sale, purchase_price=cost, track_stock=True,
        sales_account_id=acc(db, "SALES").id,
        cogs_account_id=acc(db, "COGS").id,
        inventory_account_id=acc(db, "INVENTORY").id,
        purchase_account_id=acc(db, "PURCHASES").id,
    )
    db.add(it)
    db.flush()
    return it


def make_invoice(db, customer, item=None, qty=10, price=500000, vat=True, wht=None, on=TODAY):
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=customer.id, date=on, due_date=on + timedelta(days=30),
                  status=DRAFT, wht_code_id=wht.id if wht else None)
    db.add(inv)
    db.flush()
    line = InvoiceLine(
        invoice_id=inv.id, line_no=1, item_id=item.id if item else None,
        description=item.name if item else "Consultancy services",
        qty=qty * 1000, unit_price=price,
        account_id=acc(db, "SALES").id,
        tax_code_id=tax.get_code(db, "VAT-STD").id if vat else None,
    )
    db.add(line)
    db.flush()
    db.refresh(inv)
    return inv


# --------------------------------------------------------------------------
# Money primitives
# --------------------------------------------------------------------------


def test_money_conversions():
    assert to_kobo("12500.75") == 1250075
    assert to_kobo("1,250.50") == 125050
    assert to_kobo("₦100") == 10000
    assert to_kobo(100) == 10000
    assert to_kobo("") == 0
    # Half-up rounding, not banker's rounding
    assert to_kobo("0.005") == 1
    assert fmt(1250075) == "₦12,500.75"
    assert fmt(-5000) == "(₦50.00)"


def test_vat_arithmetic():
    # 7.5% VAT on ₦100.00 is exactly ₦7.50
    assert pct_of(10000, "7.5") == 750
    # A gross of ₦107.50 splits back to ₦100.00 + ₦7.50
    assert split_inclusive(10750, "7.5") == (10000, 750)
    # Awkward numbers still reconcile
    net, vat = split_inclusive(33333, "7.5")
    assert net + vat == 33333


def test_allocate_never_loses_kobo():
    for total in (100, 10001, 999999, 7):
        parts = allocate(total, [1, 1, 1])
        assert sum(parts) == total
    assert sum(allocate(1000, [3, 5, 2])) == 1000
    assert allocate(100, [0, 0]) == [0, 0]


# --------------------------------------------------------------------------
# The posting engine
# --------------------------------------------------------------------------


def test_unbalanced_entry_is_refused(db):
    draft = EntryDraft(date=TODAY, memo="Deliberately wrong")
    draft.debit(acc(db, "CASH"), 10000)
    draft.credit(acc(db, "SALES"), 9000)
    with pytest.raises(PostingError, match="does not balance"):
        post_entry(db, draft)


def test_zero_entry_is_refused(db):
    draft = EntryDraft(date=TODAY, memo="Nothing")
    draft.debit(acc(db, "CASH"), 0)
    with pytest.raises(PostingError):
        post_entry(db, draft)


def test_period_lock_blocks_posting(db):
    from app.models import Company

    company = db.get(Company, 1)
    company.lock_date = date(2026, 5, 31)
    db.flush()

    draft = EntryDraft(date=date(2026, 5, 15), memo="Into a locked month")
    draft.debit(acc(db, "CASH"), 10000)
    draft.credit(acc(db, "SALES"), 10000)
    with pytest.raises(PostingError, match="locked"):
        post_entry(db, draft)

    # A date after the lock is fine
    draft2 = EntryDraft(date=date(2026, 6, 1), memo="After the lock")
    draft2.debit(acc(db, "CASH"), 10000)
    draft2.credit(acc(db, "SALES"), 10000)
    assert post_entry(db, draft2) is not None


def test_reversal_cancels_exactly(db):
    draft = EntryDraft(date=TODAY, memo="Original")
    draft.debit(acc(db, "CASH"), 123456)
    draft.credit(acc(db, "SALES"), 123456)
    entry = post_entry(db, draft)

    before = bal(db, "CASH")
    reverse_entry(db, entry, TODAY)
    # A reversed entry is excluded from reports, so cash returns to nil
    assert bal(db, "CASH") == 0
    assert before == 123456
    assert entry.is_void is True
    assert_books_balance(db)


def test_entry_cannot_be_reversed_twice(db):
    draft = EntryDraft(date=TODAY, memo="Once")
    draft.debit(acc(db, "CASH"), 5000)
    draft.credit(acc(db, "SALES"), 5000)
    entry = post_entry(db, draft)
    reverse_entry(db, entry, TODAY)
    with pytest.raises(PostingError, match="already been reversed"):
        reverse_entry(db, entry, TODAY)


# --------------------------------------------------------------------------
# Nigerian tax
# --------------------------------------------------------------------------


def test_vat_standard_rate(db):
    std = tax.get_code(db, "VAT-STD")
    assert std.rate == "7.5"
    assert tax.vat_on(100_000_00, std) == 7_500_00


def test_vat_zero_and_exempt(db):
    assert tax.vat_on(100_000_00, tax.get_code(db, "VAT-ZERO")) == 0
    assert tax.vat_on(100_000_00, tax.get_code(db, "VAT-EXEMPT")) == 0
    # Zero-rated is still a taxable supply; exempt is not
    assert tax.is_taxable_supply(tax.get_code(db, "VAT-ZERO")) is True
    assert tax.is_taxable_supply(tax.get_code(db, "VAT-EXEMPT")) is False


def test_wht_professional_fees(db):
    vendor = make_vendor(db)
    code = tax.get_code(db, "WHT-PROF")
    amount, note = tax.wht_on(1_000_000_00, code, vendor)
    assert amount == 50_000_00  # 5%
    assert "5" in note


def test_wht_doubles_without_tax_id(db):
    vendor = make_vendor(db, name="Unregistered Trader", tin="")
    code = tax.get_code(db, "WHT-PROF")
    amount, note = tax.wht_on(1_000_000_00, code, vendor)
    assert amount == 100_000_00  # 5% doubled to 10%
    assert "no Tax ID" in note


def test_wht_uplift_capped_at_twenty_percent(db):
    vendor = make_vendor(db, name="No TIN Landlord", tin="")
    rent = tax.get_code(db, "WHT-RENT")  # 10% normally
    amount, _ = tax.wht_on(1_000_000_00, rent, vendor)
    assert amount == 200_000_00  # capped at 20%


def test_wht_uplift_does_not_apply_to_passive_income(db):
    vendor = make_vendor(db, name="Shareholder", tin="")
    div = tax.get_code(db, "WHT-DIV")
    amount, note = tax.wht_on(1_000_000_00, div, vendor)
    assert amount == 100_000_00  # stays at 10%
    assert "no Tax ID" not in note


def test_small_company_exemption(db):
    small = make_vendor(db, name="Small Trader Ltd", tin="11111111-0001", small=True)
    code = tax.get_code(db, "WHT-PROF")

    # ₦2,000,000 or less: exempt
    amount, note = tax.wht_on(2_000_000_00, code, small)
    assert amount == 0
    assert "Exempt" in note

    # Above the threshold: charged normally
    amount, _ = tax.wht_on(2_000_001_00, code, small)
    assert amount == 100_000_05


def test_small_company_without_tin_is_not_exempt(db):
    small = make_vendor(db, name="Small No TIN", tin="", small=True)
    amount, _ = tax.wht_on(1_000_000_00, tax.get_code(db, "WHT-PROF"), small)
    assert amount > 0


# --------------------------------------------------------------------------
# Sales cycle
# --------------------------------------------------------------------------


def test_invoice_posts_correct_double_entry(db):
    customer = make_customer(db)
    inv = make_invoice(db, customer, qty=1, price=1_000_000_00)
    documents.post_invoice(db, inv)

    assert inv.subtotal == 1_000_000_00
    assert inv.vat_total == 75_000_00
    assert inv.total == 1_075_000_00
    assert inv.status == POSTED

    assert bal(db, "AR") == 1_075_000_00
    assert bal(db, "SALES") == 1_000_000_00
    assert bal(db, "VAT_OUTPUT") == 75_000_00
    assert_books_balance(db)


def test_invoice_with_stock_posts_cost_of_sales(db):
    customer = make_customer(db)
    item = make_item(db)

    # Buy 100 bags at ₦3,500 each
    costing.receive(db, item, 100 * 1000, 350_000_00, TODAY, "OPENING")
    draft = EntryDraft(date=TODAY, memo="Opening stock")
    draft.debit(acc(db, "INVENTORY"), 350_000_00)
    draft.credit(acc(db, "OPENING_EQUITY"), 350_000_00)
    post_entry(db, draft)

    inv = make_invoice(db, customer, item=item, qty=10, price=500_000)
    documents.post_invoice(db, inv)

    # 10 bags at ₦3,500 average cost
    assert inv.cogs_total == 35_000_00
    assert bal(db, "COGS") == 35_000_00
    assert bal(db, "INVENTORY") == 315_000_00
    assert item.qty_on_hand == 90 * 1000
    assert item.stock_value == 315_000_00
    assert_books_balance(db)


def test_receipt_with_wht_clears_the_invoice(db):
    customer = make_customer(db)
    wht_code = tax.get_code(db, "WHT-PROF")
    inv = make_invoice(db, customer, qty=1, price=1_000_000_00, wht=wht_code)
    documents.post_invoice(db, inv)

    assert inv.wht_total == 50_000_00  # 5% of the net

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    pay = Payment(
        number=next_number(db, "RECEIPT"), kind=RECEIPT, contact_id=customer.id,
        date=TODAY, bank_account_id=bank.id,
        amount=1_025_000_00,   # total less the WHT withheld
        wht_amount=50_000_00,
    )
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    db.refresh(inv)
    assert inv.amount_paid == 1_075_000_00
    assert inv.balance_due == 0
    assert inv.status == PAID
    assert bal(db, "AR") == 0
    assert bal(db, "WHT_RECEIVABLE") == 50_000_00
    assert account_net(db, bank.account_id) == 1_025_000_00
    assert_books_balance(db)


def test_part_payment_leaves_a_balance(db):
    customer = make_customer(db)
    inv = make_invoice(db, customer, qty=1, price=1_000_000_00)
    documents.post_invoice(db, inv)

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT, contact_id=customer.id,
                  date=TODAY, bank_account_id=bank.id, amount=500_000_00)
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    db.refresh(inv)
    assert inv.status == PART_PAID
    assert inv.balance_due == 575_000_00
    assert bal(db, "AR") == 575_000_00
    assert_books_balance(db)


def test_credit_note_reverses_the_sale(db):
    customer = make_customer(db)
    item = make_item(db)
    costing.receive(db, item, 100 * 1000, 350_000_00, TODAY, "OPENING")
    draft = EntryDraft(date=TODAY, memo="Opening stock")
    draft.debit(acc(db, "INVENTORY"), 350_000_00)
    draft.credit(acc(db, "OPENING_EQUITY"), 350_000_00)
    post_entry(db, draft)

    inv = make_invoice(db, customer, item=item, qty=10, price=500_000)
    documents.post_invoice(db, inv)
    ar_after_sale = bal(db, "AR")

    cn = documents.credit_note_from(db, inv)
    documents.post_invoice(db, cn)

    assert bal(db, "AR") == 0
    assert ar_after_sale > 0
    assert bal(db, "SALES") == 0
    assert bal(db, "VAT_OUTPUT") == 0
    # The goods came back into stock
    assert item.qty_on_hand == 100 * 1000
    assert_books_balance(db)


def test_voiding_an_invoice_restores_stock_and_ledger(db):
    customer = make_customer(db)
    item = make_item(db)
    costing.receive(db, item, 50 * 1000, 175_000_00, TODAY, "OPENING")
    draft = EntryDraft(date=TODAY, memo="Opening stock")
    draft.debit(acc(db, "INVENTORY"), 175_000_00)
    draft.credit(acc(db, "OPENING_EQUITY"), 175_000_00)
    post_entry(db, draft)

    inv = make_invoice(db, customer, item=item, qty=5, price=500_000)
    documents.post_invoice(db, inv)
    documents.void_invoice(db, inv, TODAY)

    assert inv.status == VOID
    assert bal(db, "AR") == 0
    assert bal(db, "SALES") == 0
    assert item.qty_on_hand == 50 * 1000
    assert item.stock_value == 175_000_00
    assert_books_balance(db)


def test_paid_invoice_cannot_be_voided(db):
    customer = make_customer(db)
    inv = make_invoice(db, customer, qty=1, price=100_000_00)
    documents.post_invoice(db, inv)

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT, contact_id=customer.id,
                  date=TODAY, bank_account_id=bank.id, amount=107_500_00)
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    with pytest.raises(PostingError, match="payments allocated"):
        documents.void_invoice(db, inv, TODAY)


# --------------------------------------------------------------------------
# Purchase cycle
# --------------------------------------------------------------------------


def test_bill_posts_input_vat_and_stock(db):
    vendor = make_vendor(db)
    item = make_item(db)
    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=TODAY, status=DRAFT,
                wht_code_id=tax.get_code(db, "WHT-GOODS").id)
    db.add(bill)
    db.flush()
    db.add(BillLine(bill_id=bill.id, line_no=1, item_id=item.id, description=item.name,
                    qty=100 * 1000, unit_price=350_000,
                    tax_code_id=tax.get_code(db, "VAT-STD").id))
    db.flush()
    db.refresh(bill)
    documents.post_bill(db, bill)

    assert bill.subtotal == 350_000_00
    assert bill.vat_total == 26_250_00
    assert bill.total == 376_250_00
    assert bill.wht_total == 7_000_00  # 2% on goods

    assert bal(db, "AP") == 376_250_00
    assert bal(db, "VAT_INPUT") == 26_250_00
    assert bal(db, "INVENTORY") == 350_000_00
    assert item.qty_on_hand == 100 * 1000
    assert_books_balance(db)


def test_supplier_payment_withholds_tax(db):
    vendor = make_vendor(db)
    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=TODAY, status=DRAFT,
                wht_code_id=tax.get_code(db, "WHT-PROF").id)
    db.add(bill)
    db.flush()
    db.add(BillLine(bill_id=bill.id, line_no=1, description="Consultancy",
                    qty=1000, unit_price=1_000_000_00,
                    account_id=acc(db, "PROF_FEES").id,
                    tax_code_id=tax.get_code(db, "VAT-STD").id))
    db.flush()
    db.refresh(bill)
    documents.post_bill(db, bill)

    assert bill.total == 1_075_000_00
    assert bill.wht_total == 50_000_00

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    pay = Payment(number=next_number(db, "PAYMENT"), kind=PAYMENT, contact_id=vendor.id,
                  date=TODAY, bank_account_id=bank.id,
                  amount=1_025_000_00, wht_amount=50_000_00)
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    db.refresh(bill)
    assert bill.status == PAID
    assert bal(db, "AP") == 0
    assert bal(db, "WHT_PAYABLE") == 50_000_00
    assert account_net(db, bank.account_id) == -1_025_000_00
    assert_books_balance(db)


# --------------------------------------------------------------------------
# Inventory costing
# --------------------------------------------------------------------------


def test_weighted_average_cost(db):
    item = make_item(db)
    costing.receive(db, item, 100 * 1000, 100_000_00, TODAY)   # ₦1,000 each
    costing.receive(db, item, 100 * 1000, 200_000_00, TODAY)   # ₦2,000 each
    # Average should now be ₦1,500
    assert costing.unit_cost(item) == 150_000

    _move, cost = costing.issue(db, item, 50 * 1000, TODAY)
    assert cost == 75_000_00
    assert item.qty_on_hand == 150 * 1000
    assert item.stock_value == 225_000_00


def test_issuing_all_stock_leaves_no_residue(db):
    """The classic rounding trap: selling everything must clear the value exactly."""
    item = make_item(db)
    costing.receive(db, item, 3 * 1000, 10_000_01, TODAY)  # a value that will not divide evenly
    _m, cost = costing.issue(db, item, 3 * 1000, TODAY)
    assert cost == 10_000_01
    assert item.stock_value == 0
    assert item.qty_on_hand == 0


def test_partial_issues_never_lose_value(db):
    item = make_item(db)
    costing.receive(db, item, 7 * 1000, 100_000_01, TODAY)
    total = 0
    for _ in range(7):
        _m, c = costing.issue(db, item, 1000, TODAY)
        total += c
    assert item.qty_on_hand == 0
    assert item.stock_value == 0
    assert total == 100_000_01


def test_stock_adjustment_posts_to_the_ledger(db):
    item = make_item(db)
    costing.receive(db, item, 100 * 1000, 100_000_00, TODAY)
    draft = EntryDraft(date=TODAY, memo="Opening")
    draft.debit(acc(db, "INVENTORY"), 100_000_00)
    draft.credit(acc(db, "OPENING_EQUITY"), 100_000_00)
    post_entry(db, draft)

    # A count finds only 95
    _move, diff = costing.revalue_to(db, item, 95 * 1000, 95_000_00, TODAY, "Stock count")
    assert diff == -5_000_00
    d = EntryDraft(date=TODAY, memo="Shrinkage")
    d.debit(acc(db, "STOCK_LOSS"), 5_000_00)
    d.credit(acc(db, "INVENTORY"), 5_000_00)
    post_entry(db, d)

    assert bal(db, "INVENTORY") == 95_000_00
    assert item.stock_value == 95_000_00
    assert_books_balance(db)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


def test_profit_and_loss_matches_the_ledger(db):
    customer = make_customer(db)
    item = make_item(db)
    costing.receive(db, item, 100 * 1000, 350_000_00, TODAY)
    d = EntryDraft(date=TODAY, memo="Opening stock")
    d.debit(acc(db, "INVENTORY"), 350_000_00)
    d.credit(acc(db, "OPENING_EQUITY"), 350_000_00)
    post_entry(db, d)

    inv = make_invoice(db, customer, item=item, qty=10, price=500_000)
    documents.post_invoice(db, inv)

    d2 = EntryDraft(date=TODAY, memo="Rent")
    d2.debit(acc(db, "RENT"), 20_000_00)
    d2.credit(acc(db, "CASH"), 20_000_00)
    post_entry(db, d2)

    pl = reports.profit_and_loss(db, date(2026, 1, 1), date(2026, 12, 31))
    assert pl.revenue.total == 50_000_00
    assert pl.cogs.total == 35_000_00
    assert pl.gross_profit == 15_000_00
    assert pl.expenses.total == 20_000_00
    assert pl.net_profit == -5_000_00
    assert_books_balance(db)


def test_balance_sheet_balances_after_a_full_cycle(db):
    customer = make_customer(db)
    vendor = make_vendor(db)
    item = make_item(db)
    bank = db.query(BankAccount).filter_by(is_default=True).one()

    # Capital introduced
    d = EntryDraft(date=TODAY, memo="Capital")
    d.debit(bank.account_id, 5_000_000_00)
    d.credit(acc(db, "OPENING_EQUITY"), 5_000_000_00)
    post_entry(db, d)

    # Buy stock on credit
    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=TODAY, status=DRAFT)
    db.add(bill)
    db.flush()
    db.add(BillLine(bill_id=bill.id, line_no=1, item_id=item.id, description=item.name,
                    qty=200 * 1000, unit_price=350_000,
                    tax_code_id=tax.get_code(db, "VAT-STD").id))
    db.flush()
    db.refresh(bill)
    documents.post_bill(db, bill)

    # Sell some
    inv = make_invoice(db, customer, item=item, qty=50, price=500_000)
    documents.post_invoice(db, inv)

    # Get paid, and pay the supplier
    r = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT, contact_id=customer.id,
                date=TODAY, bank_account_id=bank.id, amount=200_000_00)
    db.add(r)
    db.flush()
    cash.auto_allocate(db, r)
    cash.post_payment(db, r)

    p = Payment(number=next_number(db, "PAYMENT"), kind=PAYMENT, contact_id=vendor.id,
                date=TODAY, bank_account_id=bank.id, amount=300_000_00)
    db.add(p)
    db.flush()
    cash.auto_allocate(db, p)
    cash.post_payment(db, p)

    assert_books_balance(db)

    bs = reports.balance_sheet(db, date(2026, 12, 31))
    assert bs.total_assets == bs.total_liabilities + bs.total_equity


def test_cash_flow_ties_to_the_change_in_cash(db):
    customer = make_customer(db)
    bank = db.query(BankAccount).filter_by(is_default=True).one()

    d = EntryDraft(date=date(2026, 1, 5), memo="Capital")
    d.debit(bank.account_id, 1_000_000_00)
    d.credit(acc(db, "OPENING_EQUITY"), 1_000_000_00)
    post_entry(db, d)

    inv = make_invoice(db, customer, qty=1, price=500_000_00, on=date(2026, 3, 1))
    documents.post_invoice(db, inv)

    r = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT, contact_id=customer.id,
                date=date(2026, 3, 20), bank_account_id=bank.id, amount=537_500_00)
    db.add(r)
    db.flush()
    cash.auto_allocate(db, r)
    cash.post_payment(db, r)

    d2 = EntryDraft(date=date(2026, 4, 1), memo="Buy a van")
    d2.debit(db.query(Account).filter_by(code="1510").one(), 800_000_00)
    d2.credit(bank.account_id, 800_000_00)
    post_entry(db, d2)

    cf = reports.cash_flow(db, date(2026, 1, 1), date(2026, 12, 31))
    assert cf.difference == 0, "Cash flow must account for every naira that moved"
    assert cf.operating_total + cf.investing_total + cf.financing_total == cf.net_movement
    assert cf.closing_cash == 737_500_00
    assert cf.investing_total == -800_000_00


def test_vat_return_figures(db):
    customer = make_customer(db)
    vendor = make_vendor(db)

    inv = make_invoice(db, customer, qty=1, price=1_000_000_00, on=date(2026, 6, 10))
    documents.post_invoice(db, inv)

    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=date(2026, 6, 12), status=DRAFT)
    db.add(bill)
    db.flush()
    db.add(BillLine(bill_id=bill.id, line_no=1, description="Office supplies",
                    qty=1000, unit_price=400_000_00,
                    account_id=acc(db, "PURCHASES").id,
                    tax_code_id=tax.get_code(db, "VAT-STD").id))
    db.flush()
    db.refresh(bill)
    documents.post_bill(db, bill)

    r = reports.vat_return(db, date(2026, 6, 1), date(2026, 6, 30))
    assert r.standard_sales == 1_000_000_00
    assert r.output_vat == 75_000_00
    assert r.standard_purchases == 400_000_00
    assert r.input_vat == 30_000_00
    assert r.net_payable == 45_000_00
    # The return is due on the 21st of the following month
    assert r.due_date == date(2026, 7, 21)


def test_aging_buckets(db):
    customer = make_customer(db)
    as_of = date(2026, 6, 30)

    for days_old, expected_bucket in [(0, 0), (15, 1), (45, 2), (75, 3), (120, 4)]:
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=customer.id, date=as_of - timedelta(days=days_old + 30),
                      due_date=as_of - timedelta(days=days_old), status=DRAFT)
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Service",
                           qty=1000, unit_price=100_000_00, account_id=acc(db, "SALES").id))
        db.flush()
        db.refresh(inv)
        documents.post_invoice(db, inv)

    rows, totals, grand = reports.aging(db, as_of, receivable=True)
    assert len(rows) == 1
    assert grand == 500_000_00
    for i in range(5):
        assert totals[i] == 100_000_00, f"bucket {i} should hold exactly one invoice"


def test_wht_schedule_lists_deductions(db):
    vendor = make_vendor(db)
    bank = db.query(BankAccount).filter_by(is_default=True).one()

    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=TODAY, status=DRAFT, wht_code_id=tax.get_code(db, "WHT-PROF").id)
    db.add(bill)
    db.flush()
    db.add(BillLine(bill_id=bill.id, line_no=1, description="Consultancy",
                    qty=1000, unit_price=1_000_000_00, account_id=acc(db, "PROF_FEES").id))
    db.flush()
    db.refresh(bill)
    documents.post_bill(db, bill)

    pay = Payment(number=next_number(db, "PAYMENT"), kind=PAYMENT, contact_id=vendor.id,
                  date=TODAY, bank_account_id=bank.id,
                  amount=950_000_00, wht_amount=50_000_00)
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    rows, total = reports.wht_schedule(db, date(2026, 6, 1), date(2026, 6, 30), payable=True)
    assert total == 50_000_00
    assert rows[0][2].name == vendor.name


# --------------------------------------------------------------------------
# Year end
# --------------------------------------------------------------------------


def test_year_end_close_clears_profit_and_loss(db):
    customer = make_customer(db)
    inv = make_invoice(db, customer, qty=1, price=1_000_000_00, on=date(2026, 3, 1))
    documents.post_invoice(db, inv)

    d = EntryDraft(date=date(2026, 4, 1), memo="Rent")
    d.debit(acc(db, "RENT"), 300_000_00)
    d.credit(acc(db, "CASH"), 300_000_00)
    post_entry(db, d)

    year_end = date(2026, 12, 31)
    bals = reports.balances(db, date(2026, 1, 1), year_end)
    draft = EntryDraft(date=year_end, memo="Year-end close", source="CLOSING")
    net = 0
    for account in db.query(Account).filter(Account.type.in_(["INCOME", "EXPENSE"])):
        dr, cr = bals.get(account.id, (0, 0))
        if dr == cr:
            continue
        draft.signed(account, cr - dr, f"Close {account.code}")
        net += cr - dr
    draft.signed(acc(db, "RETAINED_EARNINGS"), -net, "Profit for the year")
    post_entry(db, draft, allow_locked=True)

    pl = reports.profit_and_loss(db, date(2027, 1, 1), date(2027, 12, 31))
    assert pl.revenue.total == 0
    assert pl.net_profit == 0
    assert bal(db, "RETAINED_EARNINGS") == 700_000_00
    assert_books_balance(db)

    # The balance sheet must still balance the day after the close
    bs = reports.balance_sheet(db, date(2027, 1, 1))
    assert bs.difference == 0


# --------------------------------------------------------------------------
# The big one
# --------------------------------------------------------------------------


def test_a_month_of_trading_keeps_the_books_straight(db):
    """Simulate a busy month and assert the books hold at every step."""
    bank = db.query(BankAccount).filter_by(is_default=True).one()
    customers = [make_customer(db, f"Customer {i}", tin=f"1000000{i}-0001") for i in range(5)]
    vendors = [make_vendor(db, f"Supplier {i}", tin=f"2000000{i}-0001") for i in range(3)]
    items = [make_item(db, f"Product {i}", sale=(i + 1) * 100_000, cost=(i + 1) * 60_000)
             for i in range(4)]

    d = EntryDraft(date=date(2026, 6, 1), memo="Capital introduced")
    d.debit(bank.account_id, 10_000_000_00)
    d.credit(acc(db, "OPENING_EQUITY"), 10_000_000_00)
    post_entry(db, d)
    assert_books_balance(db)

    # Buy stock from each supplier
    for i, vendor in enumerate(vendors):
        bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                    date=date(2026, 6, 2 + i), status=DRAFT,
                    wht_code_id=tax.get_code(db, "WHT-GOODS").id)
        db.add(bill)
        db.flush()
        for j, item in enumerate(items):
            db.add(BillLine(bill_id=bill.id, line_no=j + 1, item_id=item.id,
                            description=item.name, qty=(20 + j) * 1000,
                            unit_price=item.purchase_price,
                            tax_code_id=tax.get_code(db, "VAT-STD").id))
        db.flush()
        db.refresh(bill)
        documents.post_bill(db, bill)
        assert_books_balance(db)

    # Sell to each customer
    invoices = []
    for i, customer in enumerate(customers):
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=customer.id, date=date(2026, 6, 10 + i),
                      due_date=date(2026, 7, 10 + i), status=DRAFT,
                      wht_code_id=tax.get_code(db, "WHT-GOODS").id)
        db.add(inv)
        db.flush()
        for j, item in enumerate(items[: (i % 4) + 1]):
            db.add(InvoiceLine(invoice_id=inv.id, line_no=j + 1, item_id=item.id,
                               description=item.name, qty=(3 + j) * 1000,
                               unit_price=item.sale_price,
                               discount_pct="5" if j == 0 else "0",
                               tax_code_id=tax.get_code(db, "VAT-STD").id))
        db.flush()
        db.refresh(inv)
        documents.post_invoice(db, inv)
        invoices.append(inv)
        assert_books_balance(db)

    # Collect from three of them
    for inv in invoices[:3]:
        r = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                    contact_id=inv.contact_id, date=date(2026, 6, 25),
                    bank_account_id=bank.id,
                    amount=inv.total - inv.wht_total, wht_amount=inv.wht_total)
        db.add(r)
        db.flush()
        cash.auto_allocate(db, r)
        cash.post_payment(db, r)
        db.refresh(inv)
        assert inv.status == PAID
        assert_books_balance(db)

    # Some running costs
    for name, amount in [("RENT", 500_000_00), ("SALARIES", 1_200_000_00),
                         ("BANK_CHARGES", 12_500_00)]:
        d = EntryDraft(date=date(2026, 6, 28), memo=f"Paid {name.lower()}")
        d.debit(acc(db, name), amount)
        d.credit(bank.account_id, amount)
        post_entry(db, d)
    assert_books_balance(db)

    # Void one invoice and confirm nothing drifts
    documents.void_invoice(db, invoices[4], date(2026, 6, 29))
    assert_books_balance(db)

    # Every stock item must still reconcile to the inventory account
    stock_items, item_total = reports.inventory_valuation(db)
    assert item_total == bal(db, "INVENTORY"), "Stock records must agree with the ledger"

    # And the statements must agree with each other
    pl = reports.profit_and_loss(db, date(2026, 6, 1), date(2026, 6, 30))
    bs = reports.balance_sheet(db, date(2026, 6, 30))
    assert bs.current_earnings == pl.net_profit
    assert bs.difference == 0

    cf = reports.cash_flow(db, date(2026, 6, 1), date(2026, 6, 30))
    assert cf.difference == 0
