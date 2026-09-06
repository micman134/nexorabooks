"""Database models.

Money is stored as INTEGER kobo everywhere (see app/money.py).
Quantities are stored as INTEGER milli-units (3 decimal places) so that
fractional stock (kg, litres, hours) never touches a float either.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from . import clock
from .db import Base

# --------------------------------------------------------------------------
# Enumerations (plain strings — readable straight out of the database file)
# --------------------------------------------------------------------------

ASSET, LIABILITY, EQUITY, INCOME, EXPENSE = (
    "ASSET",
    "LIABILITY",
    "EQUITY",
    "INCOME",
    "EXPENSE",
)
ACCOUNT_TYPES = [ASSET, LIABILITY, EQUITY, INCOME, EXPENSE]

# Types whose normal balance is a debit
DEBIT_TYPES = {ASSET, EXPENSE}

SUBTYPES = {
    ASSET: [
        ("BANK", "Bank"),
        ("CASH", "Cash on hand"),
        ("RECEIVABLE", "Accounts receivable"),
        ("INVENTORY", "Inventory"),
        ("CURRENT_ASSET", "Other current asset"),
        ("FIXED_ASSET", "Fixed asset"),
        ("ACCUM_DEP", "Accumulated depreciation"),
        ("OTHER_ASSET", "Other non-current asset"),
    ],
    LIABILITY: [
        ("PAYABLE", "Accounts payable"),
        ("TAX_PAYABLE", "Tax payable"),
        ("CURRENT_LIABILITY", "Other current liability"),
        ("LOAN", "Loan / borrowing"),
        ("OTHER_LIABILITY", "Other non-current liability"),
    ],
    EQUITY: [
        ("CAPITAL", "Share capital / owner's equity"),
        ("RETAINED_EARNINGS", "Retained earnings"),
        ("DRAWINGS", "Drawings / dividends"),
        ("RESERVE", "Reserves"),
    ],
    INCOME: [
        ("SALES", "Sales revenue"),
        ("OTHER_INCOME", "Other income"),
    ],
    EXPENSE: [
        ("COGS", "Cost of sales"),
        ("OPERATING_EXPENSE", "Operating expense"),
        ("PAYROLL", "Payroll"),
        ("DEPRECIATION", "Depreciation & amortisation"),
        ("FINANCE_COST", "Finance cost"),
        ("TAX_EXPENSE", "Income tax expense"),
        ("OTHER_EXPENSE", "Other expense"),
    ],
}

ROLE_ADMIN, ROLE_ACCOUNTANT, ROLE_CLERK, ROLE_VIEWER = (
    "admin",
    "accountant",
    "clerk",
    "viewer",
)
ROLES = [
    (ROLE_ADMIN, "Administrator — full access including users, settings and year-end"),
    (ROLE_ACCOUNTANT, "Accountant — post, edit and void anything except users/settings"),
    (ROLE_CLERK, "Data entry — create invoices, bills, receipts and payments"),
    (ROLE_VIEWER, "Viewer — read-only access to records and reports"),
]

DRAFT, POSTED, PART_PAID, PAID, VOID = "DRAFT", "POSTED", "PART_PAID", "PAID", "VOID"

MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


# --------------------------------------------------------------------------
# Company / configuration
# --------------------------------------------------------------------------


class Company(Base):
    __tablename__ = "company"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="My Company Ltd")
    legal_name: Mapped[str] = mapped_column(String(200), default="")
    rc_number: Mapped[str] = mapped_column(String(50), default="")
    tin: Mapped[str] = mapped_column(String(50), default="")
    vat_reg_no: Mapped[str] = mapped_column(String(50), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="Lagos")
    phone: Mapped[str] = mapped_column(String(60), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    website: Mapped[str] = mapped_column(String(120), default="")
    logo_file: Mapped[str] = mapped_column(String(200), default="")

    # --- How this company writes money -----------------------------------
    # One currency per company, fixed at setup. Changing it after figures are
    # entered would silently reinterpret every stored integer, so the settings
    # screen refuses once anything has been posted.
    currency_symbol: Mapped[str] = mapped_column(String(6), default="₦")
    currency_code: Mapped[str] = mapped_column(String(6), default="NGN")
    currency_decimals: Mapped[int] = mapped_column(Integer, default=2)
    currency_symbol_after: Mapped[bool] = mapped_column(Boolean, default=False)
    currency_thousands: Mapped[str] = mapped_column(String(2), default=",")
    currency_point: Mapped[str] = mapped_column(String(2), default=".")
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1)
    date_format: Mapped[str] = mapped_column(String(20), default="%d %b %Y")

    # --- What this company's country calls things ------------------------
    # The accounting is the same everywhere; the words are not. A South
    # African says VAT and a Canadian says GST; a Nigerian quotes a TIN and a
    # Briton a VAT registration number. These are labels only — no rule
    # anywhere depends on them.
    #: The look everybody in this company gets unless they choose their own.
    theme: Mapped[str] = mapped_column(String(20), default="ledger")

    #: When set, everybody must finish two-factor setup before they can work.
    #: Deliberately not applied retrospectively as a lockout: somebody who has
    #: not set it up yet is sent to the setup screen, not turned away, because
    #: an administrator switching this on must not strand their own staff.
    require_two_factor: Mapped[bool] = mapped_column(Boolean, default=False)

    # The covering message that goes out with an invoice, a quotation or a
    # credit note. Empty means the plain wording built into the software. A
    # business that says the same thing on every invoice — "thank you for
    # registering, here is how to pay, send us the receipt" — should say it
    # once here rather than retyping it on every send.
    invoice_email_subject: Mapped[str] = mapped_column(String(200), default="")
    invoice_email_body: Mapped[str] = mapped_column(Text, default="")

    # The address staff type into their own browsers to reach this computer,
    # when it is not simply this computer's address on the office network —
    # a fixed IP, a name, or something in front of it. Anything emailed out
    # is built from this, because the address in an administrator's own
    # browser is often "127.0.0.1", which on anybody else's computer means
    # their own computer and reaches nothing. Blank means: work it out.
    staff_url: Mapped[str] = mapped_column(String(200), default="")

    country_code: Mapped[str] = mapped_column(String(2), default="NG")
    country_name: Mapped[str] = mapped_column(String(60), default="Nigeria")
    tax_label: Mapped[str] = mapped_column(String(20), default="VAT")
    tax_id_label: Mapped[str] = mapped_column(String(30), default="TIN")
    reg_no_label: Mapped[str] = mapped_column(String(30), default="RC number")
    tax_authority: Mapped[str] = mapped_column(String(60), default="NRS")

    is_vat_registered: Mapped[bool] = mapped_column(Boolean, default=True)
    vat_rate: Mapped[str] = mapped_column(String(10), default="7.5")
    annual_turnover_band: Mapped[str] = mapped_column(String(20), default="ABOVE_50M")

    # Books cannot be changed on or before this date
    lock_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_terms: Mapped[str] = mapped_column(
        Text, default="Payment due within 30 days of invoice date."
    )
    invoice_footer: Mapped[str] = mapped_column(Text, default="Thank you for your business.")
    #: Printed under the bank details on an invoice. Where a business says
    #: "quote the invoice number as your reference", or names the one thing
    #: that goes wrong most often when somebody pays them.
    payment_instructions: Mapped[str] = mapped_column(
        Text, default="Please quote the invoice number as your payment reference.")
    default_payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    setup_complete: Mapped[bool] = mapped_column(Boolean, default=False)

    # A requisition above this amount needs a director's approval as well as
    # the line manager's. Zero means the manager alone is enough, whatever
    # the amount.
    requisition_limit: Mapped[int] = mapped_column(Integer, default=0)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    #: This person's own choice of colours. Empty means "whatever the company
    #: uses" — two people on the same network need not agree about this.
    theme: Mapped[str] = mapped_column(String(20), default="")
    full_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=ROLE_CLERK)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    last_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- Requisitions -----------------------------------------------------
    # Who this person's requisitions go to for approval. Not a role: a named
    # person, so the approval trail says who was actually meant to sign it.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    job_title: Mapped[str] = mapped_column(String(120), default="")
    department: Mapped[str] = mapped_column(String(80), default="")
    # Set on the director or MD who signs off anything above the limit.
    approves_large_requisitions: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set on the finance manager who actually releases the money.
    pays_requisitions: Mapped[bool] = mapped_column(Boolean, default=False)
    # Where the money is sent when a requisition is paid.
    bank_name: Mapped[str] = mapped_column(String(80), default="")
    bank_account_no: Mapped[str] = mapped_column(String(30), default="")
    bank_account_name: Mapped[str] = mapped_column(String(120), default="")

    #: Above the administrator role, and held by very few people — often one.
    #: An administrator runs the company: users, settings, year-end. A super
    #: administrator can additionally destroy a record so that it is gone.
    #: That is a different kind of power from every other permission here,
    #: because everything else can be undone by doing the opposite, and this
    #: cannot be undone at all. So it is a flag rather than a role: it has to
    #: be granted to a named person on purpose, and it cannot arrive by
    #: somebody being promoted to administrator for an unrelated reason.
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Two-factor sign-in ----------------------------------------------
    # The shared secret an authenticator app holds. Present but unconfirmed
    # means setup was started and never finished, which must NOT lock anybody
    # out — only ``totp_enabled`` decides whether a code is asked for.
    totp_secret: Mapped[str] = mapped_column(String(64), default="")
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The last thirty-second window a code was accepted for. A code already
    # used is refused for the rest of its life, so somebody who read it over a
    # shoulder cannot walk up and use it again.
    totp_last_counter: Mapped[int] = mapped_column(Integer, default=0)
    # Recovery codes, hashed, newline separated. Each is spent when used.
    totp_recovery: Mapped[str] = mapped_column(Text, default="")
    # When the half-finished secret above was issued. A setup screen left open,
    # or reopened an hour later, must keep offering the same key: a key that
    # changes underneath a phone that has already scanned it means every code
    # is refused for ever, with nothing on screen to say why.
    totp_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # How many thirty-second steps this computer's clock is out, measured from
    # a real code at setup. Zero on a computer whose time is right, which is
    # most of them. See ``totp.verify`` — this moves the window, never widens it.
    totp_offset: Mapped[int] = mapped_column(Integer, default=0)

    # --- Invitation to set a first password -------------------------------
    # Only the hash is kept, exactly as with the password itself: a copy of
    # this file must never contain a usable way in.
    invite_hash: Mapped[str] = mapped_column(String(64), default="")
    invite_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def invite_outstanding(self) -> bool:
        """An invitation sent, not yet used, not yet expired."""
        if not self.invite_hash:
            return False
        return not self.invite_expires or self.invite_expires >= clock.now()

    manager = relationship("User", remote_side=[id], foreign_keys=[manager_id])

    @property
    def display_name(self) -> str:
        return self.full_name or self.username

    @property
    def recovery_codes(self) -> list[str]:
        return [line for line in (self.totp_recovery or "").splitlines() if line.strip()]

    @recovery_codes.setter
    def recovery_codes(self, codes: list[str]) -> None:
        self.totp_recovery = "\n".join(codes)

    @property
    def recovery_codes_left(self) -> int:
        return len(self.recovery_codes)

    @property
    def has_bank_details(self) -> bool:
        return bool(self.bank_account_no and self.bank_name)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.now, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(60), default="")
    action: Mapped[str] = mapped_column(String(40))
    entity: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(60), default="")


class NumberSequence(Base):
    __tablename__ = "number_sequence"
    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(20), default="")
    next_number: Mapped[int] = mapped_column(Integer, default=1)
    padding: Mapped[int] = mapped_column(Integer, default=5)


# --------------------------------------------------------------------------
# Chart of accounts and the general ledger
# --------------------------------------------------------------------------


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    type: Mapped[str] = mapped_column(String(20), index=True)
    subtype: Mapped[str] = mapped_column(String(30), default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # System accounts are wired into posting logic and cannot be deleted
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    system_key: Mapped[str] = mapped_column(String(40), default="", index=True)
    is_bank: Mapped[bool] = mapped_column(Boolean, default=False)
    cashflow_class: Mapped[str] = mapped_column(String(20), default="OPERATING")

    parent = relationship("Account", remote_side=[id], backref="children")

    @property
    def normal_is_debit(self) -> bool:
        return self.type in DEBIT_TYPES

    def signed(self, debit: int, credit: int) -> int:
        """Balance in the account's natural direction."""
        return (debit - credit) if self.normal_is_debit else (credit - debit)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Account {self.code} {self.name}>"


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String(60), default="")
    # MANUAL / INVOICE / BILL / RECEIPT / PAYMENT / TRANSFER / STOCK /
    # OPENING / CLOSING / REVERSAL
    source: Mapped[str] = mapped_column(String(20), default="MANUAL", index=True)
    source_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    is_posted: Mapped[bool] = mapped_column(Boolean, default=True)
    is_void: Mapped[bool] = mapped_column(Boolean, default=False)
    reverses_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    total_debit: Mapped[int] = mapped_column(Integer, default=0)
    total_credit: Mapped[int] = mapped_column(Integer, default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    lines = relationship(
        "JournalLine",
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_no",
    )
    created_by = relationship("User")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[int] = mapped_column(Integer, default=0)
    credit: Mapped[int] = mapped_column(Integer, default=0)
    memo: Mapped[str] = mapped_column(String(255), default="")
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True, index=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    #: The job this line belongs to, if any. Job costing reads only this — a
    #: project's figures are the ledger's figures, filtered.
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    tax_base: Mapped[int] = mapped_column(Integer, default=0)

    # Bank reconciliation
    cleared: Mapped[bool] = mapped_column(Boolean, default=False)
    cleared_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reconciliation_id: Mapped[int | None] = mapped_column(
        ForeignKey("reconciliations.id"), nullable=True
    )

    entry = relationship("JournalEntry", back_populates="lines")
    account = relationship("Account")
    contact = relationship("Contact")


Index("ix_jl_account_entry", JournalLine.account_id, JournalLine.entry_id)


# --------------------------------------------------------------------------
# Tax
# --------------------------------------------------------------------------

VAT, WHT = "VAT", "WHT"


class TaxCode(Base):
    __tablename__ = "tax_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(10), default=VAT)  # VAT | WHT
    rate: Mapped[str] = mapped_column(String(10), default="0")
    # WHT only: the rate that applies when the counterparty has no Tax ID
    rate_no_tin: Mapped[str] = mapped_column(String(10), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    is_zero_rated: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------
# Contacts (customers and suppliers)
# --------------------------------------------------------------------------

COMPANY_CONTACT, INDIVIDUAL_CONTACT = "COMPANY", "INDIVIDUAL"


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    contact_type: Mapped[str] = mapped_column(String(15), default=COMPANY_CONTACT)
    is_customer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_vendor: Mapped[bool] = mapped_column(Boolean, default=False)
    tin: Mapped[str] = mapped_column(String(50), default="")
    rc_number: Mapped[str] = mapped_column(String(50), default="")
    contact_person: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    credit_limit: Mapped[int] = mapped_column(Integer, default=0)
    default_wht_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("tax_codes.id"), nullable=True
    )
    # Small companies (turnover <= N50m) with a valid Tax ID are exempt from
    # suffering WHT on monthly transactions of N2m or less.
    is_small_company: Mapped[bool] = mapped_column(Boolean, default=False)
    # Government agencies and companies in oil and gas are appointed by the NRS
    # to withhold VAT at source and remit it themselves. Invoices to them are
    # paid net of VAT, and the VAT comes back as a credit rather than cash.
    withholds_vat: Mapped[bool] = mapped_column(Boolean, default=False)
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    bank_account_no: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    default_wht_code = relationship("TaxCode")

    @property
    def has_tin(self) -> bool:
        return bool((self.tin or "").strip())


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

STOCK_ITEM, SERVICE_ITEM = "STOCK", "SERVICE"


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    item_type: Mapped[str] = mapped_column(String(10), default=STOCK_ITEM)
    unit: Mapped[str] = mapped_column(String(20), default="each")
    category: Mapped[str] = mapped_column(String(80), default="")
    barcode: Mapped[str] = mapped_column(String(60), default="")

    sale_price: Mapped[int] = mapped_column(Integer, default=0)     # kobo, VAT-exclusive
    purchase_price: Mapped[int] = mapped_column(Integer, default=0)  # kobo

    sales_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    purchase_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    inventory_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    cogs_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    sale_tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    purchase_tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)

    # Weighted-average cost is maintained as running totals, never as a
    # per-unit float: unit cost = stock_value / qty_on_hand, computed on demand.
    qty_on_hand: Mapped[int] = mapped_column(Integer, default=0)     # milli-units
    stock_value: Mapped[int] = mapped_column(Integer, default=0)     # kobo
    reorder_level: Mapped[int] = mapped_column(Integer, default=0)   # milli-units
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    track_stock: Mapped[bool] = mapped_column(Boolean, default=True)

    # Depth, item by item. An ordinary bag of cement needs none of this; a
    # generator needs a serial number and a drum of paint needs a batch.
    costing_method: Mapped[str] = mapped_column(String(10), default="AVERAGE")
    track_batches: Mapped[bool] = mapped_column(Boolean, default=False)
    track_serials: Mapped[bool] = mapped_column(Boolean, default=False)
    shelf_life_days: Mapped[int] = mapped_column(Integer, default=0)
    warranty_months: Mapped[int] = mapped_column(Integer, default=0)

    sales_account = relationship("Account", foreign_keys=[sales_account_id])
    purchase_account = relationship("Account", foreign_keys=[purchase_account_id])
    inventory_account = relationship("Account", foreign_keys=[inventory_account_id])
    cogs_account = relationship("Account", foreign_keys=[cogs_account_id])
    sale_tax_code = relationship("TaxCode", foreign_keys=[sale_tax_code_id])
    purchase_tax_code = relationship("TaxCode", foreign_keys=[purchase_tax_code_id])

    @property
    def unit_cost(self) -> int:
        if self.qty_on_hand <= 0:
            return self.purchase_price
        return round(self.stock_value * 1000 / self.qty_on_hand)


