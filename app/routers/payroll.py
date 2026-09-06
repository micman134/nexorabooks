"""Payroll: employees, pay runs, payslips, remittances and payroll reports."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import func, or_, select

from ..models import (
    CONTRIBUTION_BASES,
    ACTIVE,
    DEDUCTION,
    DRAFT,
    EARNING,
    EMPLOYEE_STATUSES,
    LEFT,
    PAID,
    POSTED,
    VOID,
    Account,
    BankAccount,
    Employee,
    EmployeeComponent,
    EmployeeLoan,
    JournalEntry,
    Payslip,
    PayrollBand,
    PayrollCheck,
    PayrollRun,
)
from ..money import fmt
from ..security import P_ADMIN, P_ENTRY, P_JOURNAL, P_VIEW, P_VOID
from ..services import payroll as P
from ..services import payroll_check as PC
from ..services import payroll_run as PR
from ..services.posting import PostingError, audit, next_number
from ._common import (
    client_ip,
    db_of,
    month_bounds,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_int,
    parse_money,
    period_from_query,
    redirect,
)

router = APIRouter(prefix="/payroll")


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    runs = list(
        db.scalars(select(PayrollRun).order_by(PayrollRun.period_end.desc(),
                                               PayrollRun.id.desc()).limit(24))
    )
    headcount = db.scalar(
        select(func.count(Employee.id)).where(Employee.status == ACTIVE)
    ) or 0
    remittances = PR.outstanding_remittances(db)

    this_year = [r for r in runs if r.status != VOID
                 and r.period_end.year == date.today().year]
    return render(
        request, "payroll/index.html",
        runs=runs, headcount=headcount, remittances=remittances,
        remit_total=sum(r["balance"] for r in remittances),
        ytd_gross=sum(r.gross_total for r in this_year),
        ytd_paye=sum(r.paye_total for r in this_year),
        ytd_cost=sum(r.employer_cost_total for r in this_year),
        settings=PR.settings(db),
    )


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------


@router.get("/employees")
def employees(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    show_left = parse_bool(request.query_params.get("left"))

    stmt = select(Employee)
    if not show_left:
        stmt = stmt.where(Employee.status != LEFT)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                Employee.staff_no.ilike(like), Employee.job_title.ilike(like),
                Employee.department.ilike(like))
        )
    people = list(db.scalars(stmt.order_by(Employee.last_name, Employee.first_name)))
    monthly_cost = sum(
        PR.compute_for(db, e).employer_cost for e in people if e.is_on_payroll
    )
    return render(
        request, "payroll/employees.html",
        people=people, q=q, show_left=show_left, monthly_cost=monthly_cost,
        statuses=EMPLOYEE_STATUSES,
    )


@router.get("/employees/new")
def employee_new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    employee = Employee(staff_no="", first_name="", last_name="")
    return render(request, "payroll/employee_form.html", employee=employee, is_new=True,
                  accounts=_deduction_accounts(db), statuses=EMPLOYEE_STATUSES,
                  P=P, settings=PR.settings(db))


def _deduction_accounts(db):
    return list(
        db.scalars(
            select(Account)
            .where(Account.type.in_(("LIABILITY", "EXPENSE", "ASSET")),
                   Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


@router.get("/employees/{emp_id}")
def employee_detail(request: Request, emp_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    employee = db.get(Employee, emp_id)
    if employee is None:
        return redirect("/payroll/employees")

    preview = PR.compute_for(db, employee)
    slips = list(
        db.scalars(
            select(Payslip).join(PayrollRun, Payslip.run_id == PayrollRun.id)
            .where(Payslip.employee_id == emp_id, PayrollRun.status != VOID)
            .order_by(PayrollRun.period_end.desc()).limit(24)
        )
    )
    ytd = {
        "gross": sum(s.gross for s in slips if s.run.period_end.year == date.today().year),
        "paye": sum(s.paye for s in slips if s.run.period_end.year == date.today().year),
        "pension": sum(s.pension_employee for s in slips
                       if s.run.period_end.year == date.today().year),
    }
    banks = list(
        db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                   .order_by(BankAccount.sort))
    )
    from ..services import attachments as A

    return render(
        request, "payroll/employee_detail.html",
        employee=employee, preview=preview, slips=slips, ytd=ytd, banks=banks,
        files=A.list_for(db, "EMPLOYEE", employee.id),
        P=P, today=date.today(),
    )


@router.get("/employees/{emp_id}/edit")
def employee_edit(request: Request, emp_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    employee = db.get(Employee, emp_id)
    if employee is None:
        return redirect("/payroll/employees")
    return render(request, "payroll/employee_form.html", employee=employee, is_new=False,
                  accounts=_deduction_accounts(db), statuses=EMPLOYEE_STATUSES,
                  P=P, settings=PR.settings(db))


@router.post("/employees/save")
async def employee_save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    emp_id = parse_id(form.get("id"))

    first = (form.get("first_name") or "").strip()
    last = (form.get("last_name") or "").strip()
    if not first or not last:
        flash(request, "An employee needs a first and last name.", "danger")
        return redirect("/payroll/employees/new")

    employee = db.get(Employee, emp_id) if emp_id else None
    is_new = employee is None
    if is_new:
        employee = Employee(
            staff_no=(form.get("staff_no") or "").strip() or next_number(db, "EMPLOYEE"),
            first_name=first, last_name=last,
        )
        db.add(employee)
        db.flush()
    elif form.get("staff_no"):
        employee.staff_no = form.get("staff_no").strip()

    employee.first_name = first
    employee.last_name = last
    employee.middle_name = (form.get("middle_name") or "").strip()
    employee.status = form.get("status") or ACTIVE
    employee.is_director = parse_bool(form.get("is_director"))
    employee.job_title = (form.get("job_title") or "").strip()
    employee.department = (form.get("department") or "").strip()
    employee.hire_date = parse_date(form.get("hire_date"), None) if form.get("hire_date") else None
    employee.leave_date = parse_date(form.get("leave_date"), None) if form.get("leave_date") else None
    employee.date_of_birth = (
        parse_date(form.get("date_of_birth"), None) if form.get("date_of_birth") else None
    )
    employee.gender = form.get("gender") or ""
    employee.email = (form.get("email") or "").strip()
    employee.phone = (form.get("phone") or "").strip()
    employee.address = form.get("address") or ""
    employee.state_of_residence = (form.get("state_of_residence") or "").strip()
    employee.tin = (form.get("tin") or "").strip()
    employee.nin = (form.get("nin") or "").strip()
    employee.pfa_name = (form.get("pfa_name") or "").strip()
    employee.pension_pin = (form.get("pension_pin") or "").strip()
    employee.nhf_number = (form.get("nhf_number") or "").strip()
    employee.bank_name = (form.get("bank_name") or "").strip()
    employee.bank_account_no = (form.get("bank_account_no") or "").strip()
    employee.bank_account_name = (form.get("bank_account_name") or "").strip() or employee.full_name

    employee.frequency = form.get("frequency") or P.MONTHLY
    employee.pay_basis = form.get("pay_basis") or P.FIXED
    employee.basic = parse_money(form.get("basic"))
    employee.housing = parse_money(form.get("housing"))
    employee.transport = parse_money(form.get("transport"))
    employee.default_units = (form.get("default_units") or "1").strip() or "1"

    employee.pension_enrolled = parse_bool(form.get("pension_enrolled"))
    employee.nhf_enrolled = parse_bool(form.get("nhf_enrolled"))
    employee.nhis_enrolled = parse_bool(form.get("nhis_enrolled"))
    employee.paye_exempt = parse_bool(form.get("paye_exempt"))
    employee.annual_rent_paid = parse_money(form.get("annual_rent_paid"))
    employee.notes = form.get("notes") or ""
    db.flush()

    # Rebuild the recurring allowances and deductions
    for old in list(employee.components):
        db.delete(old)
    db.flush()
    employee.components = []

    get = lambda k, i: (form.getlist(k)[i] if i < len(form.getlist(k)) else None)  # noqa: E731
    names = form.getlist("comp_name")
    for i, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        amount = parse_money(get("comp_amount", i))
        rate = (get("comp_rate", i) or "").strip()
        if not name or (not amount and not rate):
            continue
        comp = EmployeeComponent(
            employee_id=employee.id,
            name=name,
            kind=get("comp_kind", i) or EARNING,
            amount=amount,
            is_percentage=bool(rate),
            rate=rate,
            taxable=parse_bool(get("comp_taxable", i)),
            pensionable=parse_bool(get("comp_pensionable", i)),
            reduces_tax=parse_bool(get("comp_reduces_tax", i)),
            account_id=parse_id(get("comp_account", i)),
            sort=i + 1,
        )
        db.add(comp)
        employee.components.append(comp)
    db.flush()

    audit(db, user, "CREATE" if is_new else "UPDATE", "Employee", employee.id,
          detail=employee.full_name, ip=client_ip(request))
    db.commit()
    flash(request, f"{employee.full_name} saved.")
    return redirect(f"/payroll/employees/{employee.id}")


@router.post("/employees/{emp_id}/loan")
async def employee_loan(request: Request, emp_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    employee = db.get(Employee, emp_id)
    bank_id = parse_id(form.get("bank_account_id"))
    bank = db.get(BankAccount, bank_id) if bank_id else None
    try:
        loan = PR.grant_loan(
            db, employee,
            amount=parse_money(form.get("amount")),
            repayment=parse_money(form.get("repayment")),
            on=parse_date(form.get("date")),
            bank=bank,
            description=(form.get("description") or "Staff loan").strip(),
            user=user,
        )
        audit(db, user, "LOAN", "Employee", employee.id,
              detail=f"{fmt(loan.principal)} to {employee.full_name}", ip=client_ip(request))
        db.commit()
        flash(request, f"{fmt(loan.principal)} advanced to {employee.full_name}, "
                       f"recovering {fmt(loan.repayment)} each payslip.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/payroll/employees/{emp_id}")


@router.post("/employees/{emp_id}/loan/{loan_id}/write-off")
def loan_stop(request: Request, emp_id: int, loan_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    loan = db.get(EmployeeLoan, loan_id)
    if loan:
        loan.is_active = False
        audit(db, user, "LOAN_STOP", "Employee", emp_id,
              detail=f"Stopped recovering {loan.description}", ip=client_ip(request))
        db.commit()
        flash(request, "Recovery stopped. The balance stays on record.", "warning")
    return redirect(f"/payroll/employees/{emp_id}")


# --------------------------------------------------------------------------
# Pay runs
# --------------------------------------------------------------------------


@router.get("/runs/new")
def run_new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    today = date.today()
    start, end = month_bounds(today)
    counts = {
        f: len(PR.employees_on(db, f, end)) for f in P.PERIODS_PER_YEAR
    }
    return render(request, "payroll/run_new.html", start=start, end=end,
                  pay_date=end, counts=counts, P=P)


@router.post("/runs/create")
async def run_create(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    frequency = form.get("frequency") or P.MONTHLY
    start = parse_date(form.get("period_start"))
    end = parse_date(form.get("period_end"))
    pay_date = parse_date(form.get("pay_date"), end)

    if end < start:
        flash(request, "The period ends before it starts — check the dates.", "danger")
        return redirect("/payroll/runs/new")

    clash = db.scalar(
        select(PayrollRun).where(
            PayrollRun.frequency == frequency,
            PayrollRun.period_start == start,
            PayrollRun.period_end == end,
            PayrollRun.status != VOID,
        )
    )
    if clash:
        flash(request, f"{clash.number} already covers that period. "
                       "Void it first if you need to run it again.", "warning")
        return redirect(f"/payroll/runs/{clash.id}")

    try:
        run = PR.build_run(db, frequency, start, end, pay_date, user)
        audit(db, user, "CREATE", "PayrollRun", run.id,
              detail=f"{run.number} — {run.employee_count} employees", ip=client_ip(request))
        db.commit()
        flash(request, f"{run.number} drafted for {run.employee_count} employee(s). "
                       "Check it over, then post it.")
        return redirect(f"/payroll/runs/{run.id}")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/payroll/runs/new")


@router.get("/runs/{run_id}")
def run_detail(request: Request, run_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    if run is None:
        return redirect("/payroll")
    entry = db.get(JournalEntry, run.journal_entry_id) if run.journal_entry_id else None
    payment = db.get(JournalEntry, run.payment_entry_id) if run.payment_entry_id else None
    banks = list(
        db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                   .order_by(BankAccount.sort))
    )
    return render(request, "payroll/run_detail.html", run=run, entry=entry,
                  payment=payment, banks=banks, P=P, today=date.today())


@router.post("/runs/{run_id}/units")
async def run_units(request: Request, run_id: int):
    from ..main import flash

    need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    run = db.get(PayrollRun, run_id)
    if run is None or run.status != DRAFT:
        flash(request, "Only a draft run can be changed.", "danger")
        return redirect(f"/payroll/runs/{run_id}")

    ids = form.getlist("payslip_id")
    values = form.getlist("units")
    changed = 0
    try:
        for i, raw in enumerate(ids):
            slip_id = parse_id(raw)
            new_units = (values[i] if i < len(values) else "1") or "1"
            slip = db.get(Payslip, slip_id)
            if slip and str(slip.units) != str(new_units):
                PR.rebuild_payslip(db, slip, new_units)
                changed += 1
        db.commit()
        flash(request, f"{changed} payslip(s) recalculated." if changed
              else "Nothing needed changing.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.post("/runs/{run_id}/remove/{slip_id}")
def run_remove(request: Request, run_id: int, slip_id: int):
    from ..main import flash

    need(request, P_ENTRY)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    slip = db.get(Payslip, slip_id)
    if run and run.status == DRAFT and slip:
        name = slip.employee_name
        db.delete(slip)
        db.flush()
        PR.recalc_run(db, run)
        db.commit()
        flash(request, f"{name} taken off this run.", "warning")
    else:
        flash(request, "Only a draft run can be changed.", "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.post("/runs/{run_id}/post")
def run_post(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    try:
        PR.post_run(db, run, user)
        audit(db, user, "POST", "PayrollRun", run.id,
              detail=f"{run.number} gross {fmt(run.gross_total)}", ip=client_ip(request))
        db.commit()
        flash(request, f"{run.number} posted. Net pay of {fmt(run.net_total)} is now owed to "
                       f"staff, and {fmt(run.paye_total)} of PAYE to the tax office.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.post("/runs/{run_id}/pay")
async def run_pay(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    run = db.get(PayrollRun, run_id)
    bank = db.get(BankAccount, parse_id(form.get("bank_account_id")))
    if bank is None:
        flash(request, "Choose the account the salaries were paid from.", "danger")
        return redirect(f"/payroll/runs/{run_id}")
    try:
        PR.pay_run(db, run, bank, parse_date(form.get("pay_date"), run.pay_date), user)
        audit(db, user, "PAY", "PayrollRun", run.id,
              detail=f"{run.number} net {fmt(run.net_total)} from {bank.name}",
              ip=client_ip(request))
        db.commit()
        flash(request, f"{fmt(run.net_total)} paid out of {bank.name}.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.post("/runs/{run_id}/void")
async def run_void(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    form = await request.form()
    run = db.get(PayrollRun, run_id)
    try:
        PR.void_run(db, run, parse_date(form.get("void_date"), date.today()), user)
        audit(db, user, "VOID", "PayrollRun", run.id, detail=run.number, ip=client_ip(request))
        db.commit()
        flash(request, f"{run.number} voided and reversed in the ledger.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.post("/runs/{run_id}/delete")
def run_delete(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    if run and run.status == DRAFT:
        number = run.number
        audit(db, user, "DELETE", "PayrollRun", run.id, detail=number, ip=client_ip(request))
        db.delete(run)
        db.commit()
        flash(request, f"Draft {number} deleted.", "warning")
        return redirect("/payroll")
    flash(request, "Only a draft can be deleted. Posted runs are voided.", "danger")
    return redirect(f"/payroll/runs/{run_id}")


@router.get("/runs/{run_id}/payslips")
def run_payslips(request: Request, run_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    return render(request, "payroll/payslips.html", run=run,
                  slips=run.payslips, settings=PR.settings(db))


@router.get("/runs/{run_id}/bank-schedule")
def run_bank_schedule(request: Request, run_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    return render(request, "payroll/bank_schedule.html", run=run)


@router.get("/payslips/{slip_id}/pdf")
def payslip_pdf(request: Request, slip_id: int):
    from fastapi.responses import Response

    from ..services import pdfdocs

    need(request, P_VIEW)
    db = db_of(request)
    slip = db.get(Payslip, slip_id)
    if slip is None:
        return redirect("/payroll")
    settings = PR.settings(db)
    data = pdfdocs.payslip_pdf(db, slip, slug=request.state.company_slug,
                               note=settings.payslip_note or "")
    name = f"Payslip {slip.staff_no} {slip.run.number if slip.run else ''}.pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@router.get("/payslips/{slip_id}")
def payslip(request: Request, slip_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    slip = db.get(Payslip, slip_id)
    if slip is None:
        return redirect("/payroll")
    return render(request, "payroll/payslips.html", run=slip.run, slips=[slip],
                  settings=PR.settings(db), single=True)


# --------------------------------------------------------------------------
# Remittances
# --------------------------------------------------------------------------


@router.get("/remittances")
def remittances(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    banks = list(
        db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                   .order_by(BankAccount.sort))
    )
    items = PR.outstanding_remittances(db)
    return render(request, "payroll/remittances.html", items=items, banks=banks,
                  total=sum(i["balance"] for i in items), today=date.today())


@router.post("/remittances/pay")
async def remittance_pay(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    account = db.get(Account, parse_id(form.get("account_id")))
    bank = db.get(BankAccount, parse_id(form.get("bank_account_id")))
    amount = parse_money(form.get("amount"))
    if account is None or bank is None:
        flash(request, "Choose what you are remitting and which account it came from.", "danger")
        return redirect("/payroll/remittances")
    try:
        entry = PR.post_remittance(
            db, account, bank, amount, parse_date(form.get("date")),
            reference=(form.get("reference") or "").strip(),
            memo=(form.get("memo") or "").strip(),
            user=user,
        )
        audit(db, user, "REMIT", "JournalEntry", entry.id,
              detail=f"{account.name} {fmt(amount)}", ip=client_ip(request))
        db.commit()
        flash(request, f"{fmt(amount)} remitted from {bank.name} against {account.name}.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect("/payroll/remittances")


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


@router.get("/reports/schedules")
def schedules(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    kind = request.query_params.get("kind", "paye")

    runs = list(
        db.scalars(
            select(PayrollRun)
            .where(PayrollRun.period_end >= start, PayrollRun.period_end <= end,
                   PayrollRun.status != VOID, PayrollRun.status != DRAFT)
            .order_by(PayrollRun.period_end)
        )
    )
    rows: dict[int, dict] = {}
    for run in runs:
        for slip in run.payslips:
            r = rows.setdefault(slip.employee_id, {
                "employee": slip.employee, "name": slip.employee_name,
                "staff_no": slip.staff_no, "gross": 0, "paye": 0,
                "pension_ee": 0, "pension_er": 0, "nhf": 0, "nhis": 0, "net": 0,
            })
            r["gross"] += slip.gross
            r["paye"] += slip.paye
            r["pension_ee"] += slip.pension_employee
            r["pension_er"] += slip.pension_employer
            r["nhf"] += slip.nhf
            r["nhis"] += slip.nhis_employee
            r["net"] += slip.net_pay

    data = sorted(rows.values(), key=lambda r: r["name"])
    totals = {
        k: sum(r[k] for r in data)
        for k in ("gross", "paye", "pension_ee", "pension_er", "nhf", "nhis", "net")
    }

    if request.query_params.get("format") == "csv":
        from ..money import fmt_plain
        from .reports import csv_response

        return csv_response(
            f"{kind}-schedule-{start}-to-{end}.csv",
            ["Staff no", "Name", "TIN", "Pension PIN", "Gross", "PAYE",
             "Pension (employee)", "Pension (employer)", "NHF", "NHIS", "Net pay"],
            [[r["staff_no"], r["name"],
              r["employee"].tin if r["employee"] else "",
              r["employee"].pension_pin if r["employee"] else "",
              fmt_plain(r["gross"]), fmt_plain(r["paye"]),
              fmt_plain(r["pension_ee"]), fmt_plain(r["pension_er"]),
              fmt_plain(r["nhf"]), fmt_plain(r["nhis"]), fmt_plain(r["net"])]
             for r in data],
        )

    return render(request, "payroll/schedules.html", rows=data, totals=totals,
                  start=start, end=end, preset=preset, kind=kind, runs=runs,
                  settings=PR.settings(db))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def _bands(db) -> list:
    """The employer's own bands, or the built-in ones shown for editing."""
    rows = list(db.scalars(select(PayrollBand).order_by(PayrollBand.sort, PayrollBand.id)))
    if rows:
        return rows
    return [PayrollBand(sort=i, width=w, rate=r)
            for i, (w, r) in enumerate(P.PAYE_BANDS)]


