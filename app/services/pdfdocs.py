"""The documents a customer actually receives, as PDF.

A screen can be printed; a PDF can be emailed, filed and read on a phone six
months later. These are the same documents the print screens show, laid out for
paper — and laid out once, here, so an invoice and a statement look like they
came from the same company.

Money is written with the currency's symbol when the built-in PDF fonts have
it, and with its three-letter code when they do not. That is deliberate: on a
printed invoice "NGN 1,250,000.00" is unambiguous and correct, where a symbol
the reader's font cannot draw comes out as a box and reads as a fault.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import companies as registry
from .. import currency as currency_mod
from .. import prefs, themes
from ..models import Company
from ..money import digits
from ..pdfwriter import A4, HELVETICA_BOLD, Canvas, printable, width_of

#: The right-hand block of number/date/reference starts here — below the
#: document title, which is set large in the same corner.
FACTS_TOP = 80.0

INK = (0.09, 0.11, 0.13)
QUIET = (0.42, 0.46, 0.50)
LINE = (0.80, 0.83, 0.86)
#: Replaced per document by the company's own theme accent.
BRAND = (0.04, 0.42, 0.23)
BAND = (0.96, 0.97, 0.97)


def money(amount: int) -> str:
    """The figure, with a symbol that will certainly appear on the page.

    The symbol is used when the built-in typefaces have it or a font on this
    computer does; otherwise the three-letter code, which reads correctly to
    everybody, is used instead of a character that would print as a box.
    """
    spec = currency_mod.active()
    body = digits(amount)
    mark = spec.symbol if printable(spec.symbol) else spec.code
    text = f"{body} {mark}" if spec.symbol_after else f"{mark} {body}"
    return f"({text})" if amount < 0 else text


def when(value) -> str:
    return value.strftime(prefs.date_format()) if value else ""


# --------------------------------------------------------------------------
# The parts every document shares
# --------------------------------------------------------------------------


def logo_bytes(slug: str | None) -> bytes:
    if not slug:
        return b""
    path = registry.logo_path(slug)
    try:
        return path.read_bytes() if path else b""
    except OSError:
        return b""


def letterhead(c: Canvas, company: Company, title: str, slug: str | None = None,
               *, y: float = 46.0) -> float:
    """Company identity on the left, the document's name on the right."""
    left = c.margin
    brand = themes.accent_rgb(company)
    used = 0.0
    raw = logo_bytes(slug)
    if raw:
        _w, used = c.picture(raw, left, y - 6, 150, 46)

    text_y = y + (used + 8 if used else 6)
    c.text(left, text_y, company.name if company else "", size=14, bold=True,
           colour=brand, width=300)
    text_y += 13
    for line in _company_lines(company):
        c.text(left, text_y, line, size=8.2, colour=QUIET, width=300)
        text_y += 10.5

    c.text(c.right, y + 13, title.upper(), size=19, bold=True, align="right",
           colour=INK)
    return max(text_y, y + 30)


def _company_lines(company: Company) -> list[str]:
    if company is None:
        return []
    lines = []
    address = " ".join((company.address or "").split())
    if address:
        lines.append(address)
    place = ", ".join(x for x in (company.city, company.state) if x)
    if place:
        lines.append(place)
    contact = "  ".join(x for x in (company.phone, company.email) if x)
    if contact:
        lines.append(contact)
    ids = []
    if company.tin:
        ids.append(f"{company.tax_id_label or 'Tax ID'}: {company.tin}")
    if company.rc_number:
        ids.append(f"{company.reg_no_label or 'Reg no'}: {company.rc_number}")
    if ids:
        lines.append("   ".join(ids))
    return lines


def facts(c: Canvas, y: float, rows: list[tuple[str, str]]) -> float:
    """A small right-hand block of label/value pairs, aligned on the value.

    Each label is placed just left of its own value rather than at a fixed
    column, because "01 Jul 2026 to 31 Jul 2026" is three times the width of
    "31 Jul 2026" and a fixed column would put the label straight through it.
    """
    for label, value in rows:
        if not value:
            continue
        c.text(c.right, y, value, size=9.5, align="right", bold=True)
        c.text(c.right - width_of(str(value), 9.5, HELVETICA_BOLD) - 10, y,
               label, size=8.5, colour=QUIET, align="right")
        y += 13
    return y