class StockMove(Base):
    __tablename__ = "stock_moves"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"),
                                                    nullable=True, index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"),
                                                 nullable=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    doc_type: Mapped[str] = mapped_column(String(20), default="")
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_number: Mapped[str] = mapped_column(String(30), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)          # milli, +in / -out
    unit_cost: Mapped[int] = mapped_column(Integer, default=0)    # kobo per unit
    value: Mapped[int] = mapped_column(Integer, default=0)        # kobo, +in / -out
    balance_qty: Mapped[int] = mapped_column(Integer, default=0)
    balance_value: Mapped[int] = mapped_column(Integer, default=0)
    memo: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    item = relationship("Item")
    location = relationship("Location")
    batch = relationship("Batch")


# --------------------------------------------------------------------------
# Sales documents
# --------------------------------------------------------------------------


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    # INVOICE | CREDIT_NOTE | QUOTE
    doc_type: Mapped[str] = mapped_column(String(15), default="INVOICE", index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference: Mapped[str] = mapped_column(String(60), default="")
    po_number: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(12), default=DRAFT, index=True)

    subtotal: Mapped[int] = mapped_column(Integer, default=0)
    discount_total: Mapped[int] = mapped_column(Integer, default=0)
    vat_total: Mapped[int] = mapped_column(Integer, default=0)
    wht_total: Mapped[int] = mapped_column(Integer, default=0)  # WHT customer will deduct
    total: Mapped[int] = mapped_column(Integer, default=0)
    amount_paid: Mapped[int] = mapped_column(Integer, default=0)
    cogs_total: Mapped[int] = mapped_column(Integer, default=0)

    wht_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[str] = mapped_column(Text, default="")
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    converted_from_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    credit_of_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    # Which store the goods leave from
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    contact = relationship("Contact")
    wht_code = relationship("TaxCode")
    location = relationship("Location")
    lines = relationship(
        "InvoiceLine",
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.line_no",
    )

    @property
    def balance_due(self) -> int:
        """Outstanding amount. ``amount_paid`` already includes any WHT credit
        and settlement discount applied through a receipt allocation."""
        return self.total - self.amount_paid

    @property
    def vat_withheld_expected(self) -> int:
        """VAT this customer will keep back and pay to the NRS itself.

        Government agencies and oil-and-gas companies are appointed to do this.
        The sale is still VATable and the output VAT is still yours to declare —
        you simply do not collect it, and it comes off the return instead.
        """
        if self.contact is None or not self.contact.withholds_vat:
            return 0
        return self.vat_total

    @property
    def expected_cash(self) -> int:
        """What the customer will actually pay, after everything they keep back."""
        return self.total - self.wht_total - self.vat_withheld_expected


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    qty: Mapped[int] = mapped_column(Integer, default=1000)     # milli-units
    unit_price: Mapped[int] = mapped_column(Integer, default=0)  # kobo, VAT-exclusive
    discount_pct: Mapped[str] = mapped_column(String(8), default="0")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    net: Mapped[int] = mapped_column(Integer, default=0)
    vat_amount: Mapped[int] = mapped_column(Integer, default=0)
    cogs_amount: Mapped[int] = mapped_column(Integer, default=0)
    # Inventory depth, filled in only for items that need it
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    serials: Mapped[str] = mapped_column(Text, default="")

    invoice = relationship("Invoice", back_populates="lines")
    item = relationship("Item")
    account = relationship("Account")
    tax_code = relationship("TaxCode")


