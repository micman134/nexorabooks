"""Requisitions: the approval route, and the money.

An approval trail is only worth having if it cannot be walked around. Most of
these tests are about what the system *refuses* to do — approve your own
request, approve out of turn, reject without saying why, pay yourself — because
those are the ways a workflow like this quietly stops meaning anything.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-req-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    REQ_CANCELLED,
    REQ_PAID,
    REQ_REJECTED,
    REQ_RETIRED,
    REQ_WITH_DIRECTOR,
    REQ_WITH_FINANCE,
    REQ_WITH_MANAGER,
    Account,
    BankAccount,
    Company,
    Requisition,
    RequisitionLine,
    User,
)
from app.money import to_kobo  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import reports  # noqa: E402
from app.services import requisitions as R  # noqa: E402
from app.services.posting import account_by_code  # noqa: E402

M = to_kobo


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-req-")
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


def person(db, username, name, *, role="clerk", manager=None, director=False,
           finance=False, bank=True) -> User:
    u = User(
        username=username, full_name=name, role=role,
        password_hash=hash_password("Lagos2026"),
        manager_id=manager.id if manager else None,
        approves_large_requisitions=director,
        pays_requisitions=finance,
        bank_name="GTBank" if bank else "",
        bank_account_no="0123456789" if bank else "",
        bank_account_name=name if bank else "",
    )
    db.add(u)
    db.flush()
    return u


def team(db):
    """A small office: a storekeeper, his manager, a director and the accountant."""
    md = person(db, "bola", "Bola Adeyemi", role="admin", director=True)
    manager = person(db, "chioma", "Chioma Eze", role="accountant", manager=md)
    finance = person(db, "tunde", "Tunde Bello", role="accountant", manager=md,
                     finance=True)
    staff = person(db, "musa", "Musa Ibrahim", role="clerk", manager=manager)
    return md, manager, finance, staff


def requisition(db, staff, amount="150,000", account="6120",
                purpose="Diesel for the generator") -> Requisition:
    req = R.create(db, staff)
    req.purpose = purpose
    db.add(RequisitionLine(
        requisition_id=req.id, line_no=1, description=purpose,
        account_id=account_by_code(db, account).id,
        qty=1000, unit_price=M(amount),
    ))
    db.flush()
    db.refresh(req)
    R.recalc(db, req)
    return req


def default_bank(db) -> BankAccount:
    return db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))


def set_limit(db, amount):
    company = db.get(Company, 1)
    company.requisition_limit = M(amount) if isinstance(amount, str) else amount
    db.flush()


# --------------------------------------------------------------------------
# The happy route
# --------------------------------------------------------------------------


def test_the_whole_route_end_to_end(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff, "150,000")

    assert req.total == M("150,000")
    assert req.manager_id == manager.id

    R.submit(db, req, staff)
    assert req.status == REQ_WITH_MANAGER
    assert req.awaiting == "Chioma Eze"

    R.approve(db, req, manager, "Genset has been running on empty.")
    assert req.status == REQ_WITH_FINANCE
    assert req.manager_approved_by_id == manager.id

    entry = R.pay(db, req, finance, bank_account_id=default_bank(db).id,
                  on=date(2026, 5, 4), reference="GTB/0912")
    assert req.status == REQ_PAID
    assert req.paid_amount == M("150,000")
    assert req.paid_to_account_no == "0123456789"

    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"6120": M("150,000")}      # diesel, straight to expense
    assert credits == {"1020": M("150,000")}     # out of the current account


def test_the_trail_names_everybody(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.approve(db, req, manager, "Approved.")
    R.pay(db, req, finance, bank_account_id=default_bank(db).id)

    actions = [(e.action, e.by_name) for e in req.events]
    assert actions == [
        ("SUBMIT", "Musa Ibrahim"),
        ("MANAGER_OK", "Chioma Eze"),
        ("PAID", "Tunde Bello"),
    ]


# --------------------------------------------------------------------------
# Who may approve
# --------------------------------------------------------------------------


def test_nobody_approves_their_own_requisition(db):
    md, manager, finance, staff = team(db)
    # Even the managing director, raising one for himself
    req = R.create(db, md)
    req.purpose = "Trip to Abuja"
    req.manager_id = manager.id
    db.add(RequisitionLine(requisition_id=req.id, line_no=1, description="Flights",
                           account_id=account_by_code(db, "6300").id,
                           qty=1000, unit_price=M("400,000")))
    db.flush()
    db.refresh(req)
    R.recalc(db, req)
    R.submit(db, req, md)

    assert R.can_approve_as_manager(db, req, md) is False
    with pytest.raises(R.RequisitionError) as e:
        R.approve(db, req, md)
    assert "your own" in str(e.value)


def test_only_the_named_manager_approves(db):
    md, manager, finance, staff = team(db)
    other = person(db, "grace", "Grace Okon", role="accountant", manager=md)
    req = requisition(db, staff)
    R.submit(db, req, staff)

    assert R.can_approve_as_manager(db, req, other) is False
    with pytest.raises(R.RequisitionError) as e:
        R.approve(db, req, other)
    assert "not waiting for you" in str(e.value)

    # The named manager can
    R.approve(db, req, manager)
    assert req.status == REQ_WITH_FINANCE


def test_an_administrator_can_stand_in_for_an_absent_manager(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.approve(db, req, md, "Chioma is on leave.")
    assert req.status == REQ_WITH_FINANCE
    assert req.manager_approved_by_id == md.id


def test_finance_cannot_approve_before_the_manager_has(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)

    with pytest.raises(R.RequisitionError):
        R.pay(db, req, finance, bank_account_id=default_bank(db).id)


def test_somebody_with_no_manager_cannot_send_one(db):
    md, manager, finance, staff = team(db)
    orphan = person(db, "sade", "Sade Coker", role="clerk")
    req = requisition(db, orphan)
    with pytest.raises(R.RequisitionError) as e:
        R.submit(db, req, orphan)
    assert "no manager set" in str(e.value)


def test_you_cannot_be_your_own_manager(db):
    md, manager, finance, staff = team(db)
    loop = person(db, "kola", "Kola Bright", role="clerk")
    loop.manager_id = loop.id
    db.flush()
    req = requisition(db, loop)
    with pytest.raises(R.RequisitionError) as e:
        R.submit(db, req, loop)
    assert "your own manager" in str(e.value)


# --------------------------------------------------------------------------
# The limit
# --------------------------------------------------------------------------


def test_a_small_one_goes_straight_to_finance(db):
    md, manager, finance, staff = team(db)
    set_limit(db, "500,000")
    req = requisition(db, staff, "150,000")
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    assert req.status == REQ_WITH_FINANCE


def test_a_large_one_needs_a_director_too(db):
    md, manager, finance, staff = team(db)
    set_limit(db, "500,000")
    req = requisition(db, staff, "1,200,000", account="6140",
                      purpose="Rewire the Ikeja yard")
    R.submit(db, req, staff)
    R.approve(db, req, manager, "Needed — the wiring is a fire risk.")
    assert req.status == REQ_WITH_DIRECTOR

    # Finance cannot take it yet
    with pytest.raises(R.RequisitionError):
        R.pay(db, req, finance, bank_account_id=default_bank(db).id)

    R.approve(db, req, md, "Agreed. Get three quotes.")
    assert req.status == REQ_WITH_FINANCE
    assert req.director_approved_by_id == md.id


def test_an_ordinary_manager_cannot_give_the_directors_approval(db):
    md, manager, finance, staff = team(db)
    set_limit(db, "500,000")
    req = requisition(db, staff, "1,200,000")
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    assert req.status == REQ_WITH_DIRECTOR

    assert R.can_approve_as_director(db, req, manager) is False
    with pytest.raises(R.RequisitionError):
        R.approve(db, req, manager)


def test_no_limit_means_the_manager_is_enough(db):
    md, manager, finance, staff = team(db)
    set_limit(db, 0)
    req = requisition(db, staff, "9,000,000")
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    assert req.status == REQ_WITH_FINANCE


# --------------------------------------------------------------------------
# Rejection
# --------------------------------------------------------------------------


def test_a_rejection_must_say_why(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)

    with pytest.raises(R.RequisitionError) as e:
        R.reject(db, req, manager, "")
    assert "Say why" in str(e.value)

    with pytest.raises(R.RequisitionError) as e:
        R.reject(db, req, manager, "no")
    assert "more detail" in str(e.value)


def test_a_rejection_goes_back_to_the_person_who_raised_it(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.reject(db, req, manager, "We filled the tank on Tuesday. Check with the yard first.")

    assert req.status == REQ_REJECTED
    assert req.rejected_by_id == manager.id
    assert req.rejected_stage == "MANAGER"
    assert "Check with the yard" in req.rejection_reason

    # It shows up as sent back to the staff member, and to nobody else
    assert [r.id for r in R.sent_back_to(db, staff)] == [req.id]
    assert R.sent_back_to(db, manager) == []
    assert R.waiting_for(db, manager) == []


def test_a_rejected_one_can_be_corrected_and_sent_again(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff, "150,000")
    R.submit(db, req, staff)
    R.reject(db, req, manager, "Too much. Half a tank will do.")

    assert req.is_editable is True
    req.lines[0].unit_price = M("75,000")
    db.flush()
    R.recalc(db, req)
    R.submit(db, req, staff)

    assert req.status == REQ_WITH_MANAGER
    assert req.total == M("75,000")
    assert req.rejection_reason == ""            # cleared from the record
    # but the history still shows it was sent back
    actions = [e.action for e in req.events]
    assert actions == ["SUBMIT", "REJECT", "RESUBMIT"]


def test_finance_can_send_it_back_too(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    R.reject(db, req, finance, "No vendor invoice attached. Please attach it.")
    assert req.status == REQ_REJECTED
    assert req.rejected_stage == "FINANCE"


def test_you_cannot_reject_your_own(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    with pytest.raises(R.RequisitionError) as e:
        R.reject(db, req, staff, "Changed my mind about this one.")
    assert "withdraw it instead" in str(e.value)


def test_withdrawing_one(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.withdraw(db, req, staff, "Sorted it another way.")
    assert req.status == REQ_CANCELLED
    assert R.waiting_for(db, manager) == []


# --------------------------------------------------------------------------
# Paying it
# --------------------------------------------------------------------------


def test_only_finance_releases_the_money(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.approve(db, req, manager)

    assert R.can_pay(db, req, manager) is False
    with pytest.raises(R.RequisitionError) as e:
        R.pay(db, req, manager, bank_account_id=default_bank(db).id)
    assert "not set up to release money" in str(e.value)


def test_finance_cannot_pay_their_own(db):
    md, manager, finance, staff = team(db)
    req = R.create(db, finance)
    req.purpose = "Courier to Abuja"
    req.manager_id = md.id
    db.add(RequisitionLine(requisition_id=req.id, line_no=1, description="Courier",
                           account_id=account_by_code(db, "6220").id,
                           qty=1000, unit_price=M("40,000")))
    db.flush()
    db.refresh(req)
    R.recalc(db, req)
    R.submit(db, req, finance)
    R.approve(db, req, md)

    assert R.can_pay(db, req, finance) is False
    with pytest.raises(R.RequisitionError) as e:
        R.pay(db, req, finance, bank_account_id=default_bank(db).id)
    assert "your own" in str(e.value)


def test_finance_can_pay_less_but_never_more(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff, "150,000")
    R.submit(db, req, staff)
    R.approve(db, req, manager)

    with pytest.raises(R.RequisitionError) as e:
        R.pay(db, req, finance, bank_account_id=default_bank(db).id, amount=M("200,000"))
    assert "fresh approval" in str(e.value)

    R.pay(db, req, finance, bank_account_id=default_bank(db).id, amount=M("120,000"))
    assert req.paid_amount == M("120,000")


def test_paying_spreads_across_the_lines(db):
    md, manager, finance, staff = team(db)
    req = R.create(db, staff)
    req.purpose = "Yard running costs"
    req.manager_id = manager.id
    for n, (desc, code, amount) in enumerate([
        ("Diesel", "6120", "90,000"),
        ("Security night shift", "6130", "60,000"),
    ], start=1):
        db.add(RequisitionLine(requisition_id=req.id, line_no=n, description=desc,
                               account_id=account_by_code(db, code).id,
                               qty=1000, unit_price=M(amount)))
    db.flush()
    db.refresh(req)
    R.recalc(db, req)
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    entry = R.pay(db, req, finance, bank_account_id=default_bank(db).id)

    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    assert debits == {"6120": M("90,000"), "6130": M("60,000")}


def test_the_books_balance_after_paying(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    R.pay(db, req, finance, bank_account_id=default_bank(db).id)

    _rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert dr == cr


# --------------------------------------------------------------------------
# Retiring it
# --------------------------------------------------------------------------


def paid_requisition(db, amount="150,000"):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff, amount)
    R.submit(db, req, staff)
    R.approve(db, req, manager)
    R.pay(db, req, finance, bank_account_id=default_bank(db).id, on=date(2026, 5, 4))
    return md, manager, finance, staff, req


def test_retiring_in_full_posts_nothing(db):
    """Spent exactly what was taken. The expense is already right."""
    md, manager, finance, staff, req = paid_requisition(db)
    entry = R.retire(db, req, staff, spent={req.lines[0].id: M("150,000")},
                     on=date(2026, 5, 20), note="Two drums of diesel, receipt attached.")

    assert entry is None
    assert req.status == REQ_RETIRED
    assert req.amount_spent == M("150,000")
    assert req.balance_to_return == 0


def test_spending_less_returns_the_balance_and_cuts_the_expense(db):
    md, manager, finance, staff, req = paid_requisition(db, "200,000")
    entry = R.retire(db, req, staff, spent={req.lines[0].id: M("180,000")},
                     on=date(2026, 5, 20), note="Diesel was cheaper at the Ogba station.")

    assert req.amount_spent == M("180,000")
    assert req.balance_to_return == M("20,000")

    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"1020": M("20,000")}       # cash back into the bank
    assert credits == {"6120": M("20,000")}      # and off the diesel account

    _rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert dr == cr


def test_the_expense_ends_up_at_what_was_actually_spent(db):
    md, manager, finance, staff, req = paid_requisition(db, "200,000")
    R.retire(db, req, staff, spent={req.lines[0].id: M("180,000")}, on=date(2026, 5, 20))

    rows, _dr, _cr = reports.trial_balance(db, None, date(2026, 12, 31))
    by_code = {r.account.code: r.debit - r.credit for r in rows}
    assert by_code["6120"] == M("180,000")


def test_spending_more_is_reimbursed(db):
    md, manager, finance, staff, req = paid_requisition(db, "150,000")
    entry = R.retire(db, req, staff, spent={req.lines[0].id: M("175,000")},
                     on=date(2026, 5, 20), note="Price went up. Receipt attached.")

    assert req.balance_to_return == M("-25,000")
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"6120": M("25,000")}
    assert credits == {"1020": M("25,000")}


def test_only_the_person_who_took_the_money_retires_it(db):
    md, manager, finance, staff, req = paid_requisition(db)
    assert R.can_retire(db, req, manager) is False
    with pytest.raises(R.RequisitionError) as e:
        R.retire(db, req, manager, spent={req.lines[0].id: M("150,000")})
    assert "Only Musa Ibrahim" in str(e.value)


def test_it_cannot_be_retired_twice(db):
    md, manager, finance, staff, req = paid_requisition(db)
    R.retire(db, req, staff, spent={req.lines[0].id: M("150,000")})
    with pytest.raises(R.RequisitionError) as e:
        R.retire(db, req, staff, spent={req.lines[0].id: M("150,000")})
    assert "already been retired" in str(e.value)


def test_an_administrator_can_reopen_a_wrong_retirement(db):
    md, manager, finance, staff, req = paid_requisition(db, "200,000")
    R.retire(db, req, staff, spent={req.lines[0].id: M("180,000")})
    assert req.status == REQ_RETIRED

    R.unretire(db, req, md)
    assert req.status == REQ_PAID
    assert req.amount_spent == 0

    rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    by_code = {r.account.code: r.debit - r.credit for r in rows}
    assert by_code["6120"] == M("200,000")      # back to what was paid out
    assert dr == cr

    R.retire(db, req, staff, spent={req.lines[0].id: M("195,000")})
    rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    by_code = {r.account.code: r.debit - r.credit for r in rows}
    assert by_code["6120"] == M("195,000")


def test_a_clerk_cannot_reopen_a_retirement(db):
    md, manager, finance, staff, req = paid_requisition(db)
    R.retire(db, req, staff, spent={req.lines[0].id: M("150,000")})
    with pytest.raises(R.RequisitionError):
        R.unretire(db, req, staff)


# --------------------------------------------------------------------------
# What is outstanding
# --------------------------------------------------------------------------


def test_unretired_money_is_easy_to_find(db):
    md, manager, finance, staff, req = paid_requisition(db)
    assert [r.id for r in R.unretired(db)] == [req.id]
    assert [r.id for r in R.unretired(db, staff)] == [req.id]
    assert R.unretired(db, manager) == []

    R.retire(db, req, staff, spent={req.lines[0].id: M("150,000")})
    assert R.unretired(db) == []


def test_who_is_holding_company_money(db):
    md, manager, finance, staff = team(db)
    second = person(db, "ada", "Ada Nwosu", role="clerk", manager=manager)

    for who, amount in ((staff, "150,000"), (staff, "80,000"), (second, "300,000")):
        req = requisition(db, who, amount)
        R.submit(db, req, who)
        R.approve(db, req, manager)
        R.pay(db, req, finance, bank_account_id=default_bank(db).id)

    rows = R.outstanding_by_person(db)
    assert rows[0][0].id == second.id           # biggest first
    assert rows[0][2] == M("300,000")
    by_id = {u.id: (n, v) for u, n, v in rows}
    assert by_id[staff.id] == (2, M("230,000"))


def test_the_dashboard_summary(db):
    md, manager, finance, staff = team(db)
    waiting = requisition(db, staff, "150,000")
    R.submit(db, waiting, staff)

    rejected = requisition(db, staff, "40,000")
    R.submit(db, rejected, staff)
    R.reject(db, rejected, manager, "Get a quote from two vendors first.")

    s = R.summary(db, manager)
    assert s.waiting_for_me == 1
    assert s.waiting_value == M("150,000")

    s = R.summary(db, staff)
    assert s.waiting_for_me == 0
    assert s.sent_back_to_me == 1


# --------------------------------------------------------------------------
# Who can see what
# --------------------------------------------------------------------------


def test_staff_see_their_own_and_nothing_else(db):
    md, manager, finance, staff = team(db)
    other = person(db, "ada", "Ada Nwosu", role="clerk", manager=manager)
    mine = requisition(db, staff)
    theirs = requisition(db, other)

    visible = [r.id for r in R.visible_to(db, staff)]
    assert mine.id in visible
    assert theirs.id not in visible


def test_a_manager_sees_their_teams(db):
    md, manager, finance, staff = team(db)
    theirs = requisition(db, staff)
    visible = [r.id for r in R.visible_to(db, manager)]
    assert theirs.id in visible


def test_finance_and_administrators_see_everything(db):
    md, manager, finance, staff = team(db)
    theirs = requisition(db, staff)
    assert theirs.id in [r.id for r in R.visible_to(db, finance)]
    assert theirs.id in [r.id for r in R.visible_to(db, md)]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def test_an_empty_requisition_cannot_be_sent(db):
    md, manager, finance, staff = team(db)
    req = R.create(db, staff)
    req.purpose = "Something"
    db.flush()
    with pytest.raises(R.RequisitionError) as e:
        R.submit(db, req, staff)
    assert "at least one line" in str(e.value)


def test_a_line_with_no_account_cannot_be_sent(db):
    md, manager, finance, staff = team(db)
    req = R.create(db, staff)
    req.purpose = "Something"
    req.manager_id = manager.id
    db.add(RequisitionLine(requisition_id=req.id, line_no=1, description="Bits",
                           qty=1000, unit_price=M("10,000")))
    db.flush()
    db.refresh(req)
    R.recalc(db, req)
    with pytest.raises(R.RequisitionError) as e:
        R.submit(db, req, staff)
    assert "account to charge" in str(e.value)


def test_a_paid_one_cannot_be_withdrawn(db):
    md, manager, finance, staff, req = paid_requisition(db)
    with pytest.raises(R.RequisitionError) as e:
        R.withdraw(db, req, staff)
    assert "already been paid" in str(e.value)


def test_it_cannot_be_sent_twice(db):
    md, manager, finance, staff = team(db)
    req = requisition(db, staff)
    R.submit(db, req, staff)
    with pytest.raises(R.RequisitionError) as e:
        R.submit(db, req, staff)
    assert "already been sent" in str(e.value)
