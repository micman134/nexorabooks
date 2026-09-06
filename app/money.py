"""Money handling.

Every monetary value in this application is an **integer of the minor unit** —
kobo in Nigeria, cents in America, fils in Kuwait, and in Japan the yen itself,
because the yen has no smaller unit. Floating point is never used for money
anywhere: that is what keeps the ledger exact and the trial balance at zero.

How many minor units make a major one is not a constant. It is a property of
the currency the open company keeps its books in:

    12,500.75 naira  ->  1250075   (100 kobo to the naira)
    1,500 yen        ->  1500      (the yen has no minor unit)
    12.345 dinar     ->  12345     (1000 fils to the dinar)

So the helpers here take their scale, their symbol and their separators from
``currency.active()``, which the request middleware sets from the open company.
Outside a request they fall back to the naira, which is where this application
started.

Passing ``cur=`` explicitly formats an amount in a currency other than the
active one — useful when one company's figures are shown while another's books
are open.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

from . import currency
from .currency import Currency

Number = Union[str, int, float, Decimal]

ONE = Decimal("1")
TWO_PLACES = Decimal("0.01")        # kept for callers that predate currencies


def _cur(cur: Currency | None) -> Currency:
    return cur or currency.active()


# --------------------------------------------------------------------------
# Reading what somebody typed
# --------------------------------------------------------------------------


def _separators(cur: Currency) -> set[str]:
    """Characters that might be grouping the digits.

    The comma and the full stop are always considered, whatever the currency,
    because a customer typing in a hurry uses whichever one their keyboard and
    their habits produce. Spaces never are: a space is removed outright.
    """
    seps = {",", "."}
    for ch in (cur.thousands, cur.point):
        if ch and not ch.isspace():
            seps.add(ch)
    return seps


def _parse(text: str, cur: Currency) -> Decimal:
    """Turn typed text into a Decimal of major units.

    The awkward part is telling a thousands separator from a decimal point when
    only one of them is present. The rules, in order:

      * two different separators — the rightmost is the decimal point, because
        no notation puts grouping after the fraction;
      * one separator appearing more than once — grouping, since a number has
        only ever one decimal point;
      * one separator, matching this currency's thousands character — grouping;
      * anything else — a decimal point.

    In naira that makes "1,500" fifteen hundred and "1.500" one and a half,
    which is what a Nigerian bookkeeper means by each. In reais it makes
    "1.500" fifteen hundred and "1,500" one and a half, which is what a
    Brazilian bookkeeper means. Neither has to think about it.
    """
    seps = _separators(cur)
    kept = [ch for ch in text if ch.isdigit() or ch in seps or ch in "+-"]
    cleaned = "".join(kept)
    negative = "-" in cleaned or ("(" in text and ")" in text)
    cleaned = cleaned.replace("-", "").replace("+", "")

    present = [ch for ch in seps if ch in cleaned]
    if len(present) >= 2:
        point = max(present, key=cleaned.rfind)
    elif len(present) == 1:
        only = present[0]
        point = None if (cleaned.count(only) > 1 or only == cur.thousands) else only
    else:
        point = None

    if point:
        whole, _, frac = cleaned.rpartition(point)
        whole = "".join(c for c in whole if c.isdigit()) or "0"
        frac = "".join(c for c in frac if c.isdigit()) or "0"
        value = Decimal(f"{whole}.{frac}")
    else:
        digits = "".join(c for c in cleaned if c.isdigit()) or "0"
        value = Decimal(digits)
    return -value if negative else value


def to_minor(value: Number, cur: Currency | None = None) -> int:
    """Convert a typed amount into whole minor units, rounding half-up.

    A plain ``int`` is taken to be major units — ``to_minor(5)`` is five naira,
    five dollars or five yen depending on the books that are open.
    """
    cur = _cur(cur)
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * cur.scale
    d = value if isinstance(value, Decimal) else _parse(str(value), cur)
    return int((d * cur.scale).quantize(ONE, rounding=ROUND_HALF_UP))


def to_major(minor: int, cur: Currency | None = None) -> Decimal:
    """Convert whole minor units back to a Decimal of major units."""
    cur = _cur(cur)
    d = Decimal(int(minor or 0)) / Decimal(cur.scale)
    return d.quantize(Decimal(1).scaleb(-cur.decimals)) if cur.decimals else d.quantize(ONE)


# --------------------------------------------------------------------------
# Writing it back out
# --------------------------------------------------------------------------


def _grouped(whole: int, sep: str) -> str:
    text = f"{whole:,}"
    return text if sep == "," else text.replace(",", sep)


def digits(minor: int, cur: Currency | None = None) -> str:
    """The figure alone: grouped, with the fraction, and no symbol or sign."""
    cur = _cur(cur)
    n = abs(int(minor or 0))
    if not cur.decimals:
        return _grouped(n, cur.thousands)
    whole, frac = divmod(n, cur.scale)
    return f"{_grouped(whole, cur.thousands)}{cur.point}{frac:0{cur.decimals}d}"


def fmt(minor: int, symbol: str | None = None, blank_zero: bool = False,
        cur: Currency | None = None) -> str:
    """Format for display: 1250075 -> '₦12,500.75', or '¥1,500', or '12.345 KD'.

    Negatives are shown in brackets, which is how accounts have been written
    for a very long time and reads unambiguously in every column.
    Pass ``symbol=""`` for the bare figure.
    """
    cur = _cur(cur)
    minor = int(minor or 0)
    if blank_zero and minor == 0:
        return ""
    sym = cur.symbol if symbol is None else symbol
    body = digits(minor, cur)
    if sym:
        if cur.symbol_after:
            body = f"{body} {sym}"
        else:
            # A symbol that is a word needs air: "AED 1,500" and "KSh 1,500"
            # read properly, "AED1,500" does not. A sign like ₦ or $ sits
            # tight against the figure, which is how people write it.
            gap = " " if len(sym) > 1 and sym.isalpha() else ""
            body = f"{sym}{gap}{body}"
    return f"({body})" if minor < 0 else body


def fmt_plain(minor: int, cur: Currency | None = None) -> str:
    """Format without symbol or grouping, for CSV and spreadsheet export.

    Always a full stop for the decimal point and never a thousands separator,
    because this is read by a machine, not a person.
    """
    cur = _cur(cur)
    minor = int(minor or 0)
    sign = "-" if minor < 0 else ""
    n = abs(minor)
    if not cur.decimals:
        return f"{sign}{n}"
    whole, frac = divmod(n, cur.scale)
    return f"{sign}{whole}.{frac:0{cur.decimals}d}"


# --------------------------------------------------------------------------
# Arithmetic — none of this depends on the scale
# --------------------------------------------------------------------------


def pct_of(minor: int, rate: Number) -> int:
    """Apply a percentage rate to an amount, rounding half-up.

    pct_of(100_00, '7.5') -> 750  (₦7.50 VAT on ₦100.00)
    """
    d = Decimal(int(minor)) * Decimal(str(rate)) / Decimal(100)
    return int(d.quantize(ONE, rounding=ROUND_HALF_UP))


def split_inclusive(gross: int, rate: Number) -> tuple[int, int]:
    """Split a tax-inclusive gross amount into (net, tax).

    split_inclusive(107_50, '7.5') -> (10000, 750)
    """
    r = Decimal(str(rate))
    net = (Decimal(int(gross)) * Decimal(100) / (Decimal(100) + r)).quantize(
        ONE, rounding=ROUND_HALF_UP
    )
    net_i = int(net)
    return net_i, int(gross) - net_i


def allocate(total: int, weights: list[int]) -> list[int]:
    """Distribute a total across weights with nothing lost or invented.

    Used for prorating discounts, freight and rounding across document lines.
    The largest-remainder method guarantees the parts sum exactly to the total.
    """
    total_w = sum(weights)
    if total_w == 0:
        return [0] * len(weights)
    raw = [Decimal(total) * Decimal(w) / Decimal(total_w) for w in weights]
    floors = [int(x.to_integral_value(rounding="ROUND_FLOOR")) for x in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in range(remainder):
        floors[order[i % len(order)]] += 1
    return floors


# --------------------------------------------------------------------------
# The names this module used before it knew about other currencies.
# Kept so that nothing which already reads clearly has to be disturbed.
# --------------------------------------------------------------------------

to_kobo = to_minor
to_naira = to_major