# --------------------------------------------------------------------------
# Purchase documents
# --------------------------------------------------------------------------


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    # BILL | DEBIT_NOTE | PO
    doc_type: Mapped[str] = mapped_column(String(15), default="BILL", index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    vendor_invoice_no: Mapped[str] = mapped_column(String(60), default="")
    reference: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(12), default=DRAFT, index=True)

    subtotal: Mapped[int] = mapped_column(Integer, default=0)
    discount_total: Mapped[int] = mapped_column(Integer, default=0)
    vat_total: Mapped[int] = mapped_column(Integer, default=0)
    wht_total: Mapped[int] = mapped_column(Integer, default=0)  # WHT we must deduct
    total: Mapped[int] = mapped_column(Integer, default=0)
    amount_paid: Mapped[int] = mapped_column(Integer, default=0)

    wht_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    converted_from_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    debit_of_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    # Which store the goods arrive into
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    contact = relationship("Contact")
    wht_code = relationship("TaxCode")
    location = relationship("Location")
    lines = relationship(
        "BillLine",
        back_populates="bill",
        cascade="all, delete-orphan",
        order_by="BillLine.line_no",
    )

    @property
    def balance_due(self) -> int:
        """Outstanding amount. ``amount_paid`` already includes any WHT withheld
        and settlement discount taken through a payment allocation."""
        return self.total - self.amount_paid



class BillLine(Base):
    __tablename__ = "bill_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, default="")
    qty: Mapped[int] = mapped_column(Integer, default=1000)
    unit_price: Mapped[int] = mapped_column(Integer, default=0)
    discount_pct: Mapped[str] = mapped_column(String(8), default="0")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    net: Mapped[int] = mapped_column(Integer, default=0)
    vat_amount: Mapped[int] = mapped_column(Integer, default=0)
    # Inventory depth, filled in only for items that need it
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    serials: Mapped[str] = mapped_column(Text, default="")

    bill = relationship("Bill", back_populates="lines")
    item = relationship("Item")
    account = relationship("Account")
    tax_code = relationship("TaxCode")


# --------------------------------------------------------------------------
# Money in / money out
# --------------------------------------------------------------------------

RECEIPT, PAYMENT = "RECEIPT", "PAYMENT"


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(10), default=RECEIPT, index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    method: Mapped[str] = mapped_column(String(30), default="Bank transfer")
    reference: Mapped[str] = mapped_column(String(60), default="")
    # Cash actually moved
    amount: Mapped[int] = mapped_column(Integer, default=0)
    # WHT credit note received/issued alongside the cash
    wht_amount: Mapped[int] = mapped_column(Integer, default=0)
    vat_withheld: Mapped[int] = mapped_column(Integer, default=0)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    bank_charge: Mapped[int] = mapped_column(Integer, default=0)
    unallocated: Mapped[int] = mapped_column(Integer, default=0)
    memo: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(12), default=POSTED)
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    contact = relationship("Contact")
    bank_account = relationship("BankAccount")
    allocations = relationship(
        "PaymentAllocation", back_populates="payment", cascade="all, delete-orphan"
    )


class PaymentAllocation(Base):
    __tablename__ = "payment_allocations"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    wht_amount: Mapped[int] = mapped_column(Integer, default=0)
    vat_withheld: Mapped[int] = mapped_column(Integer, default=0)
    discount: Mapped[int] = mapped_column(Integer, default=0)

    payment = relationship("Payment", back_populates="allocations")
    invoice = relationship("Invoice")
    bill = relationship("Bill")


# --------------------------------------------------------------------------
# Banking
# --------------------------------------------------------------------------


class BankAccount(Base):
    __tablename__ = "bank_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), unique=True)
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    account_number: Mapped[str] = mapped_column(String(40), default="")
    account_type: Mapped[str] = mapped_column(String(20), default="CURRENT")
    branch: Mapped[str] = mapped_column(String(120), default="")
    currency_code: Mapped[str] = mapped_column(String(6), default="NGN")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    # --- What a customer needs in order to pay into it --------------------
    #: Printed on invoices when set. Off by default and per account, because a
    #: business often holds one it does not want customers paying into — a
    #: savings account, a domiciliary account kept for one contract, the petty
    #: cash tin. The account the money arrives in is the one the ledger already
    #: knows about, so these details live here rather than being typed a second
    #: time somewhere else and left to drift out of step.
    show_on_invoices: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The name the bank holds on the account, which is often not the short
    #: label staff use in the app. A customer's bank checks this one.
    account_name: Mapped[str] = mapped_column(String(160), default="")
    #: For customers whose banks ask for them. Blank ones are never printed.
    sort_code: Mapped[str] = mapped_column(String(20), default="")
    swift: Mapped[str] = mapped_column(String(20), default="")
    iban: Mapped[str] = mapped_column(String(40), default="")

    account = relationship("Account")

    @property
    def payable_lines(self) -> list[tuple[str, str]]:
        """The account written out the way it goes on an invoice.

        Only what is filled in. An invoice carrying "IBAN: —" tells a customer
        nothing except that somebody was careless.
        """
        rows: list[tuple[str, str]] = []
        if self.bank_name:
            rows.append(("Bank", self.bank_name))
        if self.account_name:
            rows.append(("Account name", self.account_name))
        if self.account_number:
            rows.append(("Account number", self.account_number))
        if self.sort_code:
            rows.append(("Sort code", self.sort_code))
        if self.iban:
            rows.append(("IBAN", self.iban))
        if self.swift:
            rows.append(("SWIFT/BIC", self.swift))
        if self.branch:
            rows.append(("Branch", self.branch))
        return rows

    @property
    def can_be_shown(self) -> bool:
        """Enough filled in that printing it would actually help somebody."""
        return bool(self.bank_name and self.account_number)


# --------------------------------------------------------------------------
# Projects and job costing
# --------------------------------------------------------------------------

PROJECT_OPEN, PROJECT_DONE, PROJECT_ABANDONED = "OPEN", "DONE", "ABANDONED"

PROJECT_STATUSES = [
    (PROJECT_OPEN, "Open"),
    (PROJECT_DONE, "Finished"),
    (PROJECT_ABANDONED, "Abandoned"),
]


class Project(Base):
    """A job, contract or site that money can be coded to.

    Deliberately light. A project is a label on ledger lines plus a budget to
    compare them with — not a second set of books. Everything it reports is
    read back out of the general ledger, so a project's profit and the
    company's profit can never drift apart.
    """

    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default=PROJECT_OPEN, index=True)
    started_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    finished_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    #: What the customer agreed to pay. Used to spot work done and not billed.
    contract_value: Mapped[int] = mapped_column(Integer, default=0)
    #: What it was expected to cost.
    budget_cost: Mapped[int] = mapped_column(Integer, default=0)
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    contact = relationship("Contact")
    manager = relationship("User")

    @property
    def is_open(self) -> bool:
        return self.status == PROJECT_OPEN


# --------------------------------------------------------------------------
# Bank statement import
# --------------------------------------------------------------------------

#: What has been decided about one imported statement line.
UNMATCHED, SUGGESTED, CONFIRMED, IGNORED = "UNMATCHED", "SUGGESTED", "CONFIRMED", "IGNORED"

#: What confirming a line will actually do.
ACTION_CLEAR = "CLEAR"        # it is already in the books; just tick it off
ACTION_RECEIPT = "RECEIPT"    # money in, settling one or more sales invoices
ACTION_PAYMENT = "PAYMENT"    # money out, settling one or more supplier bills
ACTION_POST = "POST"          # neither: post it straight to an account
ACTION_IGNORE = "IGNORE"      # not ours — a transfer already recorded, say


class BankImport(Base):
    """One statement file, and what came of it.

    Kept after the import rather than thrown away so that a person can see
    which statement a transaction came from, and so a file imported twice by
    accident can be recognised.
    """

    __tablename__ = "bank_imports"
    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    filename: Mapped[str] = mapped_column(String(200), default="")
    file_format: Mapped[str] = mapped_column(String(10), default="csv")
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    imported_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    first_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    opening_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closing_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_in: Mapped[int] = mapped_column(Integer, default=0)
    total_out: Mapped[int] = mapped_column(Integer, default=0)
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")

    bank_account = relationship("BankAccount")
    imported_by = relationship("User")
    lines = relationship(
        "BankImportLine", back_populates="batch", cascade="all, delete-orphan",
        order_by="BankImportLine.date, BankImportLine.id",
    )

    @property
    def settled(self) -> int:
        return sum(1 for line in self.lines if line.status in (CONFIRMED, IGNORED))

    @property
    def outstanding(self) -> int:
        return sum(1 for line in self.lines if line.status not in (CONFIRMED, IGNORED))

    @property
    def is_done(self) -> bool:
        return self.outstanding == 0


class BankImportLine(Base):
    """One line off the statement, and what was decided about it."""

    __tablename__ = "bank_import_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("bank_imports.id"), index=True)
    row_no: Mapped[int] = mapped_column(Integer, default=0)
    date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String(120), default="")
    payee: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[int] = mapped_column(Integer, default=0)
    balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: What makes this line the same line if the file is imported again.
    fingerprint: Mapped[str] = mapped_column(String(200), default="", index=True)

    status: Mapped[str] = mapped_column(String(12), default=UNMATCHED, index=True)
    action: Mapped[str] = mapped_column(String(12), default="")
    #: How the suggestion was arrived at, in words, so a person can judge it.
    reason: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int] = mapped_column(Integer, default=0)

    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    #: Comma separated invoice or bill ids this line settles.
    document_ids: Mapped[str] = mapped_column(String(200), default="")
    #: The journal line this was matched to, when it was already in the books.
    journal_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_lines.id"), nullable=True
    )
    #: What confirming it created, once it has been confirmed.
    entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)

    batch = relationship("BankImport", back_populates="lines")
    contact = relationship("Contact")
    account = relationship("Account")

    @property
    def is_money_in(self) -> bool:
        return self.amount > 0

    @property
    def documents(self) -> list[int]:
        return [int(x) for x in self.document_ids.split(",") if x.strip().isdigit()]

    @documents.setter
    def documents(self, ids: list[int]) -> None:
        self.document_ids = ",".join(str(i) for i in ids)


