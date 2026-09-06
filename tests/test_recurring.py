"""Invoices and bills that repeat.

The date arithmetic gets most of the attention here, because it is where every
recurring-billing feature goes wrong: a monthly template anchored on the 31st
that walks backwards down the calendar until it is billing on the 28th all
year, or a template neglected for three months that quietly bills once and
forgets the other two.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-rec-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    POSTED,
    Account,
    Bill,
    Contact,
    Invoice,
    RecurringDocument,
    RecurringLine,
    RecurringTemplate,
    TaxCode,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import recurring as R  # noqa: E402

M = to_kobo


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-rec-")
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


def customer(db, name="Ikoyi Properties Ltd") -> Contact:
    from app.services.posting import next_number

    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                is_vendor=True, payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def template(db, *, name="Monthly office rent", doc_type="INVOICE",
             frequency="MONTHLY", start=date(2026, 1, 31), anchor=None,
             price="450,000", end=None, limit=0, auto_post=False,
             contact=None) -> RecurringTemplate:
    contact = contact or customer(db)
    sales = db.scalar(select(Account).where(Account.code == "4010"))
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    t = RecurringTemplate(
        name=name, doc_type=doc_type, contact_id=contact.id,
        frequency=frequency, anchor_day=anchor or start.day,
        start_date=start, next_date=start, end_date=end, max_occurrences=limit,
        auto_post=auto_post, payment_terms_days=30,
        memo="Rent for the Ikoyi office.",
    )
    db.add(t)
    db.flush()
    db.add(RecurringLine(
        template_id=t.id, line_no=1, description="Office rent",
        qty=1000, unit_price=M(price), account_id=sales.id,
        tax_code_id=vat.id if vat else None,
    ))
    db.flush()
    db.refresh(t)
    return t


# --------------------------------------------------------------------------
# The calendar
# --------------------------------------------------------------------------


def test_a_month_end_template_does_not_walk_backwards():
    """31 Jan → 28 Feb → 31 Mar → 30 Apr → 31 May. The anchor is clamped, not lost."""
    on = date(2026, 1, 31)
    got = []
    for _ in range(5):
        on = R.next_after(on, "MONTHLY", anchor_day=31)
        got.append(on)
    assert got == [
        date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30),
        date(2026, 5, 31), date(2026, 6, 30),
    ]


def test_february_in_a_leap_year_takes_the_29th():
    assert R.next_after(date(2024, 1, 31), "MONTHLY", anchor_day=31) == date(2024, 2, 29)


def test_the_other_frequencies():
    assert R.next_after(date(2026, 3, 5), "WEEKLY") == date(2026, 3, 12)
    assert R.next_after(date(2026, 3, 5), "FORTNIGHTLY") == date(2026, 3, 19)
    assert R.next_after(date(2026, 3, 5), "QUARTERLY", 5) == date(2026, 6, 5)
    assert R.next_after(date(2026, 3, 5), "HALF_YEARLY", 5) == date(2026, 9, 5)
    assert R.next_after(date(2026, 3, 5), "YEARLY", 5) == date(2027, 3, 5)


def test_a_year_boundary():
    assert R.next_after(date(2026, 12, 15), "MONTHLY", 15) == date(2027, 1, 15)
    assert R.next_after(date(2026, 11, 30), "QUARTERLY", 30) == date(2027, 2, 28)


# --------------------------------------------------------------------------
# What is owed
# --------------------------------------------------------------------------


def test_nothing_is_due_before_the_start_date(db):
    template(db, start=date(2099, 1, 1))
    assert R.due(db, date.today()) == []


def test_a_neglected_template_owes_every_missed_month(db):
    """Three months ignored means three invoices, not one."""
    t = template(db, start=date(2026, 1, 31))
    owed = R.due(db, date(2026, 4, 15))
    assert len(owed) == 1
    assert owed[0].dates == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert owed[0].count == 3
    assert R.due_count(db, date(2026, 4, 15)) == 3


def test_a_paused_template_owes_nothing(db):
    t = template(db, start=date(2026, 1, 31))
    R.pause(db, t)
    assert t.status == "PAUSED"
    assert R.due(db, date(2026, 6, 1)) == []


def test_an_end_date_stops_the_list(db):
    t = template(db, start=date(2026, 1, 31), end=date(2026, 3, 15))
    owed = R.due(db, date(2026, 12, 31))
    assert owed[0].dates == [date(2026, 1, 31), date(2026, 2, 28)]


def test_an_occurrence_limit_stops_the_list(db):
    t = template(db, start=date(2026, 1, 31), limit=2)
    owed = R.due(db, date(2026, 12, 31))
    assert owed[0].count == 2


# --------------------------------------------------------------------------
# Generating
# --------------------------------------------------------------------------


def test_generating_makes_a_draft_invoice(db):
    t = template(db, price="450,000")
    inv = R.generate_one(db, t)

    assert isinstance(inv, Invoice)
    assert inv.status == DRAFT
    assert inv.date == date(2026, 1, 31)
    assert inv.due_date == date(2026, 3, 2)          # 30 days on
    assert inv.subtotal == M("450,000")
    assert inv.vat_total == M("33,750")              # 7.5%
    assert inv.total == M("483,750")
    assert inv.memo == "Rent for the Ikoyi office."

    # and the template has moved on
    assert t.next_date == date(2026, 2, 28)
    assert t.occurrences == 1
    assert t.last_generated == date(2026, 1, 31)


def test_the_document_is_linked_back_to_its_template(db):
    t = template(db)
    inv = R.generate_one(db, t)
    link = db.scalar(select(RecurringDocument).where(RecurringDocument.template_id == t.id))
    assert link.doc_id == inv.id
    assert link.doc_number == inv.number
    assert link.was_posted is False
    assert len(t.generated) == 1


def test_auto_post_puts_it_straight_in_the_books(db):
    t = template(db, auto_post=True)
    inv = R.generate_one(db, t)
    assert inv.status == POSTED
    assert inv.journal_entry_id
    link = t.generated[0]
    assert link.was_posted is True


def test_a_locked_period_leaves_the_document_as_a_draft(db):
    """Nothing is lost when the books are closed — it waits as a draft."""
    from app.models import Company

    company = db.get(Company, 1)
    company.lock_date = date(2026, 6, 30)
    db.flush()

    t = template(db, start=date(2026, 1, 31), auto_post=True)
    inv = R.generate_one(db, t)
    assert inv.status == DRAFT
    assert inv.journal_entry_id is None
    assert t.generated[0].was_posted is False


def test_catching_up_generates_every_missed_month(db):
    t = template(db, start=date(2026, 1, 31), price="450,000")
    made = R.catch_up(db, t, date(2026, 4, 15))

    assert len(made) == 3
    assert [i.date for i in made] == [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    assert t.next_date == date(2026, 4, 30)
    assert t.occurrences == 3
    # Each one is its own document with its own number
    assert len({i.number for i in made}) == 3


def test_a_template_finishes_when_it_hits_its_limit(db):
    t = template(db, start=date(2026, 1, 31), limit=2)
    made = R.catch_up(db, t, date(2026, 12, 31))
    assert len(made) == 2
    assert t.status == "FINISHED"
    with pytest.raises(R.RecurringError) as e:
        R.generate_one(db, t)
    assert "finished" in str(e.value)


def test_a_template_finishes_at_its_end_date(db):
    t = template(db, start=date(2026, 1, 31), end=date(2026, 3, 15))
    made = R.catch_up(db, t, date(2026, 12, 31))
    assert len(made) == 2
    assert t.status == "FINISHED"


def test_a_recurring_bill_works_the_same_way(db):
    supplier = customer(db, "Ikeja Electric Plc")
    t = template(db, name="Monthly electricity", doc_type="BILL",
                 contact=supplier, price="180,000", start=date(2026, 2, 1))
    bill = R.generate_one(db, t)
    assert isinstance(bill, Bill)
    assert bill.status == DRAFT
    assert bill.subtotal == M("180,000")


def test_running_everything_at_once(db):
    template(db, name="Rent — Ikoyi", start=date(2026, 1, 31), price="450,000")
    template(db, name="Retainer — Legal", start=date(2026, 2, 1),
             frequency="QUARTERLY", price="750,000")
    result = R.run_all(db, date(2026, 4, 15))
    assert len(result["made"]) == 4     # three rents and one retainer
    assert result["failed"] == []


def test_a_template_with_no_lines_says_so(db):
    contact = customer(db)
    t = RecurringTemplate(name="Empty", doc_type="INVOICE", contact_id=contact.id,
                          frequency="MONTHLY", anchor_day=1, start_date=date(2026, 1, 1),
                          next_date=date(2026, 1, 1))
    db.add(t)
    db.flush()
    with pytest.raises(R.RecurringError) as e:
        R.generate_one(db, t)
    assert "nothing to bill" in str(e.value)


def test_skipping_a_month_moves_on_without_billing(db):
    t = template(db, start=date(2026, 1, 31))
    nxt = R.skip_next(db, t)
    assert nxt == date(2026, 2, 28)
    assert t.occurrences == 1
    assert t.generated == []
    assert db.scalar(select(Invoice)) is None


def test_pausing_and_starting_again(db):
    t = template(db, start=date(2026, 1, 31))
    R.pause(db, t)
    assert t.status == "PAUSED"
    R.pause(db, t)
    assert t.status == "ACTIVE"


def test_a_finished_template_cannot_simply_be_restarted(db):
    t = template(db, start=date(2026, 1, 31), limit=1)
    R.generate_one(db, t)
    assert t.status == "FINISHED"
    t.status = "PAUSED"           # as if somebody paused it by hand
    db.flush()
    with pytest.raises(R.RecurringError) as e:
        R.pause(db, t)
    assert "end date or its limit" in str(e.value)


def test_generated_invoices_carry_the_right_numbers_in_sequence(db):
    t = template(db, start=date(2026, 1, 31))
    made = R.catch_up(db, t, date(2026, 3, 31))
    numbers = [i.number for i in made]
    assert numbers == sorted(numbers)
    assert all(n.startswith("INV-") for n in numbers)


def test_the_estimated_value_of_what_is_waiting(db):
    template(db, start=date(2026, 1, 31), price="450,000")
    owed = R.due(db, date(2026, 3, 15))
    assert owed[0].count == 2
    assert owed[0].value == M("900,000")     # net of VAT — a guide, not a posting
