"""Payroll runs end to end, and what they do to the ledger."""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-pr-")

from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    ACTIVE,
    DEDUCTION,
    DRAFT,
    EARNING,
    PAID,
    POSTED,
    VOID,
    Account,
    BankAccount,
    Employee,
    EmployeeComponent,
    JournalEntry,
    PayrollRun,
)
from app.money import fmt  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import payroll as P  # noqa: E402
from app.services import payroll_run as PR  # noqa: E402
from app.services import reports as R  # noqa: E402
from app.services.posting import PostingError, account_net, sys_account  # noqa: E402

M = 100
JUNE_START, JUNE_END = date(2026, 6, 1), date(2026, 6, 30)


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-pr-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    dbmod.init_db()
    session = dbmod.SessionLocal()
    bootstrap(session)
    session.commit()
    yield session
    session.close()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def bal(db, key):
    return account_net(db, sys_account(db, key).id, None, date(2030, 1, 1))


def assert_books_balance(db):
    _rows, td, tc = R.trial_balance(db, None, date(2030, 1, 1))
    assert td == tc, f"Trial balance out by {fmt(td - tc)}"
    bs = R.balance_sheet(db, date(2030, 1, 1))
    assert bs.difference == 0, f"Balance sheet out by {fmt(bs.difference)}"


def hire(
    db, first="Ada", last="Okafor", basic=300_000, housing=100_000, transport=50_000,
    frequency=P.MONTHLY, **kw
):
    e = Employee(
        staff_no=f"EMP-{first[:2]}{last[:2]}{basic}",
        first_name=first, last_name=last, status=ACTIVE,
        basic=basic * M, housing=housing * M, transport=transport * M,
        frequency=frequency,
        bank_name="GTBank", bank_account_no="0123456789",
        tin="10203040-0001", pfa_name="Stanbic IBTC Pensions",
        hire_date=date(2025, 1, 1),
        **kw,
    )
    db.add(e)
    db.flush()
    return e


# --------------------------------------------------------------------------
# Building a run
# --------------------------------------------------------------------------


def test_a_run_covers_everyone_on_that_frequency(db):
    hire(db, "Ada", "Okafor")
    hire(db, "Bola", "Adeniyi", basic=200_000)
    hire(db, "Chike", "Eze", basic=40_000, housing=0, transport=0, frequency=P.WEEKLY)

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    assert run.employee_count == 2
    assert run.status == DRAFT
    names = sorted(s.employee_name for s in run.payslips)
    assert names == ["Ada Okafor", "Bola Adeniyi"]


def test_people_who_have_left_are_not_paid(db):
    hire(db, "Ada", "Okafor")
    gone = hire(db, "Chidi", "Nwosu")
    gone.leave_date = date(2026, 5, 31)
    db.flush()

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    assert run.employee_count == 1


def test_people_hired_later_are_not_paid_yet(db):
    hire(db, "Ada", "Okafor")
    future = hire(db, "Ngozi", "Bello")
    future.hire_date = date(2026, 8, 1)
    db.flush()

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    assert run.employee_count == 1


def test_a_run_with_nobody_on_it_is_refused(db):
    with pytest.raises(PostingError, match="Nobody"):
        PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)


def test_run_totals_are_the_sum_of_the_payslips(db):
    hire(db, "Ada", "Okafor")
    hire(db, "Bola", "Adeniyi", basic=800_000, housing=200_000, transport=100_000)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)

    assert run.gross_total == sum(s.gross for s in run.payslips)
    assert run.paye_total == sum(s.paye for s in run.payslips)
    assert run.net_total == sum(s.net_pay for s in run.payslips)
    assert run.net_total == run.gross_total - sum(s.total_deductions for s in run.payslips)


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------


def test_posting_a_run_balances_and_creates_every_liability(db):
    hire(db, "Ada", "Okafor", basic=400_000, housing=150_000, transport=75_000)
    hire(db, "Bola", "Adeniyi", basic=250_000, housing=80_000, transport=40_000)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)

    assert run.status == POSTED
    assert run.journal_entry_id is not None

    # The cost went to the P&L
    assert bal(db, "SALARIES") == run.gross_total
    assert bal(db, "PENSION_EXPENSE") == run.pension_employer_total

    # And every deduction became a liability
    assert bal(db, "PAYE_PAYABLE") == run.paye_total
    assert bal(db, "PENSION_PAYABLE") == (
        run.pension_employee_total + run.pension_employer_total
    )
    assert bal(db, "NHF_PAYABLE") == run.nhf_total
    assert bal(db, "NSITF_PAYABLE") == run.nsitf_total
    assert bal(db, "WAGES_PAYABLE") == run.net_total

    assert_books_balance(db)


def test_a_posted_run_cannot_be_posted_twice(db):
    hire(db, "Ada", "Okafor")
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)
    with pytest.raises(PostingError, match="already"):
        PR.post_run(db, run)