class PayeeRule(Base):
    """What this business has decided a particular payee means.

    Learned from what the person actually chose, never invented: the first
    time somebody says a line reading "MTN NIGERIA" is a telephone cost, the
    rule is written down, and every later statement suggests it. It is a
    suggestion to the end — nothing posts without a person confirming it.
    """

    __tablename__ = "payee_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    #: The normalised fragment matched against the statement description.
    pattern: Mapped[str] = mapped_column(String(120), index=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)
    #: Only applies to money in, money out, or either.
    direction: Mapped[str] = mapped_column(String(4), default="BOTH")
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    last_used: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    account = relationship("Account")
    contact = relationship("Contact")


class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id: Mapped[int] = mapped_column(primary_key=True)
    bank_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"), index=True)
    statement_date: Mapped[date] = mapped_column(Date)
    statement_balance: Mapped[int] = mapped_column(Integer, default=0)
    opening_balance: Mapped[int] = mapped_column(Integer, default=0)
    cleared_total: Mapped[int] = mapped_column(Integer, default=0)
    difference: Mapped[int] = mapped_column(Integer, default=0)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")

    bank_account = relationship("BankAccount")


class FiledDocument(Base):
    """A piece of paperwork from before the books, kept because it matters.

    Every business changing accounting systems has a drawer of invoices and
    receipts that predate the new one. Some are settled and only need to be
    findable; some are still owed and belong in the ledger. Retyping the first
    kind as invoices pollutes the accounts with money that has already moved,
    and leaving the second kind out understates what the business is owed.

    So this is a filing cabinet with a door into the ledger. Every filed
    document holds the scan, who it was with, when, for how much, and what it
    was. ``invoice_id`` is set only for the ones that were also entered as real
    invoices — for the rest it stays empty, and nothing they carry touches a
    single account.
    """

    __tablename__ = "filed_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    #: INVOICE / RECEIPT / BILL / STATEMENT / OTHER
    kind: Mapped[str] = mapped_column(String(20), default="INVOICE", index=True)
    #: The other party — a customer for an invoice, a supplier for a bill.
    party: Mapped[str] = mapped_column(String(160), default="", index=True)
    doc_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    reference: Mapped[str] = mapped_column(String(60), default="", index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    #: Set when this was also entered into the books, so the two can never be
    #: confused for one another and a duplicate is obvious on the screen.
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id"), nullable=True, index=True)
    filed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    filed_by_name: Mapped[str] = mapped_column(String(60), default="")
    filed_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    invoice = relationship("Invoice")

    @property
    def in_the_books(self) -> bool:
        return self.invoice_id is not None

    @property
    def kind_label(self) -> str:
        return {
            "INVOICE": "Invoice", "RECEIPT": "Receipt", "BILL": "Bill",
            "STATEMENT": "Statement", "OTHER": "Other",
        }.get(self.kind, self.kind.title())


class Attachment(Base):
    """A file kept alongside a record — a receipt, delivery note, WHT credit note.

    The file itself lives in the company's attachments folder; this row is the
    index. ``doc_type`` and ``doc_id`` say what it belongs to.
    """

    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(20), index=True)
    doc_id: Mapped[int] = mapped_column(Integer, index=True)
    filename: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(120))
    content_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(255), default="")
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    uploaded_by_name: Mapped[str] = mapped_column(String(60), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image/")

    @property
    def size_label(self) -> str:
        if self.size < 1024:
            return f"{self.size} B"
        if self.size < 1024 * 1024:
            return f"{self.size / 1024:.0f} KB"
        return f"{self.size / (1024 * 1024):.1f} MB"


# --------------------------------------------------------------------------
# Payroll
# --------------------------------------------------------------------------

ACTIVE, ON_LEAVE, SUSPENDED, LEFT = "ACTIVE", "ON_LEAVE", "SUSPENDED", "LEFT"

EMPLOYEE_STATUSES = [
    (ACTIVE, "Active"),
    (ON_LEAVE, "On leave"),
    (SUSPENDED, "Suspended"),
    (LEFT, "No longer employed"),
]

EARNING, DEDUCTION = "EARNING", "DEDUCTION"


#: What a contribution is worked out on. A percentage has to be a percentage
#: of something, and countries disagree about what: Nigeria's pension is on
#: basic plus housing plus transport, its NHF on basic alone, its NSITF on the
#: whole payroll.
CONTRIBUTION_BASES = {
    "PENSIONABLE": "Basic + housing + transport",
    "BASIC": "Basic salary only",
    "GROSS": "Total gross pay",
    "TAXABLE": "Taxable pay",
}


class EmailLog(Base):
    """What was emailed, to whom, and whether it actually left.

    Kept because "did you send that invoice?" is a question every business asks
    at least once a week, and because a send that failed has to be visible
    rather than assumed. A failure is recorded exactly as a success is — the
    row is the evidence either way.
    """

    __tablename__ = "email_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    to_address: Mapped[str] = mapped_column(String(300), default="")
    cc: Mapped[str] = mapped_column(String(300), default="")
    subject: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(30), default="", index=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ref_number: Mapped[str] = mapped_column(String(40), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    sent_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    sent_by = relationship("User", lazy="joined")


class PayrollBand(Base):
    """One slice of the income-tax table.

    Bands are annual and cumulative: the first covers the lowest slice of
    chargeable income, the next carries on where it stopped, and the last —
    the one with no width — takes everything above. That is how every
    progressive income tax in the world is written, so it is how the screen
    asks for it.
    """

    __tablename__ = "payroll_bands"
    id: Mapped[int] = mapped_column(primary_key=True)
    sort: Mapped[int] = mapped_column(Integer, default=0, index=True)
    #: Width of this band per year. Null means "and everything above".
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rate: Mapped[str] = mapped_column(String(10), default="0")


class PayrollCheck(Base):
    """A salary whose answer is already known, kept so it can be re-run.

    Typing your own tax rates is the fastest way to get payroll wrong, and a
    wrong payslip is somebody's rent. So the scheme can be pointed at a case
    the employer already has the right answer for — last month's payslip, the
    tax office's own worked example — and told to reproduce it. Every check is
    re-run whenever the rates are edited, which turns "I think these rates are
    right" into something the employer can actually see.
    """

    __tablename__ = "payroll_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    basic: Mapped[int] = mapped_column(Integer, default=0)
    housing: Mapped[int] = mapped_column(Integer, default=0)
    transport: Mapped[int] = mapped_column(Integer, default=0)
    frequency: Mapped[str] = mapped_column(String(15), default="MONTHLY")
    annual_rent_paid: Mapped[int] = mapped_column(Integer, default=0)
    pension_enrolled: Mapped[bool] = mapped_column(Boolean, default=True)
    nhf_enrolled: Mapped[bool] = mapped_column(Boolean, default=True)
    nhis_enrolled: Mapped[bool] = mapped_column(Boolean, default=False)

    #: What the employer says the answer is. A zero means "do not check this".
    expected_gross: Mapped[int] = mapped_column(Integer, default=0)
    expected_tax: Mapped[int] = mapped_column(Integer, default=0)
    expected_net: Mapped[int] = mapped_column(Integer, default=0)
    #: How far out is still acceptable — rounding differs between systems.
    tolerance: Mapped[int] = mapped_column(Integer, default=100)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_result: Mapped[str] = mapped_column(String(10), default="")     # PASS / FAIL
    last_detail: Mapped[str] = mapped_column(Text, default="")


class PayrollSetting(Base):
    """One row holding the statutory rates. Editable when the law changes."""

    __tablename__ = "payroll_settings"
    id: Mapped[int] = mapped_column(primary_key=True)

    # --- What this country's payroll is called ---------------------------
    scheme_name: Mapped[str] = mapped_column(String(120), default="Nigeria — Tax Act 2025")
    #: "PAYE" in Nigeria and Britain, "Income Tax" almost everywhere else.
    tax_name: Mapped[str] = mapped_column(String(40), default="PAYE")
    #: Off means the built-in Nigerian bands; on means the bands table below.
    use_custom_bands: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Whether the scheme has been checked against a salary with a known answer
    scheme_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    minimum_wage: Mapped[int] = mapped_column(Integer, default=70_000_00)
    #: What that threshold is called where the employer is. Nigeria ties it to
    #: the national minimum wage; elsewhere it is a personal allowance, or
    #: simply "the tax-free threshold".
    threshold_name: Mapped[str] = mapped_column(
        String(60), default="the national minimum wage")
    #: What the relief below is called. Nigeria's is rent relief; elsewhere it
    #: might be a personal allowance, or nothing at all.
    relief_name: Mapped[str] = mapped_column(String(60), default="Rent relief")
    rent_relief_rate: Mapped[str] = mapped_column(String(10), default="20")
    rent_relief_cap: Mapped[int] = mapped_column(Integer, default=500_000_00)
    pension_employee: Mapped[str] = mapped_column(String(10), default="8")
    pension_employer: Mapped[str] = mapped_column(String(10), default="10")
    nhf_rate: Mapped[str] = mapped_column(String(10), default="2.5")
    nsitf_rate: Mapped[str] = mapped_column(String(10), default="1")
    itf_rate: Mapped[str] = mapped_column(String(10), default="1")
    nhis_employee: Mapped[str] = mapped_column(String(10), default="5")
    nhis_employer: Mapped[str] = mapped_column(String(10), default="10")

    # --- The five contribution slots, renameable -------------------------
    # Nigeria fills all five. Elsewhere an employer renames the ones their
    # country has — NSSF, NHIF, EPF, superannuation, social security — and
    # switches off the rest. Five covers what a small employer anywhere
    # actually deducts; the names are what make it theirs.
    pension_name: Mapped[str] = mapped_column(String(60), default="Pension")
    pension_base: Mapped[str] = mapped_column(String(15), default="PENSIONABLE")
    pension_employee_cap: Mapped[int] = mapped_column(Integer, default=0)
    pension_employer_cap: Mapped[int] = mapped_column(Integer, default=0)
    pension_reduces_tax: Mapped[bool] = mapped_column(Boolean, default=True)

    nhf_name: Mapped[str] = mapped_column(String(60), default="NHF")
    nhf_base: Mapped[str] = mapped_column(String(15), default="BASIC")
    nhf_cap: Mapped[int] = mapped_column(Integer, default=0)
    nhf_reduces_tax: Mapped[bool] = mapped_column(Boolean, default=True)

    nhis_name: Mapped[str] = mapped_column(String(60), default="NHIS")
    nhis_base: Mapped[str] = mapped_column(String(15), default="BASIC")
    nhis_employee_cap: Mapped[int] = mapped_column(Integer, default=0)
    nhis_employer_cap: Mapped[int] = mapped_column(Integer, default=0)
    nhis_reduces_tax: Mapped[bool] = mapped_column(Boolean, default=True)

    nsitf_name: Mapped[str] = mapped_column(String(60), default="NSITF")
    nsitf_base: Mapped[str] = mapped_column(String(15), default="GROSS")
    nsitf_cap: Mapped[int] = mapped_column(Integer, default=0)

    itf_name: Mapped[str] = mapped_column(String(60), default="ITF")
    itf_base: Mapped[str] = mapped_column(String(15), default="GROSS")
    itf_cap: Mapped[int] = mapped_column(Integer, default=0)
    #: Nigeria's ITF only applies above a size threshold. Off elsewhere.
    itf_size_test: Mapped[bool] = mapped_column(Boolean, default=True)

    # Which schemes this employer operates at all
    operates_pension: Mapped[bool] = mapped_column(Boolean, default=True)
    operates_nhf: Mapped[bool] = mapped_column(Boolean, default=True)
    operates_nsitf: Mapped[bool] = mapped_column(Boolean, default=True)
    operates_itf: Mapped[bool] = mapped_column(Boolean, default=False)
    operates_nhis: Mapped[bool] = mapped_column(Boolean, default=False)

    paye_state: Mapped[str] = mapped_column(String(60), default="Lagos")
    default_pfa: Mapped[str] = mapped_column(String(120), default="")
    payslip_note: Mapped[str] = mapped_column(
        Text, default="This payslip is confidential. Query any error within 7 days."
    )


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    staff_no: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(80))
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    middle_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(15), default=ACTIVE, index=True)
    is_director: Mapped[bool] = mapped_column(Boolean, default=False)

    job_title: Mapped[str] = mapped_column(String(120), default="")
    department: Mapped[str] = mapped_column(String(80), default="")
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    leave_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str] = mapped_column(String(10), default="")

    email: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(60), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    state_of_residence: Mapped[str] = mapped_column(String(60), default="")

    tin: Mapped[str] = mapped_column(String(50), default="")
    nin: Mapped[str] = mapped_column(String(30), default="")
    pfa_name: Mapped[str] = mapped_column(String(120), default="")
    pension_pin: Mapped[str] = mapped_column(String(40), default="")
    nhf_number: Mapped[str] = mapped_column(String(40), default="")

    bank_name: Mapped[str] = mapped_column(String(120), default="")
    bank_account_no: Mapped[str] = mapped_column(String(40), default="")
    bank_account_name: Mapped[str] = mapped_column(String(120), default="")

    # How and how often they are paid
    frequency: Mapped[str] = mapped_column(String(15), default="MONTHLY")
    pay_basis: Mapped[str] = mapped_column(String(15), default="FIXED")
    # For FIXED these are amounts per period; for a rate basis, per day or hour
    basic: Mapped[int] = mapped_column(Integer, default=0)
    housing: Mapped[int] = mapped_column(Integer, default=0)
    transport: Mapped[int] = mapped_column(Integer, default=0)
    default_units: Mapped[str] = mapped_column(String(10), default="1")

    pension_enrolled: Mapped[bool] = mapped_column(Boolean, default=True)
    nhf_enrolled: Mapped[bool] = mapped_column(Boolean, default=True)
    nhis_enrolled: Mapped[bool] = mapped_column(Boolean, default=False)
    paye_exempt: Mapped[bool] = mapped_column(Boolean, default=False)
    annual_rent_paid: Mapped[int] = mapped_column(Integer, default=0)

    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    components = relationship(
        "EmployeeComponent", back_populates="employee", cascade="all, delete-orphan",
        order_by="EmployeeComponent.sort",
    )
    loans = relationship("EmployeeLoan", back_populates="employee",
                         cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.middle_name, self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def short_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def gross_per_period(self) -> int:
        extra = sum(c.amount for c in self.components
                    if c.kind == EARNING and c.is_active and not c.is_percentage)
        pct = sum(
            round(self.basic * float(c.rate or 0) / 100)
            for c in self.components
            if c.kind == EARNING and c.is_active and c.is_percentage
        )
        return self.basic + self.housing + self.transport + extra + pct

    @property
    def is_on_payroll(self) -> bool:
        return self.status in (ACTIVE, ON_LEAVE)


class EmployeeComponent(Base):
    """A recurring allowance or deduction on someone's pay."""

    __tablename__ = "employee_components"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12), default=EARNING)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    # Alternatively express it as a percentage of basic
    is_percentage: Mapped[bool] = mapped_column(Boolean, default=False)
    rate: Mapped[str] = mapped_column(String(10), default="")
    # Earnings: does it attract PAYE, and does it count towards pension?
    taxable: Mapped[bool] = mapped_column(Boolean, default=True)
    pensionable: Mapped[bool] = mapped_column(Boolean, default=False)
    # Deductions: does it reduce chargeable income (life assurance) or not (union dues)?
    reduces_tax: Mapped[bool] = mapped_column(Boolean, default=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    employee = relationship("Employee", back_populates="components")
    account = relationship("Account")


class EmployeeLoan(Base):
    __tablename__ = "employee_loans"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(200), default="Staff loan")
    principal: Mapped[int] = mapped_column(Integer, default=0)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    repayment: Mapped[int] = mapped_column(Integer, default=0)   # deducted each period
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")

    employee = relationship("Employee", back_populates="loans")


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    frequency: Mapped[str] = mapped_column(String(15), default="MONTHLY")
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    pay_date: Mapped[date] = mapped_column(Date)
    # DRAFT -> POSTED -> PAID, or VOID
    status: Mapped[str] = mapped_column(String(12), default=DRAFT, index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    gross_total: Mapped[int] = mapped_column(Integer, default=0)
    paye_total: Mapped[int] = mapped_column(Integer, default=0)
    pension_employee_total: Mapped[int] = mapped_column(Integer, default=0)
    pension_employer_total: Mapped[int] = mapped_column(Integer, default=0)
    nhf_total: Mapped[int] = mapped_column(Integer, default=0)
    nhis_employee_total: Mapped[int] = mapped_column(Integer, default=0)
    nhis_employer_total: Mapped[int] = mapped_column(Integer, default=0)
    nsitf_total: Mapped[int] = mapped_column(Integer, default=0)
    itf_total: Mapped[int] = mapped_column(Integer, default=0)
    loan_total: Mapped[int] = mapped_column(Integer, default=0)
    other_deductions_total: Mapped[int] = mapped_column(Integer, default=0)
    net_total: Mapped[int] = mapped_column(Integer, default=0)
    employer_cost_total: Mapped[int] = mapped_column(Integer, default=0)

    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    payment_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    paid_from_bank_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_accounts.id"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    payslips = relationship(
        "Payslip", back_populates="run", cascade="all, delete-orphan",
        order_by="Payslip.id",
    )
    paid_from = relationship("BankAccount")

    @property
    def employee_count(self) -> int:
        return len(self.payslips)


