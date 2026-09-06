"""Payroll runs: building them, posting them, paying them.

The arithmetic lives in payroll.py. This module turns it into employees,
payslips and journal entries, and — like everything else — writes to the
ledger only through ``posting.post_entry``.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    ACTIVE,
    DEDUCTION,
    DRAFT,
    EARNING,
    ON_LEAVE,
    PAID,
    POSTED,
    VOID,
    Account,
    BankAccount,
    Employee,
    EmployeeLoan,
    JournalEntry,
    Payslip,
    PayslipLine,
    PayrollBand,
    PayrollRun,
    PayrollSetting,
    User,
)
from ..money import fmt
from . import payroll
from .posting import EntryDraft, PostingError, next_number, post_entry, reverse_entry, sys_account


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def settings(db: Session) -> PayrollSetting:
    s = db.get(PayrollSetting, 1)
    if s is None:
        s = PayrollSetting(id=1)
        db.add(s)
        db.flush()
    return s


def bands_for(db: Session) -> list[tuple[int | None, str]] | None:
    """The employer's own tax table, or None to use the built-in Nigerian one.

    An empty table counts as none: an employer who has switched custom bands on
    but not typed any yet must not silently pay nobody any tax.
    """
    s = settings(db)
    if not getattr(s, "use_custom_bands", False):
        return None
    rows = list(db.scalars(select(PayrollBand).order_by(PayrollBand.sort, PayrollBand.id)))
    if not rows:
        return None
    return [(r.width, r.rate or "0") for r in rows]


def rules_for(db: Session) -> payroll.PayrollRules:
    return payroll.PayrollRules.from_settings(settings(db), bands_for(db))


def employees_on(db: Session, frequency: str, on: Date) -> list[Employee]:
    """Everyone who should appear on a run of this frequency."""
    people = db.scalars(
        select(Employee)
        .where(Employee.frequency == frequency, Employee.status.in_((ACTIVE, ON_LEAVE)))
        .order_by(Employee.last_name, Employee.first_name)
    )
    out = []
    for e in people:
        if e.hire_date and e.hire_date > on:
            continue
        if e.leave_date and e.leave_date < on:
            continue
        out.append(e)
    return out


def itf_applies(db: Session) -> bool:
    """ITF is due from employers with 5+ staff, or turnover of ₦50m or more."""
    s = settings(db)
    if not s.operates_itf:
        return False
    count = len(list(db.scalars(select(Employee).where(Employee.status == ACTIVE))))
    from ..models import Company

    company = db.get(Company, 1)
    turnover = (
        payroll.ITF_TURNOVER_THRESHOLD
        if company and company.annual_turnover_band == "ABOVE_50M"
        else 0
    )
    return payroll.itf_applies_to(count, turnover)


# --------------------------------------------------------------------------
# Building a run
# --------------------------------------------------------------------------


def compute_for(
    db: Session, employee: Employee, units: str | Decimal = "1", rules=None
) -> payroll.PayslipResult:
    """Run one employee's numbers, honouring the company's scheme settings."""
    rules = rules or rules_for(db)
    s = settings(db)

    earnings, deductions = [], []
    for c in employee.components:
        if not c.is_active:
            continue
        amount = (
            round(employee.basic * Decimal(c.rate or "0") / 100)
            if c.is_percentage
            else c.amount
        )
        if not amount:
            continue
        if c.kind == EARNING:
            earnings.append(payroll.Earning(c.name, amount, c.taxable, c.pensionable))
        else:
            deductions.append(payroll.Deduction(c.name, amount, c.reduces_tax))

    loan_repayment = sum(
        min(l.repayment, l.balance) for l in employee.loans if l.is_active and l.balance > 0
    )

    return payroll.compute_payslip(
        basic=employee.basic,
        housing=employee.housing,
        transport=employee.transport,
        earnings=earnings,
        deductions=deductions,
        loan_repayment=loan_repayment,
        frequency=employee.frequency,
        units=units,
        annual_rent_paid=employee.annual_rent_paid,
        pension_enrolled=employee.pension_enrolled and s.operates_pension,
        nhf_enrolled=employee.nhf_enrolled and s.operates_nhf,
        nhis_enrolled=employee.nhis_enrolled and s.operates_nhis,
        paye_exempt=employee.paye_exempt,
        nsitf_applies=s.operates_nsitf,
        itf_applies=itf_applies(db),
        rules=rules,
    )


def _write_payslip(db: Session, run: PayrollRun, employee: Employee, units: str) -> Payslip:
    r = compute_for(db, employee, units)

    slip = Payslip(
        run_id=run.id,
        employee_id=employee.id,
        staff_no=employee.staff_no,
        employee_name=employee.full_name,
        job_title=employee.job_title,
        department=employee.department,
        bank_name=employee.bank_name,
        bank_account_no=employee.bank_account_no,
        units=str(units),
        basic=r.basic,
        housing=r.housing,
        transport=r.transport,
        gross=r.gross,
        pensionable=r.pensionable,
        pension_employee=r.pension_employee,
        pension_employer=r.pension_employer,
        nhf=r.nhf,
        nhis_employee=r.nhis_employee,
        nhis_employer=r.nhis_employer,
        nsitf=r.nsitf,
        itf=r.itf,
        annual_gross=r.annual_gross,
        rent_relief=r.rent_relief,
        annual_reliefs=r.annual_reliefs,
        annual_chargeable=r.annual_chargeable,
        annual_paye=r.annual_paye,
        paye=r.paye,
        paye_note=r.paye_exempt_reason[:255],
        loan_repayment=r.loan_repayment,
        other_deductions=r.other_deductions_total - r.loan_repayment,
        total_deductions=r.total_deductions,
        net_pay=r.net_pay,
        employer_cost=r.employer_cost,
    )
    db.add(slip)
    db.flush()

    # The itemised lines a payslip is expected to show
    by_name = {c.name: c for c in employee.components}
    sort = 0
    for e in r.earnings:
        sort += 1
        comp = by_name.get(e.name)
        db.add(PayslipLine(payslip_id=slip.id, name=e.name, kind=EARNING, amount=e.amount,
                           account_id=comp.account_id if comp else None, sort=sort))
    rules = rules_for(db)
    lines = [(rules.tax_name, r.paye)]
    for c in r.contributions:
        if c.employee:
            slot = rules.slot(c.key)
            rate = (slot.employee_rate or "").strip()
            lines.append((f"{c.name} ({rate}%)" if rate not in ("", "0") else c.name,
                          c.employee))
    for name, amount in lines:
        if amount and name:
            sort += 1
            db.add(PayslipLine(payslip_id=slip.id, name=name, kind=DEDUCTION,
                               amount=amount, is_statutory=True, sort=sort))
    for d in r.other_deductions:
        sort += 1
        comp = by_name.get(d.name)
        db.add(PayslipLine(payslip_id=slip.id, name=d.name, kind=DEDUCTION, amount=d.amount,
                           account_id=comp.account_id if comp else None, sort=sort))
    if r.loan_repayment:
        sort += 1
        db.add(PayslipLine(payslip_id=slip.id, name="Loan repayment", kind=DEDUCTION,
                           amount=r.loan_repayment, sort=sort))
    db.flush()
    return slip


def build_run(
    db: Session,
    frequency: str,
    period_start: Date,
    period_end: Date,
    pay_date: Date,
    user: User | None = None,
    units_by_employee: dict[int, str] | None = None,
) -> PayrollRun:
    """Create a draft run with a payslip for everyone due to be paid."""
    people = employees_on(db, frequency, period_end)
    if not people:
        raise PostingError(
            f"Nobody is set up on {payroll.FREQUENCY_LABELS.get(frequency, frequency).lower()} "
            "pay. Add employees first, or check their pay frequency."
        )

    run = PayrollRun(
        number=next_number(db, "PAYROLL"),
        frequency=frequency,
        period_start=period_start,
        period_end=period_end,
        pay_date=pay_date,
        status=DRAFT,
        created_by_id=user.id if user else None,
    )
    db.add(run)
    db.flush()

    units_by_employee = units_by_employee or {}
    for e in people:
        units = units_by_employee.get(e.id, e.default_units or "1")
        _write_payslip(db, run, e, units)

    recalc_run(db, run)
    return run


def rebuild_payslip(db: Session, slip: Payslip, units: str) -> Payslip:
    """Recompute one payslip — after changing days worked, say."""
    run = db.get(PayrollRun, slip.run_id)
    if run.status != DRAFT:
        raise PostingError(f"{run.number} has been posted and can no longer be changed.")
    employee = db.get(Employee, slip.employee_id)
    db.delete(slip)
    db.flush()
    fresh = _write_payslip(db, run, employee, units)
    recalc_run(db, run)
    return fresh


def recalc_run(db: Session, run: PayrollRun) -> PayrollRun:
    db.refresh(run)
    slips = run.payslips
    run.gross_total = sum(s.gross for s in slips)
    run.paye_total = sum(s.paye for s in slips)
    run.pension_employee_total = sum(s.pension_employee for s in slips)
    run.pension_employer_total = sum(s.pension_employer for s in slips)
    run.nhf_total = sum(s.nhf for s in slips)
    run.nhis_employee_total = sum(s.nhis_employee for s in slips)
    run.nhis_employer_total = sum(s.nhis_employer for s in slips)
    run.nsitf_total = sum(s.nsitf for s in slips)
    run.itf_total = sum(s.itf for s in slips)
    run.loan_total = sum(s.loan_repayment for s in slips)
    run.other_deductions_total = sum(s.other_deductions for s in slips)
    run.net_total = sum(s.net_pay for s in slips)
    run.employer_cost_total = sum(s.employer_cost for s in slips)
    db.flush()
    return run


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------


def post_run(db: Session, run: PayrollRun, user: User | None = None) -> JournalEntry:
    """Post the payroll to the ledger and set up every liability it creates."""
    if run.status != DRAFT:
        raise PostingError(f"{run.number} is already {run.status.lower()}.")
    if not run.payslips:
        raise PostingError("This run has no employees on it.")

    recalc_run(db, run)
    if run.gross_total <= 0:
        raise PostingError("This run comes to zero — there is nothing to post.")

    salaries = sys_account(db, "SALARIES")
    draft = EntryDraft(
        date=run.period_end,
        memo=f"Payroll {run.number} — {run.period_start:%d %b} to {run.period_end:%d %b %Y}",
        reference=run.number,
        source="PAYROLL",
        source_id=run.id,
    )

    # --- The cost to the business -----------------------------------------
    # Earnings go to Salaries and Wages unless a component names its own account.
    by_account: dict[int, int] = {}
    for slip in run.payslips:
        for line in slip.lines:
            if line.kind != EARNING:
                continue
            acc_id = line.account_id or salaries.id
            by_account[acc_id] = by_account.get(acc_id, 0) + line.amount
    for acc_id, amount in by_account.items():
        acc = db.get(Account, acc_id)
        draft.debit(acc_id, amount, f"Payroll {run.number} — {acc.name if acc else 'wages'}")

    for key, amount, label in (
        ("PENSION_EXPENSE", run.pension_employer_total, "Employer pension contribution"),
        ("NSITF_EXPENSE", run.nsitf_total, "NSITF employee compensation"),
        ("ITF_EXPENSE", run.itf_total, "Industrial Training Fund"),
        ("NHIS_EXPENSE", run.nhis_employer_total, "Employer NHIS contribution"),
    ):
        if amount:
            draft.debit(sys_account(db, key), amount, f"{label} — {run.number}")

    # --- What is now owed to whom ------------------------------------------
    for key, amount, label in (
        ("PAYE_PAYABLE", run.paye_total, "PAYE deducted"),
        ("PENSION_PAYABLE", run.pension_employee_total + run.pension_employer_total,
         "Pension due to PFAs"),
        ("NHF_PAYABLE", run.nhf_total, "NHF deducted"),
        ("NSITF_PAYABLE", run.nsitf_total, "NSITF due"),
        ("ITF_PAYABLE", run.itf_total, "ITF due"),
        ("NHIS_PAYABLE", run.nhis_employee_total + run.nhis_employer_total, "NHIS due"),
    ):
        if amount:
            draft.credit(sys_account(db, key), amount, f"{label} — {run.number}")

    # Loan repayments reduce the advance, which is an asset
    if run.loan_total:
        draft.credit(_account_by_code(db, "1310"), run.loan_total,
                     f"Staff loan repayments — {run.number}")

    # Other deductions to their own account where one is named
    other_by_account: dict[int, int] = {}
    staff_deductions = sys_account(db, "STAFF_DEDUCTIONS")
    for slip in run.payslips:
        for line in slip.lines:
            if line.kind != DEDUCTION or line.is_statutory or line.name == "Loan repayment":
                continue
            acc_id = line.account_id or staff_deductions.id
            other_by_account[acc_id] = other_by_account.get(acc_id, 0) + line.amount
    for acc_id, amount in other_by_account.items():
        draft.credit(acc_id, amount, f"Staff deductions — {run.number}")

    draft.credit(sys_account(db, "WAGES_PAYABLE"), run.net_total,
                 f"Net pay due to staff — {run.number}")

    entry = post_entry(db, draft, user=user)
    run.journal_entry_id = entry.id
    run.status = POSTED
    run.posted_at = clock.now()

    # Work the loan balances down
    for slip in run.payslips:
        if not slip.loan_repayment:
            continue
        left = slip.loan_repayment
        for loan in db.scalars(
            select(EmployeeLoan)
            .where(EmployeeLoan.employee_id == slip.employee_id,
                   EmployeeLoan.is_active.is_(True))
            .order_by(EmployeeLoan.date)
        ):
            if left <= 0:
                break
            take = min(left, loan.balance)
            loan.balance -= take
            left -= take
            if loan.balance <= 0:
                loan.is_active = False
    db.flush()
    return entry


def _account_by_code(db: Session, code: str) -> Account:
    acc = db.scalar(select(Account).where(Account.code == code))
    if acc is None:
        raise PostingError(f"Account {code} is missing from the chart of accounts.")
    return acc


def pay_run(
    db: Session,
    run: PayrollRun,
    bank: BankAccount,
    on: Date | None = None,
    user: User | None = None,
) -> JournalEntry:
    """Pay the net wages out of a bank account."""
    if run.status != POSTED:
        raise PostingError(
            "Post the payroll to the ledger before paying it."
            if run.status == DRAFT
            else f"{run.number} is already {run.status.lower()}."
        )
    if run.net_total <= 0:
        raise PostingError("There is no net pay to settle.")

    on = on or run.pay_date
    draft = EntryDraft(
        date=on,
        memo=f"Net salaries paid — payroll {run.number}",
        reference=run.number,
        source="PAYROLL",
        source_id=run.id,
    )
    draft.debit(sys_account(db, "WAGES_PAYABLE"), run.net_total,
                f"Net pay settled — {run.number}")
    draft.credit(bank.account_id, run.net_total,
                 f"Salaries for {run.period_end:%B %Y} paid from {bank.name}")

    entry = post_entry(db, draft, user=user)
    run.payment_entry_id = entry.id
    run.paid_from_bank_id = bank.id
    run.status = PAID
    db.flush()
    return entry


def void_run(db: Session, run: PayrollRun, on: Date | None = None, user: User | None = None) -> None:
    if run.status == VOID:
        raise PostingError(f"{run.number} is already void.")
    on = on or run.period_end

    for entry_id in (run.payment_entry_id, run.journal_entry_id):
        if not entry_id:
            continue
        entry = db.get(JournalEntry, entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, on=on, user=user, memo=f"Void of payroll {run.number}")

    # Put any loan balances back
    for slip in run.payslips:
        if not slip.loan_repayment:
            continue
        left = slip.loan_repayment
        for loan in db.scalars(
            select(EmployeeLoan)
            .where(EmployeeLoan.employee_id == slip.employee_id)
            .order_by(EmployeeLoan.date.desc())
        ):
            if left <= 0:
                break
            room = loan.principal - loan.balance
            give = min(left, room)
            loan.balance += give
            left -= give
            if loan.balance > 0:
                loan.is_active = True

    run.status = VOID
    db.flush()


# --------------------------------------------------------------------------
# Remittances
# --------------------------------------------------------------------------

REMITTANCE_ACCOUNTS = [
    ("PAYE_PAYABLE", "PAYE", "PAYE to the State Internal Revenue Service",
     "by the 10th of the following month"),
    ("PENSION_PAYABLE", "PENSION", "Pension to the employees' PFAs",
     "within 7 working days of payday"),
    ("NHF_PAYABLE", "NHF", "NHF to the Federal Mortgage Bank of Nigeria",
     "within 1 month of deduction"),
    ("NSITF_PAYABLE", "NSITF", "NSITF employee compensation contribution",
     "by the 16th of the following month"),
    ("ITF_PAYABLE", "ITF", "Industrial Training Fund contribution",
     "annually, by 1 April"),
    ("NHIS_PAYABLE", "NHIS", "NHIS contributions", "monthly, with the scheme"),
    ("STAFF_DEDUCTIONS", "OTHER", "Other staff deductions held", "as agreed"),
]


def outstanding_remittances(db: Session, as_of: Date | None = None) -> list[dict]:
    """What is sitting in each payroll liability account, and when it is due."""
    from .posting import account_net

    as_of = as_of or Date.today()
    out = []
    for key, code, label, deadline in REMITTANCE_ACCOUNTS:
        acc = db.scalar(select(Account).where(Account.system_key == key))
        if acc is None:
            continue
        balance = account_net(db, acc.id, None, as_of)
        if balance == 0:
            continue
        out.append({
            "key": key, "code": code, "label": label, "deadline": deadline,
            "account": acc, "balance": balance,
        })
    return out


def post_remittance(
    db: Session,
    account: Account,
    bank: BankAccount,
    amount: int,
    on: Date,
    reference: str = "",
    memo: str = "",
    user: User | None = None,
) -> JournalEntry:
    """Pay a payroll liability — PAYE, pension, NHF and the rest — to whoever is owed."""
    if amount <= 0:
        raise PostingError("Enter the amount being remitted.")
    draft = EntryDraft(
        date=on,
        memo=memo or f"Remittance of {account.name}",
        reference=reference,
        source="PAYROLL",
    )
    draft.debit(account, amount, memo or f"{account.name} remitted")
    draft.credit(bank.account_id, amount, f"Paid from {bank.name}")
    return post_entry(db, draft, user=user)


# --------------------------------------------------------------------------
# Staff loans
# --------------------------------------------------------------------------


def grant_loan(
    db: Session,
    employee: Employee,
    amount: int,
    repayment: int,
    on: Date,
    bank: BankAccount | None,
    description: str = "Staff loan",
    user: User | None = None,
) -> EmployeeLoan:
    """Advance money to an employee, recovered from later payslips."""
    if amount <= 0:
        raise PostingError("A loan must be for a positive amount.")
    if repayment <= 0:
        raise PostingError("Set how much to recover from each payslip.")

    loan = EmployeeLoan(
        employee_id=employee.id,
        date=on,
        description=description,
        principal=amount,
        balance=amount,
        repayment=repayment,
        is_active=True,
    )
    db.add(loan)
    db.flush()

    if bank is not None:
        advances = _account_by_code(db, "1310")
        draft = EntryDraft(
            date=on,
            memo=f"{description} — {employee.full_name}",
            reference=f"LOAN-{loan.id}",
            source="PAYROLL",
        )
        draft.debit(advances, amount, f"{description} to {employee.full_name}")
        draft.credit(bank.account_id, amount, f"Paid from {bank.name}")
        entry = post_entry(db, draft, user=user)
        loan.journal_entry_id = entry.id
        db.flush()
    return loan
