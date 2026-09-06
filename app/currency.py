"""What one unit of money looks like, for the company whose books are open.

Every company keeps its books in exactly one currency, chosen when the company
is created. That is deliberate: a set of books in a single currency is a set of
books that always balances, and a small business almost never wants anything
else. A business in Accra keeps cedis, a business in Nairobi keeps shillings, a
business in Lagos keeps naira — and each of them gets a ledger that adds up.

The currency is more than a symbol. It decides:

  * how many minor units there are in a major one — 100 kobo in a naira, but
    1000 fils in a dinar and none at all in a yen;
  * where the symbol sits, before the figure or after it;
  * which character groups the thousands and which one marks the decimal.

Amounts are still stored as integers of the minor unit, exactly as before. What
changes with the currency is only how many of them make one, and how they are
written down.

The active currency is held in a context variable, set once per request from
the open company. Outside a request — a test, a script, the seeder — it falls
back to the naira, which is where this application started.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Currency:
    """How one currency is written."""

    code: str                       # ISO 4217, e.g. "NGN"
    symbol: str                     # what people actually write, e.g. "₦"
    name: str                       # for the settings screen
    decimals: int = 2               # minor units per major, as a power of ten
    symbol_after: bool = False      # "1 234,56 kr" rather than "₦1,234.56"
    thousands: str = ","            # groups the whole part
    point: str = "."                # separates the fraction

    @property
    def scale(self) -> int:
        """Minor units in one major unit: 100 for naira, 1 for yen."""
        return 10 ** self.decimals

    @property
    def label(self) -> str:
        return f"{self.code} — {self.name} ({self.symbol})"


# --------------------------------------------------------------------------
# The currencies people actually keep books in
# --------------------------------------------------------------------------
#
# Decimals matter more than they look. Getting the yen wrong by a factor of a
# hundred is not a rounding error, it is a wrong set of accounts — so the
# zero-decimal and three-decimal currencies below are stated explicitly rather
# than left to a default.

def _c(code, symbol, name, decimals=2, after=False, thousands=",", point="."):
    return Currency(code, symbol, name, decimals, after, thousands, point)


PRESETS: dict[str, Currency] = {c.code: c for c in [
    # Africa
    _c("NGN", "₦", "Nigerian Naira"),
    _c("GHS", "GH₵", "Ghanaian Cedi"),
    _c("KES", "KSh", "Kenyan Shilling"),
    _c("UGX", "USh", "Ugandan Shilling", decimals=0),
    _c("TZS", "TSh", "Tanzanian Shilling", decimals=0),
    _c("RWF", "FRw", "Rwandan Franc", decimals=0),
    _c("ZAR", "R", "South African Rand", thousands=" ", point=","),
    _c("EGP", "E£", "Egyptian Pound"),
    _c("MAD", "DH", "Moroccan Dirham", after=True),
    _c("XOF", "CFA", "West African CFA Franc", decimals=0, after=True, thousands=" "),
    _c("XAF", "FCFA", "Central African CFA Franc", decimals=0, after=True, thousands=" "),
    _c("ETB", "Br", "Ethiopian Birr"),
    _c("ZMW", "ZK", "Zambian Kwacha"),
    _c("BWP", "P", "Botswana Pula"),
    _c("MUR", "Rs", "Mauritian Rupee"),
    # Americas
    _c("USD", "$", "US Dollar"),
    _c("CAD", "C$", "Canadian Dollar"),
    _c("MXN", "$", "Mexican Peso"),
    _c("BRL", "R$", "Brazilian Real", thousands=".", point=","),
    _c("ARS", "$", "Argentine Peso", thousands=".", point=","),
    _c("CLP", "$", "Chilean Peso", decimals=0, thousands="."),
    _c("COP", "$", "Colombian Peso", thousands=".", point=","),
    _c("JMD", "J$", "Jamaican Dollar"),
    _c("TTD", "TT$", "Trinidad and Tobago Dollar"),
    # Europe
    _c("EUR", "€", "Euro"),
    _c("GBP", "£", "Pound Sterling"),
    _c("CHF", "CHF", "Swiss Franc", after=True, thousands="'"),
    _c("SEK", "kr", "Swedish Krona", after=True, thousands=" ", point=","),
    _c("NOK", "kr", "Norwegian Krone", after=True, thousands=" ", point=","),
    _c("DKK", "kr", "Danish Krone", after=True, thousands=".", point=","),
    _c("PLN", "zł", "Polish Zloty", after=True, thousands=" ", point=","),
    _c("CZK", "Kč", "Czech Koruna", after=True, thousands=" ", point=","),
    _c("HUF", "Ft", "Hungarian Forint", after=True, thousands=" ", point=","),
    _c("RON", "lei", "Romanian Leu", after=True, thousands=".", point=","),
    _c("TRY", "₺", "Turkish Lira", thousands=".", point=","),
    _c("UAH", "₴", "Ukrainian Hryvnia", after=True, thousands=" ", point=","),
    _c("RUB", "₽", "Russian Rouble", after=True, thousands=" ", point=","),
    # Middle East
    _c("AED", "AED", "UAE Dirham"),
    _c("SAR", "SR", "Saudi Riyal"),
    _c("QAR", "QR", "Qatari Riyal"),
    _c("KWD", "KD", "Kuwaiti Dinar", decimals=3),
    _c("BHD", "BD", "Bahraini Dinar", decimals=3),
    _c("OMR", "OMR", "Omani Rial", decimals=3),
    _c("JOD", "JD", "Jordanian Dinar", decimals=3),
    _c("TND", "DT", "Tunisian Dinar", decimals=3),
    _c("ILS", "₪", "Israeli Shekel"),
    # Asia and the Pacific
    _c("INR", "₹", "Indian Rupee"),
    _c("PKR", "Rs", "Pakistani Rupee"),
    _c("BDT", "৳", "Bangladeshi Taka"),
    _c("LKR", "Rs", "Sri Lankan Rupee"),
    _c("CNY", "¥", "Chinese Yuan"),
    _c("JPY", "¥", "Japanese Yen", decimals=0),
    _c("KRW", "₩", "South Korean Won", decimals=0),
    _c("SGD", "S$", "Singapore Dollar"),
    _c("MYR", "RM", "Malaysian Ringgit"),
    _c("IDR", "Rp", "Indonesian Rupiah", decimals=0, thousands=".", point=","),
    _c("THB", "฿", "Thai Baht"),
    _c("VND", "₫", "Vietnamese Dong", decimals=0, after=True, thousands="."),
    _c("PHP", "₱", "Philippine Peso"),
    _c("HKD", "HK$", "Hong Kong Dollar"),
    _c("AUD", "A$", "Australian Dollar"),
    _c("NZD", "NZ$", "New Zealand Dollar"),
]}

NGN = PRESETS["NGN"]
DEFAULT = NGN

#: Offered first on the setup screen; the rest are alphabetical after these.
POPULAR = ("NGN", "USD", "EUR", "GBP", "GHS", "KES", "ZAR", "INR", "AED", "CAD", "AUD")


def preset(code: str) -> Currency | None:
    return PRESETS.get((code or "").strip().upper())


def choices() -> list[Currency]:
    """Every preset, popular ones first, then alphabetically by code."""
    head = [PRESETS[c] for c in POPULAR if c in PRESETS]
    rest = sorted((c for c in PRESETS.values() if c.code not in POPULAR),
                  key=lambda c: c.code)
    return head + rest


# --------------------------------------------------------------------------
# Which one is in force
# --------------------------------------------------------------------------

_active: ContextVar[Currency] = ContextVar("active_currency", default=DEFAULT)


def active() -> Currency:
    """The currency of the company whose books are open."""
    return _active.get()


def set_active(cur: Currency | None) -> None:
    _active.set(cur or DEFAULT)


class using:
    """Run a block in a given currency, then put back what was there.

        with currency.using(currency.preset("JPY")):
            assert money.fmt(1500) == "¥1,500"

    Handy in tests and in anything that formats one company's figures while
    another company's books are open.
    """

    def __init__(self, cur: Currency | None):
        self.cur = cur or DEFAULT
        self.token = None

    def __enter__(self) -> Currency:
        self.token = _active.set(self.cur)
        return self.cur

    def __exit__(self, *exc):
        _active.reset(self.token)
        return False


def from_company(company) -> Currency:
    """Build the spec from a Company row, falling back to its preset.

    The columns are the authority — a customer is allowed to change the symbol
    or the separators to suit how their country actually writes money, and the
    preset is only where those values started.
    """
    if company is None:
        return DEFAULT
    base = preset(getattr(company, "currency_code", "") or "") or DEFAULT
    return replace(
        base,
        code=(getattr(company, "currency_code", "") or base.code).strip().upper(),
        symbol=(getattr(company, "currency_symbol", "") or base.symbol),
        decimals=_decimals_of(company, base),
        symbol_after=bool(getattr(company, "currency_symbol_after", base.symbol_after)),
        thousands=(getattr(company, "currency_thousands", None) or base.thousands),
        point=(getattr(company, "currency_point", None) or base.point),
    )


def _decimals_of(company, base: Currency) -> int:
    """Decimals, clamped to what the ledger can actually store sensibly."""
    value = getattr(company, "currency_decimals", None)
    if value is None:
        return base.decimals
    try:
        value = int(value)
    except (TypeError, ValueError):
        return base.decimals
    return value if value in (0, 2, 3) else base.decimals