class Payslip(Base):
    __tablename__ = "payslips"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)

    # Snapshot, so an old payslip still reads correctly after someone's
    # details change or they leave
    staff_no: Mapped[str] = mapped_column(String(30), default="")
    employee_name: Mapped[str] = mapped_column(String(200), default="")
    job_title: Mapped[str] = mapped_column(String(120), default="")
    department: Mapped[str] = mapped_column(String(80), default="")
    bank_name: Mapped[str] = mapped_column(String(120), default="")
    bank_account_no: Mapped[str] = mapped_column(String(40), default="")

    units: Mapped[str] = mapped_column(String(10), default="1")
    basic: Mapped[int] = mapped_column(Integer, default=0)
    housing: Mapped[int] = mapped_column(Integer, default=0)
    transport: Mapped[int] = mapped_column(Integer, default=0)
    gross: Mapped[int] = mapped_column(Integer, default=0)
    pensionable: Mapped[int] = mapped_column(Integer, default=0)

    pension_employee: Mapped[int] = mapped_column(Integer, default=0)
    pension_employer: Mapped[int] = mapped_column(Integer, default=0)
    nhf: Mapped[int] = mapped_column(Integer, default=0)
    nhis_employee: Mapped[int] = mapped_column(Integer, default=0)
    nhis_employer: Mapped[int] = mapped_column(Integer, default=0)
    nsitf: Mapped[int] = mapped_column(Integer, default=0)
    itf: Mapped[int] = mapped_column(Integer, default=0)

    annual_gross: Mapped[int] = mapped_column(Integer, default=0)
    rent_relief: Mapped[int] = mapped_column(Integer, default=0)
    annual_reliefs: Mapped[int] = mapped_column(Integer, default=0)
    annual_chargeable: Mapped[int] = mapped_column(Integer, default=0)
    annual_paye: Mapped[int] = mapped_column(Integer, default=0)
    paye: Mapped[int] = mapped_column(Integer, default=0)
    paye_note: Mapped[str] = mapped_column(String(255), default="")

    loan_repayment: Mapped[int] = mapped_column(Integer, default=0)
    other_deductions: Mapped[int] = mapped_column(Integer, default=0)
    total_deductions: Mapped[int] = mapped_column(Integer, default=0)
    net_pay: Mapped[int] = mapped_column(Integer, default=0)
    employer_cost: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")

    run = relationship("PayrollRun", back_populates="payslips")
    employee = relationship("Employee")
    lines = relationship(
        "PayslipLine", back_populates="payslip", cascade="all, delete-orphan",
        order_by="PayslipLine.sort",
    )


