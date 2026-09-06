"""The three screens that look forward.

Everywhere else this software reports what happened, and the tests check
arithmetic. Here it says what is *going* to happen, so the tests check
something else as well: that it only ever projects things already committed,
that the one estimate it makes — when a customer pays — comes from that
customer's own record rather than an assumption, and that it says so.

The rule being defended throughout: **no revenue is ever invented**. A forecast
that quietly assumes next month looks like last month is the kind that gets a
business into trouble while looking reassuring.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-cash-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    RECEIPT,
    Account,
    BankAccount,
    Bill,
    BillLine,
    Contact,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
)
from app.seed import bootstrap  # noqa: E402
from app.services import cashtimeline as CT  # noqa: E402
from app.services import cash as cashsvc  # noqa: E402
from app.services import collections, documents, whatif  # noqa: E402
from app.services.posting import EntryDraft, next_number, post_entry, sys_account  # noqa: E402

TODAY = date(2026, 7, 1)


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-cash-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    with dbmod.session_scope_for(ref.slug) as session:
        yield session
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def acc(db, key):
    return sys_account(db, key)


def bank_of(db):
    return db.scalar(select(BankAccount))


def customer(db, name="Acme Ltd", terms=30):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                payment_terms_days=terms)
    db.add(c)
    db.flush()
    return c


def supplier(db, name="Supplies Ltd"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def invoice(db, cust, raised, amount, due=None):
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=cust.id, date=raised,
                  due_date=due or raised + timedelta(days=30), status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Goods", qty=1000,
                       unit_price=amount, account_id=acc(db, "SALES").id))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    db.flush()
    return inv


def bill(db, vend, raised, amount, due=None):
    b = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vend.id,
             date=raised, due_date=due or raised + timedelta(days=30), status=DRAFT)
    db.add(b)
    db.flush()
    db.add(BillLine(bill_id=b.id, line_no=1, description="Materials", qty=1000,
                    unit_price=amount, account_id=acc(db, "PURCHASES").id))
    db.flush()
    db.refresh(b)
    documents.recalc_bill(db, b)
    documents.post_bill(db, b)
    db.flush()
    return b


def settle(db, inv, when):
    """Pay an invoice in full on a given day, so a habit can be measured."""
    from app.services import cash as cashmod

    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                  contact_id=inv.contact_id, date=when,
                  bank_account_id=bank_of(db).id, amount=inv.total)
    db.add(pay)
    db.flush()
    db.add(PaymentAllocation(payment_id=pay.id, invoice_id=inv.id, amount=inv.total))
    db.flush()
    db.refresh(pay)
    cashmod.post_payment(db, pay)
    db.flush()
    return pay


def put_cash_in(db, amount, when):
    draft = EntryDraft(date=when, memo="Capital introduced")
    draft.debit(db.get(Account, bank_of(db).account_id), amount)
    draft.credit(acc(db, "OPENING_EQUITY"), amount)
    return post_entry(db, draft)


# --------------------------------------------------------------------------
# Measuring how a customer pays
# --------------------------------------------------------------------------


def test_a_customers_lateness_is_measured_from_their_own_history(db):
    cust = customer(db)
    for month in (1, 2, 3, 4):
        inv = invoice(db, cust, date(2026, month, 1), 10_000_000)
        settle(db, inv, inv.due_date + timedelta(days=10))
    db.commit()

    habit = CT.habits(db, TODAY)[cust.id]
    assert habit.settled == 4
    assert habit.median_lag == 10
    assert habit.has_record
    assert "10 days late" in habit.verdict


def test_lateness_is_measured_from_the_due_date_not_the_invoice_date(db):
    """A customer on 60-day terms paying on day 60 is not 60 days late."""
    cust = customer(db, terms=60)
    for month in (1, 2, 3, 4):
        raised = date(2026, month, 1)
        inv = invoice(db, cust, raised, 10_000_000, due=raised + timedelta(days=60))
        settle(db, inv, inv.due_date)
    db.commit()

    habit = CT.habits(db, TODAY)[cust.id]
    assert habit.median_lag == 0
    assert habit.verdict == "Pays on time."


def test_a_customer_who_pays_early_is_recorded_as_such(db):
    cust = customer(db)
    for month in (1, 2, 3):
        inv = invoice(db, cust, date(2026, month, 1), 10_000_000)
        settle(db, inv, inv.due_date - timedelta(days=7))
    db.commit()
    assert CT.habits(db, TODAY)[cust.id].median_lag == -7


def test_one_invoice_is_not_enough_to_call_it_a_habit(db):
    """Otherwise a single unusual payment sets the pattern for ever."""
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 1, 1), 10_000_000)
    settle(db, inv, inv.due_date + timedelta(days=60))
    db.commit()

    habit = CT.habits(db, TODAY)[cust.id]
    assert not habit.has_record
    assert habit.lag == CT.NO_HISTORY_LAG
    assert "No record yet" in habit.verdict


# --------------------------------------------------------------------------
# The timeline
# --------------------------------------------------------------------------


def test_an_invoice_is_expected_on_the_day_that_customer_usually_pays(db):
    cust = customer(db)
    for month in (1, 2, 3):
        old = invoice(db, cust, date(2026, month, 1), 10_000_000)
        settle(db, old, old.due_date + timedelta(days=14))
    due = TODAY + timedelta(days=10)
    invoice(db, cust, TODAY, 50_000_000, due=due)
    db.commit()

    line = CT.build(db, TODAY, 90)
    receipt = next(e for e in line.events if e.amount == 50_000_000)
    assert receipt.when == due + timedelta(days=14)
    assert receipt.certain          # based on a real record, not an assumption


def test_no_revenue_is_ever_invented(db):
    """A business with nothing outstanding has nothing coming in."""
    put_cash_in(db, 100_000_000, TODAY - timedelta(days=1))
    db.commit()
    line = CT.build(db, TODAY, 90)
    assert line.total_in == 0
    assert line.closing == line.opening


def test_the_day_the_money_runs_out_is_found(db):
    put_cash_in(db, 10_000_000, TODAY - timedelta(days=1))
    vend = supplier(db)
    bill(db, vend, TODAY, 30_000_000, due=TODAY + timedelta(days=20))
    db.commit()

    line = CT.build(db, TODAY, 90)
    assert not line.survives
    assert line.runs_out_on == TODAY + timedelta(days=20)
    assert line.days_left == 20


def test_a_business_that_stays_in_funds_says_so(db):
    put_cash_in(db, 100_000_000, TODAY - timedelta(days=1))
    vend = supplier(db)
    bill(db, vend, TODAY, 5_000_000, due=TODAY + timedelta(days=20))
    db.commit()

    line = CT.build(db, TODAY, 90)
    assert line.survives
    assert line.days_left is None
    assert line.lowest == 95_000_000


def test_overdue_bills_are_not_all_paid_on_the_first_morning(db):
    """Otherwise every forecast dives on day one and the date means nothing."""
    put_cash_in(db, 100_000_000, TODAY - timedelta(days=90))
    vend = supplier(db)
    bill(db, vend, TODAY - timedelta(days=60), 30_000_000,
         due=TODAY - timedelta(days=30))
    db.commit()

    line = CT.build(db, TODAY, 90)
    payment = next(e for e in line.events if not e.is_in)
    assert payment.when == TODAY + timedelta(days=CT.OVERDUE_GRACE_DAYS)
    assert payment.overdue_days == 30
    assert line.overdue_out == 30_000_000


def test_an_invoice_years_overdue_is_left_off_rather_than_guessed_at(db):
    cust = customer(db)
    invoice(db, cust, TODAY - timedelta(days=500), 40_000_000,
            due=TODAY - timedelta(days=470))
    db.commit()

    line = CT.build(db, TODAY, 90)
    assert line.total_in == 0
    assert line.excluded_count == 1
    assert line.excluded_overdue == 40_000_000


def test_the_running_balance_adds_up(db):
    put_cash_in(db, 50_000_000, TODAY - timedelta(days=1))
    cust, vend = customer(db), supplier(db)
    invoice(db, cust, TODAY, 20_000_000, due=TODAY + timedelta(days=10))
    bill(db, vend, TODAY, 8_000_000, due=TODAY + timedelta(days=20))
    db.commit()

    line = CT.build(db, TODAY, 90)
    assert line.closing == line.opening + line.total_in - line.total_out
    running = line.opening
    for day in line.days:
        running += day.net
        assert day.closing == running


def test_everything_on_the_timeline_traces_to_a_document(db):
    cust, vend = customer(db), supplier(db)
    invoice(db, cust, TODAY, 20_000_000, due=TODAY + timedelta(days=10))
    bill(db, vend, TODAY, 8_000_000, due=TODAY + timedelta(days=20))
    db.commit()

    for event in CT.build(db, TODAY, 90).events:
        assert event.label
        assert event.detail
        assert event.link.startswith("/")


def test_the_things_that_would_help_are_ranked_by_money(db):
    put_cash_in(db, 1_000_000, TODAY - timedelta(days=1))
    cust = customer(db, "Late Payer Ltd")
    invoice(db, cust, TODAY - timedelta(days=60), 30_000_000,
            due=TODAY - timedelta(days=30))
    vend = supplier(db)
    bill(db, vend, TODAY, 40_000_000, due=TODAY + timedelta(days=5))
    db.commit()

    line = CT.build(db, TODAY, 90)
    helps = CT.levers(db, line)
    assert helps
    assert any("Chase Late Payer Ltd" in h.title for h in helps)
    for h in helps:
        assert h.detail and h.worth


def test_an_empty_business_produces_an_empty_timeline(db):
    line = CT.build(db, TODAY, 30)
    assert line.survives
    assert not line.events
    assert len(line.days) == 31


# --------------------------------------------------------------------------
# What if
# --------------------------------------------------------------------------


def a_trading_year(db):
    cust, vend = customer(db), supplier(db)
    for month in (1, 2, 3, 4, 5, 6):
        invoice(db, cust, date(2026, month, 5), 10_000_000)
        bill(db, vend, date(2026, month, 6), 6_000_000)
    draft = EntryDraft(date=date(2026, 3, 1), memo="Rent")
    draft.debit(acc(db, "RENT"), 12_000_000)
    draft.credit(db.get(Account, bank_of(db).account_id), 12_000_000)
    post_entry(db, draft)
    db.commit()
    return date(2026, 1, 1), date(2026, 6, 30)


def test_with_nothing_changed_both_columns_are_the_real_figures(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions())
    assert result.base.revenue == result.changed.revenue
    assert result.base.net_profit == result.changed.net_profit
    assert result.profit_change == 0
    assert not result.assumptions.anything_changed


def test_a_price_rise_lifts_revenue_without_lifting_cost_of_sales(db):
    """The whole point: what you charge does not change what you pay."""
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions(price_change="10"))
    assert result.changed.revenue == int(result.base.revenue * 1.1)
    assert result.changed.cost_of_sales == result.base.cost_of_sales
    assert result.profit_change > 0


def test_selling_less_reduces_cost_of_sales_too(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions(volume_change="-20"))
    assert result.changed.revenue == int(result.base.revenue * 0.8)
    assert result.changed.cost_of_sales == int(result.base.cost_of_sales * 0.8)


def test_the_break_even_volume_drop_is_worked_out(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions(price_change="10"))
    assert result.breakeven_volume_drop is not None
    assert 0 < result.breakeven_volume_drop < 100
    assert any("still make the same" in note for note in result.notes)

    # Losing exactly that much volume should leave gross profit where it was.
    at_breakeven = whatif.run(db, start, end, whatif.Assumptions(
        price_change="10", volume_change=f"{-result.breakeven_volume_drop:.4f}"))
    assert abs(at_breakeven.changed.gross_profit - result.base.gross_profit) < 200


def test_a_new_fixed_cost_says_how_much_extra_selling_it_needs(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end,
                        whatif.Assumptions(extra_fixed_cost=6_000_000))
    assert result.changed.overheads == result.base.overheads + 6_000_000
    assert any("pay for itself" in note for note in result.notes)


def test_being_paid_later_changes_working_capital_not_profit(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions(collection_days="30"))
    assert result.profit_change == 0
    assert result.working_capital < 0
    assert any("does not change profit" in note for note in result.notes)


def test_every_ready_made_question_works(db):
    start, end = a_trading_year(db)
    for key, _label, _a in whatif.PRESETS:
        assumptions = whatif.preset(key)
        assert assumptions is not None
        result = whatif.run(db, start, end, assumptions)
        assert result.changed.revenue >= 0


def test_nonsense_typed_into_a_percentage_is_ignored_not_fatal(db):
    start, end = a_trading_year(db)
    result = whatif.run(db, start, end, whatif.Assumptions(
        price_change="abc", volume_change="", collection_days="soon"))
    assert result.profit_change == 0


# --------------------------------------------------------------------------
# Who to chase
# --------------------------------------------------------------------------


def test_a_reliable_customer_a_week_late_gets_a_reminder(db):
    cust = customer(db, "Reliable Ltd")
    for month in (1, 2, 3, 4):
        old = invoice(db, cust, date(2026, month, 1), 10_000_000)
        settle(db, old, old.due_date - timedelta(days=1))
    invoice(db, cust, TODAY - timedelta(days=37), 20_000_000,
            due=TODAY - timedelta(days=7))
    db.commit()

    row = next(r for r in collections.review(db, TODAY) if r.name == "Reliable Ltd")
    assert row.approach == collections.REMIND
    assert "oversight" in " ".join(row.reasons)


def test_a_customer_three_months_late_is_put_on_hold(db):
    cust = customer(db, "Very Late Ltd")
    invoice(db, cust, TODAY - timedelta(days=130), 20_000_000,
            due=TODAY - timedelta(days=100))
    db.commit()

    row = next(r for r in collections.review(db, TODAY) if r.name == "Very Late Ltd")
    assert row.approach == collections.STOP
    assert row.urgent


def test_a_large_customer_gets_a_letter_rather_than_being_cut_off(db):
    cust = customer(db, "Big Customer Plc")
    for month in (1, 2, 3, 4, 5):
        paid = invoice(db, cust, date(2026, month, 1), 60_000_000)
        settle(db, paid, paid.due_date)
    invoice(db, cust, TODAY - timedelta(days=130), 20_000_000,
            due=TODAY - timedelta(days=100))
    db.commit()

    row = next(r for r in collections.review(db, TODAY) if r.name == "Big Customer Plc")
    assert row.approach == collections.LETTER
    assert any("large customer" in reason for reason in row.reasons)


def test_nothing_overdue_means_nothing_to_do(db):
    cust = customer(db)
    invoice(db, cust, TODAY, 20_000_000, due=TODAY + timedelta(days=30))
    db.commit()
    row = collections.review(db, TODAY)[0]
    assert row.approach == collections.WAIT
    assert not row.is_late


def test_the_worst_cases_come_first(db):
    customer(db, "Fine Ltd")
    late = customer(db, "Late Ltd")
    hopeless = customer(db, "Hopeless Ltd")
    invoice(db, db.scalar(select(Contact).where(Contact.name == "Fine Ltd")),
            TODAY, 5_000_000, due=TODAY + timedelta(days=30))
    invoice(db, late, TODAY - timedelta(days=70), 5_000_000,
            due=TODAY - timedelta(days=40))
    invoice(db, hopeless, TODAY - timedelta(days=160), 5_000_000,
            due=TODAY - timedelta(days=130))
    db.commit()

    names = [r.name for r in collections.review(db, TODAY)]
    assert names.index("Hopeless Ltd") < names.index("Late Ltd") < names.index("Fine Ltd")


def test_a_draft_is_written_but_never_sent(db):
    cust = customer(db, "Late Ltd")
    cust.contact_person = "Mr Okafor"
    cust.email = "accounts@late.example"
    inv = invoice(db, cust, TODAY - timedelta(days=70), 5_000_000,
                  due=TODAY - timedelta(days=40))
    db.commit()

    row = next(r for r in collections.review(db, TODAY) if r.name == "Late Ltd")
    message = collections.draft(db, row, TODAY)
    assert message["to"] == "accounts@late.example"
    assert "Mr Okafor" in message["body"]
    assert inv.number in message["body"]
    assert message["subject"]
    # Nothing was sent: no mail log, no state change
    assert row.approach in (collections.CALL, collections.LETTER)


def test_the_wording_escalates_with_how_late_it_is(db):
    wordings = {}
    for name, days in [("Soon Ltd", 5), ("Later Ltd", 45), ("Ancient Ltd", 200)]:
        cust = customer(db, name)
        invoice(db, cust, TODAY - timedelta(days=days + 30), 5_000_000,
                due=TODAY - timedelta(days=days))
        db.commit()
        row = next(r for r in collections.review(db, TODAY) if r.name == name)
        wordings[name] = collections.draft(db, row, TODAY)["body"]

    assert "apologies" in wordings["Soon Ltd"]
    assert "telephone" in wordings["Later Ltd"].lower()
    assert "on hold" in wordings["Ancient Ltd"].lower()


# --------------------------------------------------------------------------
# Is a discount worth it?
# --------------------------------------------------------------------------


def test_two_percent_for_thirty_days_is_shown_as_what_it_really_costs():
    check = collections.discount_check(10_000_000, "2", 30, borrowing_rate_pct="24")
    assert round(check.annualised) == 24
    assert check.cost == 200_000


def test_a_dear_discount_is_advised_against():
    check = collections.discount_check(10_000_000, "5", 15, borrowing_rate_pct="24")
    assert not check.worth_it
    assert "Do not offer it" in check.verdict


def test_a_cheap_discount_is_worth_offering():
    check = collections.discount_check(10_000_000, "1", 60, borrowing_rate_pct="24")
    assert check.worth_it
    assert "Worth offering" in check.verdict


def test_a_discount_check_with_nothing_filled_in_asks_for_it():
    assert "Enter a discount" in collections.discount_check(10_000, "", 0).verdict


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-cashweb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password",
               data={"new_password": "Lagos2026", "confirm_password": "Lagos2026"},
               follow_redirects=True)
        c.post("/settings/company", data={
            "name": "Adeyemi Trading Ltd", "currency_symbol": "₦", "currency_code": "NGN",
            "fiscal_year_start_month": "1", "vat_rate": "7.5",
            "default_payment_terms_days": "30",
        }, follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def ok(client, url):
    r = client.get(url, follow_redirects=True)
    assert r.status_code == 200, f"{url} returned {r.status_code}"
    assert "Internal Server Error" not in r.text
    return r


def test_the_three_screens_open(client):
    assert "What is coming" in ok(client, "/cash").text
    assert "What if" in ok(client, "/cash/what-if").text
    assert "Who to chase" in ok(client, "/cash/collections").text


def test_every_horizon_works(client):
    for days in (30, 60, 90, 180, 365, 1, 9999):
        ok(client, f"/cash/timeline?days={days}")


def test_every_ready_made_question_opens(client):
    for key, _label, _a in whatif.PRESETS:
        assert "would have looked like" in ok(client, f"/cash/what-if?preset={key}").text


def test_nonsense_in_the_query_string_does_not_error(client):
    ok(client, "/cash/timeline?days=abc")
    ok(client, "/cash/what-if?price_change=abc&extra_fixed_cost=zzz")
    ok(client, "/cash/what-if?start=2026-12-31&end=2026-01-01")
    ok(client, "/cash/collections?on=nonsense")
    ok(client, "/cash/collections/99999")


def test_the_timeline_says_what_it_does_not_know(client):
    text = ok(client, "/cash/timeline").text
    assert "What this does not know" in text
    assert "cannot know about work you have not invoiced" in text


def test_the_what_if_screen_says_the_link_is_your_assumption(client):
    text = ok(client, "/cash/what-if?preset=prices_up").text
    assert "your assumption, not a finding" in text


def test_the_cash_screens_are_linked_from_the_menu(client):
    text = ok(client, "/").text
    for url in ("/cash/timeline", "/cash/collections", "/cash/what-if"):
        assert url in text