def footer(c: Canvas, company: Company, page_no: int, pages: int,
           extra: str = "") -> None:
    if not extra and pages < 2:
        return                       # a rule across an empty foot is just a mark
    y = c.height - 34
    c.rule(y - 10, colour=LINE)
    if extra:
        c.text(c.margin, y, extra, size=8, colour=QUIET,
               width=c.usable - 90)
    if pages > 1:
        c.text(c.right, y, f"Page {page_no} of {pages}", size=8,
               colour=QUIET, align="right")


def stamp_pages(c: Canvas, company: Company, extra: str = "") -> None:
    """Footers, added last because until now nobody knew how many pages there are."""
    total = len(c.pages)
    for index in range(total):
        with c.on_page(index):
            footer(c, company, index + 1, total, extra)


# --------------------------------------------------------------------------
# Invoices, quotations, credit notes
# --------------------------------------------------------------------------


LABELS = {
    "INVOICE": "Invoice",
    "QUOTE": "Quotation",
    "CREDIT_NOTE": "Credit note",
    "BILL": "Bill",
    "PO": "Purchase order",
    "DEBIT_NOTE": "Debit note",
}


def invoice_pdf(db: Session, inv, *, slug: str | None = None) -> bytes:
    company = db.get(Company, 1)
    label = LABELS.get(inv.doc_type, "Invoice")
    c = Canvas(A4)

    y = letterhead(c, company, label, slug) + 6
    right_y = facts(c, FACTS_TOP, [
        ("Number", inv.number),
        ("Date", when(inv.date)),
        ("Valid until" if inv.doc_type == "QUOTE" else "Due", when(inv.due_date)),
        ("Your order", getattr(inv, "po_number", "")),
    ])
    y = max(y, right_y) + 10

    # --- who it is for, and what is owed ---------------------------------
    c.rule(y, colour=LINE)
    y += 16
    c.text(c.margin, y, "Quotation for" if inv.doc_type == "QUOTE" else "Bill to",
           size=8, colour=QUIET)
    contact = inv.contact
    y += 13
    c.text(c.margin, y, contact.name, size=11, bold=True, width=300)
    block = y + 12
    for line in _contact_lines(contact, company):
        c.text(c.margin, block, line, size=8.5, colour=QUIET, width=300)
        block += 10.5

    due = inv.total if inv.doc_type == "QUOTE" else inv.balance_due
    c.text(c.right, y - 13, "Total" if inv.doc_type == "QUOTE" else "Amount due",
           size=8, colour=QUIET, align="right")
    c.text(c.right, y + 4, money(due), size=17, bold=True, align="right",
           colour=themes.accent_rgb(company))
    y = max(block, y + 14) + 12

    # --- the lines --------------------------------------------------------
    columns = _line_columns(c, bool(inv.discount_total))
    y = _table_head(c, y, columns)
    for line in inv.lines:
        y = _page_break(c, y, company, columns)
        y = _line_row(c, y, columns, line, inv)
    c.rule(y - 4, colour=LINE)
    y += 10

    # --- totals -----------------------------------------------------------
    rows = [("Subtotal", money(inv.subtotal), False)]
    if inv.discount_total:
        rows.append(("Discount", money(-inv.discount_total), False))
    tax_label = (company.tax_label or "VAT") if company else "VAT"
    rows.append((tax_label, money(inv.vat_total), False))
    rows.append(("Total", money(inv.total), True))
    if getattr(inv, "vat_withheld_expected", 0):
        rows.append((f"Less {tax_label} withheld at source",
                     money(-inv.vat_withheld_expected), False))
    if inv.wht_total:
        rows.append(("Less tax to be withheld", money(-inv.wht_total), False))
    if inv.wht_total or getattr(inv, "vat_withheld_expected", 0):
        rows.append(("Net payable", money(inv.expected_cash), True))
    if inv.amount_paid:
        rows.append(("Already paid", money(-inv.amount_paid), False))
        rows.append(("Balance due", money(inv.balance_due), True))
    y = _totals(c, y, rows)

    # --- the small print ---------------------------------------------------
    y += 14
    notes = []
    if inv.wht_total and getattr(inv, "wht_code", None):
        notes.append(
            f"Withholding tax of {money(inv.wht_total)} ({inv.wht_code.rate}%) applies "
            f"to this supply. Please send the withholding tax credit note once it has "
            f"been remitted.")
    if getattr(inv, "vat_withheld_expected", 0):
        authority = (company.tax_authority or "the tax authority") if company else ""
        notes.append(
            f"{tax_label} of {money(inv.vat_withheld_expected)} on this supply is to be "
            f"withheld at source and remitted to {authority}.")
    if inv.memo:
        notes.append(inv.memo)
    if inv.terms:
        notes.append(inv.terms)
    for note in notes:
        y = c.paragraph(c.margin, y, note, size=8.5, colour=QUIET,
                        limit=c.usable) + 4

    # --- how to pay --------------------------------------------------------
    # Only on an invoice. A quotation is not yet owed, and a credit note is
    # money going the other way — printing bank details on either invites a
    # customer to pay something that is not due.
    if inv.doc_type == "INVOICE":
        y = _how_to_pay(c, db, company, y)

    # --- what the Revenue Service said about it ---------------------------
    if inv.doc_type in ("INVOICE", "CREDIT_NOTE"):
        y = _clearance_block(c, db, inv, y)

    stamp_pages(c, company, (company.invoice_footer or "") if company else "")
    return c.output()