def test_paying_the_staff_clears_what_is_owed_to_them(db):
    hire(db, "Ada", "Okafor")
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    PR.pay_run(db, run, bank, JUNE_END)

    assert run.status == PAID
    assert bal(db, "WAGES_PAYABLE") == 0
    assert account_net(db, bank.account_id) == -run.net_total
    # The tax and pension are still held — they have not been remitted yet
    assert bal(db, "PAYE_PAYABLE") == run.paye_total
    assert_books_balance(db)


def test_you_cannot_pay_before_posting(db):
    hire(db, "Ada", "Okafor")
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    bank = db.query(BankAccount).filter_by(is_default=True).one()
    with pytest.raises(PostingError, match="Post the payroll"):
        PR.pay_run(db, run, bank, JUNE_END)


def test_voiding_a_run_reverses_everything(db):
    hire(db, "Ada", "Okafor")
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)
    bank = db.query(BankAccount).filter_by(is_default=True).one()
    PR.pay_run(db, run, bank, JUNE_END)

    PR.void_run(db, run, JUNE_END)

    assert run.status == VOID
    assert bal(db, "SALARIES") == 0
    assert bal(db, "PAYE_PAYABLE") == 0
    assert bal(db, "WAGES_PAYABLE") == 0
    assert account_net(db, bank.account_id) == 0
    assert_books_balance(db)


# --------------------------------------------------------------------------
# Remittances
# --------------------------------------------------------------------------


def test_remitting_paye_clears_the_liability(db):
    hire(db, "Ada", "Okafor", basic=600_000, housing=200_000, transport=100_000)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)
    assert run.paye_total > 0

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    paye_account = sys_account(db, "PAYE_PAYABLE")
    PR.post_remittance(db, paye_account, bank, run.paye_total, date(2026, 7, 9),
                       reference="LIRS-8891", memo="PAYE for June 2026")

    assert bal(db, "PAYE_PAYABLE") == 0
    assert_books_balance(db)


def test_outstanding_remittances_lists_what_is_held(db):
    hire(db, "Ada", "Okafor", basic=600_000, housing=200_000, transport=100_000)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)

    items = {i["code"]: i["balance"] for i in PR.outstanding_remittances(db)}
    assert items["PAYE"] == run.paye_total
    assert items["PENSION"] == run.pension_employee_total + run.pension_employer_total
    assert items["NHF"] == run.nhf_total


# --------------------------------------------------------------------------
# Loans
# --------------------------------------------------------------------------


def test_a_loan_is_recovered_from_payslips(db):
    employee = hire(db, "Ada", "Okafor")
    bank = db.query(BankAccount).filter_by(is_default=True).one()

    PR.grant_loan(db, employee, amount=120_000 * M, repayment=20_000 * M,
                  on=date(2026, 5, 20), bank=bank, description="School fees loan")
    assert account_net(db, db.query(Account).filter_by(code="1310").one().id) == 120_000 * M

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    slip = run.payslips[0]
    assert slip.loan_repayment == 20_000 * M

    PR.post_run(db, run)
    db.refresh(employee)
    assert employee.loans[0].balance == 100_000 * M
    # The advance on the balance sheet came down by the same amount
    assert account_net(db, db.query(Account).filter_by(code="1310").one().id) == 100_000 * M
    assert_books_balance(db)


def test_a_loan_never_over_recovers(db):
    employee = hire(db, "Ada", "Okafor")
    PR.grant_loan(db, employee, amount=15_000 * M, repayment=20_000 * M,
                  on=date(2026, 5, 20), bank=None)

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    # Only the ₦15,000 outstanding is taken, not the ₦20,000 instalment
    assert run.payslips[0].loan_repayment == 15_000 * M

    PR.post_run(db, run)
    db.refresh(employee)
    assert employee.loans[0].balance == 0
    assert employee.loans[0].is_active is False


def test_voiding_a_run_restores_the_loan_balance(db):
    employee = hire(db, "Ada", "Okafor")
    PR.grant_loan(db, employee, amount=120_000 * M, repayment=20_000 * M,
                  on=date(2026, 5, 20), bank=None)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)
    db.refresh(employee)
    assert employee.loans[0].balance == 100_000 * M

    PR.void_run(db, run, JUNE_END)
    db.refresh(employee)
    assert employee.loans[0].balance == 120_000 * M
    assert employee.loans[0].is_active is True


# --------------------------------------------------------------------------
# Allowances and deductions
# --------------------------------------------------------------------------


def test_allowances_and_deductions_flow_through(db):
    employee = hire(db, "Ada", "Okafor")
    db.add(EmployeeComponent(employee_id=employee.id, name="Leave allowance",
                             kind=EARNING, amount=50_000 * M, taxable=True))
    db.add(EmployeeComponent(employee_id=employee.id, name="Union dues",
                             kind=DEDUCTION, amount=2_000 * M, reduces_tax=False))
    db.flush()
    db.refresh(employee)

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    slip = run.payslips[0]
    assert slip.gross == (300_000 + 100_000 + 50_000 + 50_000) * M
    assert slip.other_deductions == 2_000 * M

    names = [l.name for l in slip.lines]
    assert "Leave allowance" in names
    assert "Union dues" in names
    assert "PAYE" in names

    PR.post_run(db, run)
    assert bal(db, "STAFF_DEDUCTIONS") == 2_000 * M
    assert_books_balance(db)


