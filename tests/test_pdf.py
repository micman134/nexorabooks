"""PDFs, written by hand so that nothing has to be installed.

Two things are being checked. That the file is a real PDF — structurally sound,
with the text actually in it, so a customer's reader opens it rather than
shrugging. And that the awkward inputs a real business produces — a very long
customer name, a hundred lines, a currency whose symbol no built-in font has —
produce a document that still reads correctly.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zlib
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-pdf-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import currency, db as dbmod, prefs  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    Company,
    Contact,
    Invoice,
    InvoiceLine,
    TaxCode,
)
from app.money import to_minor as M  # noqa: E402
from app.pdfwriter import (  # noqa: E402
    A4,
    Canvas,
    encodable,
    read_picture,
    read_png,
    truncate,
    width_of,
    wrap,
)
from app import fonts as fontfinder  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from tests.pdftext import text_of  # noqa: E402
from app.services import documents, pdfdocs  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-pdf-")
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
# It has to be a real PDF
# --------------------------------------------------------------------------


def test_the_file_says_it_is_a_pdf_and_ends_properly():
    c = Canvas()
    c.text(40, 40, "Hello")
    data = c.output()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")


def test_the_cross_reference_table_points_at_the_real_objects():
    """A reader uses these offsets to find anything at all. If they are wrong
    the file opens as an error message."""
    data = Canvas().output()
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[start:start + 4] == b"xref"

    # "xref", then "0 N", then the free entry, then one line per object.
    body = data[start:].split(b"trailer")[0].decode("ascii")
    offsets = [int(line.split()[0]) for line in body.splitlines()[3:] if line.strip()]
    for number, offset in enumerate(offsets, start=1):
        assert data[offset:offset + len(f"{number} 0 obj")] == f"{number} 0 obj".encode()


def test_every_page_is_listed_in_the_page_tree():
    c = Canvas()
    c.text(40, 40, "one")
    c.new_page()
    c.text(40, 40, "two")
    c.new_page()
    c.text(40, 40, "three")
    data = c.output()
    assert b"/Count 3" in data
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 3


def test_the_text_really_is_in_the_file():
    c = Canvas()
    c.text(40, 40, "Adeyemi Building Materials Ltd")
    assert "Adeyemi Building Materials Ltd" in text_of(c.output())


def test_brackets_and_backslashes_do_not_break_the_syntax():
    """An unescaped bracket ends the string early and corrupts everything after."""
    c = Canvas()
    c.text(40, 40, r"Payment (part) \ balance")
    data = c.output()
    assert rb"\(part\)" in zlib.decompress(
        re.search(rb"stream\n(.*?)\nendstream", data, re.S).group(1))
    assert "Payment (part) \\ balance" in text_of(data)


@pytest.mark.skipif(shutil.which("qpdf") is None, reason="qpdf not installed")
def test_a_real_pdf_tool_finds_nothing_wrong(tmp_path):
    c = Canvas()
    c.text(40, 40, "Structure check", size=14, bold=True)
    c.rect(40, 60, 200, 40, fill=(0.9, 0.9, 0.9), stroke=(0, 0, 0))
    c.new_page()
    c.text(40, 40, "Second page")
    path = tmp_path / "check.pdf"
    path.write_bytes(c.output())

    result = subprocess.run(["qpdf", "--check", str(path)],
                            capture_output=True, text=True)
    assert "No syntax or stream encoding errors" in result.stdout, result.stdout


# --------------------------------------------------------------------------
# Measuring text
# --------------------------------------------------------------------------


def test_a_wider_string_measures_wider():
    assert width_of("WWWWW", 10) > width_of("iiiii", 10)


def test_measurement_scales_with_the_font_size():
    assert width_of("hello", 20) == pytest.approx(width_of("hello", 10) * 2)


def test_wrapping_never_exceeds_the_width_it_was_given():
    text = ("Payment for the supply and delivery of building materials to the "
            "Alausa road works, as agreed in writing on the fourteenth.")
    for line in wrap(text, 9, 200):
        assert width_of(line, 9) <= 200


def test_a_single_word_longer_than_the_line_still_comes_out():
    lines = wrap("Supercalifragilisticexpialidocious", 9, 40)
    assert lines and "Supercali" in lines[0]


def test_a_long_name_is_shortened_rather_than_running_into_the_next_column():
    long = "Federal Ministry of Works and Housing, Directorate of Highways"
    short = truncate(long, 9, 120)
    assert short.endswith("...")
    assert width_of(short, 9) <= 120


# --------------------------------------------------------------------------
# Currencies whose symbol the built-in fonts do not have
# --------------------------------------------------------------------------


def test_the_symbols_the_fonts_do_have_are_used():
    for symbol in ("$", "£", "€", "¥"):
        assert encodable(symbol), symbol


def test_the_naira_sign_is_not_one_of_them():
    """Which is why it has to come from a font on the computer instead."""
    assert encodable("₦") is False


def test_a_symbol_no_font_here_can_draw_falls_back_to_the_code(monkeypatch):
    """The rule that must never break: a box in front of a customer.

    On a computer with no font holding the naira sign, the invoice says
    "NGN 1,250,000.00" — which reads correctly to everybody — rather than
    printing a character the reader cannot draw.
    """
    monkeypatch.setattr(fontfinder, "find", lambda text, bold=False: None)
    with currency.using(currency.preset("NGN")):
        assert pdfdocs.money(M("1,250,000")) == "NGN 1,250,000.00"


def test_a_symbol_a_font_here_does_have_is_used():
    found = fontfinder.find("₦")
    if found is None:
        pytest.skip("no font on this computer holds the naira sign")
    with currency.using(currency.preset("NGN")):
        assert pdfdocs.money(M("1,250,000")) == "₦ 1,250,000.00"


def test_a_currency_with_a_symbol_keeps_the_symbol():
    with currency.using(currency.preset("USD")):
        assert pdfdocs.money(M("1,250,000")) == "$ 1,250,000.00"


def test_a_currency_with_no_minor_unit_shows_none_on_paper():
    with currency.using(currency.preset("JPY")):
        assert pdfdocs.money(1500) == "¥ 1,500"


def test_a_negative_is_shown_in_brackets_as_accounts_always_have():
    with currency.using(currency.preset("USD")):
        assert pdfdocs.money(-M("500")) == "($ 500.00)"


def test_typography_the_fonts_cannot_draw_is_folded_not_dropped():
    c = Canvas()
    c.text(40, 40, "Terms — net 30 · “as agreed”")
    printed = text_of(c.output())
    assert "-" in printed and "..." not in printed
    assert "as agreed" in printed


# --------------------------------------------------------------------------
# Logos
# --------------------------------------------------------------------------


def png(width: int, height: int, pixels: list[int], colour: int = 2) -> bytes:
    """A minimal PNG, so the decoder can be tested without a picture library."""
    import struct

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    channels = {0: 1, 2: 3, 6: 4}[colour]
    raw = b"".join(b"\x00" + bytes(pixels[y * width * channels:(y + 1) * width * channels])
                   for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, colour, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def test_a_png_logo_is_read():
    picture = read_png(png(2, 1, [255, 0, 0, 0, 0, 255]))
    assert (picture.width, picture.height) == (2, 1)
    assert picture.colourspace == "DeviceRGB"
    assert list(zlib.decompress(picture.data)) == [255, 0, 0, 0, 0, 255]


def test_transparency_is_flattened_onto_white_as_paper_is():
    picture = read_png(png(2, 1, [255, 0, 0, 255, 0, 0, 255, 0], colour=6))
    pixels = list(zlib.decompress(picture.data))
    assert pixels[:3] == [255, 0, 0]          # opaque red stays red
    assert pixels[3:] == [255, 255, 255]      # fully transparent becomes paper


def test_something_that_is_not_a_picture_is_refused_rather_than_crashing():
    assert read_picture(b"this is not an image") is None
    assert read_picture(b"") is None


def test_a_logo_this_cannot_read_leaves_the_document_alone():
    """A GIF logo should cost the customer a logo, not an error page."""
    c = Canvas()
    used = c.picture(b"GIF89a nonsense", 40, 40, 100, 40)
    assert used == (0.0, 0.0)
    assert c.output().startswith(b"%PDF")


def test_a_logo_keeps_its_shape_when_it_is_scaled():
    c = Canvas()
    w, h = c.picture(png(40, 10, [128] * (40 * 10 * 3)), 40, 40, 100, 100)
    assert w == pytest.approx(100)
    assert h == pytest.approx(25)


def test_the_same_logo_on_two_pages_is_stored_once():
    logo = png(4, 4, [200] * 48)
    c = Canvas()
    c.picture(logo, 40, 40, 50, 50)
    c.new_page()
    c.picture(logo, 40, 40, 50, 50)
    assert c.output().count(b"/Subtype /Image") == 1


# --------------------------------------------------------------------------
# The documents themselves
# --------------------------------------------------------------------------


def customer(db, name="Zenith Construction Ltd") -> Contact:
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                address="4 Marina Road", city="Lagos", state="Lagos",
                tin="01234567-0001", payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def invoice(db, contact, lines=1, amount="1,250,000") -> Invoice:
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=contact.id, date=date(2026, 6, 14),
                  due_date=date(2026, 7, 14), status=DRAFT,
                  memo="Thank you for your order.")
    db.add(inv)
    db.flush()
    for i in range(lines):
        db.add(InvoiceLine(invoice_id=inv.id, line_no=i + 1,
                           description=f"Supply of building materials, load {i + 1}",
                           qty=1000, unit_price=M(amount),
                           account_id=account_by_code(db, "4000").id,
                           tax_code_id=vat.id))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    return inv


def test_an_invoice_carries_everything_a_customer_needs(db):
    inv = invoice(db, customer(db))
    printed = text_of(pdfdocs.invoice_pdf(db, inv))

    assert "INVOICE" in printed
    assert inv.number in printed
    assert "Zenith Construction Ltd" in printed
    assert "Supply of building materials" in printed
    assert pdfdocs.money(inv.total) in printed
    assert "Thank you for your order." in printed


def test_the_company_letterhead_is_on_it(db):
    company = db.get(Company, 1)
    company.name = "Adeyemi Building Materials Ltd"
    company.phone = "+234 803 555 0142"
    db.flush()
    printed = text_of(pdfdocs.invoice_pdf(db, invoice(db, customer(db))))
    assert "Adeyemi Building Materials Ltd" in printed
    assert "+234 803 555 0142" in printed


def test_a_quotation_says_quotation_and_not_amount_due(db):
    contact = customer(db)
    quote = Invoice(number=next_number(db, "QUOTE"), doc_type="QUOTE",
                    contact_id=contact.id, date=date(2026, 6, 14),
                    due_date=date(2026, 7, 14), status=DRAFT)
    db.add(quote)
    db.flush()
    db.add(InvoiceLine(invoice_id=quote.id, line_no=1, description="Materials",
                       qty=1000, unit_price=M("500,000"),
                       account_id=account_by_code(db, "4000").id))
    db.flush()
    db.refresh(quote)
    documents.recalc_invoice(db, quote)

    printed = text_of(pdfdocs.invoice_pdf(db, quote))
    assert "QUOTATION" in printed
    assert "Amount due" not in printed


def test_a_long_invoice_runs_onto_a_second_page_with_a_heading(db):
    inv = invoice(db, customer(db), lines=60, amount="1,000")
    data = pdfdocs.invoice_pdf(db, inv)
    assert b"/Count 1" not in data, "sixty lines should not fit on one page"
    printed = text_of(data)
    assert printed.count("Description") >= 2      # the table heading repeats
    assert "Page 1 of" in printed


def test_the_last_line_of_a_long_invoice_is_not_lost(db):
    inv = invoice(db, customer(db), lines=60, amount="1,000")
    printed = text_of(pdfdocs.invoice_pdf(db, inv))
    assert "load 60" in printed


def test_a_customer_name_far_too_long_for_the_box_is_shortened(db):
    contact = customer(db, "Federal Ministry of Works and Housing, Directorate "
                           "of Highways, Bridges and Ancillary Structures")
    printed = text_of(pdfdocs.invoice_pdf(db, invoice(db, contact)))
    assert "Federal Ministry of Works" in printed
    assert "..." in printed


def test_the_country_s_own_word_for_tax_is_used(db):
    company = db.get(Company, 1)
    company.tax_label = "GST"
    db.flush()
    printed = text_of(pdfdocs.invoice_pdf(db, invoice(db, customer(db))))
    assert "GST" in printed
    assert "\nVAT\n" not in printed


def test_a_receipt_shows_what_it_settled(db):
    from app.models import BankAccount, Payment, RECEIPT
    from app.services import cash

    contact = customer(db)
    inv = invoice(db, contact)
    bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))
    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                  contact_id=contact.id, date=date(2026, 7, 1),
                  bank_account_id=bank.id, amount=inv.total,
                  method="Bank transfer")
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)

    printed = text_of(pdfdocs.receipt_pdf(db, pay))
    assert "RECEIPT" in printed
    assert inv.number in printed
    assert "Bank transfer" in printed


def test_every_document_is_structurally_sound(db, tmp_path):
    """Whatever else is wrong, the customer's reader must open it."""
    inv = invoice(db, customer(db), lines=3)
    for name, data in (("invoice", pdfdocs.invoice_pdf(db, inv)),):
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(data)
        if shutil.which("qpdf"):
            out = subprocess.run(["qpdf", "--check", str(path)],
                                 capture_output=True, text=True)
            assert "No syntax or stream encoding errors" in out.stdout, name


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123",
                               "next": "/"}, follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def test_an_invoice_can_be_downloaded_as_a_pdf(client, db):
    inv = invoice(db, customer(db))
    db.commit()
    r = client.get(f"/sales/invoices/{inv.id}/pdf", follow_redirects=True)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert inv.number in r.headers.get("content-disposition", "")


def test_a_statement_can_be_downloaded_as_a_pdf(client, db):
    contact = customer(db)
    invoice(db, contact)
    db.commit()
    r = client.get(f"/contacts/{contact.id}/statement/pdf", follow_redirects=True)
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")


def test_asking_for_a_document_that_is_not_there_does_not_crash(client):
    r = client.get("/sales/invoices/999999/pdf", follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text
