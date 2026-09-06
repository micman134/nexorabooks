"""Adding several companies up into one set of figures.

The arithmetic here is the kind that looks right and is wrong: a group whose
revenue counts the same sale twice because one member sold it to another, or a
balance sheet that balances only because a translation difference was quietly
pushed into an asset. So the tests below check the awkward parts on purpose —
what the group owes itself, what the members disagree about, and what happens
when one of them keeps its books in another currency.

Everything is read-only. The last test in the file proves it: after a
consolidation, the member companies are byte for byte what they were.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-group-")

from app import companies as registry  # noqa: E402
from app import currency as currency_mod  # noqa: E402
from app import db as dbmod  # noqa: E402
from app import group as group_mod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    Bill,
    BillLine,
    Company,
    Contact,
    Invoice,
    InvoiceLine,
)
from app.money import to_minor as M  # noqa: E402
from app.services import consolidation as C  # noqa: E402
from app.services import documents  # noqa: E402
from app.services.posting import next_number, sys_account  # noqa: E402

START = date(2026, 1, 1)
END = date(2026, 12, 31)


@pytest.fixture()
def home():
    """A data folder with nothing in it, so the group is ours to build."""
    tmp = tempfile.mkdtemp(prefix="nexora-group-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Building a group to look at
# --------------------------------------------------------------------------


def make_company(name: str, currency_code: str = "NGN") -> str:
    ref = registry.create(name)
    with dbmod.session_scope_for(ref.slug) as db:
        company = db.get(Company, 1)
        company.name = name
        spec = currency_mod.preset(currency_code)
        company.currency_code = spec.code
        company.currency_symbol = spec.symbol
        company.currency_decimals = spec.decimals
    return ref.slug


def customer(slug: str, name: str) -> int:
    with dbmod.session_scope_for(slug) as db:
        contact = Contact(code=next_number(db, "CONTACT"), name=name,
                          is_customer=True, is_vendor=True,
                          payment_terms_days=30)
        db.add(contact)
        db.flush()
        return contact.id


def sell(slug: str, contact_id: int, amount: int, on: date = date(2026, 6, 1),
         paid: bool = False) -> None:
    with dbmod.session_scope_for(slug) as db:
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=contact_id, date=on,
                      due_date=on + timedelta(days=30), status=DRAFT)
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Goods",
                           qty=1000, unit_price=amount,
                           account_id=sys_account(db, "SALES").id))
        db.flush()
        db.refresh(inv)
        documents.recalc_invoice(db, inv)
        documents.post_invoice(db, inv)


def buy(slug: str, contact_id: int, amount: int, on: date = date(2026, 6, 1)) -> None:
    with dbmod.session_scope_for(slug) as db:
        bill = Bill(number=next_number(db, "BILL"), doc_type="BILL",
                    contact_id=contact_id, date=on,
                    due_date=on + timedelta(days=30), status=DRAFT)
        db.add(bill)
        db.flush()
        db.add(BillLine(bill_id=bill.id, line_no=1, description="Goods",
                        qty=1000, unit_price=amount,
                        account_id=sys_account(db, "PURCHASES").id))
        db.flush()
        db.refresh(bill)
        documents.recalc_bill(db, bill)
        documents.post_bill(db, bill)


def a_group(*slugs: str, currency: str = "NGN") -> group_mod.Group:
    group = group_mod.load()
    group.name = "Adeyemi Group"
    group.currency = currency
    for member in group.members:
        member.include = member.slug in slugs
    group_mod.save(group)
    return group_mod.load()


@pytest.fixture()
def two(home):
    """Trading Ltd sells to Logistics Ltd; both also sell to the outside world."""
    trading = make_company("Adeyemi Trading Ltd")
    logistics = make_company("Adeyemi Logistics Ltd")

    outside_a = customer(trading, "Zenith Construction Ltd")
    outside_b = customer(logistics, "Dangote Cement Plc")
    sell(trading, outside_a, M("10,000,000"))
    sell(logistics, outside_b, M("4,000,000"))

    each_other_a = customer(trading, "Adeyemi Logistics Ltd")
    each_other_b = customer(logistics, "Adeyemi Trading Ltd")
    sell(trading, each_other_a, M("2,000,000"))
    buy(logistics, each_other_b, M("2,000,000"))

    group = a_group(trading, logistics)
    group.member(trading).internal = {str(each_other_a): logistics}
    group.member(logistics).internal = {str(each_other_b): trading}
    group_mod.save(group)
    return group_mod.load(), trading, logistics


# --------------------------------------------------------------------------
# The settings
# --------------------------------------------------------------------------


def test_a_new_company_appears_in_the_group_switched_off(home):
    first = make_company("First Ltd")
    a_group(first)
    second = make_company("Second Ltd")

    group = group_mod.load()
    assert group.member(second) is not None
    assert group.member(second).include is False, (
        "a company must never be consolidated because somebody forgot to look")
    assert group.member(first).include is True


def test_a_group_of_one_is_not_a_group(home):
    only = make_company("Only Ltd")
    assert a_group(only).is_set_up is False


def test_a_rate_that_would_wipe_a_company_out_is_refused():
    assert group_mod.rate_of("0") == 1
    assert group_mod.rate_of("-2") == 1
    assert group_mod.rate_of("nonsense") == 1
    assert group_mod.rate_of("") == 1
    assert float(group_mod.rate_of("0.0125")) == pytest.approx(0.0125)
    assert float(group_mod.rate_of("1,250.5")) == pytest.approx(1250.5)


def test_the_settings_survive_being_written_and_read_back(home):
    first = make_company("First Ltd")
    second = make_company("Second Ltd")
    group = a_group(first, second)
    group.member(second).closing_rate = "0.0125"
    group.member(second).internal = {"7": first}
    group_mod.save(group)

    again = group_mod.load()
    assert again.name == "Adeyemi Group"
    assert float(again.member(second).closing) == pytest.approx(0.0125)
    assert again.member(second).internal == {"7": first}


# --------------------------------------------------------------------------
# Adding the companies up
# --------------------------------------------------------------------------


def test_revenue_is_the_two_companies_added_together(two):
    group, trading, logistics = two
    out = C.build(group, START, END)
    # 10m + 4m outside, and the 2m sold inside the group taken out.
    assert out.revenue.combined == M("16,000,000")
    assert out.revenue.eliminated == M("2,000,000")
    assert out.revenue.total == M("14,000,000")


def test_what_the_group_sold_itself_is_taken_out_of_costs_as_well(two):
    """Otherwise the group's profit changes when it moves goods between its own
    companies, which would be nonsense."""
    group, trading, logistics = two
    out = C.build(group, START, END)
    assert out.cogs.eliminated + out.expenses.eliminated == M("2,000,000")
    assert out.net_profit == M("14,000,000")


def test_what_the_group_owes_itself_is_taken_out_of_both_sides(two):
    group, trading, logistics = two
    out = C.build(group, START, END)
    assert out.current_assets.eliminated == M("2,000,000")
    assert out.current_liabilities.eliminated == M("2,000,000")
    assert out.total_assets == out.current_assets.combined \
        + out.fixed_assets.combined - M("2,000,000")


def test_the_group_balance_sheet_balances(two):
    group, trading, logistics = two
    out = C.build(group, START, END)
    assert out.balances_ok, out.difference


def test_each_company_can_still_be_seen_on_its_own_line(two):
    group, trading, logistics = two
    out = C.build(group, START, END)
    revenue = out.revenue
    assert revenue.of(trading) == M("12,000,000")
    assert revenue.of(logistics) == M("4,000,000")


def test_a_group_that_has_marked_nothing_internal_says_so(home):
    trading = make_company("Trading Ltd")
    logistics = make_company("Logistics Ltd")
    other = customer(trading, "Logistics Ltd")
    sell(trading, other, M("2,000,000"))
    group = a_group(trading, logistics)

    out = C.build(group, START, END)
    assert out.revenue.eliminated == 0
    assert any("count that trade twice" in note for note in out.notes)


# --------------------------------------------------------------------------
# When the two sides do not agree
# --------------------------------------------------------------------------


def test_only_the_agreed_part_is_eliminated(home):
    """A sale one side has posted and the other has not is a real error. It
    must survive consolidation, not be tidied away by it."""
    trading = make_company("Trading Ltd")
    logistics = make_company("Logistics Ltd")
    theirs = customer(trading, "Logistics Ltd")
    ours = customer(logistics, "Trading Ltd")
    sell(trading, theirs, M("2,000,000"))
    buy(logistics, ours, M("1,200,000"))          # 800,000 never posted

    group = a_group(trading, logistics)
    group.member(trading).internal = {str(theirs): logistics}
    group.member(logistics).internal = {str(ours): trading}
    group_mod.save(group)

    out = C.build(group_mod.load(), START, END)
    assert out.revenue.eliminated == M("1,200,000")
    assert out.mismatches
    worst = max(out.mismatches, key=lambda m: abs(m.difference))
    assert abs(worst.difference) == M("800,000")
    assert any("do not agree" in note for note in out.notes)


def test_a_disagreement_is_reported_once_not_twice(home):
    trading = make_company("Trading Ltd")
    logistics = make_company("Logistics Ltd")
    theirs = customer(trading, "Logistics Ltd")
    ours = customer(logistics, "Trading Ltd")
    sell(trading, theirs, M("2,000,000"))
    buy(logistics, ours, M("1,200,000"))

    group = a_group(trading, logistics)
    group.member(trading).internal = {str(theirs): logistics}
    group.member(logistics).internal = {str(ours): trading}
    group_mod.save(group)

    out = C.build(group_mod.load(), START, END)
    traded = [m for m in out.mismatches if m.what == "traded between them"]
    assert len(traded) == 1


# --------------------------------------------------------------------------
# Another currency
# --------------------------------------------------------------------------


def test_a_member_in_another_currency_is_translated(home):
    naira = make_company("Lagos Trading Ltd", "NGN")
    cedis = make_company("Accra Trading Ltd", "GHS")
    sell(naira, customer(naira, "Zenith Ltd"), M("10,000,000"))
    sell(cedis, customer(cedis, "Accra Stores"), M("100,000"))

    group = a_group(naira, cedis)
    group.member(cedis).closing_rate = "120"
    group.member(cedis).average_rate = "120"
    group_mod.save(group)

    out = C.build(group_mod.load(), START, END)
    assert out.currency.code == "NGN"
    assert out.revenue.of(cedis) == M("12,000,000")
    assert out.revenue.total == M("22,000,000")


def test_a_currency_with_a_different_number_of_kobo_is_not_out_by_a_hundred(home):
    """A thousand naira and a thousand yen are not the same number of anything."""
    naira = make_company("Lagos Ltd", "NGN")
    yen = make_company("Tokyo Ltd", "JPY")
    sell(naira, customer(naira, "Zenith Ltd"), M("1,000,000"))
    with currency_mod.using(currency_mod.preset("JPY")):
        sell(yen, customer(yen, "Tokyo Stores"), 1_000_000)     # ¥1,000,000

    group = a_group(naira, yen)
    group.member(yen).closing_rate = "10"
    group.member(yen).average_rate = "10"
    group_mod.save(group)

    out = C.build(group_mod.load(), START, END)
    assert out.revenue.of(yen) == M("10,000,000")


def test_translating_at_two_rates_leaves_a_difference_that_is_named(home):
    naira = make_company("Lagos Ltd", "NGN")
    cedis = make_company("Accra Ltd", "GHS")
    sell(naira, customer(naira, "Zenith Ltd"), M("10,000,000"))
    sell(cedis, customer(cedis, "Accra Stores"), M("100,000"))

    group = a_group(naira, cedis)
    group.member(cedis).average_rate = "120"
    group.member(cedis).closing_rate = "150"        # the cedi moved
    group_mod.save(group)

    out = C.build(group_mod.load(), START, END)
    assert out.translation[cedis] != 0
    assert out.balances_ok, out.difference
    assert any("translation difference" in note for note in out.notes)


def test_the_group_reports_in_the_currency_it_was_told_to(home):
    naira = make_company("Lagos Ltd", "NGN")
    cedis = make_company("Accra Ltd", "GHS")
    group = a_group(naira, cedis, currency="GHS")
    out = C.build(group, START, END)
    assert out.currency.code == "GHS"


# --------------------------------------------------------------------------
# Guessing which contact is which company
# --------------------------------------------------------------------------


def test_a_contact_named_like_another_member_is_suggested(two):
    group, trading, logistics = two
    found = C.suggest_internal(trading, group.chosen)
    assert logistics in found.values()


def test_the_outside_world_is_not_suggested(two):
    group, trading, logistics = two
    found = C.suggest_internal(trading, group.chosen)
    with dbmod.session_scope_for(trading) as db:
        outside = db.query(Contact).filter(
            Contact.name == "Zenith Construction Ltd").one()
    assert str(outside.id) not in found


def test_a_name_written_slightly_differently_is_still_found(home):
    first = make_company("Adeyemi Trading Limited")
    second = make_company("Adeyemi Logistics Ltd")
    customer(first, "Adeyemi Logistics Limited")     # "Limited", not "Ltd"
    group = a_group(first, second)
    assert second in C.suggest_internal(first, group.chosen).values()


# --------------------------------------------------------------------------
# It only ever reads
# --------------------------------------------------------------------------


def contents(slug: str) -> str:
    """Everything in one company's database, as text."""
    import sqlite3

    con = sqlite3.connect(registry.company_db(slug))
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def test_consolidating_changes_nothing_in_any_member_company(two):
    """The whole design rests on this: a group view is a way of looking at the
    books, not a transaction in them."""
    group, trading, logistics = two
    dbmod.reset_all()
    before = {slug: contents(slug) for slug in (trading, logistics)}

    C.build(group, START, END)
    dbmod.reset_all()

    for slug, was in before.items():
        assert contents(slug) == was, (
            f"{slug} was written to during a consolidation")


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(two):
    """Signed in, with the two-company group of the fixture above in place."""
    from fastapi.testclient import TestClient

    from app.main import app

    dbmod.reset_all()
    with TestClient(app) as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123",
                                   "next": "/"}, follow_redirects=True)
        assert r.status_code == 200
        yield c, two


