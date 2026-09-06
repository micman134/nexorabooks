"""VAT withheld at source by government and oil-and-gas customers.

The NRS appoints certain buyers — ministries, agencies, and companies in oil and
gas — to keep the VAT back on what they buy and remit it themselves. The supply
is still VATable and the output VAT is still yours to declare. What changes is
that you never see the cash: it goes straight to the Service, and it must come
off your return, or you would pay the same VAT twice.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-whv-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    PAID,
    RECEIPT,
    Account,
    BankAccount,
    Contact,
    Invoice,
    InvoiceLine,
    Payment,
    TaxCode,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import cash, documents, reports  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402

M = to_kobo


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-whv-")
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


def customer(db, name, *, withholds=False) -> Contact:
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                withholds_vat=withholds, payment_terms_days=30, tin="01234567-0001")
    db.add(c)
    db.flush()
    return c


def invoice(db, contact, amount="10,000,000", on=date(2026, 4, 10)) -> Invoice:
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=contact.id, date=on, due_date=on, status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Supply of materials",
                       qty=1000, unit_price=M(amount),
                       account_id=account_by_code(db, "4000").id, tax_code_id=vat.id))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    return inv


def receipt(db, contact, *, cash_amount, vat_withheld=0, wht=0,
            on=date(2026, 4, 25)) -> Payment:
    bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))
    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                  contact_id=contact.id, date=on, bank_account_id=bank.id,
                  amount=M(cash_amount), vat_withheld=vat_withheld, wht_amount=wht,
                  method="Bank transfer")
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)
    return pay


# --------------------------------------------------------------------------
# The invoice
# --------------------------------------------------------------------------


def test_an_ordinary_customer_pays_the_vat(db):
    ordinary = customer(db, "Zenith Construction Ltd")
    inv = invoice(db, ordinary)
    assert inv.vat_total == M("750,000")
    assert inv.vat_withheld_expected == 0
    assert inv.expected_cash == M("10,750,000")


def test_an_appointed_customer_keeps_the_vat_back(db):
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    inv = invoice(db, ministry)

    # The supply is still VATable and the output VAT is still declared
    assert inv.vat_total == M("750,000")
    assert inv.total == M("10,750,000")
    # But the cash that arrives is the net
    assert inv.vat_withheld_expected == M("750,000")
    assert inv.expected_cash == M("10,000,000")


def test_the_output_vat_is_still_posted_on_the_invoice(db):
    """The liability is yours. What changes is who pays it over."""
    ministry = customer(db, "NNPC Limited", withholds=True)
    inv = invoice(db, ministry)
    entry = db.get(__import__("app.models", fromlist=["x"]).JournalEntry,
                   inv.journal_entry_id)
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert credits["2200"] == M("750,000")     # Output VAT payable


# --------------------------------------------------------------------------
# The receipt
# --------------------------------------------------------------------------


def test_the_receipt_clears_the_invoice_in_full(db):
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    inv = invoice(db, ministry)

    pay = receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))

    assert cash.settled_total(pay) == M("10,750,000")
    assert inv.amount_paid == M("10,750,000")
    assert inv.balance_due == 0
    assert inv.status == PAID


def test_the_withheld_vat_becomes_a_credit_not_a_loss(db):
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    invoice(db, ministry)
    pay = receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))

    entry = db.get(__import__("app.models", fromlist=["x"]).JournalEntry,
                   pay.journal_entry_id)
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    assert debits["1020"] == M("10,000,000")      # cash actually received
    assert debits["1415"] == M("750,000")         # VAT withheld at source
    assert entry.total_debit == entry.total_credit


def test_the_business_does_not_pay_the_same_vat_twice(db):
    """The whole point: it comes off the return."""
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    invoice(db, ministry)
    receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))

    r = reports.vat_return(db, date(2026, 4, 1), date(2026, 4, 30))
    assert r.output_vat == M("750,000")
    assert r.vat_withheld == M("750,000")
    assert r.net_payable == 0                     # nothing left to pay


def test_a_month_with_both_kinds_of_customer(db):
    ordinary = customer(db, "Zenith Construction Ltd")
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)

    invoice(db, ordinary, amount="4,000,000")
    invoice(db, ministry, amount="10,000,000")

    receipt(db, ordinary, cash_amount="4,300,000")
    receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))

    r = reports.vat_return(db, date(2026, 4, 1), date(2026, 4, 30))
    assert r.output_vat == M("1,050,000")         # 300,000 + 750,000
    assert r.vat_withheld == M("750,000")
    assert r.net_payable == M("300,000")          # only the ordinary customer's VAT


def test_withholding_tax_and_withheld_vat_together(db):
    """A ministry keeps back both: 5% WHT on the net, and all the VAT."""
    ministry = customer(db, "Federal Ministry of Works", withholds=True)
    inv = invoice(db, ministry, amount="10,000,000")

    pay = receipt(db, ministry, cash_amount="9,500,000",
                  vat_withheld=M("750,000"), wht=M("500,000"))

    assert cash.settled_total(pay) == M("10,750,000")
    assert inv.balance_due == 0
    entry = db.get(__import__("app.models", fromlist=["x"]).JournalEntry,
                   pay.journal_entry_id)
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    assert debits["1410"] == M("500,000")         # WHT credit receivable
    assert debits["1415"] == M("750,000")         # VAT withheld at source
    assert debits["1020"] == M("9,500,000")


def test_voiding_the_receipt_puts_everything_back(db):
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    inv = invoice(db, ministry)
    pay = receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))
    assert inv.balance_due == 0

    cash.void_payment(db, pay, date(2026, 4, 28))
    assert inv.balance_due == M("10,750,000")

    rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert dr == cr
    by_code = {r.account.code: r.debit - r.credit for r in rows}
    assert by_code.get("1415", 0) == 0


def test_the_books_balance_throughout(db):
    ministry = customer(db, "Lagos State Ministry of Works", withholds=True)
    invoice(db, ministry)
    receipt(db, ministry, cash_amount="10,000,000", vat_withheld=M("750,000"))

    rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert dr == cr
    bs = reports.balance_sheet(db, date(2026, 12, 31))
    assert bs.difference == 0


def test_the_withheld_vat_account_exists_and_is_an_asset(db):
    acc = db.scalar(select(Account).where(Account.system_key == "VAT_WITHHELD"))
    assert acc is not None
    assert acc.code == "1415"
    assert acc.type == "ASSET"
    assert acc.is_system is True


def test_an_older_company_file_gains_the_account_on_start(db):
    """A company created before this existed must pick the account up quietly."""
    acc = db.scalar(select(Account).where(Account.code == "1415"))
    db.delete(acc)
    db.flush()
    assert db.scalar(select(Account).where(Account.code == "1415")) is None

    bootstrap(db)
    restored = db.scalar(select(Account).where(Account.code == "1415"))
    assert restored is not None
    assert restored.system_key == "VAT_WITHHELD"
