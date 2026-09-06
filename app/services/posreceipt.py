"""The slip that comes out of the printer beside the till.

A till receipt is not a small invoice. It goes on a roll eighty millimetres
wide, it is read standing up, and it is the customer's only proof of what they
paid — so it carries the things a customer argues about (what was bought, what
it cost, what they handed over, what came back) and nothing else.

The page is as long as it needs to be and no longer, because a receipt printer
cuts the paper where the page ends: a fixed page height would either cut a long
sale in half or feed six inches of blank roll after a short one.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import TENDER_CASH, Company, Invoice
from ..pdfwriter import Canvas, wrap
from .pdfdocs import money, when

#: 80mm at 72 points to the inch, which is the common roll.
WIDTH = 226.77
MARGIN = 10.0

INK = (0.05, 0.05, 0.05)
QUIET = (0.35, 0.35, 0.35)
LINE = (0.6, 0.6, 0.6)


def _height(inv: Invoice, tenders, company) -> float:
    """How long the paper has to be for this particular sale."""
    height = 150.0                                    # header, totals, footer
    for line in inv.lines:
        height += 11 + 11 * max(1, len(wrap(line.description, 7.5, WIDTH - 2 * MARGIN)))
    height += 12 * len(list(tenders))
    if inv.vat_total:
        height += 12
    if company is not None and (company.address or "").strip():
        height += 10 * len((company.address or "").splitlines())
    return height


def render(db: Session, inv: Invoice, tenders=None) -> bytes:
    """One receipt, on a roll of the right length."""
    tenders = list(tenders or [])
    company = db.get(Company, 1)
    c = Canvas((WIDTH, _height(inv, tenders, company)), margin=MARGIN)
    middle = WIDTH / 2
    y = 20.0

    # --- who sold it ------------------------------------------------------
    c.text(middle, y, company.name if company else "", size=10, bold=True,
           align="centre", width=WIDTH - 2 * MARGIN)
    y += 11
    for part in (company.address or "").splitlines() if company else []:
        c.text(middle, y, part.strip(), size=7, colour=QUIET, align="centre",
               width=WIDTH - 2 * MARGIN)
        y += 9
    if company is not None and getattr(company, "phone", ""):
        c.text(middle, y, company.phone, size=7, colour=QUIET, align="centre")
        y += 9
    if company is not None and getattr(company, "vat_reg_no", ""):
        c.text(middle, y, f"VAT {company.vat_reg_no}", size=7, colour=QUIET,
               align="centre")
        y += 9

    y += 4
    c.rule(y, colour=LINE)
    y += 12

    # --- which sale -------------------------------------------------------
    c.text(MARGIN, y, inv.number, size=8, bold=True)
    c.text(WIDTH - MARGIN, y, when(inv.date), size=8, align="right")
    y += 10
    if inv.reference:
        c.text(MARGIN, y, inv.reference, size=7, colour=QUIET)
        y += 9
    if inv.contact is not None and inv.contact.name != "Walk-in customer":
        c.text(MARGIN, y, inv.contact.name, size=7.5, colour=QUIET,
               width=WIDTH - 2 * MARGIN)
        y += 9

    y += 2
    c.rule(y, colour=LINE)
    y += 12

    # --- what was bought --------------------------------------------------
    for line in inv.lines:
        for part in wrap(line.description, 7.5, WIDTH - 2 * MARGIN):
            c.text(MARGIN, y, part, size=7.5)
            y += 9
        quantity = line.qty / 1000
        left = f"{quantity:g} × {money(line.unit_price)}"
        c.text(MARGIN + 6, y, left, size=7.5, colour=QUIET)
        c.text(WIDTH - MARGIN, y, money(line.net), size=8, align="right")
        y += 12

    c.rule(y, colour=LINE)
    y += 12

    # --- what it came to --------------------------------------------------
    if inv.vat_total:
        c.text(MARGIN, y, "Subtotal", size=7.5, colour=QUIET)
        c.text(WIDTH - MARGIN, y, money(inv.subtotal), size=7.5, align="right")
        y += 10
        c.text(MARGIN, y, "VAT", size=7.5, colour=QUIET)
        c.text(WIDTH - MARGIN, y, money(inv.vat_total), size=7.5, align="right")
        y += 12

    c.text(MARGIN, y, "TOTAL", size=10, bold=True)
    c.text(WIDTH - MARGIN, y, money(inv.total), size=11, bold=True, align="right")
    y += 14

    # --- how it was paid --------------------------------------------------
    change = 0
    for tender in tenders:
        label = {"CASH": "Cash", "CARD": "Card", "TRANSFER": "Transfer"}.get(
            tender.kind, tender.kind.title())
        shown = tender.tendered if tender.kind == TENDER_CASH else tender.amount
        c.text(MARGIN, y, label, size=7.5, colour=QUIET)
        c.text(WIDTH - MARGIN, y, money(shown), size=7.5, align="right")
        change += tender.change
        y += 10
    if change:
        c.text(MARGIN, y, "Change", size=8, bold=True)
        c.text(WIDTH - MARGIN, y, money(change), size=8, bold=True, align="right")
        y += 12

    y += 6
    c.rule(y, colour=LINE)
    y += 12
    c.text(middle, y, "Thank you", size=8, align="centre")
    y += 10
    c.text(middle, y, "Goods may be exchanged with this receipt", size=6.5,
           colour=QUIET, align="centre", width=WIDTH - 2 * MARGIN)
    return c.output()