def _clearance_block(c: Canvas, db: Session, inv, y: float) -> float:
    """The Revenue Service reference and its QR code, printed on the invoice.

    Under the mandate the buyer's copy carries the reference number and a QR
    code that anybody can scan to check the invoice is genuine. An invoice
    without them, once the business is in scope, is not a valid invoice.

    A rehearsal prints too — but says on its face that it is a rehearsal. A
    printed page outlives the screen that produced it, and somebody filing it
    in a folder six months from now must not mistake practice for compliance.
    """
    from .. import qrcode
    from . import einvoice as ei

    record = ei.status_of(db, inv)
    if record is None or not record.is_cleared:
        return y

    y += 10
    if y > c.height - 150:
        c.new_page()
        y = 60

    box_top = y
    label = "Rehearsal — NOT filed" if record.was_a_rehearsal else "Cleared for issue"
    c.text(c.margin, y, label, size=8, bold=True, colour=QUIET)
    y += 12
    c.text(c.margin, y, "Reference number", size=7.5, colour=QUIET)
    y += 10
    c.text(c.margin, y, record.irn, size=9, width=c.usable - 110)
    y += 13
    if record.was_a_rehearsal:
        y = c.paragraph(
            c.margin, y,
            "This document has not been sent to the Nigeria Revenue Service. The "
            "reference above is a rehearsal and is not valid for any purpose.",
            size=7.5, colour=QUIET, limit=c.usable - 110) + 2

    # The QR itself, drawn as squares rather than an image so it stays sharp at
    # any size and needs no image library.
    payload = record.qr_payload or record.irn
    try:
        grid = qrcode.matrix(payload)
    except Exception:                                     # noqa: BLE001
        return y

    side = 82.0
    module = side / len(grid)
    left = c.right - side
    top = box_top
    c.rect(left - 4, top - 4, side + 8, side + 8, fill=(1, 1, 1), stroke=LINE)
    for row_no, row in enumerate(grid):
        run_start = None
        for col_no in range(len(row) + 1):
            dark = col_no < len(row) and row[col_no]
            if dark and run_start is None:
                run_start = col_no
            elif not dark and run_start is not None:
                c.rect(left + run_start * module, top + row_no * module,
                       (col_no - run_start) * module, module, fill=(0, 0, 0))
                run_start = None
    return max(y, top + side + 6)


def payable_accounts(db: Session) -> list:
    """The accounts a customer should be told about, in the order set for them."""
    from ..models import BankAccount

    return [
        b for b in db.scalars(
            select(BankAccount)
            .where(BankAccount.show_on_invoices.is_(True), BankAccount.is_active.is_(True))
            .order_by(BankAccount.sort, BankAccount.id))
        if b.can_be_shown
    ]


