"""Deleting a record for good, and filing the paperwork that never was one.

Deletion is the exception to everything else in this software, so it is tested
harder than the rules it breaks. Two properties matter above all:

  * **Only one kind of person can do it.** Not a role — a flag, granted by name.
  * **The books survive it.** An invoice can be destroyed; a trial balance that
    no longer balances is a different and much worse thing, and no amount of
    "the owner asked for it" makes a set of accounts that disagrees with itself
    acceptable. Every deletion test below checks the ledger afterwards.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-del-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod, store  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    Company,
    Contact,
    FiledDocument,
    Invoice,
    InvoiceLine,
    JournalEntry,
    JournalLine,
    User,
)
from app.money import to_minor as M  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.security import P_DELETE, can, hash_password  # noqa: E402
from app.services import deletion, documents, reports  # noqa: E402
from app.services.posting import PostingError, next_number, sys_account  # noqa: E402


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-del-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
        db.get(Company, 1).setup_complete = True
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        yield c


def an_invoice(amount="500,000", when=None, post=True) -> int:
    slug = dbmod.current_slug() or registry.default_slug()
    dbmod.init_db(slug)
    with dbmod.session_scope_for(slug) as db:
        contact = db.scalar(select(Contact).where(Contact.name == "Zenith Ltd"))
        if contact is None:
            contact = Contact(code=next_number(db, "CONTACT"), name="Zenith Ltd",
                              is_customer=True)
            db.add(contact)
            db.flush()
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=contact.id, date=when or date.today(),
                      due_date=when or date.today(), status=DRAFT)
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Training",
                           qty=1000, unit_price=M(amount),
                           account_id=sys_account(db, "SALES").id))
        db.flush()
        db.refresh(inv)
        documents.recalc_invoice(db, inv)
        if post:
            documents.post_invoice(db, inv)
        db.commit()
        return inv.id


def trial_balance():
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        rows, dr, cr = reports.trial_balance(db, None, date.today() + timedelta(days=400))
        return dr, cr


def orphan_lines() -> int:
    """Journal lines whose entry is gone. Must always be nought."""
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        return db.scalar(
            select(func.count(JournalLine.id)).where(
                ~JournalLine.entry_id.in_(select(JournalEntry.id)))) or 0


# --------------------------------------------------------------------------
# Who is allowed
# --------------------------------------------------------------------------


def test_the_first_administrator_is_a_super_administrator(home):
    with dbmod.session_scope_for(registry.default_slug()) as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin.is_super_admin


def test_an_ordinary_administrator_cannot_delete(home):
    """Being promoted to administrator must not confer this by accident."""
    plain = User(username="tunde", role="admin", is_active=True,
                 password_hash=hash_password("x"))
    assert not can(plain, P_DELETE)


def test_the_flag_alone_is_not_enough_without_the_role(home):
    """Somebody who cannot be held responsible for the settings should not hold this."""
    clerk = User(username="ada", role="clerk", is_active=True, is_super_admin=True,
                 password_hash=hash_password("x"))
    assert not can(clerk, P_DELETE)


def test_a_clerk_is_refused_at_the_service_layer_not_only_the_screen(home):
    doc_id = an_invoice()
    clerk = User(username="ada", role="clerk", is_active=True,
                 password_hash=hash_password("x"))
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        with pytest.raises(PostingError, match="super administrator"):
            deletion.delete_invoice(db, db.get(Invoice, doc_id), clerk)


def test_the_last_super_administrator_cannot_be_stood_down(client):
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        uid = db.scalar(select(User).where(User.username == "admin")).id
    page = client.post("/settings/users/save", data={
        "id": str(uid), "username": "admin", "role": "admin", "is_active": "on",
        # is_super_admin deliberately absent
    }, follow_redirects=True)
    assert "only super administrator" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(User, uid).is_super_admin


def test_the_flag_cannot_be_given_to_somebody_who_is_not_an_administrator(client):
    page = client.post("/settings/users/save", data={
        "username": "ada", "role": "clerk", "is_active": "on",
        "is_super_admin": "1"}, follow_redirects=True)
    assert "Only an administrator" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(User).where(User.username == "ada")) is None


# --------------------------------------------------------------------------
# What deletion does to the books
# --------------------------------------------------------------------------


def test_the_trial_balance_still_agrees_after_a_deletion(client):
    """The single most important test in this file."""
    doc_id = an_invoice("500,000")
    before_dr, before_cr = trial_balance()
    assert before_dr == before_cr and before_dr > 0

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        number = db.get(Invoice, doc_id).number
    client.post(f"/sales/invoices/{doc_id}/delete", data={"confirm": number},
                follow_redirects=True)

    after_dr, after_cr = trial_balance()
    assert after_dr == after_cr, "the books must never be left disagreeing"
    assert after_dr == 0, "the invoice's own figures must have gone with it"
    assert orphan_lines() == 0


def test_nothing_is_left_pointing_at_the_deleted_invoice(client):
    doc_id = an_invoice()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        number = db.get(Invoice, doc_id).number
    client.post(f"/sales/invoices/{doc_id}/delete", data={"confirm": number},
                follow_redirects=True)

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(Invoice, doc_id) is None
        assert db.scalar(select(func.count(InvoiceLine.id))
                         .where(InvoiceLine.invoice_id == doc_id)) == 0
        assert db.scalar(select(func.count(JournalEntry.id))
                         .where(JournalEntry.source_id == doc_id,
                                JournalEntry.source == "INVOICE")) == 0


def test_a_voided_invoice_takes_both_of_its_entries_with_it(client):
    """A void leaves an entry and its reversal. Neither may be orphaned."""
    doc_id = an_invoice()
    client.post(f"/sales/invoices/{doc_id}/void",
                data={"void_date": date.today().isoformat(), "reason": "test"},
                follow_redirects=True)
    dr, cr = trial_balance()
    assert dr == cr

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        number = db.get(Invoice, doc_id).number
    client.post(f"/sales/invoices/{doc_id}/delete", data={"confirm": number},
                follow_redirects=True)

    after_dr, after_cr = trial_balance()
    assert after_dr == after_cr == 0
    assert orphan_lines() == 0


def test_a_draft_can_always_go(client):
    doc_id = an_invoice(post=False)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        number = db.get(Invoice, doc_id).number
    page = client.post(f"/sales/invoices/{doc_id}/delete", data={"confirm": number},
                       follow_redirects=True)
    assert "deleted" in page.text.lower()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(Invoice, doc_id) is None


# --------------------------------------------------------------------------
# When it must be refused
# --------------------------------------------------------------------------


def test_typing_the_wrong_number_deletes_nothing(client):
    """A dialog dismissed by reflex is not consent to destroy a record."""
    doc_id = an_invoice()
    page = client.post(f"/sales/invoices/{doc_id}/delete",
                       data={"confirm": "whatever"}, follow_redirects=True)
    assert "type its number exactly" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(Invoice, doc_id) is not None


def test_an_invoice_with_a_payment_on_it_is_refused(client):
    doc_id = an_invoice("100,000")
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        inv = db.get(Invoice, doc_id)
        inv.amount_paid = M("40,000")
        db.commit()
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        refused = deletion.why_not(db, db.get(Invoice, doc_id))
    assert "money allocated" in refused
    assert "receipt" in refused


def test_a_locked_period_is_refused_with_the_date_in_the_message(client):
    when = date.today() - timedelta(days=90)
    doc_id = an_invoice(when=when)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        db.get(Company, 1).lock_date = date.today() - timedelta(days=30)
        db.commit()

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        refused = deletion.why_not(db, db.get(Invoice, doc_id))
    assert "locked" in refused

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        number = db.get(Invoice, doc_id).number
    client.post(f"/sales/invoices/{doc_id}/delete", data={"confirm": number},
                follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(Invoice, doc_id) is not None, "a locked period must hold"


def test_the_screen_says_why_before_offering_the_button(client):
    doc_id = an_invoice("100,000")
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        db.get(Invoice, doc_id).amount_paid = M("40,000")
        db.commit()
    page = client.get(f"/sales/invoices/{doc_id}", follow_redirects=True).text
    assert "money allocated" in page
    assert "Delete it for good" not in page


def test_the_screen_offers_voiding_as_the_thing_to_do_instead(client):
    doc_id = an_invoice()
    page = client.get(f"/sales/invoices/{doc_id}", follow_redirects=True).text
    assert "void" in page.lower()
    assert "leaves no record" in page


# --------------------------------------------------------------------------
# The filing cabinet
# --------------------------------------------------------------------------


A_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def file_one(client, **over):
    data = {"kind": "INVOICE", "party": "Zenith Construction Ltd",
            "doc_date": "2025-03-14", "reference": "ZN-4471",
            "amount": "250,000", "note": "Contract bidding course"}
    data.update({k: v for k, v in over.items() if k != "into_books"})
    if over.get("into_books"):
        data["into_books"] = "1"
    return client.post("/archive/new", data=data,
                       files={"file": ("old-invoice.pdf", io.BytesIO(A_PDF),
                                       "application/pdf")},
                       follow_redirects=True)


def test_filing_a_document_keeps_it_out_of_the_ledger(client):
    before = trial_balance()
    page = file_one(client)
    assert "Filed" in page.text
    assert trial_balance() == before, "filing paperwork must not invent income"

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        row = db.scalar(select(FiledDocument))
        assert row.party == "Zenith Construction Ltd"
        assert row.reference == "ZN-4471"
        assert row.amount == M("250,000")
        assert row.doc_date == date(2025, 3, 14)
        assert not row.in_the_books


def test_the_scan_is_kept_and_can_be_opened_again(client):
    file_one(client)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        row = db.scalar(select(FiledDocument))
    from app.services import attachments as A

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        files = A.list_for(db, "FILED", row.id)
    assert len(files) == 1 and files[0].filename == "old-invoice.pdf"
    assert client.get(f"/attachments/{files[0].id}").status_code == 200


def test_an_unpaid_old_invoice_can_also_go_into_the_books(client):
    before_dr, _ = trial_balance()
    page = file_one(client, into_books=True)
    assert "money owed to you" in page.text

    after_dr, after_cr = trial_balance()
    assert after_dr == after_cr
    assert after_dr - before_dr == M("250,000")

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        row = db.scalar(select(FiledDocument))
        assert row.in_the_books
        invoice = db.get(Invoice, row.invoice_id)
        assert invoice.status != DRAFT, "it must be posted, or it owes nothing"
        assert invoice.date == date(2025, 3, 14), "dated as the paper is"
        assert invoice.contact.name == "Zenith Construction Ltd"


def test_the_customer_is_created_if_they_are_new(client):
    file_one(client, party="Somebody Quite New Ltd", into_books=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        contact = db.scalar(select(Contact).where(Contact.name == "Somebody Quite New Ltd"))
        assert contact is not None and contact.is_customer


def test_a_receipt_is_only_ever_filed(client):
    """Money that has already moved must not be entered as money still owed."""
    before = trial_balance()
    file_one(client, kind="RECEIPT", into_books=True)
    assert trial_balance() == before
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert not db.scalar(select(FiledDocument)).in_the_books


def test_going_into_the_books_needs_an_amount(client):
    page = file_one(client, amount="", into_books=True)
    assert "nothing to owe" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(func.count(FiledDocument.id))) == 0


def test_a_document_with_nobody_on_it_is_refused(client):
    page = file_one(client, party="")
    assert "who the document is with" in page.text


def test_the_archive_can_be_searched(client):
    file_one(client, party="Zenith Construction Ltd", reference="ZN-4471")
    file_one(client, party="Dangote Cement Plc", reference="DC-99", kind="RECEIPT")

    assert "Dangote" not in client.get("/archive?q=Zenith", follow_redirects=True).text
    assert "Dangote" in client.get("/archive?q=Dangote", follow_redirects=True).text
    assert "Zenith" not in client.get("/archive?kind=RECEIPT", follow_redirects=True).text


def test_only_a_super_administrator_can_take_something_out_of_the_archive(client):
    file_one(client)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        row_id = db.scalar(select(FiledDocument)).id

    # Sign in as somebody who is not one
    client.post("/settings/users/save", data={
        "username": "ada", "role": "admin", "is_active": "on"}, follow_redirects=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        ada = db.scalar(select(User).where(User.username == "ada"))
        ada.password_hash = hash_password("Lagos2026")
        ada.must_change_password = False
        db.commit()

    with TestClient(app) as other:
        other.post("/login", data={"username": "ada", "password": "Lagos2026"},
                   follow_redirects=True)
        refused = other.post(f"/archive/{row_id}/delete", follow_redirects=False)
    assert refused.status_code in (303, 403)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(FiledDocument, row_id) is not None


def test_one_that_is_in_the_books_is_not_quietly_removed_from_underneath_it(client):
    file_one(client, into_books=True)
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        row_id = db.scalar(select(FiledDocument)).id
    page = client.post(f"/archive/{row_id}/delete", follow_redirects=True)
    assert "Delete the invoice it made first" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.get(FiledDocument, row_id) is not None


# --------------------------------------------------------------------------
# Paying through a gateway
# --------------------------------------------------------------------------


def test_a_gateway_link_carries_the_amount_and_the_reference():
    g = store.Gateway("Paystack",
                      "https://pay.example/x?amount={amount_minor}&ref={reference}")
    link = g.link_for(228_000_00, "C6BA-1314", 6)
    assert "amount=22800000" in link
    assert "ref=C6BA-1314" in link


def test_the_major_unit_form_is_available_for_providers_that_want_it():
    g = store.Gateway("Flutterwave", "https://pay.example/y?a={amount}&c={currency}")
    assert "a=228000.00" in g.link_for(228_000_00, "X", 1)
    assert f"c={store.CURRENCY}" in g.link_for(228_000_00, "X", 1)


def test_a_reference_is_made_safe_for_a_url():
    g = store.Gateway("X", "https://pay.example/?ref={reference}")
    assert " " not in g.link_for(100, "AB CD/EF", 1).split("ref=")[1]


def test_bank_details_are_not_shown_unless_they_are_switched_on(client):
    """A gateway confirms itself; a transfer needs somebody to read a statement."""
    assert not store.bank_shown()
    page = client.get("/settings/licence", follow_redirects=True).text
    assert "Account number" not in page


def test_the_licence_screen_offers_every_gateway_that_is_set_up(client, monkeypatch):
    monkeypatch.setattr(store, "GATEWAYS", (
        store.Gateway("Paystack", "https://pay.example/p?amount={amount_minor}",
                      "Card, transfer or USSD"),
        store.Gateway("Flutterwave", "https://pay.example/f?amount={amount}"),
    ))
    page = client.get("/settings/licence?users=6", follow_redirects=True).text
    assert "Pay with Paystack" in page
    assert "Pay with Flutterwave" in page
    assert "Card, transfer or USSD" in page
    assert f"amount={store.quote(6).total}" in page
    assert "never sees or stores a card number" in page
