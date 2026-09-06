"""One set of books, one currency — whichever currency that is.

A business in Lagos keeps naira, one in Tokyo keeps yen and one in Kuwait City
keeps dinars. The accounting is identical; what differs is how many minor units
make a major one, and how the figure is written down. These tests hold that
distinction still, because getting the yen wrong by a factor of a hundred is
not a rounding error, it is a wrong set of accounts.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-cur-")

from fastapi.testclient import TestClient  # noqa: E402

from app import countries, currency, db as dbmod, prefs  # noqa: E402
from app import companies as registry  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company  # noqa: E402
from app.money import (  # noqa: E402
    allocate,
    fmt,
    fmt_plain,
    pct_of,
    to_major,
    to_minor,
)
from app.seed import bootstrap  # noqa: E402

NGN = currency.preset("NGN")
JPY = currency.preset("JPY")
KWD = currency.preset("KWD")
USD = currency.preset("USD")
BRL = currency.preset("BRL")
SEK = currency.preset("SEK")
GHS = currency.preset("GHS")


# --------------------------------------------------------------------------
# How many minor units make one
# --------------------------------------------------------------------------


def test_the_naira_has_a_hundred_kobo():
    assert to_minor("1", NGN) == 100
    assert to_minor("12,500.75", NGN) == 1250075


def test_the_yen_has_no_smaller_unit():
    """¥1,500 is 1500, not 150000. This is the one that ruins a ledger."""
    assert to_minor("1500", JPY) == 1500
    assert to_minor("1,500", JPY) == 1500
    assert to_major(1500, JPY) == 1500


def test_the_dinar_has_a_thousand_fils():
    assert to_minor("12.345", KWD) == 12345
    assert to_minor("1", KWD) == 1000
    assert fmt(12345, cur=KWD) == "KD 12.345"


def test_a_plain_integer_means_major_units_in_every_currency():
    assert to_minor(5, NGN) == 500
    assert to_minor(5, JPY) == 5
    assert to_minor(5, KWD) == 5000


# --------------------------------------------------------------------------
# Telling a thousands separator from a decimal point
# --------------------------------------------------------------------------


def test_a_nigerian_typing_a_comma_means_thousands():
    assert to_minor("1,500", NGN) == 150000        # one and a half thousand naira


def test_a_nigerian_typing_a_full_stop_means_kobo():
    assert to_minor("1.50", NGN) == 150            # one naira fifty


def test_a_brazilian_means_the_opposite_by_the_same_characters():
    assert to_minor("1.500", BRL) == 150000        # mil e quinhentos
    assert to_minor("1,50", BRL) == 150


def test_both_separators_together_are_unambiguous():
    assert to_minor("1,234.56", NGN) == 123456
    assert to_minor("1.234,56", BRL) == 123456


def test_a_repeated_separator_is_always_grouping():
    assert to_minor("10,000,000", NGN) == 1000000000
    assert to_minor("10.000.000", BRL) == 1000000000


def test_the_symbol_and_spaces_are_ignored():
    assert to_minor("₦ 1,200.50", NGN) == 120050
    assert to_minor("1 234,56", SEK) == 123456     # Swedish spacing


def test_brackets_and_a_minus_sign_both_mean_negative():
    assert to_minor("(1,200.00)", NGN) == -120000
    assert to_minor("-1,200.00", NGN) == -120000


def test_nothing_at_all_is_zero_not_an_error():
    for value in (None, "", "   ", "abc"):
        assert to_minor(value, NGN) == 0


# --------------------------------------------------------------------------
# Writing it back out
# --------------------------------------------------------------------------


def test_the_naira_still_looks_exactly_as_it_always_did():
    assert fmt(1250075, cur=NGN) == "₦12,500.75"
    assert fmt(-1250075, cur=NGN) == "(₦12,500.75)"
    assert fmt(0, blank_zero=True, cur=NGN) == ""


def test_a_currency_with_no_fraction_shows_none():
    assert fmt(1500, cur=JPY) == "¥1,500"
    assert "." not in fmt(1500, cur=JPY)


def test_the_symbol_can_sit_after_the_figure():
    assert fmt(123456, cur=SEK) == "1 234,56 kr"


def test_the_bare_figure_carries_no_symbol_and_no_stray_space():
    assert fmt(123456, symbol="", cur=SEK) == "1 234,56"
    assert fmt(1250075, symbol="", cur=NGN) == "12,500.75"


def test_export_is_written_for_a_machine_not_a_person():
    """A spreadsheet wants a full stop and no grouping, whatever the locale."""
    assert fmt_plain(123456, SEK) == "1234.56"
    assert fmt_plain(-123456, BRL) == "-1234.56"
    assert fmt_plain(1500, JPY) == "1500"
    assert fmt_plain(12345, KWD) == "12.345"


# --------------------------------------------------------------------------
# The invariant that matters
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", sorted(currency.PRESETS))
def test_every_currency_can_read_back_what_it_wrote(code):
    """Format an amount, type it back in, get the same amount.

    This is the property that keeps editable money cells honest: the grid
    prints a figure with the company's own separators, the user changes one
    digit, and the parser has to understand what came back. If a currency's
    grouping character and its decimal point could ever be confused, this is
    where it shows up — in all of them at once, rather than in one customer's
    accounts six months from now.
    """
    cur = currency.PRESETS[code]
    for amount in (0, 1, 7, 99, 100, 1500, 123456, 99999999, -123456, -5):
        written = fmt(amount, symbol="", cur=cur)
        assert to_minor(written, cur) == amount, f"{code}: {written!r}"


@pytest.mark.parametrize("code", sorted(currency.PRESETS))
def test_the_symbol_never_confuses_the_parser(code):
    cur = currency.PRESETS[code]
    for amount in (123456, 1000000, -7500):
        assert to_minor(fmt(amount, cur=cur), cur) == amount


def test_arithmetic_does_not_care_about_the_scale():
    """Percentages and allocations work on minor units, whatever they are."""
    assert pct_of(100_00, "7.5") == 750
    assert allocate(1000, [1, 1, 1]) == [334, 333, 333]
    assert sum(allocate(9999, [5, 3, 2])) == 9999


# --------------------------------------------------------------------------
# Which currency is in force
# --------------------------------------------------------------------------


def test_outside_a_request_the_naira_is_assumed():
    assert currency.active().code == "NGN"
    assert fmt(1250075) == "₦12,500.75"


def test_a_block_can_borrow_another_currency_and_give_it_back():
    with currency.using(JPY):
        assert fmt(1500) == "¥1,500"
        assert to_minor("1,500") == 1500
    assert currency.active().code == "NGN"
    assert fmt(1500) == "₦15.00"


def test_the_currency_is_read_off_the_company_row():
    class Row:
        currency_code = "GHS"
        currency_symbol = "GH₵"
        currency_decimals = 2
        currency_symbol_after = False
        currency_thousands = ","
        currency_point = "."

    spec = currency.from_company(Row())
    assert spec.code == "GHS"
    assert fmt(500000, cur=spec) == "GH₵5,000.00"


def test_a_customised_symbol_beats_the_preset():
    """Somebody who writes their currency differently is not overruled."""
    class Row:
        currency_code = "USD"
        currency_symbol = "US$"
        currency_decimals = 2
        currency_symbol_after = False
        currency_thousands = ","
        currency_point = "."

    assert fmt(150000, cur=currency.from_company(Row())) == "US$1,500.00"


def test_an_unknown_currency_code_falls_back_rather_than_failing():
    class Row:
        currency_code = "ZZZ"
        currency_symbol = "?"
        currency_decimals = None
        currency_symbol_after = False
        currency_thousands = ","
        currency_point = "."

    spec = currency.from_company(Row())
    assert spec.decimals == 2               # the naira's, not a crash


def test_a_nonsense_number_of_decimals_is_ignored():
    class Row:
        currency_code = "NGN"
        currency_symbol = "₦"
        currency_decimals = 7
        currency_symbol_after = False
        currency_thousands = ","
        currency_point = "."

    assert currency.from_company(Row()).decimals == 2


# --------------------------------------------------------------------------
# Countries: wording, not rules
# --------------------------------------------------------------------------


def test_every_country_names_a_currency_this_application_knows():
    for c in countries.COUNTRIES:
        assert currency.preset(c.currency) is not None, c.name


def test_every_country_offers_a_date_format_the_settings_screen_lists():
    for c in countries.COUNTRIES:
        assert c.date_format in {f for f, _ in prefs.DATE_FORMATS}, c.name


def test_an_unknown_country_gets_neutral_wording_not_nigerian_wording():
    c = countries.get("XX")
    assert c.tax_id_label == "Tax ID"
    assert c.reg_no_label == "Registration number"
    assert "NRS" not in c.tax_authority


def test_choosing_a_country_sets_the_wording_and_the_currency():
    class Row:
        pass

    row = Row()
    countries.apply_to(row, countries.get("KE"))
    assert row.tax_label == "VAT"
    assert row.tax_id_label == "KRA PIN"
    assert row.tax_authority == "KRA"
    assert row.currency_code == "KES"


def test_correcting_the_country_later_leaves_the_currency_alone():
    """Labels are free to change. The currency is not, once figures exist."""
    class Row:
        currency_code = "NGN"

    row = Row()
    countries.apply_to(row, countries.get("GB"), wording_only=True)
    assert row.tax_authority == "HMRC"
    assert row.currency_code == "NGN"


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-curweb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def save_company(client, **over):
    data = {
        "name": "Sakura Trading KK", "country_code": "JP",
        "currency_code": "JPY", "currency_symbol": "¥",
        "tax_label": "Consumption Tax", "tax_id_label": "Corporate number",
        "reg_no_label": "Corporate number", "tax_authority": "the NTA",
        "date_format": "%Y-%m-%d",
        "fiscal_year_start_month": "4", "vat_rate": "10",
        "annual_turnover_band": "ABOVE_50M", "default_payment_terms_days": "30",
    }
    data.update(over)
    return client.post("/settings/company", data=data, follow_redirects=True)


def test_a_company_can_be_set_up_in_yen(client):
    assert save_company(client).status_code == 200
    page = client.get("/settings/company").text
    assert 'value="JPY" selected' in page or 'value="JPY"  selected' in page
    assert "Sakura Trading KK" in page


def test_the_countrys_wording_replaces_the_nigerian_wording(client):
    save_company(client)
    page = client.get("/settings/company").text
    assert 'id="lbl-tin">Corporate number</label>' in page
    assert 'id="lbl-rc">Corporate number</label>' in page
    assert 'id="lbl-vr">Consumption Tax registration number</label>' in page


def test_amounts_typed_in_are_read_as_yen(client):
    save_company(client, requisition_limit="50,000")
    with dbmod.session_scope_for(registry.default_slug()) as db:
        company = db.get(Company, 1)
        # ¥50,000 is 50000 minor units. Read as naira it would be 5,000,000.
        assert company.requisition_limit == 50000


def _accounts():
    from sqlalchemy import select

    from app.models import Account

    with dbmod.session_scope_for(registry.default_slug()) as db:
        rent = db.scalar(select(Account).where(Account.code == "6100"))
        bank = db.scalar(select(Account).where(Account.code == "1020"))
        return rent.id, bank.id


def test_the_currency_is_offered_until_the_first_entry(client):
    save_company(client)
    assert "is now fixed for these books" not in client.get("/settings/company").text


def test_yen_reach_the_reports_without_a_fictional_fraction(client):
    """¥250,000 is ¥250,000 — not ¥2,500.00, which is what a hardcoded
    two-decimal scale would have made of the very same stored integer."""
    save_company(client)
    rent, bank = _accounts()
    r = client.post("/journals/save", data={
        "date": date.today().isoformat(), "memo": "Office rent",
        "reference": "RENT-1",
        "line_account": [str(rent), str(bank)],
        "line_debit": ["250,000", ""],
        "line_credit": ["", "250,000"],
        "line_memo": ["Rent", "Paid"],
        "line_contact": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert r.status_code == 200

    page = client.get("/reports/trial-balance").text
    assert "¥250,000" in page
    assert "250,000.00" not in page
    assert "₦" not in page


def test_the_currency_is_fixed_once_something_is_posted(client):
    page = client.get("/settings/company").text
    assert "is now fixed for these books" in page


def test_the_locked_currency_cannot_be_changed_by_posting_the_form(client):
    """The screen hides it; the route has to refuse it as well."""
    save_company(client, currency_code="USD", currency_symbol="$")
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.get(Company, 1).currency_code == "JPY"


def test_the_wording_can_still_be_corrected_after_the_books_are_open(client):
    save_company(client, tax_authority="The National Tax Agency")
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.get(Company, 1).tax_authority == "The National Tax Agency"


def test_dates_are_written_the_way_the_company_asked(client):
    from app.main import date_fmt

    prefs.set_date_format("%Y-%m-%d")
    assert date_fmt(date(2026, 8, 23)) == "2026-08-23"
    prefs.set_date_format("%d %b %Y")
    assert date_fmt(date(2026, 8, 23)) == "23 Aug 2026"