class PayslipLine(Base):
    """The itemised earnings and deductions printed on a payslip."""

    __tablename__ = "payslip_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    payslip_id: Mapped[int] = mapped_column(ForeignKey("payslips.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(12), default=EARNING)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    is_statutory: Mapped[bool] = mapped_column(Boolean, default=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    payslip = relationship("Payslip", back_populates="lines")


class FiscalYear(Base):
    __tablename__ = "fiscal_years"
    id: Mapped[int] = mapped_column(primary_key=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(40))
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closing_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

    __table_args__ = (UniqueConstraint("start_date", "end_date"),)


# --------------------------------------------------------------------------
# Fixed assets
# --------------------------------------------------------------------------

STRAIGHT_LINE, REDUCING_BALANCE, NO_DEPRECIATION = "STRAIGHT", "REDUCING", "NONE"

DEPRECIATION_METHODS = [
    (STRAIGHT_LINE, "Straight line — the same charge every month"),
    (REDUCING_BALANCE, "Reducing balance — a percentage of what is left"),
    (NO_DEPRECIATION, "None — land, or an asset held at cost"),
]

ASSET_ACTIVE, ASSET_DISPOSED, ASSET_WRITTEN_OFF = "ACTIVE", "DISPOSED", "WRITTEN_OFF"

ASSET_STATUSES = [
    (ASSET_ACTIVE, "In use"),
    (ASSET_DISPOSED, "Disposed"),
    (ASSET_WRITTEN_OFF, "Written off"),
]


class AssetCategory(Base):
    """A class of assets — vehicles, generators, office equipment.

    The category carries the three accounts and the default depreciation
    policy, so adding an asset is a matter of a name, a cost and a date.
    """

    __tablename__ = "asset_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    method: Mapped[str] = mapped_column(String(10), default=STRAIGHT_LINE)
    useful_life_months: Mapped[int] = mapped_column(Integer, default=48)
    rate_pct: Mapped[str] = mapped_column(String(10), default="25")   # reducing balance, % a year
    residual_pct: Mapped[str] = mapped_column(String(10), default="0")

    asset_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    accum_dep_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"), nullable=True
    )
    expense_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)

    asset_account = relationship("Account", foreign_keys=[asset_account_id])
    accum_dep_account = relationship("Account", foreign_keys=[accum_dep_account_id])
    expense_account = relationship("Account", foreign_keys=[expense_account_id])

    @property
    def life_label(self) -> str:
        if self.method == REDUCING_BALANCE:
            return f"{self.rate_pct}% reducing balance"
        if self.method == NO_DEPRECIATION:
            return "Not depreciated"
        years, months = divmod(self.useful_life_months, 12)
        parts = []
        if years:
            parts.append(f"{years} year{'s' if years != 1 else ''}")
        if months:
            parts.append(f"{months} month{'s' if months != 1 else ''}")
        return " ".join(parts) or "—"


class FixedAsset(Base):
    """One item on the asset register.

    ``cost`` and ``accumulated_depreciation`` are kobo and are the asset's own
    record of itself; the ledger holds the same figures in total. The two are
    reconciled by the asset schedule, which reads the ledger, not this table.
    """

    __tablename__ = "fixed_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("asset_categories.id"), nullable=True, index=True
    )

    purchase_date: Mapped[date] = mapped_column(Date, index=True)
    # Depreciation starts the month an asset is put to work, which is not
    # always the month it was bought.
    in_service_date: Mapped[date] = mapped_column(Date, index=True)
    cost: Mapped[int] = mapped_column(Integer, default=0)              # kobo
    residual_value: Mapped[int] = mapped_column(Integer, default=0)    # kobo

    method: Mapped[str] = mapped_column(String(10), default=STRAIGHT_LINE)
    useful_life_months: Mapped[int] = mapped_column(Integer, default=48)
    rate_pct: Mapped[str] = mapped_column(String(10), default="25")

    accumulated_depreciation: Mapped[int] = mapped_column(Integer, default=0)  # kobo
    # The last period charged, as YYYYMM, so a run can never charge twice.
    last_depreciated_period: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(12), default=ASSET_ACTIVE, index=True)
    serial_no: Mapped[str] = mapped_column(String(80), default="")
    registration_no: Mapped[str] = mapped_column(String(60), default="")
    location: Mapped[str] = mapped_column(String(120), default="")
    custodian: Mapped[str] = mapped_column(String(120), default="")

    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    # Set when the asset was capitalised through this application rather than
    # brought in as an opening balance.
    acquisition_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

    disposal_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    disposal_proceeds: Mapped[int] = mapped_column(Integer, default=0)
    disposal_note: Mapped[str] = mapped_column(String(255), default="")
    disposal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

    notes: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    category = relationship("AssetCategory")
    supplier = relationship("Contact")
    bill = relationship("Bill")

    @property
    def net_book_value(self) -> int:
        return self.cost - self.accumulated_depreciation

    @property
    def depreciable_amount(self) -> int:
        return max(0, self.cost - self.residual_value)

    @property
    def is_active(self) -> bool:
        return self.status == ASSET_ACTIVE

    @property
    def is_fully_depreciated(self) -> bool:
        return self.net_book_value <= self.residual_value

    @property
    def method_label(self) -> str:
        return dict(DEPRECIATION_METHODS).get(self.method, self.method).split("—")[0].strip()


class DepreciationRun(Base):
    """One month's depreciation across the whole register."""

    __tablename__ = "depreciation_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    period: Mapped[int] = mapped_column(Integer, index=True)   # YYYYMM
    date: Mapped[date] = mapped_column(Date, index=True)       # last day of the month
    status: Mapped[str] = mapped_column(String(12), default=DRAFT, index=True)
    total: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")

    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lines = relationship(
        "DepreciationLine", back_populates="run", cascade="all, delete-orphan",
        order_by="DepreciationLine.id",
    )

    @property
    def period_label(self) -> str:
        y, m = divmod(self.period, 100)
        return f"{MONTH_NAMES[m - 1]} {y}"

    @property
    def asset_count(self) -> int:
        return len(self.lines)


class DepreciationLine(Base):
    __tablename__ = "depreciation_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("depreciation_runs.id"), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("fixed_assets.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    nbv_before: Mapped[int] = mapped_column(Integer, default=0)
    nbv_after: Mapped[int] = mapped_column(Integer, default=0)
    months_charged: Mapped[int] = mapped_column(Integer, default=1)
    memo: Mapped[str] = mapped_column(String(255), default="")

    run = relationship("DepreciationRun", back_populates="lines")
    asset = relationship("FixedAsset")


# --------------------------------------------------------------------------
# Recurring invoices and bills
# --------------------------------------------------------------------------

WEEKLY_R, FORTNIGHTLY_R, MONTHLY_R, QUARTERLY_R, HALF_YEARLY_R, YEARLY_R = (
    "WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "HALF_YEARLY", "YEARLY"
)

RECURRENCE_LABELS = {
    WEEKLY_R: "Every week",
    FORTNIGHTLY_R: "Every two weeks",
    MONTHLY_R: "Every month",
    QUARTERLY_R: "Every three months",
    HALF_YEARLY_R: "Every six months",
    YEARLY_R: "Every year",
}

RECUR_ACTIVE, RECUR_PAUSED, RECUR_FINISHED = "ACTIVE", "PAUSED", "FINISHED"


class RecurringTemplate(Base):
    """A document that repeats — a monthly rent invoice, a quarterly retainer.

    The template is not a document. Nothing is in the books until it generates
    one, and by default the generated document is a draft somebody has to look
    at before it is posted.
    """

    __tablename__ = "recurring_templates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    doc_type: Mapped[str] = mapped_column(String(20), default="INVOICE", index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)

    frequency: Mapped[str] = mapped_column(String(15), default=MONTHLY_R)
    # The day of the month a monthly template lands on. Kept separately so a
    # template set to the 31st still lands on the 30th in April and comes back
    # to the 31st in May, rather than walking backwards down the calendar.
    anchor_day: Mapped[int] = mapped_column(Integer, default=1)
    start_date: Mapped[date] = mapped_column(Date)
    next_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    max_occurrences: Mapped[int] = mapped_column(Integer, default=0)   # 0 = no limit
    occurrences: Mapped[int] = mapped_column(Integer, default=0)

    # Post the generated document straight away instead of leaving it a draft
    auto_post: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default=RECUR_ACTIVE, index=True)

    payment_terms_days: Mapped[int] = mapped_column(Integer, default=30)
    reference: Mapped[str] = mapped_column(String(60), default="")
    memo: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[str] = mapped_column(Text, default="")
    wht_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)

    last_generated: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    contact = relationship("Contact")
    wht_code = relationship("TaxCode")
    lines = relationship(
        "RecurringLine", back_populates="template", cascade="all, delete-orphan",
        order_by="RecurringLine.line_no",
    )
    generated = relationship(
        "RecurringDocument", back_populates="template", cascade="all, delete-orphan",
        order_by="RecurringDocument.id.desc()",
    )

    @property
    def frequency_label(self) -> str:
        return RECURRENCE_LABELS.get(self.frequency, self.frequency)

    @property
    def is_sales(self) -> bool:
        return self.doc_type in ("INVOICE", "QUOTE", "CREDIT_NOTE")

    @property
    def remaining(self) -> int | None:
        if not self.max_occurrences:
            return None
        return max(0, self.max_occurrences - self.occurrences)

    @property
    def estimated_total(self) -> int:
        """What one run is worth, before tax — enough for a listing."""
        from .services.documents import line_net

        return sum(line_net(l.qty, l.unit_price, l.discount_pct) for l in self.lines)


class RecurringLine(Base):
    __tablename__ = "recurring_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("recurring_templates.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"), nullable=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    qty: Mapped[int] = mapped_column(Integer, default=1000)        # milli-units
    unit_price: Mapped[int] = mapped_column(Integer, default=0)    # kobo
    discount_pct: Mapped[str] = mapped_column(String(10), default="0")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    tax_code_id: Mapped[int | None] = mapped_column(ForeignKey("tax_codes.id"), nullable=True)

    template = relationship("RecurringTemplate", back_populates="lines")
    item = relationship("Item")
    account = relationship("Account")
    tax_code = relationship("TaxCode")


class RecurringDocument(Base):
    """The link from a template to something it produced."""

    __tablename__ = "recurring_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("recurring_templates.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(20), default="INVOICE")
    doc_id: Mapped[int] = mapped_column(Integer, index=True)
    doc_number: Mapped[str] = mapped_column(String(30), default="")
    date: Mapped[date] = mapped_column(Date)
    total: Mapped[int] = mapped_column(Integer, default=0)
    was_posted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    template = relationship("RecurringTemplate", back_populates="generated")


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------