def test_a_percentage_allowance_is_worked_off_basic(db):
    employee = hire(db, "Ada", "Okafor", basic=400_000)
    db.add(EmployeeComponent(employee_id=employee.id, name="Utility allowance",
                             kind=EARNING, is_percentage=True, rate="10", taxable=True))
    db.flush()
    db.refresh(employee)

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    slip = run.payslips[0]
    assert slip.gross == (400_000 + 100_000 + 50_000 + 40_000) * M


# --------------------------------------------------------------------------
# Daily-rate staff
# --------------------------------------------------------------------------


def test_daily_rate_worker_paid_for_days_worked(db):
    hire(db, "Musa", "Ibrahim", basic=6_000, housing=0, transport=0,
         pay_basis=P.DAILY_RATE, default_units="20", pension_enrolled=False,
         nhf_enrolled=False)

    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    slip = run.payslips[0]
    assert slip.units == "20"
    assert slip.gross == 120_000 * M     # 20 days at ₦6,000

    PR.post_run(db, run)
    assert bal(db, "SALARIES") == 120_000 * M
    assert_books_balance(db)


def test_changing_days_worked_recalculates_the_payslip(db):
    hire(db, "Musa", "Ibrahim", basic=6_000, housing=0, transport=0,
         pay_basis=P.DAILY_RATE, default_units="20", pension_enrolled=False,
         nhf_enrolled=False)
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)

    PR.rebuild_payslip(db, run.payslips[0], "15")
    db.refresh(run)
    assert run.payslips[0].gross == 90_000 * M
    assert run.gross_total == 90_000 * M


def test_a_posted_run_cannot_be_recalculated(db):
    hire(db, "Musa", "Ibrahim", basic=6_000, pay_basis=P.DAILY_RATE, default_units="20")
    run = PR.build_run(db, P.MONTHLY, JUNE_START, JUNE_END, JUNE_END)
    PR.post_run(db, run)
    with pytest.raises(PostingError, match="posted"):
        PR.rebuild_payslip(db, run.payslips[0], "15")


# --------------------------------------------------------------------------
# The full picture
# --------------------------------------------------------------------------


def test_three_months_of_payroll_keeps_the_books_straight(db):
    hire(db, "Ada", "Okafor", basic=450_000, housing=150_000, transport=75_000,
         annual_rent_paid=2_400_000 * M)
    hire(db, "Bola", "Adeniyi", basic=250_000, housing=80_000, transport=40_000)
    hire(db, "Chidi", "Nwosu", basic=90_000, housing=20_000, transport=10_000)
    hire(db, "Musa", "Ibrahim", basic=6_500, housing=0, transport=0,
         pay_basis=P.DAILY_RATE, default_units="22", pension_enrolled=False)
    hire(db, "Ngozi", "Bello", basic=55_000, housing=0, transport=0)  # minimum wage

    bank = db.query(BankAccount).filter_by(is_default=True).one()
    # Fund the bank so it does not go negative
    from app.services.posting import EntryDraft, post_entry

    d = EntryDraft(date=date(2026, 5, 31), memo="Capital")
    d.debit(bank.account_id, 20_000_000 * M)
    d.credit(sys_account(db, "OPENING_EQUITY"), 20_000_000 * M)
    post_entry(db, d)

    total_paye = 0
    for month in (4, 5, 6):
        start = date(2026, month, 1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        run = PR.build_run(db, P.MONTHLY, start, end, end)
        PR.post_run(db, run)
        PR.pay_run(db, run, bank, end)
        total_paye += run.paye_total
        assert_books_balance(db)

    # Ngozi is on the minimum wage, so she never pays PAYE
    last = db.query(PayrollRun).order_by(PayrollRun.id.desc()).first()
    ngozi = [s for s in last.payslips if s.employee_name == "Ngozi Bello"][0]
    assert ngozi.paye == 0
    assert "minimum wage" in ngozi.paye_note

    # Ada's rent relief was applied
    ada = [s for s in last.payslips if s.employee_name == "Ada Okafor"][0]
    assert ada.rent_relief == 480_000 * M     # 20% of ₦2.4m, under the cap

    assert bal(db, "PAYE_PAYABLE") == total_paye
    assert bal(db, "WAGES_PAYABLE") == 0

    # Remit everything and confirm the liabilities clear
    for item in PR.outstanding_remittances(db):
        PR.post_remittance(db, item["account"], bank, item["balance"], date(2026, 7, 10))
    assert PR.outstanding_remittances(db) == []
    assert_books_balance(db)

    # The payroll cost shows up in the profit and loss
    pl = R.profit_and_loss(db, date(2026, 1, 1), date(2026, 12, 31))
    assert pl.expenses.total > 0
    assert bal(db, "SALARIES") > 0
