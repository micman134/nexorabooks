"""Payroll arithmetic — PAYE and the statutory deductions.

The bands and reliefs here are those of the Nigeria Tax Act 2025, in force
from 1 January 2026. Several tests check against published worked examples so
that a change in the code that quietly breaks the tax will be caught.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.money import fmt
from app.services.payroll import (
    DAILY_RATE,
    FIXED,
    MONTHLY,
    WEEKLY,
    Deduction,
    Earning,
    PayrollRules,
    annual_paye,
    compute_payslip,
    itf_applies_to,
    rent_relief,
)

M = 100  # kobo per naira, for readability: 500_000 * M is ₦500,000


# --------------------------------------------------------------------------
# The bands
# --------------------------------------------------------------------------


def test_first_800k_is_free_of_tax():
    assert annual_paye(800_000 * M)[0] == 0
    assert annual_paye(1)[0] == 0
    assert annual_paye(0)[0] == 0


def test_second_band_is_15_percent():
    # ₦1,000,000 chargeable: ₦200,000 falls in the 15% band
    tax, working = annual_paye(1_000_000 * M)
    assert tax == 30_000 * M
    assert [w.rate for w in working] == ["0", "15"]


def test_band_boundaries():
    # Exactly ₦3,000,000: 0% on 800k, 15% on 2,200,000
    assert annual_paye(3_000_000 * M)[0] == 330_000 * M
    # Exactly ₦12,000,000: the above plus 18% on 9,000,000
    assert annual_paye(12_000_000 * M)[0] == (330_000 + 1_620_000) * M
    # Exactly ₦25,000,000: plus 21% on 13,000,000
    assert annual_paye(25_000_000 * M)[0] == (330_000 + 1_620_000 + 2_730_000) * M
    # Exactly ₦50,000,000: plus 23% on 25,000,000
    assert annual_paye(50_000_000 * M)[0] == (330_000 + 1_620_000 + 2_730_000 + 5_750_000) * M


def test_top_band_is_25_percent():
    base = annual_paye(50_000_000 * M)[0]
    tax = annual_paye(60_000_000 * M)[0]
    assert tax - base == 2_500_000 * M   # 25% of the ₦10m above ₦50m


def test_the_working_adds_up_to_the_tax():
    for income in (900_000, 5_000_000, 20_000_000, 40_000_000, 90_000_000):
        tax, working = annual_paye(income * M)
        assert sum(w.tax for w in working) == tax
        assert sum(w.amount for w in working) == income * M


# --------------------------------------------------------------------------
# Rent relief
# --------------------------------------------------------------------------


def test_rent_relief_is_twenty_percent():
    assert rent_relief(1_000_000 * M) == 200_000 * M


def test_rent_relief_is_capped_at_500k():
    assert rent_relief(5_000_000 * M) == 500_000 * M
    assert rent_relief(100_000_000 * M) == 500_000 * M


def test_no_rent_means_no_relief():
    assert rent_relief(0) == 0


# --------------------------------------------------------------------------
# A published worked example
# --------------------------------------------------------------------------


def test_published_worked_example():
    """₦12m a year, ₦1m rent, pensionable 80% of gross.

    Published result: chargeable ₦11,032,000, annual PAYE ₦1,775,760,
    monthly PAYE ₦147,980, net pay ₦788,020 a month.
    """
    r = compute_payslip(
        basic=500_000 * M,
        housing=200_000 * M,
        transport=100_000 * M,
        earnings=[Earning("Other allowances", 200_000 * M, taxable=True, pensionable=False)],
        frequency=MONTHLY,
        annual_rent_paid=1_000_000 * M,
        pension_enrolled=True,
        nhf_enrolled=False,      # the published example ignores NHF
        nsitf_applies=False,
    )
    assert r.gross == 1_000_000 * M
    assert r.pensionable == 800_000 * M
    assert r.pension_employee == 64_000 * M
    assert r.annual_gross == 12_000_000 * M
    assert r.rent_relief == 200_000 * M
    assert r.annual_chargeable == 11_032_000 * M, fmt(r.annual_chargeable)
    assert r.annual_paye == 1_775_760 * M, fmt(r.annual_paye)
    assert r.paye == 147_980 * M, fmt(r.paye)
    assert r.net_pay == 788_020 * M, fmt(r.net_pay)


# --------------------------------------------------------------------------
# The minimum wage exemption
# --------------------------------------------------------------------------


def test_minimum_wage_earner_pays_no_paye():
    r = compute_payslip(basic=70_000 * M, frequency=MONTHLY)
    assert r.paye == 0
    assert "minimum wage" in r.paye_exempt_reason


def test_just_above_minimum_wage_pays_only_a_token_amount():
    """₦75,000 a month is above the exemption, and reliefs absorb nearly all of it.

    ₦900,000 gross a year, less 8% pension (₦72,000) and 2.5% NHF (₦22,500),
    leaves ₦805,500 chargeable — only ₦5,500 of which is taxable, at 15%.
    """
    r = compute_payslip(basic=75_000 * M, frequency=MONTHLY)
    assert r.paye_exempt_reason == ""
    assert r.annual_chargeable == 805_500 * M
    assert r.annual_paye == 825 * M
    assert r.paye < 100 * M      # under ₦100 a month


def test_explicit_exemption_flag_is_honoured():
    r = compute_payslip(basic=2_000_000 * M, frequency=MONTHLY, paye_exempt=True)
    assert r.paye == 0
    assert "exempt" in r.paye_exempt_reason.lower()


# --------------------------------------------------------------------------
# Statutory deductions
# --------------------------------------------------------------------------


def test_pension_uses_basic_housing_transport_only():
    r = compute_payslip(
        basic=300_000 * M, housing=100_000 * M, transport=50_000 * M,
        earnings=[Earning("Meal allowance", 100_000 * M, taxable=True, pensionable=False)],
        frequency=MONTHLY,
    )
    assert r.pensionable == 450_000 * M          # the meal allowance is excluded
    assert r.pension_employee == 36_000 * M      # 8%
    assert r.pension_employer == 45_000 * M      # 10%


def test_nhf_is_two_and_a_half_percent_of_basic():
    r = compute_payslip(basic=400_000 * M, housing=200_000 * M, frequency=MONTHLY)
    assert r.nhf == 10_000 * M                   # 2.5% of basic only


def test_nsitf_is_one_percent_of_gross_and_is_employer_cost():
    r = compute_payslip(basic=500_000 * M, frequency=MONTHLY, nsitf_applies=True)
    assert r.nsitf == 5_000 * M
    # It is the employer's cost — it does not reduce the employee's pay
    assert r.nsitf not in (d.amount for d in r.other_deductions)
    assert r.employer_cost == r.gross + r.pension_employer + r.nsitf


def test_itf_only_when_it_applies():
    with_itf = compute_payslip(basic=500_000 * M, frequency=MONTHLY, itf_applies=True)
    without = compute_payslip(basic=500_000 * M, frequency=MONTHLY, itf_applies=False)
    assert with_itf.itf == 5_000 * M
    assert without.itf == 0


def test_itf_threshold():
    assert itf_applies_to(5, 0) is True
    assert itf_applies_to(2, 50_000_000 * M) is True
    assert itf_applies_to(4, 10_000_000 * M) is False


def test_nhis_when_enrolled():
    r = compute_payslip(basic=400_000 * M, frequency=MONTHLY, nhis_enrolled=True)
    assert r.nhis_employee == 20_000 * M    # 5% of basic
    assert r.nhis_employer == 40_000 * M    # 10% of basic


def test_opting_out_of_pension_and_nhf():
    r = compute_payslip(
        basic=500_000 * M, frequency=MONTHLY,
        pension_enrolled=False, nhf_enrolled=False,
    )
    assert r.pension_employee == 0
    assert r.pension_employer == 0
    assert r.nhf == 0


# --------------------------------------------------------------------------
# The payslip must add up
# --------------------------------------------------------------------------


@pytest.mark.parametrize("basic", [70_000, 150_000, 400_000, 1_200_000, 5_000_000])
def test_gross_less_deductions_equals_net(basic):
    r = compute_payslip(
        basic=basic * M,
        housing=(basic // 4) * M,
        transport=(basic // 8) * M,
        earnings=[Earning("Leave allowance", (basic // 10) * M)],
        deductions=[Deduction("Union dues", 2_000 * M)],
        loan_repayment=5_000 * M,
        frequency=MONTHLY,
        annual_rent_paid=600_000 * M,
    )
    assert r.gross == sum(e.amount for e in r.earnings)
    assert r.total_deductions == (
        r.pension_employee + r.nhf + r.nhis_employee + r.paye + r.other_deductions_total
    )
    assert r.net_pay == r.gross - r.total_deductions
    assert r.net_pay > 0


def test_voluntary_deductions_do_not_reduce_tax():
    with_dues = compute_payslip(
        basic=1_000_000 * M, frequency=MONTHLY,
        deductions=[Deduction("Union dues", 50_000 * M, reduces_tax=False)],
    )
    without = compute_payslip(basic=1_000_000 * M, frequency=MONTHLY)
    assert with_dues.paye == without.paye
    assert with_dues.net_pay == without.net_pay - 50_000 * M


def test_life_assurance_does_reduce_tax():
    with_relief = compute_payslip(
        basic=1_000_000 * M, frequency=MONTHLY,
        deductions=[Deduction("Life assurance premium", 50_000 * M, reduces_tax=True)],
    )
    without = compute_payslip(basic=1_000_000 * M, frequency=MONTHLY)
    assert with_relief.paye < without.paye


# --------------------------------------------------------------------------
# Pay frequencies
# --------------------------------------------------------------------------


def test_weekly_pay_annualises_over_52_weeks():
    r = compute_payslip(basic=50_000 * M, frequency=WEEKLY)
    assert r.annual_gross == 50_000 * M * 52


def test_daily_rate_worker_paid_monthly_for_days_worked():
    r = compute_payslip(basic=8_000 * M, frequency=MONTHLY, units=22, nhf_enrolled=False)
    assert r.basic == 176_000 * M          # 22 days at ₦8,000
    assert r.gross == 176_000 * M
    assert r.annual_gross == 176_000 * M * 12


def test_daily_rate_worker_below_minimum_wage_pays_no_paye():
    """₦2,500 a day for 20 days is ₦50,000 a month — under the minimum wage."""
    r = compute_payslip(basic=2_500 * M, frequency=MONTHLY, units=20)
    assert r.gross == 50_000 * M
    assert r.paye == 0
    assert "minimum wage" in r.paye_exempt_reason


def test_daily_rate_worker_paid_weekly():
    """₦6,000 a day, 5 days, paid weekly — annualises over 52 weeks, not 260 days."""
    r = compute_payslip(basic=6_000 * M, frequency=WEEKLY, units=5, nhf_enrolled=False)
    assert r.gross == 30_000 * M
    assert r.annual_gross == 30_000 * M * 52
    # ₦1,560,000 a year is comfortably above the minimum wage, so PAYE is due
    assert r.paye > 0


def test_the_same_annual_pay_taxes_the_same_whatever_the_frequency():
    """₦1,200,000 a year should attract the same tax paid monthly or weekly."""
    monthly = compute_payslip(basic=100_000 * M, frequency=MONTHLY)
    weekly = compute_payslip(
        basic=int(Decimal(100_000 * M) * 12 / 52), frequency=WEEKLY
    )
    # Within a kobo or two of each other after rounding
    assert abs(monthly.annual_paye - weekly.annual_paye) < 500


# --------------------------------------------------------------------------
# Editable rates
# --------------------------------------------------------------------------


def test_rates_can_be_changed_without_touching_the_code():
    """If the law moves, Settings can follow it."""
    rules = PayrollRules(
        pension_employee="7.5",
        nhf_rate="2",
        minimum_wage=100_000 * M,
        rent_relief_cap=1_000_000 * M,
    )
    r = compute_payslip(basic=400_000 * M, frequency=MONTHLY, rules=rules,
                        annual_rent_paid=10_000_000 * M)
    assert r.pension_employee == 30_000 * M    # 7.5%
    assert r.nhf == 8_000 * M                  # 2%
    assert r.rent_relief == 1_000_000 * M      # raised cap