def test_the_group_screen_shows_the_combined_figures(client):
    c, (group, trading, logistics) = client
    r = c.get(f"/group?start={START}&end={END}", follow_redirects=True)
    assert r.status_code == 200
    assert "Adeyemi Group" in r.text
    assert "Profit &amp; loss for the group" in r.text
    assert "Adeyemi Trading Ltd" in r.text and "Adeyemi Logistics Ltd" in r.text


def test_the_group_screen_says_what_it_took_out(client):
    c, _ = client
    r = c.get(f"/group?start={START}&end={END}", follow_redirects=True)
    assert "sold inside the group and taken out" in r.text
    assert "not a statutory consolidation" in r.text


def test_a_lone_company_gets_an_explanation_not_an_empty_report(home):
    from fastapi.testclient import TestClient

    from app.main import app

    make_company("Only Ltd")
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        r = c.get("/group", follow_redirects=True)
        assert r.status_code == 200
        assert "A group needs at least two companies" in r.text


def test_the_settings_screen_lists_every_company_and_its_contacts(client):
    c, (group, trading, logistics) = client
    r = c.get("/group/settings", follow_redirects=True)
    assert r.status_code == 200
    assert "Zenith Construction Ltd" in r.text
    assert "Companies that trade with each other" in r.text


def test_saving_the_settings_keeps_them(client):
    c, (group, trading, logistics) = client
    r = c.post("/group/settings", data={
        "name": "Ogunlesi Group", "currency": "GHS",
        f"include:{trading}": "on", f"include:{logistics}": "on",
        f"average:{logistics}": "16", f"closing:{logistics}": "17",
    }, follow_redirects=True)
    assert r.status_code == 200

    saved = group_mod.load()
    assert saved.name == "Ogunlesi Group"
    assert saved.currency == "GHS"
    assert saved.member(logistics).closing_rate == "17"


