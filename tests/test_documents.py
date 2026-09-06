"""The documents a customer reads must be finished before they can be shipped.

This exists because of a real incident. The licence agreement was drafted with
a block at the top addressed to the seller — "READ THIS FIRST, AS THE SELLER",
followed by a warning that it was not legal advice — and nine [SQUARE BRACKET]
placeholders through the body. The installer displays that file verbatim on its
licence page.

So every person who ran the installer read a private note to the seller, and a
contract naming [YOUR BUSINESS NAME], before they had installed anything. It
was found by the seller installing his own product, which is a lucky way to
find it and not a repeatable one.

Two documents had a second problem: the agreement says at clause 14 that
REFUNDS.txt and PRIVACY.txt form part of it, and the installer shipped neither.
An agreement that incorporates documents nobody receives is not much of an
agreement.

These tests are cheap and they close both holes for good.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent

#: Everything a customer can end up reading. Adding a document here is how it
#: gets protected; forgetting to is how this happens again, so the last test
#: checks that the installer ships nothing that is not on this list.
CUSTOMER_FACING = (
    "LICENCE-AGREEMENT.txt",
    "REFUNDS.txt",
    "PRIVACY.txt",
    "LICENCE.txt",
    "INSTALL.txt",
    "README.md",
    "installer/after-install.txt",
)

#: A placeholder is an all-capitals phrase in square brackets — [YOUR EMAIL],
#: [DATE], [SEVEN YEARS]. Ordinary prose does not look like that, and neither
#: does Markdown, whose [links](...) are followed by a bracket.
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9 ()/.,'-]{2,}\](?!\()")

#: The marker the drafts used for notes addressed to the seller.
SELLER_NOTE = ">>>"


def read(name: str) -> str:
    return (SOURCE / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", CUSTOMER_FACING)
def test_no_placeholder_is_left_in_a_document_a_customer_reads(name):
    found = PLACEHOLDER.findall(read(name))
    assert not found, (
        f"{name} still has placeholders that would be shown to a customer: "
        f"{found}. Fill them in — this file is displayed by the installer or "
        f"installed alongside the program.")


@pytest.mark.parametrize("name", CUSTOMER_FACING)
def test_no_note_to_the_seller_survives_into_a_document_a_customer_reads(name):
    offenders = [line.strip() for line in read(name).splitlines()
                 if line.lstrip().startswith(SELLER_NOTE)]
    assert not offenders, (
        f"{name} still contains a note addressed to the seller: "
        f"{offenders[:2]}. The customer reads this file.")


@pytest.mark.parametrize("name", CUSTOMER_FACING)
def test_every_document_says_who_it_is_from(name):
    """A contract with no party named is not a contract."""
    if name in ("INSTALL.txt", "README.md", "installer/after-install.txt"):
        pytest.skip("guidance, not part of the agreement")
    text = read(name)
    assert "Tavo Networks Limited" in text, f"{name} does not name the seller"
    assert "RC 8237044" in text, (
        f"{name} does not carry the registration number, which is how a "
        f"customer checks the company is real")


def test_the_agreement_does_not_point_at_documents_nobody_receives():
    """Clause 14 names three documents. The installer must ship all three."""
    installer = read("installer/NexoraBooks.iss")
    for named in ("LICENCE-AGREEMENT.txt", "REFUNDS.txt", "PRIVACY.txt"):
        assert named in installer, (
            f"the agreement says {named} forms part of it, but the installer "
            f"does not put it on the customer's computer")


def test_the_mit_licence_is_not_sitting_beside_the_commercial_one():
    """It gave the software away to anybody holding a copy."""
    assert not (SOURCE / "LICENSE-MIT-old.txt").exists(), (
        "LICENSE-MIT-old.txt is back. Shipping an MIT licence beside a "
        "commercial one is how software gets given away by accident.")


def test_the_publisher_name_is_the_company_that_sells_it():
    """Windows shows this on the security prompt, and it has to match the
    name on the code-signing certificate exactly."""
    installer = read("installer/NexoraBooks.iss")
    assert '#define AppPublisher   "Tavo Networks Limited"' in installer


def test_the_seller_details_in_the_software_match_the_documents():
    """The buying screen and the agreement must not disagree about who to pay
    or where to write."""
    from app import store

    assert store.SELLER["business"] == "Tavo Networks Limited"
    assert "example.com" not in store.SELLER["email"]
    assert store.SELLER["email"] in read("REFUNDS.txt")
    assert store.SELLER["phone"].replace(" ", "") in \
        read("REFUNDS.txt").replace(" ", "")


# --------------------------------------------------------------------------
# The way people pay
# --------------------------------------------------------------------------
#
# A broken payment link is worse than no payment link: the screen says "pay
# here", the customer clicks, and the sale is lost somewhere neither party can
# see. The fallback when no gateway is configured is honest ("get in touch"),
# so an empty list is safe and a wrong list is not.


def test_a_payment_link_is_a_real_link_or_there_is_none():
    from app import store

    for gateway in store.GATEWAYS:
        assert gateway.url.startswith("https://"), (
            f"{gateway.name}: a checkout link must be https")
        for placeholder in ("example.com", "YOUR", "your-page", "xxx", "TODO"):
            assert placeholder.lower() not in gateway.url.lower(), (
                f"{gateway.name}: the checkout link still has a placeholder in "
                f"it ({placeholder}). Leave GATEWAYS empty rather than shipping "
                f"a link that goes nowhere.")


def test_the_amount_reaches_the_checkout_page():
    """Whatever placeholder style the provider wants, a number must arrive."""
    from app import store

    if not store.GATEWAYS:
        pytest.skip("no gateway configured, which is a safe state")
    quote = store.quote(3)
    for gateway in store.GATEWAYS:
        link = gateway.link_for(quote.total, "ABCD1234", quote.users)
        assert "{" not in link, f"{gateway.name}: unsubstituted placeholder in {link}"
        assert str(quote.total // 100) in link or str(quote.total) in link, (
            f"{gateway.name}: the price does not appear in the checkout link")


def test_the_three_ways_of_writing_an_amount_do_not_drift():
    """A hundredfold error in either direction is the failure mode here."""
    from app import store

    g = store.Gateway(name="t", url="{amount}|{amount_whole}|{amount_minor}")
    assert g.link_for(96_000_000, "R", 10) == "960000.00|960000|96000000"