class Budget(Base):
    """A year's plan for income and expenditure, month by month.

    Budgets never touch the ledger. They exist to be compared with it.
    """

    __tablename__ = "budgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    lines = relationship(
        "BudgetLine", back_populates="budget", cascade="all, delete-orphan",
    )

    __table_args__ = (UniqueConstraint("name", "start_date"),)

    @property
    def total(self) -> int:
        return sum(l.amount for l in self.lines)


class BudgetLine(Base):
    """One account, one month, one figure."""

    __tablename__ = "budget_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    period: Mapped[int] = mapped_column(Integer, index=True)   # YYYYMM
    amount: Mapped[int] = mapped_column(Integer, default=0)    # kobo, natural sign

    budget = relationship("Budget", back_populates="lines")
    account = relationship("Account")

    __table_args__ = (UniqueConstraint("budget_id", "account_id", "period"),)


# --------------------------------------------------------------------------
# Inventory depth: locations, batches, serial numbers and FIFO layers
# --------------------------------------------------------------------------

AVERAGE_COST, FIFO_COST = "AVERAGE", "FIFO"

COSTING_METHODS = [
    (AVERAGE_COST, "Weighted average — one cost for everything on hand"),
    (FIFO_COST, "First in, first out — oldest stock is sold first"),
]

SERIAL_IN_STOCK, SERIAL_SOLD, SERIAL_SCRAPPED = "IN_STOCK", "SOLD", "SCRAPPED"


class Location(Base):
    """A place stock is kept — a warehouse, a yard, a shop, a van."""

    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    address: Mapped[str] = mapped_column(Text, default="")
    manager: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(60), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Batch(Base):
    """A lot of one item received together, with its own expiry date.

    Cement, paint and chemicals go off; roofing sheets from one rolling have
    one shade. Either way the business needs to know which lot a bag came from.
    """

    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(60), index=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    manufactured_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_on: Mapped[date] = mapped_column(Date, index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")

    item = relationship("Item")
    supplier = relationship("Contact")

    __table_args__ = (UniqueConstraint("item_id", "batch_no"),)

    @property
    def days_to_expiry(self) -> int | None:
        if self.expiry_date is None:
            return None
        return (self.expiry_date - date.today()).days

    @property
    def is_expired(self) -> bool:
        return self.expiry_date is not None and self.expiry_date < date.today()


class StockLevel(Base):
    """How much of one item is in one place, in one batch.

    Quantity only. Value is held by the item (weighted average) or by the cost
    layers (FIFO), so there is exactly one place value can be wrong.
    """

    __tablename__ = "stock_levels"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True,
                                                 index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)      # milli-units

    item = relationship("Item")
    location = relationship("Location")
    batch = relationship("Batch")

    __table_args__ = (UniqueConstraint("item_id", "location_id", "batch_id"),)


class StockLayer(Base):
    """One receipt of stock at one cost, for first-in-first-out valuation.

    Average-cost items never create layers. A FIFO item is issued by eating
    layers oldest first, so the cost of a sale is what those particular goods
    actually cost.
    """

    __tablename__ = "stock_layers"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    qty_in: Mapped[int] = mapped_column(Integer, default=0)        # milli-units
    qty_left: Mapped[int] = mapped_column(Integer, default=0)      # milli-units
    unit_cost: Mapped[int] = mapped_column(Integer, default=0)     # kobo per whole unit
    value_left: Mapped[int] = mapped_column(Integer, default=0)    # kobo
    doc_type: Mapped[str] = mapped_column(String(20), default="")
    doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    doc_number: Mapped[str] = mapped_column(String(30), default="")

    item = relationship("Item")
    location = relationship("Location")
    batch = relationship("Batch")

    __table_args__ = (Index("ix_layer_item_open", "item_id", "date", "id"),)


class SerialNumber(Base):
    """One physical unit, tracked from receipt to sale.

    Generators, pumps and machines are sold with a warranty against a serial
    number; when the customer comes back, this is what answers the question.
    """

    __tablename__ = "serial_numbers"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    serial: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(12), default=SERIAL_IN_STOCK, index=True)
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    cost: Mapped[int] = mapped_column(Integer, default=0)

    received_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    received_doc: Mapped[str] = mapped_column(String(30), default="")
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)

    sold_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    sold_doc: Mapped[str] = mapped_column(String(30), default="")
    sold_doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    warranty_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(String(255), default="")

    item = relationship("Item")
    location = relationship("Location")
    batch = relationship("Batch")
    supplier = relationship("Contact", foreign_keys=[supplier_id])
    customer = relationship("Contact", foreign_keys=[customer_id])

    # Deliberately not unique on (item, serial). A generator sold, taken in
    # part-exchange and sold again is the same physical unit but three separate
    # facts, and squashing them into one row would lose the first customer's
    # warranty. Only one row per serial may be IN_STOCK at a time, and
    # ``costing.receive_serials`` is what enforces that.
    __table_args__ = (Index("ix_serial_lookup", "item_id", "serial", "status"),)

    @property
    def in_warranty(self) -> bool:
        return self.warranty_until is not None and self.warranty_until >= date.today()


# --------------------------------------------------------------------------
# Landed cost
# --------------------------------------------------------------------------

BY_VALUE, BY_QUANTITY, BY_WEIGHT = "VALUE", "QUANTITY", "WEIGHT"

LANDED_BASES = [
    (BY_VALUE, "By value — the dearer goods carry more of the cost"),
    (BY_QUANTITY, "By quantity — every unit carries the same"),
    (BY_WEIGHT, "By weight — the heavier goods carry more"),
]


class LandedCost(Base):
    """Freight, duty and clearing charges moved into the cost of the goods.

    A container of cement is not worth what the supplier invoiced; it is worth
    that plus the shipping, the duty and the clearing agent. Until those are
    added to the stock, every sale from that container shows too much profit.
    """

    __tablename__ = "landed_costs"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    reference: Mapped[str] = mapped_column(String(60), default="")
    basis: Mapped[str] = mapped_column(String(10), default=BY_VALUE)
    status: Mapped[str] = mapped_column(String(12), default=DRAFT, index=True)
    note: Mapped[str] = mapped_column(Text, default="")

    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    charges = relationship(
        "LandedCostCharge", back_populates="landed_cost", cascade="all, delete-orphan",
        order_by="LandedCostCharge.id",
    )
    lines = relationship(
        "LandedCostLine", back_populates="landed_cost", cascade="all, delete-orphan",
        order_by="LandedCostLine.id",
    )

    @property
    def total_charges(self) -> int:
        return sum(c.amount for c in self.charges)

    @property
    def total_allocated(self) -> int:
        return sum(l.allocated for l in self.lines)

    @property
    def goods_value(self) -> int:
        return sum(l.value for l in self.lines)

    @property
    def basis_label(self) -> str:
        return dict(LANDED_BASES).get(self.basis, self.basis).split("—")[0].strip()


class LandedCostCharge(Base):
    """One charge being spread — the freight bill, the duty, the agent's fee."""

    __tablename__ = "landed_cost_charges"
    id: Mapped[int] = mapped_column(primary_key=True)
    landed_cost_id: Mapped[int] = mapped_column(ForeignKey("landed_costs.id"), index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    # Where the charge is sitting now — the expense account it was booked to.
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)

    landed_cost = relationship("LandedCost", back_populates="charges")
    account = relationship("Account")
    contact = relationship("Contact")


class LandedCostLine(Base):
    """One consignment line the charges are spread over."""

    __tablename__ = "landed_cost_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    landed_cost_id: Mapped[int] = mapped_column(ForeignKey("landed_costs.id"), index=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    bill_line_id: Mapped[int | None] = mapped_column(ForeignKey("bill_lines.id"), nullable=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    description: Mapped[str] = mapped_column(String(300), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)        # milli-units
    value: Mapped[int] = mapped_column(Integer, default=0)      # kobo, what was invoiced
    weight: Mapped[int] = mapped_column(Integer, default=0)     # grams
    allocated: Mapped[int] = mapped_column(Integer, default=0)  # kobo added to the cost

    landed_cost = relationship("LandedCost", back_populates="lines")
    item = relationship("Item")
    bill = relationship("Bill")

    @property
    def new_unit_cost(self) -> int:
        if not self.qty:
            return 0
        return round((self.value + self.allocated) * 1000 / self.qty)

    @property
    def uplift_pct(self) -> str:
        if not self.value:
            return "—"
        return f"{self.allocated * 100 / self.value:.1f}%"


# --------------------------------------------------------------------------
# Requisitions
# --------------------------------------------------------------------------

# The route a requisition takes. It only ever moves forward, or back to the
# person who raised it with a reason attached.
REQ_DRAFT = "DRAFT"                    # being written, nobody has seen it
REQ_WITH_MANAGER = "WITH_MANAGER"      # waiting for the line manager
REQ_WITH_DIRECTOR = "WITH_DIRECTOR"    # over the limit, waiting for a director
REQ_WITH_FINANCE = "WITH_FINANCE"      # approved, waiting for the money
REQ_PAID = "PAID"                      # money sent, waiting to be retired
REQ_RETIRED = "RETIRED"                # accounted for with receipts
REQ_REJECTED = "REJECTED"              # sent back, with a reason
REQ_CANCELLED = "CANCELLED"            # withdrawn by the person who raised it

REQUISITION_STATUSES = {
    REQ_DRAFT: "Draft",
    REQ_WITH_MANAGER: "With the manager",
    REQ_WITH_DIRECTOR: "With a director",
    REQ_WITH_FINANCE: "With finance",
    REQ_PAID: "Paid — to be retired",
    REQ_RETIRED: "Retired",
    REQ_REJECTED: "Sent back",
    REQ_CANCELLED: "Withdrawn",
}

OPEN_REQUISITIONS = (REQ_WITH_MANAGER, REQ_WITH_DIRECTOR, REQ_WITH_FINANCE)


