"""Nigerian tax logic — VAT and withholding tax.

Rates follow the Nigeria Tax Act 2025 / Deduction of Tax at Source
(Withholding) Regulations as in force for 2026:

  VAT   standard rate 7.5%; zero-rated and exempt supplies also supported.
        From January 2026 input VAT on taxable supplies is fully claimable.
        Returns are due by the 21st of the following month.

  WHT   deducted at source by the *payer*.  Where the payee has no Tax ID the
        rate doubles, capped at 20% (passive income excepted).  A small
        company (turnover <= N50m) holding a valid Tax ID does not suffer WHT
        on transactions of N2,000,000 or less in a month.
        Remittance is due by the 21st of the following month.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config
from ..models import VAT, WHT, Contact, TaxCode
from ..money import pct_of

# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------

VAT_CODES = [
    # code, name, rate, exempt, zero, note
    ("VAT-STD", "VAT — standard rate (7.5%)", "7.5", False, False,
     "Standard-rated supply. Output VAT is payable to the NRS."),
    ("VAT-ZERO", "VAT — zero rated (0%)", "0", False, True,
     "Zero-rated: exports, and basic food and medical items listed in the Act. "
     "Reported on the VAT return; input VAT remains claimable."),
    ("VAT-EXEMPT", "VAT — exempt", "0", True, False,
     "Exempt supply. Not part of taxable turnover."),
    ("VAT-NONE", "No VAT", "0", True, False,
     "Out of scope — used for transfers, capital contributions and internal entries."),
]

# rate for a payee holding a valid Tax ID / rate where none is held
WHT_CODES = [
    ("WHT-GOODS", "Supply of goods", "2", "4",
     "Supply of goods and materials."),
    ("WHT-SERV", "All other services", "2", "4",
     "Services not falling under professional or consultancy fees."),
    ("WHT-PROF", "Professional / consultancy / technical / management fees", "5", "10",
     "The most commonly applied rate for service invoices."),
    ("WHT-COMM", "Commission, brokerage and agency fees", "5", "10", ""),
    ("WHT-CONS-INFRA", "Construction — roads, bridges, buildings, power", "2", "4",
     "Reduced rate for qualifying infrastructure construction."),
    ("WHT-CONS-OTHER", "Construction — other and ancillary works", "5", "10", ""),
    ("WHT-RENT", "Rent, hire and lease of property or equipment", "10", "20", ""),
    ("WHT-DIV", "Dividends", "10", "10",
     "Passive income — the no-Tax-ID uplift does not apply."),
    ("WHT-INT", "Interest", "10", "10",
     "Passive income — the no-Tax-ID uplift does not apply. "
     "Includes 10% on foreign-currency domiciliary account interest."),
    ("WHT-ROY-CO", "Royalties — company payee", "10", "20", ""),
    ("WHT-ROY-IND", "Royalties — individual payee", "5", "10", ""),
    ("WHT-DIR", "Directors' fees", "15", "20",
     "Applies to individuals serving as directors."),
    ("WHT-NONE", "No withholding tax", "0", "0", ""),
]

PASSIVE_CODES = {"WHT-DIV", "WHT-INT"}


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def get_code(db: Session, code: str) -> TaxCode | None:
    return db.scalar(select(TaxCode).where(TaxCode.code == code))


def vat_codes(db: Session) -> list[TaxCode]:
    return list(
        db.scalars(
            select(TaxCode)
            .where(TaxCode.kind == VAT, TaxCode.is_active.is_(True))
            .order_by(TaxCode.sort, TaxCode.code)
        )
    )


def wht_codes(db: Session) -> list[TaxCode]:
    return list(
        db.scalars(
            select(TaxCode)
            .where(TaxCode.kind == WHT, TaxCode.is_active.is_(True))
            .order_by(TaxCode.sort, TaxCode.code)
        )
    )


def default_vat_code(db: Session) -> TaxCode | None:
    return get_code(db, "VAT-STD")


# --------------------------------------------------------------------------
# VAT
# --------------------------------------------------------------------------


def vat_on(net_kobo: int, code: TaxCode | None) -> int:
    """Output/input VAT on a VAT-exclusive net amount."""
    if code is None or code.kind != VAT or code.is_exempt:
        return 0
    return pct_of(net_kobo, code.rate)


def is_taxable_supply(code: TaxCode | None) -> bool:
    """Standard- and zero-rated supplies form taxable turnover; exempt ones do not."""
    if code is None:
        return False
    return code.kind == VAT and not code.is_exempt


# --------------------------------------------------------------------------
# Withholding tax
# --------------------------------------------------------------------------


def effective_wht_rate(code: TaxCode | None, contact: Contact | None) -> Decimal:
    """The rate that actually applies, after the no-Tax-ID uplift."""
    if code is None or code.kind != WHT:
        return Decimal(0)
    base = Decimal(code.rate or "0")
    if base == 0:
        return base
    if contact is not None and not contact.has_tin and code.code not in PASSIVE_CODES:
        uplifted = Decimal(code.rate_no_tin) if code.rate_no_tin else base * config.NO_TIN_MULTIPLIER
        return min(uplifted, Decimal(config.NO_TIN_CAP))
    return base


def wht_exempt(contact: Contact | None, net_kobo: int) -> bool:
    """Small-company exemption.

    A company with turnover of N50m or less that holds a valid Tax ID does not
    suffer withholding tax where the transaction value is N2,000,000 or less.
    """
    if contact is None:
        return False
    return (
        contact.is_small_company
        and contact.has_tin
        and net_kobo <= config.WHT_SMALL_TXN_EXEMPTION
    )


def wht_on(
    net_kobo: int, code: TaxCode | None, contact: Contact | None = None
) -> tuple[int, str]:
    """Withholding tax on a VAT-exclusive amount.

    Returns ``(amount_in_kobo, explanation)``. WHT is always computed on the
    net-of-VAT value — VAT is never part of the withholding base.
    """
    if code is None or code.kind != WHT:
        return 0, ""
    rate = effective_wht_rate(code, contact)
    if rate == 0:
        return 0, ""
    if wht_exempt(contact, net_kobo):
        return 0, (
            "Exempt — small company with a valid Tax ID and a transaction of "
            "₦2,000,000 or less."
        )
    amount = pct_of(net_kobo, rate)
    note = f"{code.name} at {rate}%"
    if contact is not None and not contact.has_tin and code.code not in PASSIVE_CODES:
        note += " (uplifted: no Tax ID on record)"
    return amount, note


def seed_tax_codes(db: Session) -> None:
    """Create the standard Nigerian tax codes if they are not already present."""
    sort = 0
    for code, name, rate, exempt, zero, note in VAT_CODES:
        sort += 1
        if get_code(db, code):
            continue
        db.add(
            TaxCode(
                code=code, name=name, kind=VAT, rate=rate,
                is_exempt=exempt, is_zero_rated=zero, note=note, sort=sort,
            )
        )
    sort = 0
    for code, name, rate, rate_no_tin, note in WHT_CODES:
        sort += 1
        if get_code(db, code):
            continue
        db.add(
            TaxCode(
                code=code, name=name, kind=WHT, rate=rate,
                rate_no_tin=rate_no_tin, note=note, sort=sort,
            )
        )
    db.flush()
