"""What Nexora Books costs, who to pay, and how a quote is worked out.

Everything a seller has to change in order to sell is in this one file. It is
compiled into the build the customer receives, so the numbers here are the
numbers on their screen — change them, rebuild, and that is the whole pricing
system.

Nothing here talks to a payment provider, and it never will from inside the
application. Nexora Books runs on the customer's own computer with no internet
connection; a licence is a signed text file, not an account on somebody's
server. So this module's job is to tell the customer exactly what to pay, to
whom, quoting what reference — and then to give them somewhere to paste the
licence that comes back. The money moves the way money already moves between
two businesses.

    SELLER            who you are, and how a customer reaches you
    GATEWAYS          the checkout pages a customer can pay through
    BANK              account details, for anybody who asks for them
    BANDS             price per user per year, cheaper in bulk
    quote()           what a given number of users costs

One deliberate safety net: ``PRICES_ARE_EXAMPLES`` starts True. While it is
True every price on the licence screen is labelled as an example. A seller who
forgets to set their own prices sees that warning on their own machine long
before a customer does — which is the right way round for a mistake that would
otherwise be discovered by somebody being quoted the wrong figure.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

# ---------------------------------------------------------------------------
# WHO YOU ARE
# ---------------------------------------------------------------------------

SELLER = {
    "business": "Tavo Networks Limited",
    "email": "support@tavonetworks.tech",
    "phone": "+234 808 334 2954",
    # The number a customer sends proof of payment to. Often the same as the
    # phone number above; kept separate because it often is not.
    "whatsapp": "+234 808 334 2954",
    "website": "",
}

# ---------------------------------------------------------------------------
# WHERE THE MONEY GOES
# ---------------------------------------------------------------------------
#
# A customer picks a gateway and pays through it. Nothing here handles money —
# the application has no internet connection and no business holding anybody's
# card details. It opens a checkout page you already have with your provider,
# with the amount and the machine code carried into the link so that neither
# has to be typed by hand.

@dataclass(frozen=True)
class Gateway:
    """One way to pay, and the page it opens.

    ``url`` may carry ``{amount}``, ``{amount_minor}``, ``{reference}``,
    ``{users}`` and ``{currency}``. They are filled in before the link is
    shown, so the customer arrives at a checkout that already knows what they
    are buying and which computer it is for — the commonest way a payment goes
    astray is somebody typing the amount or the reference themselves.

    There are three ways to write the amount because providers disagree about
    which they want, and being wrong is a factor-of-a-hundred error in one
    direction or the other:

        {amount}        major units with decimals   "960000.00"
        {amount_whole}  major units, no decimals    "960000"
        {amount_minor}  minor units (kobo, cents)   "96000000"

    A Paystack payment page pre-fills the amount box a customer would type in,
    so it wants naira — ``{amount_whole}``. Their transaction API wants kobo.
    Send one real payment through before you rely on either.
    """

    name: str
    url: str
    note: str = ""

    def link_for(self, total_minor: int, reference: str, users: int) -> str:
        whole, part = divmod(int(total_minor), 100)
        return (self.url
                .replace("{amount_minor}", str(int(total_minor)))
                .replace("{amount_whole}", str(whole))
                .replace("{amount}", f"{whole}.{part:02d}")
                .replace("{reference}", quote_plus(reference or ""))
                .replace("{users}", str(int(users or 0)))
                .replace("{currency}", CURRENCY))


#: How a customer pays. Put your own checkout pages in here — a Paystack or
#: Flutterwave payment page, a Paddle checkout, anything with a URL. The first
#: one listed is the one the screen leads with.
#:
#: Leave the list empty and the screen falls back to asking them to get in
#: touch, which is honest but slow. Filling in even one turns the licence
#: screen into somewhere a customer can actually pay from.
GATEWAYS: tuple[Gateway, ...] = (
    Gateway(
        name="card or bank transfer",
        # Tavo Networks Limited's Paystack payment page. "read-only=amount"
        # stops the figure being edited in the form. It does NOT stop it being
        # edited in the address bar — Paystack say so themselves — which is
        # exactly why a licence is issued only after the payment has been read
        # off the Paystack dashboard, never from the number in this link.
        url="https://paystack.shop/pay/y5cn---yaa"
            "?amount={amount_whole}&read-only=amount",
        note="Paystack — card, transfer or USSD",
    ),
)

#: Bank details, for customers who ask. Off by default: a gateway confirms the
#: payment by itself, whereas a transfer needs somebody to read a statement and
#: match it up by hand. Fill these in only if you want to offer it as well.
BANK = {
    "show": False,
    "bank": "",
    "account_name": "",
    "account_number": "",
    "note": "",
}

# ---------------------------------------------------------------------------
# WHAT IT COSTS
# ---------------------------------------------------------------------------

#: The currency prices are quoted in, and how to write it.
CURRENCY = "NGN"
SYMBOL = "₦"

#: Set this to False once the prices below are your real ones. While it is
#: True, every figure on the licence screen is marked as an example.
PRICES_ARE_EXAMPLES = False

#: How long a licence lasts. A year, because that is what funds the support
#: somebody will need over that year.
LICENCE_MONTHS = 12


@dataclass(frozen=True)
class Band:
    """A price per user per year, for companies up to a certain size.

    ``up_to = 0`` means "and everybody larger". The rate that applies is the
    band the *total* number of users falls into, applied to all of them — not
    a marginal calculation. A company of ten pays the ten-user rate on all ten,
    which is both simpler to explain and cheaper for them, and being able to
    say it in one sentence is worth more than the difference.
    """

    up_to: int
    per_user: int          # minor units per user per year (kobo, cents, …)
    label: str = ""

    def covers(self, users: int) -> bool:
        return self.up_to == 0 or users <= self.up_to


#: Prices per user per YEAR, in minor units — kobo here, so 120_000_00 is
#: ₦120,000.00. Ordered smallest band first.
#:
#: These are quoted to the customer by the month, because that is the figure
#: people compare, but a licence is bought a year at a time and the yearly
#: figure is what they actually pay. Both are shown on the screen, and the
#: number stored here is the yearly one — twelve times the monthly rate.
#:
#:      1 to 4 users        ₦10,000 per user a month     ₦120,000 a year
#:      5 to 9 users         ₦9,000 per user a month     ₦108,000 a year
#:      10 users or more     ₦8,000 per user a month      ₦96,000 a year
BANDS: tuple[Band, ...] = (
    Band(up_to=4,  per_user=120_000_00, label="1 to 4 users"),
    Band(up_to=9,  per_user=108_000_00, label="5 to 9 users"),
    Band(up_to=0,  per_user=96_000_00,  label="10 users or more"),
)

#: How the monthly figure is worked out from the yearly one. Kept here rather
#: than written into each band so the two can never drift apart.
MONTHS_IN_A_YEAR = 12

#: Above this, stop quoting and start talking. A forty-person firm wants a
#: conversation and probably a different price; a screen that silently
#: multiplies loses that sale without either side noticing.
TALK_TO_US_ABOVE = 25

#: The smallest licence that can be bought.
MINIMUM_USERS = 1


# ---------------------------------------------------------------------------
# Working out a price
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    users: int                 # what they will actually get
    per_user: int
    total: int
    band: Band | None
    too_big: bool = False
    asked_for: int = 0         # what they typed, when it differs

    @property
    def is_quotable(self) -> bool:
        return not self.too_big and self.total > 0

    @property
    def bumped(self) -> bool:
        """True when they are being given more users than they asked for.

        Flat band pricing has an awkward seam: at ₦38,000 each, nine users cost
        ₦342,000, while ten at ₦30,000 cost ₦300,000. Charging somebody more
        for less is the kind of thing a customer finds on their own and never
        quite forgives, so ``quote`` looks past the number asked for and hands
        over the larger licence when it is cheaper.
        """
        return bool(self.asked_for) and self.users > self.asked_for


def band_for(users: int) -> Band | None:
    for band in BANDS:
        if band.covers(users):
            return band
    return BANDS[-1] if BANDS else None


def _straight(users: int) -> tuple[int, Band | None]:
    band = band_for(users)
    return (band.per_user * users if band else 0), band


def quote(users: int) -> Quote:
    """What a licence for this many users costs for one year.

    Never quotes a price higher than a *larger* licence would cost. See
    ``Quote.bumped`` — at a band boundary the bigger licence is sometimes the
    cheaper one, and the customer should be given it rather than left to
    discover the seam by arithmetic.
    """
    asked = max(MINIMUM_USERS, int(users or 0))
    if TALK_TO_US_ABOVE and asked > TALK_TO_US_ABOVE:
        return Quote(users=asked, per_user=0, total=0, band=None,
                     too_big=True, asked_for=asked)

    best_users, (best_total, best_band) = asked, _straight(asked)
    if best_band is None:                               # pragma: no cover
        return Quote(users=asked, per_user=0, total=0, band=None, asked_for=asked)

    # The only sizes worth comparing are the first one in each larger band:
    # inside a band the price only rises with the count.
    for band in BANDS:
        if band.up_to and band.up_to < asked:
            continue
        start = _first_size_in(band)
        if start <= asked or (TALK_TO_US_ABOVE and start > TALK_TO_US_ABOVE):
            continue
        total, _ = _straight(start)
        if total < best_total:
            best_users, best_total, best_band = start, total, band

    return Quote(users=best_users, per_user=best_band.per_user,
                 total=best_total, band=best_band, asked_for=asked)


def _first_size_in(band: Band) -> int:
    """The smallest number of users that falls into this band."""
    smaller = [b.up_to for b in BANDS if b.up_to and b.up_to < (band.up_to or 10 ** 9)]
    return (max(smaller) + 1) if smaller else MINIMUM_USERS


def money(minor: int) -> str:
    """A price written the way this seller writes prices.

    Deliberately not the customer's own money formatter: what a licence costs
    is quoted in the seller's currency, and a Kenyan customer being shown the
    price in shillings because that is how they keep their books would be
    quoted a number nobody is going to honour.
    """
    whole, part = divmod(int(minor), 100)
    return f"{SYMBOL}{whole:,}.{part:02d}"


def per_month(yearly_minor: int) -> int:
    """The monthly figure behind a yearly price.

    Shown beside the yearly one because a monthly rate is what people compare
    and a yearly total is what they pay. Quoting only the first would surprise
    somebody at checkout; quoting only the second makes the software look
    twelve times more expensive than it is.
    """
    return int(yearly_minor) // MONTHS_IN_A_YEAR


def price_table() -> list[dict]:
    """The bands, ready to put on a screen."""
    return [
        {"label": band.label or _describe(band),
         "per_user": band.per_user,
         "per_user_text": money(band.per_user),
         "per_month": per_month(band.per_user),
         "per_month_text": money(per_month(band.per_user))}
        for band in BANDS
    ]


def _describe(band: Band) -> str:
    return "any number of users" if band.up_to == 0 else f"up to {band.up_to} users"


def can_quote() -> bool:
    """Whether this build has prices in it at all."""
    return bool(BANDS) and all(band.per_user > 0 for band in BANDS)


def payment_ready() -> bool:
    """Whether a customer could actually pay from what is filled in here."""
    return bool(GATEWAYS) or bank_shown()


def bank_shown() -> bool:
    return bool(BANK.get("show") and BANK.get("bank") and BANK.get("account_number"))


def ways_to_pay(total_minor: int, reference: str, users: int) -> list[dict]:
    """Every gateway, with its link already carrying the amount and reference."""
    return [
        {"name": g.name, "note": g.note,
         "url": g.link_for(total_minor, reference, users)}
        for g in GATEWAYS
    ]


def how_to_reach_us() -> list[tuple[str, str]]:
    """Contact details worth showing, in the order a person would try them."""
    rows: list[tuple[str, str]] = []
    if SELLER.get("whatsapp"):
        rows.append(("WhatsApp", SELLER["whatsapp"]))
    if SELLER.get("phone") and SELLER.get("phone") != SELLER.get("whatsapp"):
        rows.append(("Phone", SELLER["phone"]))
    if SELLER.get("email"):
        rows.append(("Email", SELLER["email"]))
    if SELLER.get("website"):
        rows.append(("Website", SELLER["website"]))
    return rows
