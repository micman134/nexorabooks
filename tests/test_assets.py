"""The fixed asset register, depreciation and disposals.

Depreciation is where small accounting packages quietly go wrong: an asset gets
charged twice because two people ran the month, or it depreciates past zero, or
a disposal leaves the cost in the balance sheet with no asset behind it. These
tests exist to make those three things impossible.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-fa-")

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    ASSET_ACTIVE,
    ASSET_DISPOSED,
    NO_DEPRECIATION,
    POSTED,
    REDUCING_BALANCE,
    STRAIGHT_LINE,
    VOID,
    Account,
    AssetCategory,
    FixedAsset,
    JournalEntry,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import assets as FA  # noqa: E402
from app.services import reports  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402

M = to_kobo  # money in, kobo out


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-fa-")
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


def make_asset(db, *, name="Toyota Hiace bus", cost="12,000,000", category="Motor Vehicles",
               in_service=date(2026, 1, 15), residual="0", method=None, life=None,
               rate=None) -> FixedAsset:
    cat = db.scalar(
        __import__("sqlalchemy").select(AssetCategory).where(AssetCategory.name == category)
    )
    asset = FixedAsset(
        number=next_number(db, "ASSET"),
        name=name,
        category_id=cat.id,
        purchase_date=in_service,
        in_service_date=in_service,
        cost=M(cost),
        residual_value=M(residual),
    )
    FA.apply_category_defaults(asset, cat)
    if method:
        asset.method = method
    if life is not None:
        asset.useful_life_months = life
    if rate is not None:
        asset.rate_pct = rate
    db.add(asset)
    db.flush()
    return asset


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def test_period_arithmetic():
    assert FA.period_of(date(2026, 3, 9)) == 202603
    assert FA.period_end(202602) == date(2026, 2, 28)
    assert FA.period_end(202412) == date(2024, 12, 31)  # a leap year, and a year end
    assert FA.next_period(202612) == 202701
    assert FA.months_between(202601, 202603) == 3
    assert FA.months_between(202601, 202601) == 1
    assert FA.months_between(202603, 202601) == 0


def test_february_in_a_leap_year():
    assert FA.period_end(202402) == date(2024, 2, 29)


# --------------------------------------------------------------------------
# The charge itself
# --------------------------------------------------------------------------


def test_straight_line_spreads_the_cost_evenly():
    # ₦12,000,000 over 4 years is ₦250,000 a month
    charge = FA.monthly_charge(cost=M("12,000,000"), residual=0, accumulated=0,
                               method=STRAIGHT_LINE, useful_life_months=48)
    assert charge == M("250,000")


def test_straight_line_respects_a_residual_value():
    # ₦12m van expected to fetch ₦2m at the end: only ₦10m is depreciated
    charge = FA.monthly_charge(cost=M("12,000,000"), residual=M("2,000,000"), accumulated=0,
                               method=STRAIGHT_LINE, useful_life_months=48)
    assert charge == M("208,333.33")


def test_reducing_balance_takes_a_share_of_what_is_left():
    # 25% a year on ₦12m is ₦250,000 in month one
    first = FA.monthly_charge(cost=M("12,000,000"), residual=0, accumulated=0,
                              method=REDUCING_BALANCE, useful_life_months=0, rate_pct="25")
    assert first == M("250,000")
    # and less next month, because the balance is smaller
    second = FA.monthly_charge(cost=M("12,000,000"), residual=0, accumulated=first,
                               method=REDUCING_BALANCE, useful_life_months=0, rate_pct="25")
    assert second < first
    assert second == M("244,791.67")


def test_land_is_never_depreciated():
    assert FA.monthly_charge(cost=M("80,000,000"), residual=0, accumulated=0,
                             method=NO_DEPRECIATION, useful_life_months=0) == 0


# --------------------------------------------------------------------------
# Running a month
# --------------------------------------------------------------------------


def test_a_run_charges_every_asset_in_service(db):
    make_asset(db, name="Hiace bus", cost="12,000,000", in_service=date(2026, 1, 5))
    make_asset(db, name="Office desks", cost="1,800,000",
               category="Furniture and Fittings", in_service=date(2026, 1, 20))
    run = FA.open_run(db, 202601)

    assert run.asset_count == 2
    # 12m/48 = 250,000 and 1.8m/60 = 30,000
    assert run.total == M("280,000")


def test_an_asset_not_yet_in_service_is_left_alone(db):
    """A van bought in June is not depreciated in January."""
    make_asset(db, in_service=date(2026, 6, 1))
    run = FA.open_run(db, 202601)
    assert run.lines == []
    with pytest.raises(FA.AssetError) as e:
        FA.post_run(db, run)
    assert "nothing to depreciate" in str(e.value).lower()


def test_posting_a_run_writes_a_balanced_entry(db):
    asset = make_asset(db, cost="12,000,000")
    run = FA.open_run(db, 202601)
    entry = FA.post_run(db, run)

    assert entry.total_debit == entry.total_credit == M("250,000")
    assert run.status == POSTED
    assert asset.accumulated_depreciation == M("250,000")
    assert asset.last_depreciated_period == 202601
    assert asset.net_book_value == M("11,750,000")

    # Dr depreciation expense, Cr accumulated depreciation
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"6900": M("250,000")}
    assert credits == {"1610": M("250,000")}


def test_the_same_month_cannot_be_run_twice(db):
    make_asset(db)
    FA.post_run(db, FA.open_run(db, 202601))
    with pytest.raises(FA.AssetError) as e:
        FA.open_run(db, 202601)
    assert "already exists" in str(e.value)


def test_an_asset_already_charged_is_skipped_by_a_later_run(db):
    asset = make_asset(db)
    FA.post_run(db, FA.open_run(db, 202601))
    run2 = FA.open_run(db, 202602)
    FA.post_run(db, run2)
    assert asset.accumulated_depreciation == M("500,000")
    assert run2.total == M("250,000")


def test_a_forgotten_month_catches_up(db):
    """Somebody ran January, went on leave, and came back in April."""
    asset = make_asset(db, in_service=date(2026, 1, 1))
    FA.post_run(db, FA.open_run(db, 202601))
    run = FA.open_run(db, 202604)
    line = run.lines[0]
    assert line.months_charged == 3          # February, March, April
    assert line.amount == M("750,000")
    FA.post_run(db, run)
    assert asset.accumulated_depreciation == M("1,000,000")


def test_depreciation_stops_at_the_residual_value(db):
    """The last month is trimmed so the asset lands exactly on its residual."""
    asset = make_asset(db, cost="1,000,000", residual="100,000", life=7,
                       in_service=date(2026, 1, 1))
    # 900,000 / 7 = 128,571.43 a month, which does not divide evenly
    for i in range(1, 9):
        period = 202600 + i
        run = FA.open_run(db, period)
        if run.lines:
            FA.post_run(db, run)
        else:
            db.delete(run)
            db.flush()
    assert asset.net_book_value == M("100,000")
    assert asset.accumulated_depreciation == M("900,000")
    assert asset.is_fully_depreciated


def test_an_asset_never_depreciates_below_zero(db):
    asset = make_asset(db, cost="360,000", life=3, in_service=date(2026, 1, 1))
    for i in (1, 2, 3):
        FA.post_run(db, FA.open_run(db, 202600 + i))
    assert asset.net_book_value == 0
    # A fourth month finds nothing left to charge
    run = FA.open_run(db, 202604)
    assert run.lines == []
    with pytest.raises(FA.AssetError):
        FA.post_run(db, run)


def test_land_is_not_picked_up_by_a_run(db):
    make_asset(db, name="Ikeja plot", cost="80,000,000", category="Land",
               in_service=date(2026, 1, 1))
    run = FA.open_run(db, 202601)
    assert run.lines == []


def test_voiding_a_run_gives_the_months_back(db):
    asset = make_asset(db)
    FA.post_run(db, FA.open_run(db, 202601))
    run2 = FA.open_run(db, 202602)
    FA.post_run(db, run2)
    assert asset.accumulated_depreciation == M("500,000")

    FA.void_run(db, run2)
    assert run2.status == VOID
    assert asset.accumulated_depreciation == M("250,000")
    # January is still charged, so the asset knows where it got to
    assert asset.last_depreciated_period == 202601

    # and February can now be run again
    again = FA.open_run(db, 202602)
    FA.post_run(db, again)
    assert asset.accumulated_depreciation == M("500,000")


def test_voiding_a_run_leaves_the_ledger_at_nil(db):
    make_asset(db)
    run = FA.open_run(db, 202601)
    FA.post_run(db, run)
    FA.void_run(db, run)

    rows, td, tc = reports.trial_balance(db, None, date(2026, 12, 31))
    dep = [r for r in rows if r.account.code == "6900"]
    assert not dep or dep[0].debit - dep[0].credit == 0
    assert td == tc


# --------------------------------------------------------------------------
# Disposal
# --------------------------------------------------------------------------


def test_selling_at_a_profit_books_a_gain(db):
    asset = make_asset(db, cost="12,000,000", in_service=date(2026, 1, 1))
    for i in (1, 2, 3):
        FA.post_run(db, FA.open_run(db, 202600 + i))
    # NBV is now 12,000,000 - 750,000 = 11,250,000; sold for 12,000,000
    bank = account_by_code(db, "1020")
    entry = FA.dispose(db, asset, on=date(2026, 4, 10), proceeds=M("12,000,000"),
                       proceeds_account=bank)

    assert asset.status == ASSET_DISPOSED
    assert entry.total_debit == entry.total_credit
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert credits["1510"] == M("12,000,000")      # cost out
    assert credits["4930"] == M("750,000")         # the gain
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    assert debits["1610"] == M("750,000")          # depreciation out
    assert debits["1020"] == M("12,000,000")       # cash in


def test_selling_at_a_loss_books_a_loss(db):
    asset = make_asset(db, cost="12,000,000", in_service=date(2026, 1, 1))
    FA.post_run(db, FA.open_run(db, 202601))
    bank = account_by_code(db, "1020")
    entry = FA.dispose(db, asset, on=date(2026, 2, 1), proceeds=M("9,000,000"),
                       proceeds_account=bank)
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    # NBV 11,750,000 sold for 9,000,000 — a loss of 2,750,000
    assert debits["6820"] == M("2,750,000")


def test_writing_off_a_stolen_asset(db):
    asset = make_asset(db, cost="1,200,000", category="Computer and Office Equipment",
                       in_service=date(2026, 1, 1))
    FA.post_run(db, FA.open_run(db, 202601))
    entry = FA.dispose(db, asset, on=date(2026, 2, 15), proceeds=0, proceeds_account=None,
                       note="Stolen from the Ikeja store", written_off=True)
    assert asset.status == "WRITTEN_OFF"
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    assert "6820" in debits          # the whole net book value is a loss
    assert entry.total_debit == entry.total_credit


def test_a_disposed_asset_is_not_depreciated_again(db):
    asset = make_asset(db, in_service=date(2026, 1, 1))
    FA.post_run(db, FA.open_run(db, 202601))
    FA.dispose(db, asset, on=date(2026, 2, 10), proceeds=0, proceeds_account=None)
    run = FA.open_run(db, 202602)
    assert run.lines == []


def test_a_disposal_cannot_be_done_twice(db):
    asset = make_asset(db)
    FA.dispose(db, asset, on=date(2026, 2, 1), proceeds=0, proceeds_account=None)
    with pytest.raises(FA.AssetError):
        FA.dispose(db, asset, on=date(2026, 3, 1), proceeds=0, proceeds_account=None)


def test_undoing_a_disposal_puts_the_asset_back(db):
    asset = make_asset(db, in_service=date(2026, 1, 1))
    bank = account_by_code(db, "1020")
    FA.dispose(db, asset, on=date(2026, 2, 1), proceeds=M("5,000,000"),
               proceeds_account=bank)
    FA.undo_disposal(db, asset)
    assert asset.status == ASSET_ACTIVE
    assert asset.disposal_date is None
    rows, td, tc = reports.trial_balance(db, None, date(2026, 12, 31))
    assert td == tc


# --------------------------------------------------------------------------
# The register agrees with the ledger
# --------------------------------------------------------------------------


def test_the_schedule_ties_to_the_balance_sheet(db):
    """The whole point of a schedule: it proves the balance sheet."""
    van = make_asset(db, name="Hiace bus", cost="12,000,000", in_service=date(2026, 1, 1))
    desks = make_asset(db, name="Office desks", cost="1,800,000",
                       category="Furniture and Fittings", in_service=date(2026, 1, 1))
    for a, code in ((van, "1510"), (desks, "1530")):
        FA.capitalise(db, a, paid_from_account=account_by_code(db, "1020"))
    for i in (1, 2, 3):
        FA.post_run(db, FA.open_run(db, 202600 + i))

    rows, total = FA.schedule(db, date(2026, 1, 1), date(2026, 12, 31))
    assert total.additions == M("13,800,000")
    assert total.cost_close == M("13,800,000")
    assert total.charge == M("840,000")           # (250,000 + 30,000) x 3
    assert total.nbv_close == M("12,960,000")

    # and the ledger says the same thing
    tb, _td, _tc = reports.trial_balance(db, None, date(2026, 12, 31))
    by_code = {r.account.code: r.debit - r.credit for r in tb}
    cost = by_code.get("1510", 0) + by_code.get("1530", 0)
    accum = by_code.get("1610", 0) + by_code.get("1630", 0)
    assert cost == total.cost_close
    assert -accum == total.dep_close
    assert cost + accum == total.nbv_close


def test_the_schedule_shows_an_asset_that_left_during_the_year(db):
    asset = make_asset(db, cost="12,000,000", in_service=date(2026, 1, 1))
    FA.capitalise(db, asset, paid_from_account=account_by_code(db, "1020"))
    FA.post_run(db, FA.open_run(db, 202601))
    FA.dispose(db, asset, on=date(2026, 2, 20), proceeds=M("11,000,000"),
               proceeds_account=account_by_code(db, "1020"))

    rows, total = FA.schedule(db, date(2026, 1, 1), date(2026, 12, 31))
    assert total.additions == M("12,000,000")
    assert total.disposals_cost == M("12,000,000")
    assert total.disposals_dep == M("250,000")
    assert total.cost_close == 0
    assert total.dep_close == 0
    assert total.nbv_close == 0


def test_opening_figures_carry_into_the_next_year(db):
    asset = make_asset(db, cost="12,000,000", in_service=date(2025, 1, 1))
    FA.capitalise(db, asset, paid_from_account=account_by_code(db, "1020"))
    for i in range(1, 13):
        FA.post_run(db, FA.open_run(db, 202500 + i))

    rows, total = FA.schedule(db, date(2026, 1, 1), date(2026, 12, 31))
    assert total.cost_open == M("12,000,000")
    assert total.dep_open == M("3,000,000")
    assert total.additions == 0
    assert total.nbv_open == M("9,000,000")


# --------------------------------------------------------------------------
# Capitalising and forecasting
# --------------------------------------------------------------------------


def test_capitalising_moves_money_into_the_asset_account(db):
    asset = make_asset(db, cost="12,000,000")
    entry = FA.capitalise(db, asset, paid_from_account=account_by_code(db, "1020"))
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"1510": M("12,000,000")}
    assert credits == {"1020": M("12,000,000")}
    with pytest.raises(FA.AssetError):
        FA.capitalise(db, asset, paid_from_account=account_by_code(db, "1020"))


def test_the_forecast_shows_when_an_asset_runs_out(db):
    asset = make_asset(db, cost="1,200,000", life=4, in_service=date(2026, 1, 1))
    rows = FA.forecast(asset, months=12)
    assert len(rows) == 4                      # it only has four months of life
    assert [r[1] for r in rows] == [M("300,000")] * 4
    assert rows[-1][2] == 0                    # ends at nil
    assert rows[0][0] == 202601


def test_an_asset_without_a_category_says_so(db):
    asset = FixedAsset(number="FA-9999", name="Mystery item",
                       purchase_date=date(2026, 1, 1), in_service_date=date(2026, 1, 1),
                       cost=M("500,000"))
    db.add(asset)
    db.flush()
    with pytest.raises(FA.AssetError) as e:
        FA.capitalise(db, asset, paid_from_account=account_by_code(db, "1020"))
    assert "no category" in str(e.value)


def test_the_seeded_categories_are_wired_to_real_accounts(db):
    from sqlalchemy import select

    cats = list(db.scalars(select(AssetCategory)))
    assert len(cats) >= 7
    for cat in cats:
        assert cat.asset_account_id, f"{cat.name} has no asset account"
        assert cat.accum_dep_account_id, f"{cat.name} has no depreciation account"
        assert cat.expense_account_id, f"{cat.name} has no expense account"