def test_a_company_can_be_taken_out_of_the_group_from_the_screen(client):
    c, (group, trading, logistics) = client
    c.post("/group/settings", data={"name": "Adeyemi Group", "currency": "NGN",
                                    f"include:{trading}": "on"},
           follow_redirects=True)
    saved = group_mod.load()
    assert saved.member(trading).include is True
    assert saved.member(logistics).include is False


def test_marking_a_contact_as_a_group_company_from_the_screen(client):
    c, (group, trading, logistics) = client
    with dbmod.session_scope_for(trading) as db:
        contact = db.query(Contact).filter(
            Contact.name == "Adeyemi Logistics Ltd").one()
        contact_id = contact.id
    dbmod.reset_all()

    r = c.post("/group/settings/internal", data={
        f"internal:{trading}:{contact_id}": logistics,
    }, follow_redirects=True)
    assert r.status_code == 200
    assert group_mod.load().member(trading).internal == {str(contact_id): logistics}


def test_a_company_cannot_be_marked_as_itself(client):
    c, (group, trading, logistics) = client
    with dbmod.session_scope_for(trading) as db:
        contact_id = db.query(Contact).filter(
            Contact.name == "Adeyemi Logistics Ltd").one().id
    dbmod.reset_all()

    c.post("/group/settings/internal",
           data={f"internal:{trading}:{contact_id}": trading},
           follow_redirects=True)
    assert group_mod.load().member(trading).internal == {}


def test_the_suggest_button_fills_the_mapping_in(client):
    c, (group, trading, logistics) = client
    saved = group_mod.load()
    for member in saved.members:
        member.internal = {}
    group_mod.save(saved)

    r = c.post("/group/settings/suggest", follow_redirects=True)
    assert r.status_code == 200
    assert "Check them before relying on the figures" in r.text
    assert group_mod.load().member(trading).internal


def test_the_csv_carries_the_group_column(client):
    c, _ = client
    r = c.get(f"/group/csv?start={START}&end={END}", follow_redirects=True)
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert "Total revenue" in body
    assert "Group profit for the period" in body
    assert "Note," in body


def test_an_ordinary_user_cannot_change_the_group_settings(client):
    """The settings decide what every figure on the group screen means."""
    c, _ = client
    from app.models import User
    from app.security import ROLE_VIEWER

    with dbmod.session_scope() as db:
        user = db.query(User).filter(User.username == "admin").one()
        user.role = ROLE_VIEWER
    r = c.get("/group/settings", follow_redirects=True)
    assert r.status_code == 403
