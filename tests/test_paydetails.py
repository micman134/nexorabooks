"""Telling a customer where to send the money.

An invoice that says what is owed but not where to pay it makes the customer
write back and ask, and every day of that is a day the money is not in the bank.

There was a sharper version of the problem here. The covering email this
software sends with an invoice says "using the account details shown on the
invoice" — and until now no invoice carried any. The words were written before
the block existed, so the software was politely directing people to something
that was not there.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-pay-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    BankAccount,
    Company,
    Contact,
    Invoice,
    InvoiceLine,
)
from app.money import to_minor as M  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import documents, pdfdocs  # noqa: E402
from app.services.posting import next_number, sys_account  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdftext import text_of  # noqa: E402


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-pay-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
        co = db.get(Company, 1)
        co.name = "Procert Academy Limited"
        co.setup_complete = True
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def set_up_account(**over):
    fields = {"bank_name": "Zenith Bank", "account_name": "Procert Academy Limited",
              "account_number": "1012345678", "branch": "Ikoyi",
              "show_on_invoices": True}
    fields.update(over)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        bank = db.scalar(select(BankAccount))
        for key, value in fields.items():
            setattr(bank, key, value)
        db.commit()
        return bank.id


def a_document(doc_type="INVOICE", amount="250,000") -> int:
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        contact = db.scalar(select(Contact).where(Contact.name == "Zenith Construction Ltd"))
        if contact is None:
            contact = Contact(code=next_number(db, "CONTACT"),
                              name="Zenith Construction Ltd", is_customer=True)
            db.add(contact)
            db.flush()
        doc = Invoice(number=next_number(db, "INVOICE"), doc_type=doc_type,
                      contact_id=contact.id, date=date.today(),
                      due_date=date.today(), status=DRAFT)
        db.add(doc)
        db.flush()
        db.add(InvoiceLine(invoice_id=doc.id, line_no=1, description="Training",
                           qty=1000, unit_price=M(amount),
                           account_id=sys_account(db, "SALES").id))
        db.flush()
        db.refresh(doc)
        documents.recalc_invoice(db, doc)
        if doc_type == "INVOICE":
            documents.post_invoice(db, doc)
        db.commit()
        return doc.id


def printed(doc_id: int) -> str:
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        pdf = pdfdocs.invoice_pdf(db, db.get(Invoice, doc_id),
                                  slug=dbmod.current_slug())
    return text_of(pdf)


# --------------------------------------------------------------------------
# On the invoice itself
# --------------------------------------------------------------------------


def test_the_bank_details_are_printed_on_the_invoice(home):
    set_up_account()
    page = printed(a_document())
    assert "How to pay" in page
    assert "Zenith Bank" in page
    assert "1012345678" in page
    assert "Procert Academy Limited" in page


def test_the_note_under_them_is_printed_too(home):
    set_up_account()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        db.get(Company, 1).payment_instructions = "Quote INV-00001 as your reference."
        db.commit()
    assert "Quote INV-00001 as your reference." in printed(a_document())


def test_a_company_that_has_filled_in_nothing_prints_nothing(home):
    """No half-finished block, and no empty headings."""
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        db.get(Company, 1).payment_instructions = ""
        db.commit()
    assert "How to pay" not in printed(a_document())


def test_an_account_not_ticked_for_invoices_stays_private(home):
    """A savings account is not something customers should be paying into."""
    set_up_account(show_on_invoices=False)
    assert "1012345678" not in printed(a_document())


def test_an_account_with_no_number_is_not_printed_half_finished(home):
    set_up_account(account_number="")
    page = printed(a_document())
    assert "Zenith Bank" not in page


def test_empty_fields_are_left_out_rather_than_printed_as_blanks(home):
    """"IBAN: —" tells a customer nothing except that somebody was careless."""
    set_up_account(branch="", swift="", iban="", sort_code="")
    page = printed(a_document())
    assert "IBAN" not in page
    assert "SWIFT" not in page
    assert "Branch" not in page
    assert "1012345678" in page


def test_details_for_a_customer_abroad_are_printed_when_they_are_there(home):
    set_up_account(swift="ZEIBNGLA", iban="NG29ZEIB0000001012345678")
    page = printed(a_document())
    assert "SWIFT/BIC" in page and "ZEIBNGLA" in page
    assert "IBAN" in page and "NG29ZEIB0000001012345678" in page


def test_two_accounts_are_both_offered(home):
    """A business with a naira and a dollar account needs both on the page."""
    set_up_account()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        first = db.scalar(select(BankAccount))
        db.add(BankAccount(name="Domiciliary", account_id=first.account_id + 1,
                           bank_name="GTBank", account_name="Procert Academy Limited",
                           account_number="0223344556", currency_code="USD",
                           show_on_invoices=True, is_active=True))
        db.commit()
    page = printed(a_document())
    assert "1012345678" in page and "0223344556" in page


def test_a_quotation_does_not_ask_to_be_paid(home):
    """Nothing is owed on a quotation, so nothing should invite payment."""
    set_up_account()
    assert "How to pay" not in printed(a_document("QUOTE"))


def test_a_credit_note_does_not_ask_to_be_paid(home):
    """Money on a credit note goes the other way."""
    set_up_account()
    assert "How to pay" not in printed(a_document("CREDIT_NOTE"))


def test_a_long_invoice_still_gets_its_payment_block(home):
    """It must not fall off the bottom of a page that is already full."""
    set_up_account()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        contact = Contact(code=next_number(db, "CONTACT"), name="Big Order Ltd",
                          is_customer=True)
        db.add(contact)
        db.flush()
        doc = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=contact.id, date=date.today(),
                      due_date=date.today(), status=DRAFT)
        db.add(doc)
        db.flush()
        for n in range(1, 41):
            db.add(InvoiceLine(invoice_id=doc.id, line_no=n,
                               description=f"Delegate {n} — contract bidding course",
                               qty=1000, unit_price=M("50,000"),
                               account_id=sys_account(db, "SALES").id))
        db.flush()
        db.refresh(doc)
        documents.recalc_invoice(db, doc)
        documents.post_invoice(db, doc)
        db.commit()
        doc_id = doc.id
    page = printed(doc_id)
    assert "How to pay" in page
    assert "1012345678" in page


# --------------------------------------------------------------------------
# Setting it up
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        yield c


def test_the_company_screen_warns_when_no_account_is_being_shown(client):
    page = client.get("/settings/company", follow_redirects=True).text
    assert "No account is being shown on your invoices" in page
    assert "where to send it" in page


def test_the_company_screen_lists_the_account_once_it_is_set(client):
    set_up_account()
    page = client.get("/settings/company", follow_redirects=True).text
    assert "1012345678" in page
    assert "No account is being shown" not in page


def test_the_details_can_be_saved_from_the_banking_screen(client):
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        bank = db.scalar(select(BankAccount))
        bank_id, account_id = bank.id, bank.account_id

    page = client.post("/banking/save", data={
        "id": str(bank_id), "name": "Zenith Current", "account_id": str(account_id),
        "bank_name": "Zenith Bank", "account_number": "1012345678",
        "account_name": "Procert Academy Limited", "branch": "Ikoyi",
        "swift": "zeibngla", "iban": "ng29 zeib 0000 0010 1234 5678",
        "account_type": "CURRENT", "currency_code": "NGN",
        "is_active": "1", "show_on_invoices": "1"}, follow_redirects=True)
    assert "appear on every invoice" in page.text

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        bank = db.get(BankAccount, bank_id)
        assert bank.show_on_invoices
        assert bank.swift == "ZEIBNGLA", "tidied to the form banks expect"
        assert bank.iban == "NG29ZEIB0000001012345678", "spaces taken out"


def test_ticking_it_without_the_details_says_so_rather_than_failing_quietly(client):
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        bank = db.scalar(select(BankAccount))
        bank_id, account_id = bank.id, bank.account_id

    page = client.post("/banking/save", data={
        "id": str(bank_id), "name": "Zenith Current", "account_id": str(account_id),
        "bank_name": "", "account_number": "", "account_type": "CURRENT",
        "currency_code": "NGN", "is_active": "1", "show_on_invoices": "1"},
        follow_redirects=True)
    assert "will not appear on invoices yet" in page.text


def test_the_note_can_be_saved_from_the_company_screen(client):
    client.post("/settings/company", data={
        "name": "Procert Academy Limited", "country_code": "NG",
        "payment_instructions": "Send proof of payment on WhatsApp.",
    }, follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(Company, 1).payment_instructions == \
            "Send proof of payment on WhatsApp."


def test_the_invoice_email_no_longer_points_at_something_that_is_not_there(home):
    """The wording and the document have to agree, or the customer is lost."""
    set_up_account()
    page = printed(a_document())
    assert "account details" not in page.lower() or "How to pay" in page
    assert "1012345678" in page, (
        "the covering email tells customers the account details are on the "
        "invoice — so they have to be on the invoice")