def _how_to_pay(c: Canvas, db: Session, company, y: float) -> float:
    """Where to send the money, printed on the invoice itself.

    This exists because an invoice that says what is owed but not where to send
    it makes the customer write back and ask, and every day of that is a day the
    money is not in the bank. It is also the block the covering email points at
    when it says "using the account details shown on the invoice".
    """
    accounts = payable_accounts(db)
    instructions = (getattr(company, "payment_instructions", "") or "").strip() \
        if company else ""
    if not accounts and not instructions:
        return y

    y += 10
    y = _page_break(c, y, company, None, 90)
    c.rule(y, colour=LINE)
    y += 15
    c.text(c.margin, y, "How to pay", size=9, bold=True)
    y += 14

    # Side by side while they fit, so two accounts do not push an invoice on to
    # a second page for the sake of six short lines.
    column = c.usable / max(1, min(len(accounts), 2)) if accounts else c.usable
    lowest = y
    for index, bank in enumerate(accounts[:2]):
        x = c.margin + (index * column)
        row = y
        if bank.currency_code and len(accounts) > 1:
            c.text(x, row, bank.currency_code, size=7.5, colour=QUIET)
            row += 10
        for label, value in bank.payable_lines:
            c.text(x, row, f"{label}:", size=8, colour=QUIET, width=column - 12)
            c.text(x + 74, row, value, size=8.5, bold=(label == "Account number"),
                   width=column - 86)
            row += 10.5
        lowest = max(lowest, row)

    y = lowest + (2 if accounts else 0)
    if instructions:
        y = c.paragraph(c.margin, y + 2, instructions, size=8.5, colour=QUIET,
                        limit=c.usable)
    return y


def _contact_lines(contact, company) -> list[str]:
    lines = []
    if contact.contact_person:
        lines.append(f"Attn: {contact.contact_person}")
    address = " ".join((contact.address or "").split())
    if address:
        lines.append(address)
    place = ", ".join(x for x in (contact.city, contact.state) if x)
    if place:
        lines.append(place)
    if contact.tin:
        label = (company.tax_id_label if company else None) or "Tax ID"
        lines.append(f"{label}: {contact.tin}")
    return lines


#: A money column has to hold something like "NGN 12,760,000.00" without
#: touching its neighbour, so the widths are set from the widest realistic
#: figure rather than from how the sample data happens to look.
MONEY_COLUMN = 96.0
QTY_COLUMN = 68.0
DISCOUNT_COLUMN = 40.0


def _line_columns(c: Canvas, with_discount: bool) -> list[tuple[str, float, str]]:
    """(heading, x position, alignment) — x is the right edge for numbers."""
    amount_x = c.right
    vat_x = amount_x - MONEY_COLUMN
    price_x = vat_x - MONEY_COLUMN
    disc_x = price_x - DISCOUNT_COLUMN if with_discount else None
    qty_x = (disc_x or price_x) - QTY_COLUMN
    columns = [("Description", c.margin, "left"),
               ("Qty", qty_x, "right"),
               ("Unit price", price_x, "right")]
    if disc_x:
        columns.append(("Disc", disc_x, "right"))
    columns.append(("Tax", vat_x, "right"))
    columns.append(("Amount", amount_x, "right"))
    return columns


def _table_head(c: Canvas, y: float, columns) -> float:
    c.rect(c.margin - 4, y - 10, c.usable + 8, 17, fill=BAND)
    for heading, x, align in columns:
        c.text(x, y, heading, size=8, bold=True, colour=QUIET, align=align)
    return y + 16


def _line_row(c: Canvas, y: float, columns, line, doc) -> float:
    widths = {"Description": columns[1][1] - QTY_COLUMN - c.margin - 6,
              "Qty": QTY_COLUMN - 6}
    unit = f" {line.item.unit}" if getattr(line, "item", None) else ""
    values = {
        "Description": line.description or (line.item.name if line.item else ""),
        "Qty": _qty(line.qty) + unit,
        "Unit price": money(line.unit_price),
        "Disc": f"{line.discount_pct}%",
        "Tax": money(line.vat_amount),
        "Amount": money(line.net),
    }
    for heading, x, align in columns:
        c.text(x, y, values.get(heading, ""), size=9, align=align,
               width=widths.get(heading))
    return y + 15


def _qty(milli: int) -> str:
    if milli % 1000 == 0:
        return f"{milli // 1000:,}"
    return f"{milli / 1000:,.3f}".rstrip("0").rstrip(".")


def _page_break(c: Canvas, y: float, company, columns, needed: float = 110) -> float:
    """Start a new page when what comes next will not fit on this one.

    ``columns`` is None for a block that is not part of the line table — the
    "how to pay" panel, for instance — which then simply starts at the top of
    the fresh page rather than under a repeated table heading.
    """
    if y < c.height - needed:
        return y
    c.new_page()
    return _table_head(c, 60, columns) if columns else 60


