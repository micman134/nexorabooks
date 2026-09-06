"""Budgets, and the variance report that compares them with the ledger.

The thing worth testing hardest is the sign convention. Spending ₦100,000 more
than budgeted and earning ₦100,000 more than budgeted are both "over budget",
and a report that presents them the same way is useless.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-bud-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import Account, Budget, BudgetLine  # noqa: E402
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import budgets as B  # noqa: E402
from app.services.posting import EntryDraft, account_by_code, post_entry  # noqa: E402

M = to_kobo


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-bud-")
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


def sale(db, on: date, amount: str, account="4000"):
    d = EntryDraft(date=on, memo="Sale")
    d.debit(account_by_code(db, "1020"), M(amount), "Cash in")
    d.credit(account_by_code(db, account), M(amount), "Sale")
    post_entry(db, d)


def cost(db, on: date, amount: str, account="6100"):
    d = EntryDraft(date=on, memo="Expense")
    d.debit(account_by_code(db, account), M(amount), "Cost")
    d.credit(account_by_code(db, "1020"), M(amount), "Paid")
    post_entry(db, d)


def year(db, name="2026 plan") -> Budget:
    return B.create(db, name, date(2026, 1, 1), date(2026, 12, 31))


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def test_a_year_is_twelve_months():
    periods = B.periods_in(date(2026, 1, 1), date(2026, 12, 31))
    assert len(periods) == 12
    assert periods[0] == 202601
    assert periods[-1] == 202612


def test_a_year_that_starts_in_april():
    periods = B.periods_in(date(2026, 4, 1), date(2027, 3, 31))
    assert len(periods) == 12
    assert periods[0] == 202604
    assert periods[-1] == 202703


def test_month_ends():
    assert B.period_end(202602) == date(2026, 2, 28)
    assert B.period_end(202404) == date(2024, 4, 30)


# --------------------------------------------------------------------------
# Spreading a figure
# --------------------------------------------------------------------------


def test_an_annual_figure_spreads_without_losing_a_kobo():
    periods = B.periods_in(date(2026, 1, 1), date(2026, 12, 31))
    split = B.spread(M("10,000,000"), periods)
    assert sum(split.values()) == M("10,000,000")
    assert len(split) == 12


def test_an_awkward_figure_still_adds_back():
    """₦100 across 12 months does not divide evenly. Nothing may be lost."""
    periods = B.periods_in(date(2026, 1, 1), date(2026, 12, 31))
    split = B.spread(M("100"), periods)
    assert sum(split.values()) == M("100")
    # The odd kobo lands on the earliest months, not nowhere
    assert split[202601] > split[202612]


def test_spreading_nothing_is_harmless():
    assert B.spread(0, []) == {}


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


def test_setting_a_cell(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    assert B.grid(db, budget)[(sales.id, 202601)] == M("5,000,000")


def test_a_zero_removes_the_row_rather_than_storing_it(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    B.set_line(db, budget, sales.id, 202601, 0)
    db.flush()
    assert db.scalar(select(BudgetLine).where(BudgetLine.budget_id == budget.id)) is None


def test_two_budgets_with_the_same_name_and_start_are_refused(db):
    year(db)
    with pytest.raises(B.BudgetError):
        year(db)


def test_a_budget_cannot_end_before_it_starts(db):
    with pytest.raises(B.BudgetError):
        B.create(db, "Backwards", date(2026, 12, 1), date(2026, 1, 1))


def test_building_from_last_year_with_an_uplift(db):
    """Last year plus ten per cent — how most small businesses do it."""
    for month in range(1, 13):
        sale(db, date(2025, month, 15), "1,000,000")
        cost(db, date(2025, month, 20), "400,000")

    budget = B.create(db, "2026 plan", date(2026, 1, 1), date(2026, 12, 31))
    written = B.fill_from_actuals(db, budget, date(2025, 1, 1), date(2025, 12, 31),
                                  uplift_pct="10")
    assert written == 24                       # two accounts × twelve months

    sales = account_by_code(db, "4000")
    rent = account_by_code(db, "6100")
    cells = B.grid(db, budget)
    assert cells[(sales.id, 202601)] == M("1,100,000")
    assert cells[(rent.id, 202601)] == M("440,000")


def test_filling_does_not_overwrite_figures_somebody_typed(db):
    sale(db, date(2025, 1, 15), "1,000,000")
    budget = B.create(db, "2026 plan", date(2026, 1, 1), date(2026, 12, 31))
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("9,999,999"))
    db.flush()

    B.fill_from_actuals(db, budget, date(2025, 1, 1), date(2025, 12, 31))
    assert B.grid(db, budget)[(sales.id, 202601)] == M("9,999,999")


# --------------------------------------------------------------------------
# Variance
# --------------------------------------------------------------------------


def test_beating_a_revenue_budget_is_favourable(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "6,000,000")

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    row = report.revenue.rows[0]
    assert row.budget == M("5,000,000")
    assert row.actual == M("6,000,000")
    assert row.variance == M("1,000,000")
    assert row.is_favourable is True
    assert row.variance_pct == "20.0%"


def test_overspending_is_adverse(db):
    budget = year(db)
    rent = account_by_code(db, "6100")
    B.set_line(db, budget, rent.id, 202601, M("800,000"))
    db.flush()
    cost(db, date(2026, 1, 5), "950,000")

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    row = [r for r in report.expenses.rows if r.account.code == "6100"][0]
    assert row.variance == M("150,000")
    assert row.is_favourable is False
    assert row.variance_pct == "18.8%"


def test_spending_less_than_budgeted_is_favourable(db):
    budget = year(db)
    rent = account_by_code(db, "6100")
    B.set_line(db, budget, rent.id, 202601, M("800,000"))
    db.flush()
    cost(db, date(2026, 1, 5), "600,000")

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    row = [r for r in report.expenses.rows if r.account.code == "6100"][0]
    assert row.variance == M("-200,000")
    assert row.is_favourable is True


def test_a_missed_revenue_target_is_adverse(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "3,000,000")

    row = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31)).revenue.rows[0]
    assert row.is_favourable is False


def test_the_profit_line_pulls_it_all_together(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    cogs = account_by_code(db, "5000")
    rent = account_by_code(db, "6100")
    for account, amount in ((sales, "10,000,000"), (cogs, "6,000,000"), (rent, "1,000,000")):
        B.set_line(db, budget, account.id, 202601, M(amount))
    db.flush()

    sale(db, date(2026, 1, 10), "11,000,000")
    cost(db, date(2026, 1, 11), "6,500,000", account="5000")
    cost(db, date(2026, 1, 12), "900,000", account="6100")

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    assert report.budget_profit == M("3,000,000")
    assert report.actual_profit == M("3,600,000")
    assert report.profit_variance == M("600,000")


def test_only_the_months_asked_for_are_counted(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    for period in (202601, 202602, 202603):
        B.set_line(db, budget, sales.id, period, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "4,000,000")
    sale(db, date(2026, 2, 20), "4,000,000")
    sale(db, date(2026, 3, 20), "4,000,000")

    q1 = B.variance(db, budget, date(2026, 1, 1), date(2026, 3, 31))
    assert q1.revenue.budget == M("15,000,000")
    assert q1.revenue.actual == M("12,000,000")

    jan = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    assert jan.revenue.budget == M("5,000,000")
    assert jan.revenue.actual == M("4,000,000")


def test_an_account_with_actuals_but_no_budget_still_shows(db):
    """Money spent somewhere nobody planned for is exactly what you want to see."""
    budget = year(db)
    cost(db, date(2026, 1, 5), "300,000", account="6410")

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    row = [r for r in report.expenses.rows if r.account.code == "6410"][0]
    assert row.budget == 0
    assert row.actual == M("300,000")
    assert row.is_favourable is False
    assert row.variance_pct == "n/a"


def test_an_account_budgeted_but_not_spent_shows_too(db):
    budget = year(db)
    rent = account_by_code(db, "6100")
    B.set_line(db, budget, rent.id, 202601, M("800,000"))
    db.flush()

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    row = [r for r in report.expenses.rows if r.account.code == "6100"][0]
    assert row.actual == 0
    assert row.variance == M("-800,000")
    assert row.is_favourable is True


def test_a_voided_entry_is_not_counted_as_actual(db):
    from app.services.posting import reverse_entry

    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()

    d = EntryDraft(date=date(2026, 1, 20), memo="Sale later cancelled")
    d.debit(account_by_code(db, "1020"), M("5,000,000"), "Cash in")
    d.credit(sales, M("5,000,000"), "Sale")
    entry = post_entry(db, d)
    reverse_entry(db, entry, on=date(2026, 1, 25))

    report = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31))
    assert report.revenue.actual == 0


def test_the_monthly_chart_lines_up_with_the_ledger(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    B.set_line(db, budget, sales.id, 202602, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "4,000,000")

    rows = B.monthly_totals(db, budget)
    assert len(rows) == 12
    jan = [r for r in rows if r[0] == 202601][0]
    assert jan[1] == M("5,000,000")
    assert jan[2] == M("4,000,000")
    feb = [r for r in rows if r[0] == 202602][0]
    assert feb[1] == M("5,000,000")
    assert feb[2] == 0


def test_only_income_and_expense_accounts_can_be_budgeted(db):
    accounts = B.budgetable_accounts(db)
    assert accounts
    assert all(a.type in ("INCOME", "EXPENSE") for a in accounts)
    assert not any(a.code == "1020" for a in accounts)


def test_a_rounding_sized_difference_is_not_flagged(db):
    """Twelve months of a budget leave kobo behind. That is not an overspend."""
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "4,999,999.98")

    row = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31)).revenue.rows[0]
    assert row.variance == -2
    assert row.is_material is False


def test_a_real_difference_is_flagged(db):
    budget = year(db)
    sales = account_by_code(db, "4000")
    B.set_line(db, budget, sales.id, 202601, M("5,000,000"))
    db.flush()
    sale(db, date(2026, 1, 20), "4,999,000")

    row = B.variance(db, budget, date(2026, 1, 1), date(2026, 1, 31)).revenue.rows[0]
    assert row.is_material is True
    assert row.is_favourable is False
