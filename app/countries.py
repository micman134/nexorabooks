"""What each country calls the things this application already does.

The accounting does not change when you cross a border. A debit is a debit in
Lagos, Lima and Ljubljana. What changes is the vocabulary, and getting the
vocabulary wrong makes a competent system look foreign and untrustworthy: a
South African expects to read "VAT" and a Canadian "GST"; a Nigerian company
quotes an RC number and a Kenyan one a registration number; the tax authority
has a different name and a different acronym in every one of them.

So this table carries labels, not rules. Choosing a country pre-fills the
wording, the currency and the date format on the company settings screen, and
every one of those values stays editable afterwards — the customer knows their
own country better than this table does.

The standard tax rates below are a **starting point only**. Rates change with
budgets and the settings screen says so plainly, because a wrong rate that
looks authoritative is worse than an empty box.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    code: str               # ISO 3166-1 alpha-2
    name: str
    currency: str           # ISO 4217, must exist in currency.PRESETS
    tax_label: str = "VAT"          # VAT / GST / Sales Tax / Consumption Tax
    tax_rate: str = ""              # standard rate, as a percentage string
    tax_id_label: str = "Tax ID"
    reg_no_label: str = "Registration number"
    tax_authority: str = "the tax authority"
    date_format: str = "%d/%m/%Y"


def _c(*args, **kw) -> Country:
    return Country(*args, **kw)


GENERIC = Country("ZZ", "Somewhere else", "USD", "VAT", "",
                  "Tax ID", "Registration number", "the tax authority", "%d/%m/%Y")


COUNTRIES: list[Country] = [
    # --- Africa ----------------------------------------------------------
    _c("NG", "Nigeria", "NGN", "VAT", "7.5", "TIN", "RC number",
       "NRS", "%d %b %Y"),
    _c("GH", "Ghana", "GHS", "VAT", "15", "TIN", "Registration number",
       "GRA", "%d/%m/%Y"),
    _c("KE", "Kenya", "KES", "VAT", "16", "KRA PIN", "Registration number",
       "KRA", "%d/%m/%Y"),
    _c("ZA", "South Africa", "ZAR", "VAT", "15", "VAT number", "Registration number",
       "SARS", "%Y-%m-%d"),
    _c("TZ", "Tanzania", "TZS", "VAT", "18", "TIN", "Registration number",
       "TRA", "%d/%m/%Y"),
    _c("UG", "Uganda", "UGX", "VAT", "18", "TIN", "Registration number",
       "URA", "%d/%m/%Y"),
    _c("RW", "Rwanda", "RWF", "VAT", "18", "TIN", "Registration number",
       "RRA", "%d/%m/%Y"),
    _c("EG", "Egypt", "EGP", "VAT", "14", "Tax registration number",
       "Commercial register number", "ETA", "%d/%m/%Y"),
    _c("MA", "Morocco", "MAD", "TVA", "20", "Identifiant fiscal", "RC number",
       "DGI", "%d/%m/%Y"),
    _c("CI", "Côte d'Ivoire", "XOF", "TVA", "18", "Numéro de compte contribuable",
       "RCCM number", "DGI", "%d/%m/%Y"),
    _c("SN", "Senegal", "XOF", "TVA", "18", "NINEA", "RCCM number",
       "DGID", "%d/%m/%Y"),
    _c("CM", "Cameroon", "XAF", "TVA", "19.25", "Numéro d'identifiant unique",
       "RCCM number", "DGI", "%d/%m/%Y"),
    _c("ET", "Ethiopia", "ETB", "VAT", "15", "TIN", "Registration number",
       "MoR", "%d/%m/%Y"),
    _c("ZM", "Zambia", "ZMW", "VAT", "16", "TPIN", "Registration number",
       "ZRA", "%d/%m/%Y"),
    _c("BW", "Botswana", "BWP", "VAT", "14", "TIN", "Registration number",
       "BURS", "%d/%m/%Y"),
    _c("MU", "Mauritius", "MUR", "VAT", "15", "TAN", "Business registration number",
       "MRA", "%d/%m/%Y"),
    # --- Americas --------------------------------------------------------
    _c("US", "United States", "USD", "Sales Tax", "", "EIN", "State file number",
       "the IRS", "%m/%d/%Y"),
    _c("CA", "Canada", "CAD", "GST/HST", "5", "Business Number",
       "Corporation number", "the CRA", "%Y-%m-%d"),
    _c("MX", "Mexico", "MXN", "IVA", "16", "RFC", "Folio mercantil",
       "the SAT", "%d/%m/%Y"),
    _c("BR", "Brazil", "BRL", "ICMS", "", "CNPJ", "NIRE",
       "Receita Federal", "%d/%m/%Y"),
    _c("AR", "Argentina", "ARS", "IVA", "21", "CUIT", "Registration number",
       "ARCA", "%d/%m/%Y"),
    _c("CL", "Chile", "CLP", "IVA", "19", "RUT", "Registration number",
       "the SII", "%d/%m/%Y"),
    _c("CO", "Colombia", "COP", "IVA", "19", "NIT", "Registration number",
       "the DIAN", "%d/%m/%Y"),
    _c("JM", "Jamaica", "JMD", "GCT", "15", "TRN", "Registration number",
       "TAJ", "%d/%m/%Y"),
    _c("TT", "Trinidad and Tobago", "TTD", "VAT", "12.5", "BIR number",
       "Registration number", "the BIR", "%d/%m/%Y"),
    # --- Europe ----------------------------------------------------------
    _c("GB", "United Kingdom", "GBP", "VAT", "20", "VAT registration number",
       "Company number", "HMRC", "%d/%m/%Y"),
    _c("IE", "Ireland", "EUR", "VAT", "23", "VAT number", "CRO number",
       "Revenue", "%d/%m/%Y"),
    _c("DE", "Germany", "EUR", "USt", "19", "USt-IdNr", "Handelsregisternummer",
       "the Finanzamt", "%d.%m.%Y"),
    _c("FR", "France", "EUR", "TVA", "20", "Numéro de TVA", "SIREN",
       "the DGFiP", "%d/%m/%Y"),
    _c("ES", "Spain", "EUR", "IVA", "21", "NIF", "Registration number",
       "the AEAT", "%d/%m/%Y"),
    _c("IT", "Italy", "EUR", "IVA", "22", "Partita IVA", "REA number",
       "Agenzia delle Entrate", "%d/%m/%Y"),
    _c("NL", "Netherlands", "EUR", "BTW", "21", "BTW-nummer", "KvK number",
       "the Belastingdienst", "%d-%m-%Y"),
    _c("PT", "Portugal", "EUR", "IVA", "23", "NIF", "Registration number",
       "the AT", "%d/%m/%Y"),
    _c("PL", "Poland", "PLN", "VAT", "23", "NIP", "KRS number",
       "Krajowa Administracja Skarbowa", "%d.%m.%Y"),
    _c("SE", "Sweden", "SEK", "Moms", "25", "VAT number", "Organisationsnummer",
       "Skatteverket", "%Y-%m-%d"),
    _c("NO", "Norway", "NOK", "MVA", "25", "Organisasjonsnummer",
       "Organisasjonsnummer", "Skatteetaten", "%d.%m.%Y"),
    _c("DK", "Denmark", "DKK", "Moms", "25", "CVR number", "CVR number",
       "Skattestyrelsen", "%d.%m.%Y"),
    _c("CH", "Switzerland", "CHF", "MWST", "8.1", "UID", "UID",
       "the ESTV", "%d.%m.%Y"),
    _c("TR", "Türkiye", "TRY", "KDV", "20", "Vergi kimlik numarası",
       "Registration number", "the GİB", "%d.%m.%Y"),
    _c("RO", "Romania", "RON", "TVA", "21", "CUI", "Registration number",
       "ANAF", "%d.%m.%Y"),
    _c("CZ", "Czechia", "CZK", "DPH", "21", "DIČ", "IČO",
       "Finanční správa", "%d.%m.%Y"),
    _c("HU", "Hungary", "HUF", "ÁFA", "27", "Adószám", "Registration number",
       "NAV", "%Y.%m.%d"),
    _c("UA", "Ukraine", "UAH", "VAT", "20", "Tax number", "EDRPOU code",
       "the State Tax Service", "%d.%m.%Y"),
    # --- Middle East -----------------------------------------------------
    _c("AE", "United Arab Emirates", "AED", "VAT", "5", "TRN",
       "Trade licence number", "the FTA", "%d/%m/%Y"),
    _c("SA", "Saudi Arabia", "SAR", "VAT", "15", "VAT number",
       "Commercial registration number", "ZATCA", "%d/%m/%Y"),
    _c("QA", "Qatar", "QAR", "VAT", "", "TIN", "Commercial registration number",
       "the GTA", "%d/%m/%Y"),
    _c("KW", "Kuwait", "KWD", "VAT", "", "Tax card number",
       "Commercial registration number", "the tax authority", "%d/%m/%Y"),
    _c("BH", "Bahrain", "BHD", "VAT", "10", "VAT account number",
       "CR number", "the NBR", "%d/%m/%Y"),
    _c("OM", "Oman", "OMR", "VAT", "5", "VAT identification number",
       "CR number", "the tax authority", "%d/%m/%Y"),
    _c("JO", "Jordan", "JOD", "GST", "16", "Tax number", "Registration number",
       "the ISTD", "%d/%m/%Y"),
    _c("IL", "Israel", "ILS", "VAT", "18", "Tax file number",
       "Company number", "the Tax Authority", "%d/%m/%Y"),
    # --- Asia and the Pacific --------------------------------------------
    _c("IN", "India", "INR", "GST", "18", "GSTIN", "CIN",
       "the GST department", "%d/%m/%Y"),
    _c("PK", "Pakistan", "PKR", "Sales Tax", "18", "NTN", "Incorporation number",
       "the FBR", "%d/%m/%Y"),
    _c("BD", "Bangladesh", "BDT", "VAT", "15", "BIN", "Registration number",
       "the NBR", "%d/%m/%Y"),
    _c("LK", "Sri Lanka", "LKR", "VAT", "18", "TIN", "Registration number",
       "the IRD", "%d/%m/%Y"),
    _c("CN", "China", "CNY", "VAT", "13", "Unified social credit code",
       "Unified social credit code", "the STA", "%Y-%m-%d"),
    _c("JP", "Japan", "JPY", "Consumption Tax", "10", "Corporate number",
       "Corporate number", "the NTA", "%Y-%m-%d"),
    _c("KR", "South Korea", "KRW", "VAT", "10", "Business registration number",
       "Business registration number", "the NTS", "%Y-%m-%d"),
    _c("SG", "Singapore", "SGD", "GST", "9", "GST registration number",
       "UEN", "IRAS", "%d/%m/%Y"),
    _c("MY", "Malaysia", "MYR", "SST", "8", "Tax identification number",
       "Registration number", "the LHDN", "%d/%m/%Y"),
    _c("ID", "Indonesia", "IDR", "PPN", "12", "NPWP", "NIB",
       "Direktorat Jenderal Pajak", "%d/%m/%Y"),
    _c("TH", "Thailand", "THB", "VAT", "7", "Tax identification number",
       "Registration number", "the Revenue Department", "%d/%m/%Y"),
    _c("VN", "Vietnam", "VND", "VAT", "10", "Tax code", "Enterprise code",
       "the General Department of Taxation", "%d/%m/%Y"),
    _c("PH", "Philippines", "PHP", "VAT", "12", "TIN", "SEC registration number",
       "the BIR", "%m/%d/%Y"),
    _c("HK", "Hong Kong", "HKD", "None", "", "Business registration number",
       "Company number", "the IRD", "%d/%m/%Y"),
    _c("AU", "Australia", "AUD", "GST", "10", "ABN", "ACN",
       "the ATO", "%d/%m/%Y"),
    _c("NZ", "New Zealand", "NZD", "GST", "15", "IRD number",
       "Company number", "Inland Revenue", "%d/%m/%Y"),
    GENERIC,
]

BY_CODE: dict[str, Country] = {c.code: c for c in COUNTRIES}


def get(code: str | None) -> Country:
    """The country's wording, or a neutral set if it is not in the table."""
    return BY_CODE.get((code or "").strip().upper(), GENERIC)


def choices() -> list[Country]:
    """Alphabetical by name, with 'Somewhere else' left at the end."""
    named = sorted((c for c in COUNTRIES if c.code != "ZZ"), key=lambda c: c.name)
    return named + [GENERIC]


def apply_to(company, country: Country, *, wording_only: bool = False) -> None:
    """Copy a country's wording onto a company row.

    ``wording_only`` leaves the currency alone, which is what happens when a
    company that already has figures in its books corrects its country: the
    labels can change freely, but the currency cannot, because every stored
    integer is a count of its minor units.
    """
    from . import currency as currency_mod

    company.country_code = country.code
    company.country_name = country.name
    company.tax_label = country.tax_label
    company.tax_id_label = country.tax_id_label
    company.reg_no_label = country.reg_no_label
    company.tax_authority = country.tax_authority
    company.date_format = country.date_format
    if wording_only:
        return
    spec = currency_mod.preset(country.currency) or currency_mod.DEFAULT
    company.currency_code = spec.code
    company.currency_symbol = spec.symbol
    company.currency_decimals = spec.decimals
    company.currency_symbol_after = spec.symbol_after
    company.currency_thousands = spec.thousands
    company.currency_point = spec.point
