"""Turning an invoice into the document the Revenue Service will accept.

Nigeria's e-invoicing mandate does not ask for a PDF. It asks for a structured
XML document in the **UBL 2.1 / Peppol BIS Billing 3.0** shape, validated by the
Revenue Service before the invoice reaches the customer. A neat PDF with a logo
on it satisfies nothing.

This module does one job: given an invoice from our own books, produce that XML.
It talks to no network, holds no credentials and makes no decision about whether
to send anything. That separation is deliberate — the document format is stable
and testable, while the transmission arrangements are not yet settled, and the
part that can be got right today should not be entangled with the part that
cannot.

Two conventions matter and are easy to get wrong:

* **Money.** Our books hold kobo as whole numbers. UBL wants a decimal string
  with a full stop, two places, no thousands separators and no symbol —
  ``1234567`` becomes ``12345.67``. The company's own display preferences (a
  business may print ``12.345,67``) must never reach this file.
* **Quantities.** Ours are milli-units, so ``2500`` is 2.5. Three decimal
  places, same rules.

Everything is built through :func:`_el` so the output is deterministic: the same
invoice produces byte-identical XML every time. That matters more than it looks.
An invoice is signed and cleared once; if the bytes moved between the signing
and the sending, the signature would be over a document nobody has.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# Namespaces
# --------------------------------------------------------------------------

INVOICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CREDIT_NS = "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

#: What flavour of BIS Billing this document claims to be. The Nigerian profile
#: identifier is expected to be published by the Revenue Service; until it is,
#: this is the EN 16931 base that the Nigerian rules are built on, and it is
#: settable so that a customer is not waiting on a new release of this software
#: the week the real value appears.
CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"

#: UNCL1001 document type codes.
COMMERCIAL_INVOICE = "380"
CREDIT_NOTE = "381"

#: UNCL5305 tax category codes. The five that arise in Nigerian trade.
STANDARD_RATED = "S"
ZERO_RATED = "Z"
EXEMPT = "E"
OUT_OF_SCOPE = "O"
REVERSE_CHARGE = "AE"

VAT_SCHEME = "VAT"
COUNTRY = "NG"


# --------------------------------------------------------------------------
# Number formatting
# --------------------------------------------------------------------------


def amount(kobo: int) -> str:
    """Kobo as a UBL decimal string. ``-1234567`` -> ``-12345.67``."""
    kobo = int(kobo)
    sign = "-" if kobo < 0 else ""
    whole, part = divmod(abs(kobo), 100)
    return f"{sign}{whole}.{part:02d}"


def quantity(milli: int) -> str:
    """Milli-units as a UBL decimal string. ``2500`` -> ``2.500``."""
    milli = int(milli)
    sign = "-" if milli < 0 else ""
    whole, part = divmod(abs(milli), 1000)
    return f"{sign}{whole}.{part:03d}"


def percent(rate: str | None) -> str:
    """A stored rate string as a UBL percentage. ``'7.5'`` -> ``7.50``."""
    try:
        from decimal import Decimal

        return f"{Decimal(str(rate or '0').strip() or '0'):.2f}"
    except Exception:
        return "0.00"


def _iso(d: date | None) -> str:
    return d.isoformat() if d else ""


def _clean(text: str | None, limit: int = 0) -> str:
    """Collapse whitespace and strip control characters.

    A description pasted out of a spreadsheet can carry tabs, newlines and the
    occasional stray control byte. XML rejects most control characters outright,
    and a document rejected by the Revenue Service for an invisible character is
    a maddening thing to debug at the counter.
    """
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit] if limit and len(s) > limit else s


# --------------------------------------------------------------------------
# Element building
# --------------------------------------------------------------------------


def _el(parent, tag: str, text: str | None = None, **attrs):
    """Append one namespaced element. ``cbc:ID`` and ``cac:Party`` by prefix."""
    prefix, _, local = tag.partition(":")
    ns = {"cbc": CBC, "cac": CAC}.get(prefix)
    node = ET.SubElement(parent, f"{{{ns}}}{local}" if ns else tag,
                         {k.replace("_", ""): str(v) for k, v in attrs.items()})
    if text is not None:
        node.text = str(text)
    return node


def _money(parent, tag: str, kobo: int, currency: str):
    return _el(parent, tag, amount(kobo), currencyID=currency)


# --------------------------------------------------------------------------
# What the caller gives us
# --------------------------------------------------------------------------


@dataclass
class Party:
    """One side of the transaction, in the shape UBL wants it."""

    name: str = ""
    legal_name: str = ""
    tin: str = ""
    rc_number: str = ""
    vat_reg_no: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    country: str = COUNTRY
    email: str = ""
    phone: str = ""
    contact_person: str = ""

    @property
    def registered_as(self) -> str:
        return _clean(self.legal_name or self.name)


@dataclass
class Line:
    """One line of the document."""

    line_no: int
    description: str
    qty_milli: int
    unit_price_kobo: int
    net_kobo: int
    vat_kobo: int
    tax_category: str = STANDARD_RATED
    tax_rate: str = "0"
    item_code: str = ""
    unit_code: str = "EA"
    exemption_reason: str = ""


@dataclass
class Document:
    """Everything needed to write one compliant document."""

    number: str
    issue_date: date
    supplier: Party
    customer: Party
    lines: list[Line] = field(default_factory=list)
    currency: str = "NGN"
    due_date: date | None = None
    is_credit_note: bool = False
    credit_of: str = ""
    order_reference: str = ""
    buyer_reference: str = ""
    note: str = ""
    payment_terms: str = ""
    bank_name: str = ""
    bank_account_no: str = ""
    discount_total_kobo: int = 0
    customization_id: str = CUSTOMIZATION_ID
    profile_id: str = PROFILE_ID

    # ---- derived totals, always recomputed rather than trusted -----------

    @property
    def line_extension(self) -> int:
        return sum(int(l.net_kobo) for l in self.lines)

    @property
    def tax_total(self) -> int:
        return sum(int(l.vat_kobo) for l in self.lines)

    @property
    def tax_exclusive(self) -> int:
        return self.line_extension - int(self.discount_total_kobo)

    @property
    def tax_inclusive(self) -> int:
        return self.tax_exclusive + self.tax_total

    @property
    def payable(self) -> int:
        return self.tax_inclusive

    @property
    def type_code(self) -> str:
        return CREDIT_NOTE if self.is_credit_note else COMMERCIAL_INVOICE


# --------------------------------------------------------------------------
# Parties
# --------------------------------------------------------------------------


def _party(parent, tag: str, p: Party, currency: str) -> None:
    wrapper = _el(parent, tag)
    node = _el(wrapper, "cac:Party")

    # The routing identifier. A TIN is what Nigerian trade actually keys on.
    if p.tin:
        _el(node, "cbc:EndpointID", _clean(p.tin, 50), schemeID="NG:TIN")

    if p.rc_number:
        ident = _el(node, "cac:PartyIdentification")
        _el(ident, "cbc:ID", _clean(p.rc_number, 50), schemeID="NG:RC")

    name = _el(node, "cac:PartyName")
    _el(name, "cbc:Name", _clean(p.name, 200))

    address = _el(node, "cac:PostalAddress")
    if p.address:
        _el(address, "cbc:StreetName", _clean(p.address, 200))
    if p.city:
        _el(address, "cbc:CityName", _clean(p.city, 80))
    if p.state:
        _el(address, "cbc:CountrySubentity", _clean(p.state, 80))
    country = _el(address, "cac:Country")
    _el(country, "cbc:IdentificationCode", (p.country or COUNTRY).upper()[:2])

    if p.tin or p.vat_reg_no:
        scheme = _el(node, "cac:PartyTaxScheme")
        _el(scheme, "cbc:CompanyID", _clean(p.vat_reg_no or p.tin, 50))
        _el(_el(scheme, "cac:TaxScheme"), "cbc:ID", VAT_SCHEME)

    legal = _el(node, "cac:PartyLegalEntity")
    _el(legal, "cbc:RegistrationName", p.registered_as[:200])
    if p.rc_number:
        _el(legal, "cbc:CompanyID", _clean(p.rc_number, 50))

    if p.contact_person or p.phone or p.email:
        contact = _el(node, "cac:Contact")
        if p.contact_person:
            _el(contact, "cbc:Name", _clean(p.contact_person, 120))
        if p.phone:
            _el(contact, "cbc:Telephone", _clean(p.phone, 60))
        if p.email:
            _el(contact, "cbc:ElectronicMail", _clean(p.email, 120))


# --------------------------------------------------------------------------
# Tax
# --------------------------------------------------------------------------


def _tax_totals(root, doc: Document) -> None:
    """One TaxTotal, with a subtotal per (category, rate) pair.

    Grouping matters: a document with three standard-rated lines has one
    standard-rated subtotal, not three. Validators check that the subtotals sum
    to the total and that each category appears once.
    """
    total = _el(root, "cac:TaxTotal")
    _money(total, "cbc:TaxAmount", doc.tax_total, doc.currency)

    groups: dict[tuple[str, str], dict] = {}
    for line in doc.lines:
        key = (line.tax_category, percent(line.tax_rate))
        bucket = groups.setdefault(key, {"net": 0, "vat": 0, "reason": ""})
        bucket["net"] += int(line.net_kobo)
        bucket["vat"] += int(line.vat_kobo)
        if line.exemption_reason and not bucket["reason"]:
            bucket["reason"] = line.exemption_reason

    for (category, rate), bucket in sorted(groups.items()):
        sub = _el(total, "cac:TaxSubtotal")
        _money(sub, "cbc:TaxableAmount", bucket["net"], doc.currency)
        _money(sub, "cbc:TaxAmount", bucket["vat"], doc.currency)
        cat = _el(sub, "cac:TaxCategory")
        _el(cat, "cbc:ID", category)
        _el(cat, "cbc:Percent", rate)
        # A zero-rated or exempt line must say why, or the document is rejected
        # for claiming relief it has not justified.
        if category in (ZERO_RATED, EXEMPT, OUT_OF_SCOPE, REVERSE_CHARGE):
            _el(cat, "cbc:TaxExemptionReason",
                _clean(bucket["reason"] or _default_reason(category), 300))
        _el(_el(cat, "cac:TaxScheme"), "cbc:ID", VAT_SCHEME)


def _default_reason(category: str) -> str:
    return {
        ZERO_RATED: "Zero-rated supply",
        EXEMPT: "Exempt supply",
        OUT_OF_SCOPE: "Outside the scope of VAT",
        REVERSE_CHARGE: "VAT accounted for by the recipient",
    }.get(category, "")


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------


def _lines(root, doc: Document) -> None:
    line_tag = "cac:CreditNoteLine" if doc.is_credit_note else "cac:InvoiceLine"
    qty_tag = "cbc:CreditedQuantity" if doc.is_credit_note else "cbc:InvoicedQuantity"

    for line in doc.lines:
        node = _el(root, line_tag)
        _el(node, "cbc:ID", str(line.line_no))
        _el(node, qty_tag, quantity(line.qty_milli), unitCode=line.unit_code or "EA")
        _money(node, "cbc:LineExtensionAmount", line.net_kobo, doc.currency)

        item = _el(node, "cac:Item")
        _el(item, "cbc:Name", _clean(line.description, 200) or "Item")
        if line.item_code:
            ident = _el(item, "cac:SellersItemIdentification")
            _el(ident, "cbc:ID", _clean(line.item_code, 40))
        cat = _el(item, "cac:ClassifiedTaxCategory")
        _el(cat, "cbc:ID", line.tax_category)
        _el(cat, "cbc:Percent", percent(line.tax_rate))
        _el(_el(cat, "cac:TaxScheme"), "cbc:ID", VAT_SCHEME)

        price = _el(node, "cac:Price")
        _money(price, "cbc:PriceAmount", line.unit_price_kobo, doc.currency)


# --------------------------------------------------------------------------
# The whole document
# --------------------------------------------------------------------------


def build(doc: Document) -> bytes:
    """The complete UBL document, as bytes ready to sign or send."""
    ns = CREDIT_NS if doc.is_credit_note else INVOICE_NS
    root_tag = "CreditNote" if doc.is_credit_note else "Invoice"

    ET.register_namespace("", ns)
    ET.register_namespace("cac", CAC)
    ET.register_namespace("cbc", CBC)
    root = ET.Element(f"{{{ns}}}{root_tag}")

    _el(root, "cbc:CustomizationID", doc.customization_id)
    _el(root, "cbc:ProfileID", doc.profile_id)
    _el(root, "cbc:ID", _clean(doc.number, 30))
    _el(root, "cbc:IssueDate", _iso(doc.issue_date))
    if doc.due_date and not doc.is_credit_note:
        _el(root, "cbc:DueDate", _iso(doc.due_date))
    _el(root, f"cbc:{'CreditNoteTypeCode' if doc.is_credit_note else 'InvoiceTypeCode'}",
        doc.type_code)
    if doc.note:
        _el(root, "cbc:Note", _clean(doc.note, 1000))
    _el(root, "cbc:DocumentCurrencyCode", (doc.currency or "NGN").upper()[:3])
    if doc.buyer_reference:
        _el(root, "cbc:BuyerReference", _clean(doc.buyer_reference, 60))

    if doc.order_reference:
        _el(_el(root, "cac:OrderReference"), "cbc:ID", _clean(doc.order_reference, 60))

    # A credit note must say which invoice it reverses. Without it the Revenue
    # Service has a negative document floating free of anything.
    if doc.is_credit_note and doc.credit_of:
        ref = _el(root, "cac:BillingReference")
        doc_ref = _el(ref, "cac:InvoiceDocumentReference")
        _el(doc_ref, "cbc:ID", _clean(doc.credit_of, 30))

    _party(root, "cac:AccountingSupplierParty", doc.supplier, doc.currency)
    _party(root, "cac:AccountingCustomerParty", doc.customer, doc.currency)

    if doc.bank_account_no:
        means = _el(root, "cac:PaymentMeans")
        _el(means, "cbc:PaymentMeansCode", "30")      # credit transfer
        account = _el(means, "cac:PayeeFinancialAccount")
        _el(account, "cbc:ID", _clean(doc.bank_account_no, 40))
        if doc.bank_name:
            _el(account, "cbc:Name", _clean(doc.bank_name, 120))

    if doc.payment_terms:
        _el(_el(root, "cac:PaymentTerms"), "cbc:Note", _clean(doc.payment_terms, 300))

    if doc.discount_total_kobo:
        charge = _el(root, "cac:AllowanceCharge")
        _el(charge, "cbc:ChargeIndicator", "false")
        _el(charge, "cbc:AllowanceChargeReason", "Discount")
        _money(charge, "cbc:Amount", doc.discount_total_kobo, doc.currency)

    _tax_totals(root, doc)

    totals = _el(root, "cac:LegalMonetaryTotal")
    _money(totals, "cbc:LineExtensionAmount", doc.line_extension, doc.currency)
    _money(totals, "cbc:TaxExclusiveAmount", doc.tax_exclusive, doc.currency)
    _money(totals, "cbc:TaxInclusiveAmount", doc.tax_inclusive, doc.currency)
    if doc.discount_total_kobo:
        _money(totals, "cbc:AllowanceTotalAmount", doc.discount_total_kobo, doc.currency)
    _money(totals, "cbc:PayableAmount", doc.payable, doc.currency)

    _lines(root, doc)

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def fingerprint(xml: bytes) -> str:
    """A stable SHA-256 of the document, for signing and for change detection.

    If this changes between clearing an invoice and printing it, the two are
    not the same document and the printed one is not the cleared one.
    """
    return hashlib.sha256(xml).hexdigest()
