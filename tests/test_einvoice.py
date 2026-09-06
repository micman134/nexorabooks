"""Electronic invoicing: the document, the readiness check and the clearance.

The mandate makes an invoice something a business cannot issue on its own
authority any more. That changes what "correct" means here. A rounding error in
a report is embarrassing; a document that does not balance is refused by
somebody else's computer while a customer waits at the counter.

So these tests care most about three things: that the XML says exactly what the
books say, that a business is told what is missing before the deadline rather
than after, and that the rehearsal can never be mistaken for having filed.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import date
from xml.etree import ElementTree as ET

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-ei-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT, EI_CLEARED, EI_FAILED, EI_NOT_REQUIRED, EI_REJECTED,
    Company, Contact, Invoice, InvoiceLine, TaxCode,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import documents, einvoice, transmit, ubl  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402

M = to_kobo

NS = {
    "i": ubl.INVOICE_NS,
    "c": ubl.CREDIT_NS,
    "cac": ubl.CAC,
    "cbc": ubl.CBC,
}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def books():
    tmp = tempfile.mkdtemp(prefix="nexora-ei-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.set_current(ref.slug)
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    with dbmod.session_scope_for(ref.slug) as session:
        company = session.get(Company, 1)
        company.name = "Adeyemi Trading Ltd"
        company.legal_name = "Adeyemi Trading Limited"
        company.tin = "12345678-0001"
        company.rc_number = "RC123456"
        company.vat_reg_no = "VAT-99881"
        company.address = "14 Awolowo Road, Ikoyi"
        company.city = "Lagos"
        company.state = "Lagos"
    with dbmod.session_scope_for(ref.slug) as session:
        session.info["slug"] = ref.slug
        yield session
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def rehearsing(books):
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(mode=einvoice.REHEARSAL))
    return books


def customer(db, name="Dangote Cement Plc", *, tin="98765432-0001",
             address="Union Marble House, Falomo", city="Lagos",
             kind="COMPANY") -> Contact:
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                contact_type=kind, tin=tin, address=address, city=city,
                state="Lagos", email="accounts@example.ng", payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def invoice(db, contact, *, amount="1,000,000", qty=1000, post=True,
            on=date(2026, 4, 10), tax="VAT-STD", description="Supply of cement") -> Invoice:
    code = db.scalar(select(TaxCode).where(TaxCode.code == tax)) if tax else None
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=contact.id, date=on, due_date=on, status=DRAFT,
                  po_number="PO-8842", reference="Job 17")
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description=description,
                       qty=qty, unit_price=M(amount),
                       account_id=account_by_code(db, "4000").id,
                       tax_code_id=code.id if code else None))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    if post:
        documents.post_invoice(db, inv)
    return inv


def parse(xml: bytes):
    return ET.fromstring(xml)


def text_at(root, path: str) -> str:
    node = root.find(path, NS)
    return (node.text or "") if node is not None else ""


# --------------------------------------------------------------------------
# Formatting: the two conventions that are easy to get wrong
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kobo,expected", [
    (0, "0.00"),
    (1, "0.01"),
    (99, "0.99"),
    (100, "1.00"),
    (1234567, "12345.67"),
    (-1234567, "-12345.67"),
    (100_000_000_00, "100000000.00"),      # one hundred million naira
])
def test_money_is_written_the_way_ubl_wants_it(kobo, expected):
    assert ubl.amount(kobo) == expected


@pytest.mark.parametrize("milli,expected", [
    (1000, "1.000"), (2500, "2.500"), (1, "0.001"), (0, "0.000"), (-1500, "-1.500"),
])
def test_quantities_keep_their_thousandths(milli, expected):
    assert ubl.quantity(milli) == expected


def test_a_companys_own_number_style_never_reaches_the_file(books):
    """A business that prints ``12.345,67`` still files ``12345.67``.

    The company's display preferences are for humans. A validator reading a
    comma as a decimal point would see a different number entirely.
    """
    company = books.get(Company, 1)
    company.currency_thousands = "."
    company.currency_point = ","
    books.flush()
    c = customer(books)
    inv = invoice(books, c, amount="1,234,567.89")
    xml = einvoice.xml_for(books, inv).decode()
    assert "1234567.89" in xml
    assert "1.234.567,89" not in xml


def test_control_characters_are_stripped_from_descriptions(books):
    """A description pasted from a spreadsheet must not break the document."""
    c = customer(books)
    inv = invoice(books, c, description="Cement\x07 50kg\r\n  bags\x00")
    xml = einvoice.xml_for(books, inv)
    root = parse(xml)
    name = text_at(root, "cac:InvoiceLine/cac:Item/cbc:Name")
    assert name == "Cement 50kg bags"


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def test_an_invoice_becomes_a_ubl_invoice(books):
    c = customer(books)
    inv = invoice(books, c)
    root = parse(einvoice.xml_for(books, inv))

    assert root.tag == f"{{{ubl.INVOICE_NS}}}Invoice"
    assert text_at(root, "cbc:ID") == inv.number
    assert text_at(root, "cbc:IssueDate") == "2026-04-10"
    assert text_at(root, "cbc:InvoiceTypeCode") == ubl.COMMERCIAL_INVOICE
    assert text_at(root, "cbc:DocumentCurrencyCode") == "NGN"
    assert text_at(root, "cbc:BuyerReference") == "Job 17"
    assert text_at(root, "cac:OrderReference/cbc:ID") == "PO-8842"


def test_both_parties_carry_their_tin(books):
    c = customer(books)
    inv = invoice(books, c)
    root = parse(einvoice.xml_for(books, inv))

    supplier = root.find("cac:AccountingSupplierParty/cac:Party", NS)
    buyer = root.find("cac:AccountingCustomerParty/cac:Party", NS)
    assert text_at(supplier, "cbc:EndpointID") == "12345678-0001"
    assert text_at(buyer, "cbc:EndpointID") == "98765432-0001"
    assert text_at(supplier, "cac:PartyLegalEntity/cbc:RegistrationName") \
        == "Adeyemi Trading Limited"
    assert text_at(buyer, "cac:PostalAddress/cac:Country/cbc:IdentificationCode") == "NG"


def test_the_totals_in_the_file_are_the_totals_in_the_books(books):
    c = customer(books)
    inv = invoice(books, c, amount="1,000,000")
    root = parse(einvoice.xml_for(books, inv))

    assert text_at(root, "cac:LegalMonetaryTotal/cbc:LineExtensionAmount") \
        == ubl.amount(inv.subtotal)
    assert text_at(root, "cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount") \
        == ubl.amount(inv.total)
    assert text_at(root, "cac:LegalMonetaryTotal/cbc:PayableAmount") \
        == ubl.amount(inv.total)
    assert text_at(root, "cac:TaxTotal/cbc:TaxAmount") == ubl.amount(inv.vat_total)


def test_vat_at_seven_and_a_half_percent_is_declared_as_standard_rated(books):
    c = customer(books)
    inv = invoice(books, c)
    root = parse(einvoice.xml_for(books, inv))
    category = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
    assert text_at(category, "cbc:ID") == ubl.STANDARD_RATED
    assert text_at(category, "cbc:Percent") == "7.50"


def test_an_exempt_line_says_why_it_is_exempt(books):
    """Relief claimed without a reason is relief refused."""
    exempt = TaxCode(code="VAT-EX", name="Exempt", kind="VAT", rate="0", is_exempt=True)
    books.add(exempt)
    books.flush()
    c = customer(books)
    inv = invoice(books, c, tax="VAT-EX")
    root = parse(einvoice.xml_for(books, inv))
    category = root.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", NS)
    assert text_at(category, "cbc:ID") == ubl.EXEMPT
    assert text_at(category, "cbc:TaxExemptionReason")


def test_lines_of_the_same_rate_are_one_subtotal_not_several(books):
    c = customer(books)
    inv = invoice(books, c, post=False)
    vat = books.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    for n in (2, 3):
        books.add(InvoiceLine(invoice_id=inv.id, line_no=n, description=f"Extra {n}",
                              qty=1000, unit_price=M("100,000"),
                              account_id=account_by_code(books, "4000").id,
                              tax_code_id=vat.id))
    books.flush()
    books.refresh(inv)
    documents.recalc_invoice(books, inv)
    documents.post_invoice(books, inv)

    root = parse(einvoice.xml_for(books, inv))
    subtotals = root.findall("cac:TaxTotal/cac:TaxSubtotal", NS)
    assert len(subtotals) == 1, "three standard-rated lines are one subtotal"
    assert len(root.findall("cac:InvoiceLine", NS)) == 3


def test_a_credit_note_is_a_credit_note_and_names_what_it_reverses(books):
    c = customer(books)
    original = invoice(books, c)
    note = Invoice(number=next_number(books, "INVOICE"), doc_type="CREDIT_NOTE",
                   contact_id=c.id, date=date(2026, 4, 20), status=DRAFT,
                   credit_of_id=original.id)
    books.add(note)
    books.flush()
    vat = books.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    books.add(InvoiceLine(invoice_id=note.id, line_no=1, description="Returned goods",
                          qty=1000, unit_price=M("100,000"),
                          account_id=account_by_code(books, "4000").id,
                          tax_code_id=vat.id))
    books.flush()
    books.refresh(note)
    documents.recalc_invoice(books, note)
    documents.post_invoice(books, note)

    xml = einvoice.xml_for(books, note)
    root = parse(xml)
    assert root.tag == f"{{{ubl.CREDIT_NS}}}CreditNote"
    assert text_at(root, "cbc:CreditNoteTypeCode") == ubl.CREDIT_NOTE
    assert text_at(root, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID") \
        == original.number
    assert root.find("cac:CreditNoteLine/cbc:CreditedQuantity", NS) is not None


def test_the_same_invoice_always_produces_the_same_bytes(books):
    """A clearance is over specific bytes. They must not drift."""
    c = customer(books)
    inv = invoice(books, c)
    first = einvoice.xml_for(books, inv)
    second = einvoice.xml_for(books, inv)
    assert first == second
    assert ubl.fingerprint(first) == ubl.fingerprint(second)


def test_the_document_is_well_formed_xml_whatever_is_in_it(books):
    c = customer(books, name='O\'Brien & Sons "Nigeria" <Ltd>')
    inv = invoice(books, c, description="Bags & pallets <mixed> \"grade A\"")
    parse(einvoice.xml_for(books, inv))          # would raise if it were not


# --------------------------------------------------------------------------
# Readiness — the part that matters before July 2027
# --------------------------------------------------------------------------


def test_a_customer_with_no_tin_is_named_and_the_screen_to_fix_it_given(books):
    c = customer(books, name="Corner Shop Ltd", tin="")
    invoice(books, c)
    report = einvoice.readiness(books)
    assert not report.ready
    problem = next(p for p in report.blocking if "Corner Shop" in p.where)
    assert "Tax Identification Number" in problem.what
    assert problem.fix == f"/contacts/{c.id}"


def test_a_walk_in_individual_without_a_tin_is_not_a_blocker(books):
    """A shop cannot demand a TIN from somebody buying a bag of cement."""
    c = customer(books, name="Walk-in customer", tin="", kind="INDIVIDUAL")
    invoice(books, c)
    report = einvoice.readiness(books)
    assert all("Walk-in" not in p.where for p in report.blocking)


def test_a_company_with_no_tin_cannot_file_at_all(books):
    books.get(Company, 1).tin = ""
    books.flush()
    report = einvoice.readiness(books)
    assert not report.ready
    assert any("Tax Identification Number" in p.what for p in report.company)


def test_complete_books_are_reported_ready(books):
    c = customer(books)
    invoice(books, c)
    report = einvoice.readiness(books)
    assert report.ready, [str(p) for p in report.blocking]
    assert report.customers_checked == 1
    assert report.invoices_checked == 1


def test_one_bad_customer_is_reported_once_not_once_per_invoice(books):
    c = customer(books, name="Repeat Ltd", tin="")
    for _ in range(4):
        invoice(books, c)
    report = einvoice.readiness(books)
    missing = [p for p in report.all if "Repeat Ltd" in p.where
               and "Tax Identification" in p.what]
    assert len(missing) == 1


def test_a_line_with_no_tax_code_is_flagged(books):
    c = customer(books)
    inv = invoice(books, c, tax=None)
    problems = einvoice.check_invoice(books, inv)
    assert any("tax code" in p.what for p in problems)


# --------------------------------------------------------------------------
# Clearance
# --------------------------------------------------------------------------


def test_with_e_invoicing_off_nothing_is_filed(books):
    c = customer(books)
    inv = invoice(books, c)
    record = einvoice.submit(books, inv)
    assert record.status == EI_NOT_REQUIRED
    assert not record.irn


def test_a_rehearsal_clears_and_says_it_is_a_rehearsal(rehearsing):
    db = rehearsing
    c = customer(db)
    inv = invoice(db, c)
    record = einvoice.submit(db, inv)

    assert record.status == EI_CLEARED
    assert record.irn.startswith("REHEARSAL-")
    assert record.was_a_rehearsal is True
    assert record.channel == "simulator"
    assert record.cleared_at is not None


def test_a_rehearsal_reference_can_never_be_mistaken_for_a_real_one(rehearsing):
    """Somebody reads this number down the phone. It must give itself away."""
    db = rehearsing
    inv = invoice(db, customer(db))
    record = einvoice.submit(db, inv)
    assert "REHEARSAL" in record.irn
    assert "REHEARSAL" in record.irn.upper()
    assert "not been filed" in record.qr_payload


def test_a_quotation_is_never_filed(rehearsing):
    db = rehearsing
    c = customer(db)
    quote = Invoice(number=next_number(db, "INVOICE"), doc_type="QUOTE",
                    contact_id=c.id, date=date(2026, 4, 10), status=DRAFT)
    db.add(quote)
    db.flush()
    record = einvoice.submit(db, quote)
    assert record.status == EI_NOT_REQUIRED


def test_a_draft_is_never_filed(rehearsing):
    db = rehearsing
    inv = invoice(db, customer(db), post=False)
    record = einvoice.submit(db, inv)
    assert record.status == EI_NOT_REQUIRED


def test_an_incomplete_invoice_is_refused_here_not_by_somebody_elses_computer(rehearsing):
    db = rehearsing
    c = customer(db, name="No TIN Ltd", tin="")
    inv = invoice(db, c)
    record = einvoice.submit(db, inv)
    assert record.status == EI_REJECTED
    assert "Tax Identification Number" in record.last_error
    assert not record.irn


def test_submitting_twice_does_not_file_twice(rehearsing):
    db = rehearsing
    inv = invoice(db, customer(db))
    first = einvoice.submit(db, inv)
    irn = first.irn
    second = einvoice.submit(db, inv)
    assert second.id == first.id
    assert second.irn == irn
    assert db.scalar(select(Invoice).where(Invoice.id == inv.id)) is not None


def test_the_fingerprint_records_exactly_what_was_sent(rehearsing):
    db = rehearsing
    inv = invoice(db, customer(db))
    record = einvoice.submit(db, inv)
    assert record.xml_sha256 == ubl.fingerprint(einvoice.xml_for(db, inv))


def test_a_dropped_connection_queues_rather_than_loses(books):
    """The counter case. The document is fine; the internet is not."""
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(
        mode=einvoice.LIVE, submit_url="http://127.0.0.1:9/nowhere",
        client_id="id", client_secret="secret", token_url=""))
    c = customer(books)
    inv = invoice(books, c)

    record = einvoice.submit(books, inv)
    assert record.status == EI_FAILED
    assert record.retry_after is not None, "it must come back to this by itself"
    assert record.irn == ""
    assert [r.id for r in einvoice.outbox(books)] == [record.id]


def test_an_unconfigured_live_setup_says_so_plainly(books):
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(mode=einvoice.LIVE))
    inv = invoice(books, customer(books))
    record = einvoice.submit(books, inv)
    assert record.status == EI_FAILED
    assert "Settings" in record.last_error


def test_the_outbox_leaves_alone_what_is_not_due_yet(books):
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(
        mode=einvoice.LIVE, submit_url="http://127.0.0.1:9/nowhere",
        client_id="id", client_secret="secret"))
    inv = invoice(books, customer(books))
    einvoice.submit(books, inv)
    cleared, waiting = einvoice.send_outbox(books)
    assert cleared == 0 and waiting == 1


def test_an_invoice_may_be_issued_unless_the_company_says_otherwise(rehearsing):
    db = rehearsing
    inv = invoice(db, customer(db))
    allowed, _ = einvoice.may_be_issued(db, inv)
    assert allowed is True

    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(mode=einvoice.REHEARSAL, block_uncleared=True))
    allowed, why = einvoice.may_be_issued(db, inv)
    assert allowed is False
    assert "cleared" in why

    einvoice.submit(db, inv)
    allowed, _ = einvoice.may_be_issued(db, inv)
    assert allowed is True


# --------------------------------------------------------------------------
# The rehearsal is a real check, not a rubber stamp
# --------------------------------------------------------------------------


def test_the_simulator_refuses_a_document_that_does_not_balance():
    """If a rehearsal accepted anything it would teach a business nothing."""
    doc = ubl.Document(
        number="INV-1", issue_date=date(2026, 4, 10),
        supplier=ubl.Party(name="A Ltd", tin="1", rc_number="RC1"),
        customer=ubl.Party(name="B Ltd", tin="2", rc_number="RC2"),
        lines=[ubl.Line(1, "Thing", 1000, 100_00, 100_00, 7_50)],
    )
    good = ubl.build(doc)
    assert transmit.Simulator().submit(good).ok

    # Change only the payable figure, leaving the parts it is meant to be the
    # sum of alone. That is the mistake a real platform catches.
    broken = re.sub(
        r"(<cbc:PayableAmount[^>]*>)[-\d.]+(</cbc:PayableAmount>)",
        r"\g<1>99999.99\g<2>", good.decode()).encode()
    assert broken != good
    result = transmit.Simulator().submit(broken)
    assert result.ok is False
    assert result.permanent is True
    assert "do not add up" in result.error


def test_the_simulator_refuses_a_document_missing_a_tin():
    doc = ubl.Document(
        number="INV-2", issue_date=date(2026, 4, 10),
        supplier=ubl.Party(name="A Ltd", tin="1"),
        customer=ubl.Party(name="B Ltd"),          # no TIN
        lines=[ubl.Line(1, "Thing", 1000, 100_00, 100_00, 0)],
    )
    result = transmit.Simulator().submit(ubl.build(doc))
    assert result.ok is False
    assert "Tax Identification Number" in result.error


def test_the_simulator_is_deterministic():
    doc = ubl.Document(
        number="INV-3", issue_date=date(2026, 4, 10),
        supplier=ubl.Party(name="A Ltd", tin="1", rc_number="RC1"),
        customer=ubl.Party(name="B Ltd", tin="2", rc_number="RC2"),
        lines=[ubl.Line(1, "Thing", 1000, 100_00, 100_00, 0)],
    )
    xml = ubl.build(doc)
    assert transmit.Simulator().submit(xml).irn == transmit.Simulator().submit(xml).irn


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_settings_survive_a_restart(books):
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(
        mode=einvoice.LIVE, provider_name="Some Provider",
        submit_url="https://example.test/submit", client_id="abc",
        client_secret="shhh", business_id="BIZ-1"))
    again = einvoice.load(slug)
    assert again.mode == einvoice.LIVE
    assert again.provider_name == "Some Provider"
    assert again.client_secret == "shhh"
    assert again.on and again.is_live


def test_the_credentials_file_is_not_readable_by_everybody(books):
    """It holds a secret that can file invoices as this business."""
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(mode=einvoice.LIVE, client_secret="shhh"))
    path = registry.company_dir(slug) / einvoice.SETTINGS_FILE
    assert path.exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0, "other users on the machine can read it"


def test_settings_default_to_off(books):
    assert einvoice.load(dbmod.current_slug()).mode == einvoice.OFF
    assert einvoice.load("no-such-company").on is False


def test_each_company_files_separately(books):
    """Two businesses in one installation have their own arrangements."""
    first = dbmod.current_slug()
    other = registry.create("Second Business Ltd")
    einvoice.save(first, einvoice.Settings(mode=einvoice.LIVE, client_secret="one"))
    einvoice.save(other.slug, einvoice.Settings(mode=einvoice.OFF))
    assert einvoice.load(first).is_live
    assert einvoice.load(other.slug).mode == einvoice.OFF


# --------------------------------------------------------------------------
# What reaches the printed page
# --------------------------------------------------------------------------


def test_a_cleared_invoice_prints_its_reference_and_a_qr_code(rehearsing):
    from app.services import pdfdocs

    db = rehearsing
    inv = invoice(db, customer(db))
    record = einvoice.submit(db, inv)
    assert record.status == EI_CLEARED

    pdf = pdfdocs.invoice_pdf(db, inv)
    assert pdf.startswith(b"%PDF")

    from tests.pdftext import text_of

    words = text_of(pdf)
    assert record.irn in words.replace(" ", ""), "the reference must be on the page"


def test_a_rehearsal_says_so_on_the_printed_invoice(rehearsing):
    """A printed page outlives the screen. It must not flatter itself."""
    from app.services import pdfdocs

    from tests.pdftext import text_of

    db = rehearsing
    inv = invoice(db, customer(db))
    einvoice.submit(db, inv)
    words = text_of(pdfdocs.invoice_pdf(db, inv)).lower()
    assert "rehearsal" in words
    assert "not filed" in words or "not been sent" in words


def test_an_uncleared_invoice_prints_no_reference_at_all(rehearsing):
    """Nothing invents a number for a document that has not got one."""
    from app.services import pdfdocs

    from tests.pdftext import text_of

    db = rehearsing
    inv = invoice(db, customer(db))
    words = text_of(pdfdocs.invoice_pdf(db, inv)).lower()
    assert "reference number" not in words
    assert "rehearsal" not in words


def test_posting_queues_but_never_waits_on_the_network(books):
    """A till, a bulk import and a recurring run must not stall on somebody
    else's server. Posting only ever writes a row."""
    slug = dbmod.current_slug()
    einvoice.save(slug, einvoice.Settings(
        mode=einvoice.LIVE, submit_url="http://127.0.0.1:9/nowhere",
        client_id="id", client_secret="secret"))
    c = customer(books)
    inv = invoice(books, c)                    # posts as part of the helper
    record = einvoice.status_of(books, inv)
    assert record is not None
    assert record.status == "PENDING", "posting must not have tried to send"
    assert record.attempts == 0
