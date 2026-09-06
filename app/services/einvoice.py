"""Electronic invoicing: settings, readiness, and getting a document cleared.

Three things live here, in the order a business meets them.

**Settings.** Whether e-invoicing is off, being rehearsed, or live, and the
credentials for the last of those. Per company, because two businesses in one
installation file separately, and in a file only the owner of the machine can
read, because it holds a client secret.

**Readiness.** The mandate needs about fifty-five fields across eight
categories, and most of them are not typed on the invoice — they are the
customer's TIN, the company's address, the tax code on a line. A business that
discovers this on 1 July 2027 has a bad week. So the readiness check names
every record that would fail, with the screen to go and fix it on, and can be
run today against books that are years old.

**Clearance.** Build the document, hand it to the transmitter, record what came
back. An invoice that has not been cleared has not been issued, so the failure
paths matter more than the happy one: a dropped connection must queue rather
than lose, and a rejection must say what a person has to change.

A word on honesty, because it is load-bearing. Until this has been tested
against the Revenue Service's own sandbox, the only mode that works end to end
is the rehearsal, and everything it produces is stamped as a rehearsal — in the
reference number, on the screen and on the printed invoice. Presenting a
rehearsal as compliance would expose a customer to a penalty on our word.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, companies as registry, fileguard
from ..models import (
    EI_CLEARED, EI_FAILED, EI_NOT_REQUIRED, EI_PENDING, EI_REJECTED, EI_SENDING,
    POSTED, PART_PAID, PAID,
    Company, Contact, EInvoice, Invoice, TaxCode,
)
from . import transmit, ubl

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

OFF, REHEARSAL, LIVE = "OFF", "REHEARSAL", "LIVE"

MODE_LABELS = {
    OFF: "Off",
    REHEARSAL: "Rehearsal — check my invoices without filing them",
    LIVE: "Live — file with the Revenue Service",
}

SETTINGS_FILE = "einvoice.json"


@dataclass
class Settings:
    mode: str = OFF
    #: Send automatically the moment an invoice is posted, rather than waiting
    #: for somebody to press a button. The mandate wants the reference before
    #: the invoice reaches the customer, so for most businesses this is right.
    auto_submit: bool = True
    #: Refuse to email or print an invoice that has not been cleared. Strictly
    #: correct once a business is in scope, and a good way to lock yourself out
    #: of your own sales before then, so it is off by default.
    block_uncleared: bool = False

    provider_name: str = ""
    submit_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    scope: str = ""
    business_id: str = ""
    irn_path: str = "data.irn"
    csid_path: str = "data.csid"
    qr_path: str = "data.qr"
    #: The Revenue Service will publish a Nigerian customisation identifier.
    #: Settable so a customer is not blocked on our release schedule.
    customization_id: str = ubl.CUSTOMIZATION_ID
    extra_headers: dict = field(default_factory=dict)

    @property
    def on(self) -> bool:
        return self.mode in (REHEARSAL, LIVE)

    @property
    def is_live(self) -> bool:
        return self.mode == LIVE

    @property
    def ready_to_go_live(self) -> bool:
        return bool(self.submit_url and self.client_id and self.client_secret)


def _settings_file(slug: str):
    return registry.company_dir(slug) / SETTINGS_FILE


def load(slug: str) -> Settings:
    try:
        data = json.loads(_settings_file(slug).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    known = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save(slug: str, settings: Settings) -> None:
    path = _settings_file(slug)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    # It holds a client secret. Whoever can read this can file invoices as
    # this business.
    fileguard.restrict_to_owner(path)


def transmitter_for(settings: Settings):
    """The thing that will actually carry the document."""
    if settings.mode == REHEARSAL:
        return transmit.Simulator()
    return transmit.HttpTransmitter(transmit.Endpoint(
        name=settings.provider_name or "the Revenue Service",
        submit_url=settings.submit_url,
        token_url=settings.token_url,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        scope=settings.scope,
        business_id=settings.business_id,
        irn_path=settings.irn_path,
        csid_path=settings.csid_path,
        qr_path=settings.qr_path,
        extra_headers=settings.extra_headers or {},
    ))


# --------------------------------------------------------------------------
# Readiness
# --------------------------------------------------------------------------


@dataclass
class Problem:
    """One thing that would stop a document being accepted."""

    where: str          # "Your company", "Customer: Dangote Ltd", "Invoice INV-0042"
    what: str           # what is wrong, in words a bookkeeper uses
    fix: str = ""       # the screen to go and put it right on
    blocking: bool = True

    def __str__(self) -> str:
        return f"{self.where}: {self.what}"


def check_company(company: Company | None) -> list[Problem]:
    problems: list[Problem] = []
    if company is None:
        return [Problem("Your company", "The company record is missing.", "/settings/company")]

    here = "Your company"
    fix = "/settings/company"
    if not (company.tin or "").strip():
        problems.append(Problem(
            here, "No Tax Identification Number. Every electronic invoice must "
            "carry the TIN of the business issuing it.", fix))
    if not (company.legal_name or company.name or "").strip():
        problems.append(Problem(here, "No registered name.", fix))
    if not (company.address or "").strip():
        problems.append(Problem(
            here, "No address. The mandate needs a postal address for both "
            "parties, not just a name.", fix))
    if not (company.city or "").strip():
        problems.append(Problem(here, "No city.", fix))
    if not (company.rc_number or "").strip():
        problems.append(Problem(
            here, "No RC number. Not always refused, but expected of a "
            "registered company and cheap to fill in now.", fix, blocking=False))
    if company.is_vat_registered and not (company.vat_reg_no or "").strip():
        problems.append(Problem(
            here, "Registered for VAT but no VAT registration number recorded.",
            fix, blocking=False))
    return problems


def check_customer(contact: Contact) -> list[Problem]:
    here = f"Customer: {contact.name}"
    fix = f"/contacts/{contact.id}"
    problems: list[Problem] = []
    if not (contact.tin or "").strip():
        problems.append(Problem(
            here, "No Tax Identification Number. A business customer must have "
            "one on the invoice; for a walk-in individual, record them as an "
            "individual instead.", fix,
            blocking=contact.contact_type != "INDIVIDUAL"))
    if not (contact.address or "").strip():
        problems.append(Problem(here, "No address.", fix))
    if not (contact.city or "").strip():
        problems.append(Problem(here, "No city.", fix, blocking=False))
    return problems


def check_invoice(db: Session, invoice: Invoice) -> list[Problem]:
    here = f"Invoice {invoice.number}"
    fix = f"/sales/invoices/{invoice.id}"
    problems: list[Problem] = []

    if invoice.contact is None:
        problems.append(Problem(here, "No customer on the invoice.", fix))
    else:
        problems.extend(check_customer(invoice.contact))

    if not invoice.lines:
        problems.append(Problem(here, "No lines on the invoice.", fix))

    for line in invoice.lines:
        if not (line.description or "").strip() and line.item is None:
            problems.append(Problem(
                here, f"Line {line.line_no} has no description. Every line "
                "needs one — a blank line is refused.", fix))
        if line.tax_code_id is None and line.vat_amount == 0:
            problems.append(Problem(
                here, f"Line {line.line_no} has no tax code. A line must say "
                "whether it is standard-rated, zero-rated or exempt — 'no tax "
                "code' is not an answer the platform accepts.", fix))

    # Arithmetic. If this is wrong the platform will find it, and it is far
    # better to find it here.
    expected = invoice.subtotal - invoice.discount_total + invoice.vat_total
    if invoice.total != expected:
        problems.append(Problem(
            here, f"The totals do not add up ({expected} expected, "
            f"{invoice.total} recorded). Re-open and re-save the invoice.", fix))
    return problems


@dataclass
class Readiness:
    """The whole picture, for the settings screen."""

    company: list[Problem] = field(default_factory=list)
    customers: list[Problem] = field(default_factory=list)
    invoices: list[Problem] = field(default_factory=list)
    customers_checked: int = 0
    invoices_checked: int = 0

    @property
    def all(self) -> list[Problem]:
        return self.company + self.customers + self.invoices

    @property
    def blocking(self) -> list[Problem]:
        return [p for p in self.all if p.blocking]

    @property
    def advisory(self) -> list[Problem]:
        return [p for p in self.all if not p.blocking]

    @property
    def ready(self) -> bool:
        return not self.blocking


def readiness(db: Session, sample: int = 25) -> Readiness:
    """What would stop this business filing, today.

    Checks the company, every active customer, and the most recent posted
    invoices. Deliberately run against real records rather than a checklist,
    because "have you got your customers' TINs" is answered honestly only by
    counting the ones that are missing.
    """
    report = Readiness()
    report.company = check_company(db.get(Company, 1))

    customers = list(db.scalars(
        select(Contact).where(Contact.is_customer.is_(True), Contact.is_active.is_(True))
    ))
    report.customers_checked = len(customers)
    for contact in customers:
        report.customers.extend(check_customer(contact))

    invoices = list(db.scalars(
        select(Invoice)
        .where(Invoice.doc_type == "INVOICE",
               Invoice.status.in_((POSTED, PART_PAID, PAID)))
        .order_by(Invoice.id.desc())
        .limit(sample)
    ))
    report.invoices_checked = len(invoices)
    # One missing customer TIN is one thing to go and fix. It should be listed
    # once — not once per invoice that customer has ever had, and not again
    # under "invoices" when it has already been said under "customers". A
    # readiness report three hundred lines long because of four bad records is
    # a report nobody reads.
    seen = {str(p) for p in report.company + report.customers}
    for invoice in invoices:
        for problem in check_invoice(db, invoice):
            key = str(problem)
            if key not in seen:
                seen.add(key)
                report.invoices.append(problem)
    return report


# --------------------------------------------------------------------------
# Turning our invoice into their document
# --------------------------------------------------------------------------


def _category_for(code: TaxCode | None) -> tuple[str, str]:
    """The UBL tax category and rate for one of our tax codes."""
    if code is None:
        return ubl.OUT_OF_SCOPE, "0"
    if code.is_exempt:
        return ubl.EXEMPT, "0"
    if code.is_zero_rated:
        return ubl.ZERO_RATED, "0"
    try:
        rate = float(str(code.rate or "0"))
    except ValueError:
        rate = 0.0
    if rate <= 0:
        return ubl.ZERO_RATED, "0"
    return ubl.STANDARD_RATED, str(code.rate)


def _supplier(company: Company) -> ubl.Party:
    return ubl.Party(
        name=company.name, legal_name=company.legal_name or company.name,
        tin=company.tin, rc_number=company.rc_number, vat_reg_no=company.vat_reg_no,
        address=company.address, city=company.city, state=company.state,
        country=company.country_code or "NG",
        email=company.email, phone=company.phone,
    )


def _customer(contact: Contact | None) -> ubl.Party:
    if contact is None:
        return ubl.Party(name="Unknown customer")
    return ubl.Party(
        name=contact.name, legal_name=contact.name, tin=contact.tin,
        rc_number=contact.rc_number, address=contact.address, city=contact.city,
        state=contact.state, email=contact.email, phone=contact.phone,
        contact_person=contact.contact_person,
    )


def document_for(db: Session, invoice: Invoice) -> ubl.Document:
    """The UBL document this invoice becomes."""
    company = db.get(Company, 1)
    settings = load(_slug_of(db))

    lines: list[ubl.Line] = []
    for line in invoice.lines:
        category, rate = _category_for(line.tax_code)
        lines.append(ubl.Line(
            line_no=line.line_no,
            description=(line.description or "").strip()
                        or (line.item.name if line.item else "Item"),
            qty_milli=line.qty,
            unit_price_kobo=line.unit_price,
            net_kobo=line.net,
            vat_kobo=line.vat_amount,
            tax_category=category,
            tax_rate=rate,
            item_code=(line.item.code if line.item else ""),
        ))

    credit = invoice.doc_type == "CREDIT_NOTE"
    credited = ""
    if credit and invoice.credit_of_id:
        original = db.get(Invoice, invoice.credit_of_id)
        credited = original.number if original else ""

    return ubl.Document(
        number=invoice.number,
        issue_date=invoice.date,
        due_date=invoice.due_date,
        supplier=_supplier(company),
        customer=_customer(invoice.contact),
        lines=lines,
        currency=(company.currency_code or "NGN"),
        is_credit_note=credit,
        credit_of=credited,
        order_reference=invoice.po_number,
        buyer_reference=invoice.reference,
        note=(invoice.memo or "").strip(),
        payment_terms=(invoice.terms or company.invoice_terms or "").strip(),
        discount_total_kobo=invoice.discount_total,
        customization_id=settings.customization_id or ubl.CUSTOMIZATION_ID,
    )


def xml_for(db: Session, invoice: Invoice) -> bytes:
    return ubl.build(document_for(db, invoice))


def _slug_of(db: Session) -> str:
    from .. import db as dbmod

    return dbmod.current_slug() or registry.DEFAULT_SLUG


# --------------------------------------------------------------------------
# Clearance
# --------------------------------------------------------------------------

#: How long to wait before trying a failed submission again. Grows, so a
#: platform that is down for an afternoon is not hammered by every installation
#: in the country at once.
BACKOFF_MINUTES = (1, 5, 15, 60, 180, 360)


def record_for(db: Session, invoice: Invoice) -> EInvoice:
    """The clearance record for an invoice, made if it does not exist yet."""
    existing = db.scalar(select(EInvoice).where(EInvoice.invoice_id == invoice.id))
    if existing is not None:
        return existing
    record = EInvoice(invoice_id=invoice.id, status=EI_PENDING)
    db.add(record)
    db.flush()
    return record


def enqueue(db: Session, invoice: Invoice) -> EInvoice | None:
    """Note that this invoice will need clearing, without sending anything.

    Called from posting, which happens in bulk imports, in recurring runs and
    at a point-of-sale till. None of those may be made to wait on a network
    round trip, and none of them should fail because somebody else's server is
    down — so this only ever writes a row.
    """
    try:
        settings = load(_slug_of(db))
    except Exception:                                    # noqa: BLE001
        return None
    if not needs_clearance(invoice, settings):
        return None
    record = record_for(db, invoice)
    if record.status in (EI_NOT_REQUIRED,):
        record.status = EI_PENDING
    return record


def needs_clearance(invoice: Invoice, settings: Settings) -> bool:
    """Whether this document is one the mandate covers.

    Quotations are not invoices. Drafts have not been issued. A voided invoice
    is not a document anybody should be filing.
    """
    if not settings.on:
        return False
    if invoice.doc_type not in ("INVOICE", "CREDIT_NOTE"):
        return False
    return invoice.status in (POSTED, PART_PAID, PAID)


def submit(db: Session, invoice: Invoice, *, force: bool = False) -> EInvoice:
    """Send one invoice for clearance and record what happened.

    Never raises for an ordinary failure — a bookkeeper pressing a button
    should get a message, not a stack trace, and the queue should hold what did
    not get through.
    """
    slug = _slug_of(db)
    settings = load(slug)
    record = record_for(db, invoice)

    if not settings.on:
        record.status = EI_NOT_REQUIRED
        record.last_error = "E-invoicing is switched off for this company."
        return record

    if not needs_clearance(invoice, settings):
        record.status = EI_NOT_REQUIRED
        record.last_error = (
            "Only posted invoices and credit notes are filed. Quotations and "
            "drafts are not."
        )
        return record

    if record.status == EI_CLEARED and not force:
        return record

    # Refuse before sending anything, so the person is told what to fix rather
    # than told that somebody else's computer said no.
    faults = [p for p in check_invoice(db, invoice) if p.blocking]
    faults += [p for p in check_company(db.get(Company, 1)) if p.blocking]
    if faults:
        record.status = EI_REJECTED
        record.last_error = " ".join(f"{p.what}" for p in faults[:6])
        record.attempts += 1
        return record

    xml = xml_for(db, invoice)
    record.xml_sha256 = ubl.fingerprint(xml)
    record.status = EI_SENDING
    record.submitted_at = clock.now()
    record.attempts += 1

    try:
        result = transmitter_for(settings).submit(xml, invoice.number)
    except transmit.TransmitError as misconfigured:
        record.status = EI_FAILED
        record.last_error = str(misconfigured)
        record.retry_after = None      # a person has to fix this, not the clock
        return record
    except Exception as unexpected:                     # noqa: BLE001
        # A transmitter is somebody else's code talking to somebody else's
        # server. It must not be able to take the till down.
        record.status = EI_FAILED
        record.last_error = f"Unexpected problem sending: {unexpected}"
        record.retry_after = _next_try(record.attempts)
        return record

    record.channel = result.channel
    record.response = (result.raw or "")[:20000]

    if result.ok:
        record.status = EI_CLEARED
        record.irn = result.irn
        record.csid = result.csid
        record.qr_payload = result.qr_payload or result.irn
        record.cleared_at = clock.now()
        record.last_error = ""
        record.retry_after = None
    elif result.permanent:
        record.status = EI_REJECTED
        record.last_error = result.error
        record.retry_after = None
    else:
        record.status = EI_FAILED
        record.last_error = result.error
        record.retry_after = _next_try(record.attempts)
    return record


def _next_try(attempts: int):
    minutes = BACKOFF_MINUTES[min(max(attempts, 1), len(BACKOFF_MINUTES)) - 1]
    return clock.now() + timedelta(minutes=minutes)


def outbox(db: Session, limit: int = 50) -> list[EInvoice]:
    """Everything waiting to go, oldest first."""
    return list(db.scalars(
        select(EInvoice)
        .where(EInvoice.status.in_((EI_PENDING, EI_FAILED)))
        .order_by(EInvoice.id)
        .limit(limit)
    ))


def send_outbox(db: Session, limit: int = 25) -> tuple[int, int]:
    """Try everything that is due. Returns (cleared, still waiting).

    Safe to call repeatedly and safe to call when the connection is down: an
    invoice that cannot be sent stays in the queue with a later retry time.
    """
    now = clock.now()
    cleared = waiting = 0
    for record in outbox(db, limit=limit):
        if record.retry_after and record.retry_after > now:
            waiting += 1
            continue
        invoice = db.get(Invoice, record.invoice_id)
        if invoice is None:
            continue
        submit(db, invoice)
        if record.status == EI_CLEARED:
            cleared += 1
        else:
            waiting += 1
    return cleared, waiting


def status_of(db: Session, invoice: Invoice) -> EInvoice | None:
    return db.scalar(select(EInvoice).where(EInvoice.invoice_id == invoice.id))


def may_be_issued(db: Session, invoice: Invoice) -> tuple[bool, str]:
    """Whether this invoice may go to the customer yet.

    Under the mandate the reference number must be obtained *before* the
    invoice reaches the buyer. A business that has turned that requirement on
    gets it enforced here rather than remembered.
    """
    settings = load(_slug_of(db))
    if not settings.on or not settings.block_uncleared:
        return True, ""
    if not needs_clearance(invoice, settings):
        return True, ""
    record = status_of(db, invoice)
    if record is not None and record.is_cleared:
        return True, ""
    return False, (
        "This invoice has not been cleared yet, and this company is set to "
        "obtain the reference number before an invoice goes out. Clear it "
        "first, or change the setting on Settings, E-invoicing."
    )