class Requisition(Base):
    """A request for money, and the trail of who agreed to it.

    Raised by a member of staff, approved by their named manager, approved
    again by a director if it is over the limit, then paid by finance into
    the person's own bank account. A rejection always carries a reason and
    goes straight back to the person who raised it.
    """

    __tablename__ = "requisitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    needed_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    purpose: Mapped[str] = mapped_column(Text, default="")
    department: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default=REQ_DRAFT, index=True)

    raised_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # The manager as it stood when the requisition was raised. Kept on the
    # record so a later change of line manager does not rewrite history.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    total: Mapped[int] = mapped_column(Integer, default=0)          # kobo

    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manager_approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    manager_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    manager_note: Mapped[str] = mapped_column(Text, default="")

    director_approved_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    director_approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    director_note: Mapped[str] = mapped_column(Text, default="")

    rejected_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_stage: Mapped[str] = mapped_column(String(16), default="")
    rejection_reason: Mapped[str] = mapped_column(Text, default="")

    # --- Payment ----------------------------------------------------------
    paid_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    paid_amount: Mapped[int] = mapped_column(Integer, default=0)
    bank_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_accounts.id"), nullable=True
    )
    payment_reference: Mapped[str] = mapped_column(String(60), default="")
    # The account details the money was actually sent to, copied at the time.
    paid_to_bank: Mapped[str] = mapped_column(String(80), default="")
    paid_to_account_no: Mapped[str] = mapped_column(String(30), default="")
    paid_to_account_name: Mapped[str] = mapped_column(String(120), default="")
    payment_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

    # --- Retirement -------------------------------------------------------
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_spent: Mapped[int] = mapped_column(Integer, default=0)
    retirement_note: Mapped[str] = mapped_column(Text, default="")
    retirement_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    raised_by = relationship("User", foreign_keys=[raised_by_id])
    manager = relationship("User", foreign_keys=[manager_id])
    manager_approved_by = relationship("User", foreign_keys=[manager_approved_by_id])
    director_approved_by = relationship("User", foreign_keys=[director_approved_by_id])
    rejected_by = relationship("User", foreign_keys=[rejected_by_id])
    paid_by = relationship("User", foreign_keys=[paid_by_id])
    bank_account = relationship("BankAccount")
    lines = relationship(
        "RequisitionLine", back_populates="requisition", cascade="all, delete-orphan",
        order_by="RequisitionLine.line_no",
    )
    events = relationship(
        "RequisitionEvent", back_populates="requisition", cascade="all, delete-orphan",
        order_by="RequisitionEvent.id",
    )

    @property
    def status_label(self) -> str:
        return REQUISITION_STATUSES.get(self.status, self.status)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_REQUISITIONS

    @property
    def is_editable(self) -> bool:
        """Only before anyone has approved it, or after it has come back."""
        return self.status in (REQ_DRAFT, REQ_REJECTED)

    @property
    def balance_to_return(self) -> int:
        """What is left over after retirement. Negative means the company owes."""
        return self.paid_amount - self.amount_spent

    @property
    def awaiting(self) -> str:
        """Who the ball is with, in plain words."""
        if self.status == REQ_WITH_MANAGER:
            return self.manager.display_name if self.manager else "a manager"
        if self.status == REQ_WITH_DIRECTOR:
            return "a director"
        if self.status == REQ_WITH_FINANCE:
            return "finance"
        if self.status == REQ_PAID:
            return f"{self.raised_by.display_name}, to retire"
        return ""


class RequisitionLine(Base):
    __tablename__ = "requisition_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    requisition_id: Mapped[int] = mapped_column(ForeignKey("requisitions.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str] = mapped_column(String(300), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    qty: Mapped[int] = mapped_column(Integer, default=1000)         # milli-units
    unit_price: Mapped[int] = mapped_column(Integer, default=0)     # kobo
    amount: Mapped[int] = mapped_column(Integer, default=0)         # kobo
    # What the staff says was actually spent on this line, at retirement
    spent: Mapped[int] = mapped_column(Integer, default=0)

    requisition = relationship("Requisition", back_populates="lines")
    account = relationship("Account")
    vendor = relationship("Contact")


class RequisitionEvent(Base):
    """Every step a requisition took, and who took it.

    This is what makes the workflow answerable afterwards: not just that a
    requisition was approved, but by whom, when, and what they said.
    """

    __tablename__ = "requisition_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    requisition_id: Mapped[int] = mapped_column(ForeignKey("requisitions.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    action: Mapped[str] = mapped_column(String(20))
    by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    by_name: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[int] = mapped_column(Integer, default=0)

    requisition = relationship("Requisition", back_populates="events")
    by = relationship("User")


# --------------------------------------------------------------------------
# The till
# --------------------------------------------------------------------------

TILL_OPEN, TILL_CLOSED = "OPEN", "CLOSED"

#: How a customer paid. Cash is the one that has to balance against a drawer at
#: the end of the day; the others are checked against the bank instead.
TENDER_CASH = "CASH"
TENDER_CARD = "CARD"
TENDER_TRANSFER = "TRANSFER"
TENDER_KINDS = [
    (TENDER_CASH, "Cash"),
    (TENDER_CARD, "Card"),
    (TENDER_TRANSFER, "Transfer"),
]


class TillSession(Base):
    """One person on one till, from opening the drawer to counting it.

    A shop's takings are only trustworthy if somebody counted the drawer and
    the count was compared with what the till says should be there. So a sale
    can only be rung up inside an open session, and closing a session means
    entering what was actually counted — including when that is less than
    expected, which is the number the whole thing exists to surface.
    """

    __tablename__ = "till_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(60), default="Till 1")
    location_id: Mapped[int | None] = mapped_column(
        ForeignKey("locations.id"), nullable=True)
    #: The cash account the drawer belongs to.
    cash_account_id: Mapped[int] = mapped_column(ForeignKey("bank_accounts.id"))
    status: Mapped[str] = mapped_column(String(10), default=TILL_OPEN, index=True)

    opened_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    opened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    #: What was in the drawer before trading started.
    opening_float: Mapped[int] = mapped_column(Integer, default=0)

    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    #: What was counted out of the drawer at the end.
    counted_cash: Mapped[int] = mapped_column(Integer, default=0)
    #: What the till says should have been there.
    expected_cash: Mapped[int] = mapped_column(Integer, default=0)
    #: counted less expected. Negative means the drawer is short.
    difference: Mapped[int] = mapped_column(Integer, default=0)
    #: How much of the takings was sent to the bank at close.
    banked: Mapped[int] = mapped_column(Integer, default=0)
    bank_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("bank_accounts.id"), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    #: The entry that writes the over or short into the ledger.
    journal_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True)
    #: The entry that moves the takings to the bank.
    banking_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=True)

    location = relationship("Location")
    cash_account = relationship("BankAccount", foreign_keys=[cash_account_id])
    bank_account = relationship("BankAccount", foreign_keys=[bank_account_id])
    opened_by = relationship("User", foreign_keys=[opened_by_id])
    closed_by = relationship("User", foreign_keys=[closed_by_id])
    tenders = relationship("TillTender", back_populates="session",
                           cascade="all, delete-orphan")

    @property
    def is_open(self) -> bool:
        return self.status == TILL_OPEN

    @property
    def is_short(self) -> bool:
        return self.difference < 0


class TillTender(Base):
    """One way one sale was paid for.

    A sale can be part cash and part card, which is why this is a table and not
    a column. Each row is also a real payment in the ledger — the money went
    somewhere and the ledger says where.
    """

    __tablename__ = "till_tenders"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("till_sessions.id"), index=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"),
                                                   nullable=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"),
                                                   nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default=TENDER_CASH)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    #: For cash: what the customer handed over, before change.
    tendered: Mapped[int] = mapped_column(Integer, default=0)
    change: Mapped[int] = mapped_column(Integer, default=0)
    reference: Mapped[str] = mapped_column(String(60), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    session = relationship("TillSession", back_populates="tenders")
    invoice = relationship("Invoice")
    payment = relationship("Payment")


# --------------------------------------------------------------------------
# Electronic invoicing
# --------------------------------------------------------------------------

#: Nothing has been attempted yet — the document is waiting its turn.
EI_PENDING = "PENDING"
#: Handed to the transmitter; we are waiting to hear back.
EI_SENDING = "SENDING"
#: Accepted by the Revenue Service. Carries an IRN and may be issued.
EI_CLEARED = "CLEARED"
#: Refused on its contents. Retrying the same bytes will fail the same way,
#: so a person has to change something before it can go again.
EI_REJECTED = "REJECTED"
#: Did not get through — no connection, a timeout, a five-hundred. The document
#: is fine; the journey was not. These retry on their own.
EI_FAILED = "FAILED"
#: Deliberately out of scope: a quotation, a pro-forma, a business below the
#: threshold that has not switched e-invoicing on.
EI_NOT_REQUIRED = "NOT_REQUIRED"

EI_SETTLED = (EI_CLEARED, EI_REJECTED, EI_NOT_REQUIRED)


class EInvoice(Base):
    """What the Revenue Service has been told about one invoice.

    Kept apart from ``Invoice`` on purpose. Clearance is a conversation with
    somebody else's computer: it fails, it retries, it comes back hours later,
    and it has a history worth keeping. None of that belongs in the row that
    the ledger, the ageing and the customer statement all read from.

    One row per invoice. The unique constraint is the thing that stops a
    double-tap on the submit button putting the same invoice through twice.
    """

    __tablename__ = "einvoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(15), default=EI_PENDING, index=True)

    #: The Invoice Reference Number the Revenue Service issues on clearance.
    #: Until this exists the invoice must not reach the customer.
    irn: Mapped[str] = mapped_column(String(80), default="", index=True)
    #: Cryptographic Stamp Identifier — proof the clearance is genuine.
    csid: Mapped[str] = mapped_column(String(400), default="")
    #: What the QR code on the printed invoice encodes.
    qr_payload: Mapped[str] = mapped_column(Text, default="")

    #: SHA-256 of the exact XML that was sent. If the invoice is edited after
    #: clearance this stops matching, and the clearance no longer describes the
    #: document in front of you.
    xml_sha256: Mapped[str] = mapped_column(String(64), default="")

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    #: When it is worth trying again. Backs off after each failure so a server
    #: that is down does not get hammered by every till in the country.
    retry_after: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    #: Which arrangement produced this: the built-in simulator, or a named
    #: provider. Recorded so that nobody mistakes a rehearsal for the real thing.
    channel: Mapped[str] = mapped_column(String(30), default="")
    response: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    invoice = relationship("Invoice")

    @property
    def is_cleared(self) -> bool:
        return self.status == EI_CLEARED and bool(self.irn)

    @property
    def may_retry(self) -> bool:
        return self.status in (EI_PENDING, EI_FAILED)

    @property
    def was_a_rehearsal(self) -> bool:
        """True when this clearance came from the simulator, not the Revenue
        Service. A rehearsal must never be presented to a customer or an
        auditor as compliance."""
        return self.channel == "simulator"