def _totals(c: Canvas, y: float, rows) -> float:
    left = c.right - 2 * MONEY_COLUMN - 40
    for label, value, strong in rows:
        if strong:
            c.rule(y - 11, colour=LINE)
        c.text(left, y, label, size=9.5, bold=strong,
               colour=INK if strong else QUIET)
        c.text(c.right, y, value, size=10 if strong else 9.5, bold=strong,
               align="right")
        y += 15
    return y


# --------------------------------------------------------------------------
# Receipts
# --------------------------------------------------------------------------


def receipt_pdf(db: Session, payment, *, slug: str | None = None) -> bytes:
    company = db.get(Company, 1)
    c = Canvas(A4)
    y = letterhead(c, company, "Receipt", slug) + 6
    right_y = facts(c, FACTS_TOP, [
        ("Number", payment.number),
        ("Date", when(payment.date)),
        ("Method", payment.method or ""),
        ("Reference", payment.reference or ""),
    ])
    y = max(y, right_y) + 10

    c.rule(y, colour=LINE)
    y += 16
    c.text(c.margin, y, "Received from", size=8, colour=QUIET)
    y += 13
    c.text(c.margin, y, payment.contact.name if payment.contact else "", size=11,
           bold=True, width=300)

    c.text(c.right, y - 13, "Amount received", size=8, colour=QUIET, align="right")
    c.text(c.right, y + 4, money(payment.amount), size=17, bold=True,
           align="right", colour=themes.accent_rgb(company))
    y += 30

    settled = [a for a in getattr(payment, "allocations", []) if a.amount]
    if settled:
        date_x = c.right - 2 * MONEY_COLUMN - 20
        total_x = c.right - MONEY_COLUMN
        columns = [("Against", c.margin, "left"),
                   ("Date", date_x, "right"),
                   ("Document total", total_x, "right"),
                   ("Settled", c.right, "right")]
        y = _table_head(c, y, columns)
        for a in settled:
            doc = getattr(a, "invoice", None) or getattr(a, "bill", None)
            c.text(c.margin, y, doc.number if doc else "", size=9)
            c.text(date_x, y, when(doc.date) if doc else "", size=9, align="right")
            c.text(total_x, y, money(doc.total) if doc else "", size=9,
                   align="right")
            c.text(c.right, y, money(a.amount), size=9, align="right")
            y += 15
        c.rule(y - 4, colour=LINE)
        y += 10

    extras = []
    if getattr(payment, "wht_amount", 0):
        extras.append(("Tax withheld by the payer", money(payment.wht_amount), False))
    if getattr(payment, "vat_withheld", 0):
        label = (company.tax_label or "VAT") if company else "VAT"
        extras.append((f"{label} withheld at source", money(payment.vat_withheld), False))
    if extras:
        total = payment.amount + sum(
            getattr(payment, k, 0) for k in ("wht_amount", "vat_withheld"))
        extras.append(("Total settled", money(total), True))
        y = _totals(c, y, extras)

    y += 16
    c.text(c.margin, y, "With thanks.", size=9, colour=QUIET)
    stamp_pages(c, company, (company.invoice_footer or "") if company else "")
    return c.output()


# --------------------------------------------------------------------------
# Customer statements
# --------------------------------------------------------------------------


