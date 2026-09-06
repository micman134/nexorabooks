"""Budgets and the variance report."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import Budget
from ..money import fmt_plain
from ..security import P_JOURNAL, P_VIEW
from ..services import budgets as B
from ..services.posting import PostingError, audit
from ..services.reports import fiscal_year_bounds
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_money,
    period_from_query,
    redirect,
)

router = APIRouter(prefix="/budgets")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    budgets = list(db.scalars(select(Budget).order_by(Budget.start_date.desc())))
    fy_start, fy_end = fiscal_year_bounds(db, date.today())
    return render(
        request, "budgets/index.html",
        budgets=budgets, fy_start=fy_start, fy_end=fy_end,
        suggested_name=f"{fy_start.year} budget" if fy_start.month == 1
        else f"{fy_start.year}/{str(fy_end.year)[-2:]} budget",
    )


@router.post("/create")
async def create(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    start = parse_date(form.get("start_date"))
    end = parse_date(form.get("end_date"))
    if not name:
        flash(request, "Give the budget a name.", "danger")
        return redirect("/budgets")
    try:
        budget = B.create(db, name, start, end, user=user)
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/budgets")

    # Optionally start it from what actually happened last year
    if parse_bool(form.get("from_last_year")):
        source_end = start - timedelta(days=1)
        source_start = date(source_end.year - 1, source_end.month, 1) \
            if source_end.month != 12 else date(source_end.year, 1, 1)
        source_start, source_end = fiscal_year_bounds(db, source_end)
        B.fill_from_actuals(db, budget, source_start, source_end,
                            uplift_pct=(form.get("uplift_pct") or "0").strip())

    audit(db, user, "CREATE", "Budget", budget.id, detail=budget.name, ip=client_ip(request))
    db.commit()
    flash(request, f"'{budget.name}' created. Fill in the figures below.")
    return redirect(f"/budgets/{budget.id}/edit")


@router.get("/variance")
def variance_report(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    budgets = list(db.scalars(select(Budget).order_by(Budget.start_date.desc())))
    if not budgets:
        return redirect("/budgets")

    budget_id = parse_id(request.query_params.get("budget"))
    budget = db.get(Budget, budget_id) if budget_id else budgets[0]
    if budget is None:
        budget = budgets[0]

    start, end, preset = period_from_query(request, db)
    # Keep the window inside the budget's own year, or the comparison is a lie
    start = max(start, budget.start_date)
    end = min(end, budget.end_date)
    if end < start:
        start, end = budget.start_date, budget.end_date

    report = B.variance(db, budget, start, end)

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        rows = []
        for section in report.sections:
            for r in section.rows:
                rows.append([
                    section.title, r.account.code, r.account.name,
                    fmt_plain(r.budget), fmt_plain(r.actual), fmt_plain(r.variance),
                    r.variance_pct, "Favourable" if r.is_favourable else "Adverse",
                ])
        rows.append(["Profit", "", "",
                     fmt_plain(report.budget_profit), fmt_plain(report.actual_profit),
                     fmt_plain(report.profit_variance), "", ""])
        return csv_response(
            f"budget-variance-{start}-to-{end}.csv",
            ["Section", "Code", "Account", "Budget", "Actual", "Variance", "%", ""],
            rows,
        )

    return render(
        request, "budgets/variance.html",
        report=report, budget=budget, budgets=budgets,
        start=start, end=end, preset=preset,
        monthly=B.monthly_totals(db, budget),
        month_label=B.month_label,
    )


@router.get("/{budget_id}")
def detail(request: Request, budget_id: int):
    return redirect(f"/budgets/variance?budget={budget_id}")


@router.get("/{budget_id}/edit")
def edit(request: Request, budget_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    budget = db.get(Budget, budget_id)
    if budget is None:
        return redirect("/budgets")

    periods = B.periods_in(budget.start_date, budget.end_date)
    cells = B.grid(db, budget)
    accounts = B.budgetable_accounts(db)
    # Show accounts that carry figures even if they have since been archived —
    # otherwise part of the budget would be invisible and unfixable.
    from ..models import Account

    have = {a.id for a in accounts}
    for (account_id, _period) in cells:
        if account_id not in have:
            extra = db.get(Account, account_id)
            if extra is not None:
                accounts.append(extra)
                have.add(account_id)
    accounts.sort(key=lambda a: a.code)

    # A chart of accounts has a hundred lines and a budget touches a dozen.
    # Showing every one turns the grid into a wall, so by default only the
    # accounts that carry a figure — or that money actually went through —
    # are listed, with everything else one click away.
    show_all = parse_bool(request.query_params.get("all"))
    in_use = {account_id for (account_id, _p) in cells}
    in_use |= set(B.actuals_for(db, budget.start_date, budget.end_date))
    shown = accounts if show_all else [a for a in accounts if a.id in in_use]
    if not shown:
        shown, show_all = accounts, True

    return render(
        request, "budgets/edit.html",
        budget=budget, periods=periods, cells=cells, accounts=shown,
        all_accounts=accounts, show_all=show_all, hidden=len(accounts) - len(shown),
        month_label=B.month_label,
        row_totals={a.id: sum(cells.get((a.id, p), 0) for p in periods) for a in accounts},
        col_totals={p: sum(cells.get((a.id, p), 0) for a in accounts) for p in periods},
        grand_total=sum(cells.values()),
    )


@router.post("/{budget_id}/save")
async def save(request: Request, budget_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    budget = db.get(Budget, budget_id)
    if budget is None:
        return redirect("/budgets")
    form = await request.form()

    budget.name = (form.get("name") or budget.name).strip()
    budget.notes = form.get("notes") or ""
    budget.is_active = parse_bool(form.get("is_active"))

    changed = 0
    for key, value in form.multi_items():
        if not key.startswith("cell_"):
            continue
        try:
            _, account_id, period = key.split("_")
            account_id, period = int(account_id), int(period)
        except ValueError:
            continue
        B.set_line(db, budget, account_id, period, parse_money(value))
        changed += 1

    db.flush()
    audit(db, user, "UPDATE", "Budget", budget.id,
          detail=f"{budget.name} — {changed} cells", ip=client_ip(request))
    db.commit()
    flash(request, f"'{budget.name}' saved.")
    return redirect(f"/budgets/variance?budget={budget.id}")


@router.post("/{budget_id}/spread")
async def spread(request: Request, budget_id: int):
    """Take one annual figure for one account and split it across the months."""
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    budget = db.get(Budget, budget_id)
    if budget is None:
        return redirect("/budgets")
    form = await request.form()
    account_id = parse_id(form.get("account_id"))
    annual = parse_money(form.get("annual"))
    if not account_id:
        flash(request, "Choose an account to spread across the year.", "danger")
        return redirect(f"/budgets/{budget_id}/edit")

    periods = B.periods_in(budget.start_date, budget.end_date)
    for period, amount in B.spread(annual, periods).items():
        B.set_line(db, budget, account_id, period, amount)
    db.flush()
    audit(db, user, "UPDATE", "Budget", budget.id, detail="spread an annual figure",
          ip=client_ip(request))
    db.commit()
    flash(request, "Spread evenly across the year — adjust any month that differs.")
    return redirect(f"/budgets/{budget_id}/edit")


@router.post("/{budget_id}/fill")
async def fill(request: Request, budget_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    budget = db.get(Budget, budget_id)
    if budget is None:
        return redirect("/budgets")
    form = await request.form()

    source_end = budget.start_date - timedelta(days=1)
    source_start, source_end = fiscal_year_bounds(db, source_end)
    written = B.fill_from_actuals(
        db, budget, source_start, source_end,
        uplift_pct=(form.get("uplift_pct") or "0").strip(),
        only_empty=not parse_bool(form.get("overwrite")),
    )
    audit(db, user, "UPDATE", "Budget", budget.id,
          detail=f"filled {written} cells from {source_start:%Y}", ip=client_ip(request))
    db.commit()
    if written:
        flash(request, f"{written} figures taken from {source_start:%b %Y} — "
                       f"{source_end:%b %Y} and adjusted.")
    else:
        flash(request, f"Nothing posted between {source_start:%b %Y} and "
                       f"{source_end:%b %Y} to copy from.", "warning")
    return redirect(f"/budgets/{budget_id}/edit")


@router.post("/{budget_id}/delete")
def delete(request: Request, budget_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    budget = db.get(Budget, budget_id)
    if budget is None:
        return redirect("/budgets")
    name = budget.name
    audit(db, user, "DELETE", "Budget", budget.id, detail=name, ip=client_ip(request))
    db.delete(budget)
    db.commit()
    flash(request, f"'{name}' deleted. Nothing in the ledger has changed.", "warning")
    return redirect("/budgets")