@router.get("/settings")
def payroll_settings(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    total, passing, failing = PC.summary(db)
    return render(request, "payroll/settings.html", s=PR.settings(db), P=P,
                  itf_applies=PR.itf_applies(db),
                  bands=_bands(db), bases=CONTRIBUTION_BASES,
                  rules=PR.rules_for(db),
                  checks_total=total, checks_passing=passing, checks_failing=failing,
                  verified=PC.is_verified(db))


@router.post("/settings")
async def payroll_settings_save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    s = PR.settings(db)

    s.minimum_wage = parse_money(form.get("minimum_wage")) or s.minimum_wage
    s.rent_relief_rate = (form.get("rent_relief_rate") or s.rent_relief_rate).strip()
    s.rent_relief_cap = parse_money(form.get("rent_relief_cap")) or s.rent_relief_cap
    s.pension_employee = (form.get("pension_employee") or s.pension_employee).strip()
    s.pension_employer = (form.get("pension_employer") or s.pension_employer).strip()
    s.nhf_rate = (form.get("nhf_rate") or s.nhf_rate).strip()
    s.nsitf_rate = (form.get("nsitf_rate") or s.nsitf_rate).strip()
    s.itf_rate = (form.get("itf_rate") or s.itf_rate).strip()
    s.nhis_employee = (form.get("nhis_employee") or s.nhis_employee).strip()
    s.nhis_employer = (form.get("nhis_employer") or s.nhis_employer).strip()

    s.operates_pension = parse_bool(form.get("operates_pension"))
    s.operates_nhf = parse_bool(form.get("operates_nhf"))
    s.operates_nsitf = parse_bool(form.get("operates_nsitf"))
    s.operates_itf = parse_bool(form.get("operates_itf"))
    s.operates_nhis = parse_bool(form.get("operates_nhis"))
    s.paye_state = (form.get("paye_state") or "").strip()
    s.default_pfa = (form.get("default_pfa") or "").strip()
    s.payslip_note = form.get("payslip_note") or ""

    # --- What this country calls things ----------------------------------
    s.scheme_name = (form.get("scheme_name") or s.scheme_name).strip()
    s.tax_name = (form.get("tax_name") or "Income Tax").strip()
    s.threshold_name = (form.get("threshold_name") or "the tax-free threshold").strip()
    s.relief_name = (form.get("relief_name") or "Relief").strip()

    # --- The five contributions, named and based as the employer needs ----
    for key, prefix in (("PENSION", "pension"), ("NHF", "nhf"), ("NHIS", "nhis"),
                        ("NSITF", "nsitf"), ("ITF", "itf")):
        name = (form.get(f"{prefix}_name") or "").strip()
        if name:
            setattr(s, f"{prefix}_name", name)
        base = (form.get(f"{prefix}_base") or "").strip().upper()
        if base in CONTRIBUTION_BASES:
            setattr(s, f"{prefix}_base", base)
        if prefix in ("pension", "nhis"):
            setattr(s, f"{prefix}_employee_cap", parse_money(form.get(f"{prefix}_employee_cap")))
            setattr(s, f"{prefix}_employer_cap", parse_money(form.get(f"{prefix}_employer_cap")))
        else:
            setattr(s, f"{prefix}_cap", parse_money(form.get(f"{prefix}_cap")))
        if prefix in ("pension", "nhf", "nhis"):
            setattr(s, f"{prefix}_reduces_tax", parse_bool(form.get(f"{prefix}_reduces_tax")))

    # --- The tax table ----------------------------------------------------
    s.use_custom_bands = parse_bool(form.get("use_custom_bands"))
    widths = form.getlist("band_width")
    rates = form.getlist("band_rate")
    if widths:
        for row in db.scalars(select(PayrollBand)):
            db.delete(row)
        db.flush()
        kept = 0
        for i, (width, rate) in enumerate(zip(widths, rates)):
            rate = (rate or "").strip()
            width_value = parse_money(width)
            if not rate and not width_value:
                continue
            last = i == len(widths) - 1
            db.add(PayrollBand(sort=kept, width=None if (last or not width_value)
                               else width_value, rate=rate or "0"))
            kept += 1
        db.flush()

    # Any change to the rates puts every known-answer check back in question.
    PC.run_all(db)

    audit(db, user, "UPDATE", "PayrollSetting", 1, detail="Payroll rates updated",
          ip=client_ip(request))
    db.commit()
    flash(request, "Payroll settings saved. They apply to the next run you create.")
    return redirect("/payroll/settings")


# --------------------------------------------------------------------------
# Checking the scheme against a salary whose answer is already known
# --------------------------------------------------------------------------


@router.get("/checks")
def checks(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    outcomes = [PC.run(db, c, record=False) for c in PC.all_checks(db)]
    return render(request, "payroll/checks.html", outcomes=outcomes, P=P,
                  rules=PR.rules_for(db), s=PR.settings(db),
                  verified=PC.is_verified(db))


@router.post("/checks/save")
async def check_save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()

    check = db.get(PayrollCheck, parse_id(form.get("id"))) if form.get("id") else None
    if check is None:
        check = PayrollCheck()
        db.add(check)

    check.name = (form.get("name") or "Untitled check").strip()
    check.note = form.get("note") or ""
    check.basic = parse_money(form.get("basic"))
    check.housing = parse_money(form.get("housing"))
    check.transport = parse_money(form.get("transport"))
    check.frequency = form.get("frequency") or P.MONTHLY
    check.annual_rent_paid = parse_money(form.get("annual_rent_paid"))
    check.pension_enrolled = parse_bool(form.get("pension_enrolled"))
    check.nhf_enrolled = parse_bool(form.get("nhf_enrolled"))
    check.nhis_enrolled = parse_bool(form.get("nhis_enrolled"))
    check.expected_gross = parse_money(form.get("expected_gross"))
    check.expected_tax = parse_money(form.get("expected_tax"))
    check.expected_net = parse_money(form.get("expected_net"))
    check.tolerance = parse_money(form.get("tolerance"))
    db.flush()

    outcome = PC.run(db, check)
    PC.mark(db, [PC.run(db, c, record=False) for c in PC.all_checks(db)])
    audit(db, user, "UPDATE", "PayrollCheck", check.id, detail=check.name,
          ip=client_ip(request))
    db.commit()
    flash(request, {
        PC.PASS: f"{check.name} — the scheme reproduces it.",
        PC.FAIL: f"{check.name} — the scheme does not reproduce it. {outcome.detail}",
    }.get(outcome.verdict, f"{check.name} saved. Enter the expected figures to check it."),
        "success" if outcome.verdict != PC.FAIL else "danger")
    return redirect("/payroll/checks")


@router.post("/checks/{check_id}/delete")
def check_delete(request: Request, check_id: int):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    check = db.get(PayrollCheck, check_id)
    if check is not None:
        audit(db, user, "DELETE", "PayrollCheck", check.id, detail=check.name,
              ip=client_ip(request))
        db.delete(check)
        db.flush()
        PC.mark(db, [PC.run(db, c, record=False) for c in PC.all_checks(db)])
        db.commit()
        flash(request, "Check removed.")
    return redirect("/payroll/checks")


@router.post("/checks/run")
def checks_run(request: Request):
    from ..main import flash

    need(request, P_ADMIN)
    db = db_of(request)
    outcomes = PC.run_all(db)
    db.commit()
    tested = [o for o in outcomes if o.tested]
    if not tested:
        flash(request, "Nothing to check yet — add a salary and the answer you expect.",
              "warning")
    elif all(o.passed for o in tested):
        flash(request, f"All {len(tested)} checks pass. The scheme reproduces every "
                       "answer you gave it.")
    else:
        bad = [o for o in tested if not o.passed]
        flash(request, f"{len(bad)} of {len(tested)} checks fail. Do not run payroll "
                       "until the rates are right.", "danger")
    return redirect("/payroll/checks")