def statement_pdf(db: Session, contact, rows, opening: int, closing: int,
                  start: date, end: date, *, slug: str | None = None) -> bytes:
    company = db.get(Company, 1)
    c = Canvas(A4)
    y = letterhead(c, company, "Statement", slug) + 6
    right_y = facts(c, FACTS_TOP, [
        ("Period", f"{when(start)} to {when(end)}"),
        ("Balance due", money(closing)),
    ])
    y = max(y, right_y) + 10

    c.rule(y, colour=LINE)
    y += 16
    c.text(c.margin, y, "Statement for", size=8, colour=QUIET)
    y += 13
    c.text(c.margin, y, contact.name, size=11, bold=True, width=300)
    block = y + 12
    for line in _contact_lines(contact, company):
        c.text(c.margin, block, line, size=8.5, colour=QUIET, width=300)
        block += 10.5
    y = max(block, y + 14) + 10

    debit_x = c.right - 2 * MONEY_COLUMN
    credit_x = c.right - MONEY_COLUMN
    columns = [("Date", c.margin, "left"),
               ("Document", c.margin + 74, "left"),
               ("Details", c.margin + 168, "left"),
               ("Debit", debit_x, "right"),
               ("Credit", credit_x, "right"),
               ("Balance", c.right, "right")]
    y = _table_head(c, y, columns)
    c.text(c.margin + 168, y, "Balance brought forward", size=9, colour=QUIET)
    c.text(c.right, y, money(opening), size=9, align="right")
    y += 15

    # Rows arrive from reports.statement as (entry, line, running balance).
    for entry, line, running in rows:
        y = _page_break(c, y, company, columns)
        c.text(c.margin, y, when(entry.date), size=9)
        c.text(c.margin + 74, y, entry.reference or entry.number or "", size=9,
               width=88)
        c.text(c.margin + 168, y, line.memo or entry.memo or "", size=9,
               width=debit_x - MONEY_COLUMN - c.margin - 174)
        if line.debit:
            c.text(debit_x, y, money(line.debit), size=9, align="right")
        if line.credit:
            c.text(credit_x, y, money(line.credit), size=9, align="right")
        c.text(c.right, y, money(running), size=9, align="right")
        y += 15

    c.rule(y - 4, colour=LINE)
    y += 10
    y = _totals(c, y, [("Balance now due", money(closing), True)])

    y += 16
    terms = (company.invoice_terms or "") if company else ""
    if terms:
        y = c.paragraph(c.margin, y, terms, size=8.5, colour=QUIET, limit=c.usable)
    stamp_pages(c, company, (company.invoice_footer or "") if company else "")
    return c.output()


# --------------------------------------------------------------------------
# Payslips
# --------------------------------------------------------------------------


def payslip_pdf(db: Session, slip, *, slug: str | None = None,
                note: str = "") -> bytes:
    company = db.get(Company, 1)
    c = Canvas(A4)
    run = slip.run
    y = letterhead(c, company, "Payslip", slug) + 6
    period = (f"{when(run.period_start)} to {when(run.period_end)}"
              if run else "")
    right_y = facts(c, FACTS_TOP, [
        ("Payroll", run.number if run else ""),
        ("Period", period),
        ("Pay date", when(run.pay_date) if run else ""),
        ("Staff number", slip.staff_no),
    ])
    y = max(y, right_y) + 10

    c.rule(y, colour=LINE)
    y += 16
    c.text(c.margin, y, slip.employee_name, size=12, bold=True, width=300)
    below = y + 13
    for line in (slip.job_title, slip.department,
                 f"{slip.bank_name} {slip.bank_account_no}".strip()):
        if line:
            c.text(c.margin, below, line, size=8.5, colour=QUIET, width=300)
            below += 10.5

    c.text(c.right, y - 13, "Net pay", size=8, colour=QUIET, align="right")
    c.text(c.right, y + 4, money(slip.net_pay), size=17, bold=True,
           align="right", colour=themes.accent_rgb(company))
    y = max(below, y + 14) + 12

    earnings = [l for l in slip.lines if l.kind == "EARNING"]
    deductions = [l for l in slip.lines if l.kind == "DEDUCTION"]

    half = c.usable / 2 - 10
    top = y
    y = _column(c, c.margin, top, half, "Earnings", earnings, slip.gross, "Gross pay")
    right_bottom = _column(c, c.margin + half + 20, top, half, "Deductions",
                           deductions, slip.total_deductions, "Total deductions")
    y = max(y, right_bottom) + 12

    y = _totals(c, y, [("Net pay", money(slip.net_pay), True)])

    y += 14
    if slip.paye_note:
        y = c.paragraph(c.margin, y, slip.paye_note, size=8.5, colour=QUIET,
                        limit=c.usable) + 4
    if note:
        c.paragraph(c.margin, y, note, size=8.5, colour=QUIET, limit=c.usable)
    stamp_pages(c, company)
    return c.output()


def _column(c: Canvas, x: float, y: float, width: float, title: str,
            lines, total: int, total_label: str) -> float:
    c.rect(x - 4, y - 10, width + 8, 17, fill=BAND)
    c.text(x, y, title, size=8, bold=True, colour=QUIET)
    c.text(x + width, y, "Amount", size=8, bold=True, colour=QUIET, align="right")
    y += 16
    for line in lines:
        c.text(x, y, line.name, size=9, width=width - 80)
        c.text(x + width, y, money(line.amount), size=9, align="right")
        y += 14
    c.line(x, y - 4, x + width, y - 4, colour=LINE)
    c.text(x, y + 8, total_label, size=9, bold=True)
    c.text(x + width, y + 8, money(total), size=9, bold=True, align="right")
    return y + 20
