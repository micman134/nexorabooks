"""Payroll for a country this software has never heard of.

Nigeria's rates are built in and stay built in. But an employer in Nairobi or
Lisbon has to be able to type their own tax table and their own contributions
and get right answers — and, far more importantly, has to be able to find out
that they are right before anybody is paid from them.

The numbers in these tests are worked by hand in the comments, so a reader can
check the arithmetic without running anything.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-scheme-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import PayrollBand, PayrollCheck, PayrollSetting  # noqa: E402
from app.money import to_minor  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import payroll as P  # noqa: E402
from app.services import payroll_check as PC  # noqa: E402
from app.services import payroll_run as PR  # noqa: E402

M = to_minor


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-scheme-")
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
# A scheme nobody has heard of
# --------------------------------------------------------------------------


TESTLAND_BANDS = [
    (M("1,000,000"), "0"),      # first million free
    (M("1,000,000"), "10"),     # next million at a tenth
    (None, "20"),               # everything above at a fifth
]


def scheme() -> P.PayrollRules:
    """One contribution, three bands, no threshold and no relief."""
    return P.PayrollRules(
        minimum_wage=0,
        rent_relief_rate="0",
        rent_relief_cap=0,
        tax_name="Income Tax",
        threshold_name="the tax-free allowance",
        bands=list(TESTLAND_BANDS),
        slots=[
            P.Slot("PENSION", "Provident Fund", "GROSS", "5", "5", reduces_tax=True),
            P.Slot("NHF", "unused", "BASIC", "0", "0", active=False),
            P.Slot("NHIS", "unused", "BASIC", "0", "0", active=False),
            P.Slot("NSITF", "unused", "GROSS", "0", "0", active=False),
            P.Slot("ITF", "unused", "GROSS", "0", "0", active=False),
        ],
    )


def test_a_scheme_of_ones_own_gives_the_worked_answer():
    """Basic 200,000 a month, one 5% contribution, the bands above.

        gross                200,000
        provident fund   5%   10,000        employer another 10,000
        annual taxable     2,400,000
        less contributions   120,000
        chargeable         2,280,000
            first 1,000,000 at  0%        0
            next  1,000,000 at 10%  100,000
            last    280,000 at 20%   56,000
        tax for the year     156,000  ->  13,000 a month
        net                  177,000
    """
    r = P.compute_payslip(basic=M("200,000"), frequency=P.MONTHLY, rules=scheme())

    assert r.gross == M("200,000")
    assert r.pension_employee == M("10,000")
    assert r.pension_employer == M("10,000")
    assert r.annual_chargeable == M("2,280,000")
    assert r.annual_paye == M("156,000")
    assert r.paye == M("13,000")
    assert r.net_pay == M("177,000")
    assert r.employer_cost == M("210,000")


def test_the_contribution_appears_under_the_name_the_employer_gave_it():
    r = P.compute_payslip(basic=M("200,000"), frequency=P.MONTHLY, rules=scheme())
    names = [c.name for c in r.contributions if c.employee]
    assert names == ["Provident Fund"]
    assert "NHF" not in names


def test_a_contribution_that_does_not_cut_taxable_pay_costs_more_tax():
    rules = scheme()
    rules.slot("PENSION").reduces_tax = False
    r = P.compute_payslip(basic=M("200,000"), frequency=P.MONTHLY, rules=rules)

    # Chargeable is the full 2,400,000 now, so the top slice grows by 120,000
    assert r.annual_chargeable == M("2,400,000")
    assert r.annual_paye == M("180,000")        # 100,000 + 20% of 400,000
    assert r.paye == M("15,000")


def test_a_cap_stops_a_contribution_running_away():
    """Kenya's NSSF and half the world's social security are capped."""
    rules = scheme()
    rules.slot("PENSION").employee_cap = M("2,000")
    rules.slot("PENSION").employer_cap = M("2,000")
    r = P.compute_payslip(basic=M("200,000"), frequency=P.MONTHLY, rules=rules)

    assert r.pension_employee == M("2,000")     # not 10,000
    assert r.pension_employer == M("2,000")
    assert r.net_pay == r.gross - M("2,000") - r.paye


def test_a_contribution_can_be_charged_on_whatever_the_country_says():
    """Basic only, rather than the whole gross."""
    rules = scheme()
    rules.slot("PENSION").base = "BASIC"
    r = P.compute_payslip(basic=M("100,000"), housing=M("100,000"),
                          frequency=P.MONTHLY, rules=rules)
    assert r.gross == M("200,000")
    assert r.pension_employee == M("5,000")     # 5% of basic, not of gross


def test_a_scheme_with_no_threshold_taxes_the_lowest_paid():
    """Nigeria exempts the minimum wage. Most countries do not, and saying
    'no threshold' has to actually mean no threshold."""
    # 150,000 a month is 1,800,000 a year; less the 5% contribution that is
    # 1,710,000 chargeable, of which 710,000 falls in the ten per cent band.
    r = P.compute_payslip(basic=M("150,000"), frequency=P.MONTHLY, rules=scheme())
    assert r.paye_exempt_reason == ""
    assert r.annual_paye == M("71,000")


def test_the_threshold_is_named_in_the_employees_own_words():
    rules = scheme()
    rules.minimum_wage = M("100,000")
    r = P.compute_payslip(basic=M("50,000"), frequency=P.MONTHLY, rules=rules)
    assert "the tax-free allowance" in r.paye_exempt_reason
    assert "Income Tax" in r.paye_exempt_reason
    assert r.paye == 0


def test_a_flat_tax_is_just_a_scheme_with_one_band():
    rules = scheme()
    rules.bands = [(None, "15")]
    rules.slot("PENSION").active = False
    r = P.compute_payslip(basic=M("100,000"), frequency=P.MONTHLY, rules=rules)
    assert r.paye == M("15,000")


def test_a_country_with_no_income_tax_at_all_takes_nothing():
    rules = scheme()
    rules.bands = [(None, "0")]
    rules.slot("PENSION").active = False
    r = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY, rules=rules)
    assert r.paye == 0
    assert r.net_pay == r.gross


def test_the_nigerian_scheme_is_untouched_by_any_of_this():
    """The default must still be exactly what a Nigerian employer expects."""
    r = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY)
    assert r.pension_employee == M("32,000")           # 8%
    assert r.nhf == M("10,000")                        # 2.5%
    assert [s.name for s in P.PayrollRules().slots][:2] == ["Pension", "NHF"]


# --------------------------------------------------------------------------
# The scheme as it is stored
# --------------------------------------------------------------------------


def configure(db, **over):
    s = PR.settings(db)
    for key, value in over.items():
        setattr(s, key, value)
    db.flush()
    return s


def test_renaming_a_contribution_changes_what_the_payslip_says(db):
    configure(db, pension_name="NSSF", nhf_name="NHIF", nhf_base="GROSS",
              nhf_rate="1.5", operates_nhf=True)
    rules = PR.rules_for(db)
    assert rules.slot("PENSION").name == "NSSF"
    assert rules.slot("NHF").name == "NHIF"
    assert rules.slot("NHF").base == "GROSS"


def test_switching_a_contribution_off_stops_it_being_charged(db):
    configure(db, operates_nhf=False)
    rules = PR.rules_for(db)
    r = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY, rules=rules,
                          nhf_enrolled=True)
    assert r.nhf == 0


def test_custom_bands_are_ignored_until_they_are_switched_on(db):
    db.add(PayrollBand(sort=0, width=None, rate="50"))
    db.flush()
    configure(db, use_custom_bands=False)
    assert PR.bands_for(db) is None


def test_custom_bands_replace_the_built_in_ones_when_switched_on(db):
    db.add(PayrollBand(sort=0, width=M("1,000,000"), rate="0"))
    db.add(PayrollBand(sort=1, width=None, rate="20"))
    db.flush()
    configure(db, use_custom_bands=True)
    assert PR.bands_for(db) == [(M("1,000,000"), "0"), (None, "20")]


def test_an_empty_band_table_falls_back_rather_than_taxing_nobody(db):
    """Switching custom bands on and typing none must not mean a zero tax bill."""
    configure(db, use_custom_bands=True)
    assert PR.bands_for(db) is None
    assert PR.rules_for(db).bands == P.PAYE_BANDS


# --------------------------------------------------------------------------
# Proving the scheme before anybody is paid from it
# --------------------------------------------------------------------------


def known_case(db, **over) -> PayrollCheck:
    values = dict(
        name="June payslip", basic=M("400,000"), frequency=P.MONTHLY,
        pension_enrolled=True, nhf_enrolled=True, nhis_enrolled=False,
        tolerance=M("1"),
    )
    values.update(over)
    check = PayrollCheck(**values)
    db.add(check)
    db.flush()
    return check


def test_a_check_with_no_expected_figures_proves_nothing(db):
    check = known_case(db)
    outcome = PC.run(db, check)
    assert outcome.tested is False
    assert outcome.passed is False
    assert outcome.verdict == PC.UNTESTED
    assert "No expected figures" in outcome.detail


def test_a_check_passes_when_the_scheme_reproduces_the_answer(db):
    right = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    check = known_case(db, expected_gross=right.gross, expected_tax=right.paye,
                       expected_net=right.net_pay)
    outcome = PC.run(db, check)
    assert outcome.passed
    assert check.last_result == PC.PASS


def test_a_check_fails_loudly_when_it_does_not(db):
    check = known_case(db, expected_net=M("1,000,000"))
    outcome = PC.run(db, check)
    assert outcome.tested
    assert not outcome.passed
    assert check.last_result == PC.FAIL
    assert "Net pay" in outcome.detail


def test_a_small_rounding_difference_is_forgiven(db):
    right = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    check = known_case(db, expected_net=right.net_pay + 50, tolerance=M("1"))
    assert PC.run(db, check).passed


def test_a_large_difference_is_not(db):
    right = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    check = known_case(db, expected_net=right.net_pay + M("500"), tolerance=M("1"))
    assert not PC.run(db, check).passed


def test_a_scheme_with_no_checks_is_not_verified(db):
    PC.run_all(db)
    assert PC.is_verified(db) is False


def test_a_scheme_with_only_blank_checks_is_not_verified(db):
    """An employer who has entered no answers has proved nothing, and must not
    be shown a tick that says otherwise."""
    known_case(db)
    PC.run_all(db)
    assert PC.is_verified(db) is False


def test_a_scheme_becomes_verified_only_when_every_real_check_passes(db):
    right = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    known_case(db, expected_net=right.net_pay)
    PC.run_all(db)
    assert PC.is_verified(db) is True

    known_case(db, name="A second case", expected_net=M("999,999"))
    PC.run_all(db)
    assert PC.is_verified(db) is False


def test_changing_a_rate_puts_the_verdict_back_in_question(db):
    right = P.compute_payslip(basic=M("400,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    known_case(db, expected_net=right.net_pay)
    PC.run_all(db)
    assert PC.is_verified(db)

    configure(db, pension_employee="12")        # the law moved, or a typo
    PC.run_all(db)
    assert PC.is_verified(db) is False


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-schemeweb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def test_the_scheme_screen_opens(client):
    r = client.get("/payroll/settings", follow_redirects=True)
    assert r.status_code == 200
    assert "Payroll scheme" in r.text
    assert "Tax bands" in r.text


def test_the_scheme_screen_warns_when_nothing_has_been_checked(client):
    page = client.get("/payroll/settings", follow_redirects=True).text
    assert "has not been checked against a real payslip" in page


def test_the_check_screen_opens(client):
    r = client.get("/payroll/checks", follow_redirects=True)
    assert r.status_code == 200
    assert "already know the answer to" in r.text


def scheme_form(**over):
    data = {
        "scheme_name": "Testland", "tax_name": "Income Tax",
        "threshold_name": "the tax-free allowance", "relief_name": "Relief",
        "minimum_wage": "0", "rent_relief_rate": "0", "rent_relief_cap": "0",
        "paye_state": "", "default_pfa": "", "payslip_note": "",
        "operates_pension": "1",
        "pension_name": "Provident Fund", "pension_base": "GROSS",
        "pension_employee": "5", "pension_employer": "5",
        "pension_reduces_tax": "1",
        "nhf_name": "unused", "nhf_base": "BASIC", "nhf_rate": "0",
        "nhis_name": "unused", "nhis_base": "BASIC",
        "nhis_employee": "0", "nhis_employer": "0",
        "nsitf_name": "unused", "nsitf_base": "GROSS", "nsitf_rate": "0",
        "itf_name": "unused", "itf_base": "GROSS", "itf_rate": "0",
        "use_custom_bands": "1",
        "band_width": ["1,000,000", "1,000,000", ""],
        "band_rate": ["0", "10", "20"],
    }
    data.update(over)
    return data


def test_a_whole_scheme_can_be_typed_in_through_the_screen(client):
    r = client.post("/payroll/settings", data=scheme_form(), follow_redirects=True)
    assert r.status_code == 200

    with dbmod.session_scope_for(registry.default_slug()) as db:
        rules = PR.rules_for(db)
        assert rules.tax_name == "Income Tax"
        assert rules.slot("PENSION").name == "Provident Fund"
        assert rules.slot("PENSION").base == "GROSS"
        assert rules.bands == [(M("1,000,000"), "0"), (M("1,000,000"), "10"), (None, "20")]


def test_the_typed_in_scheme_produces_the_worked_answer(client):
    client.post("/payroll/settings", data=scheme_form(), follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        r = P.compute_payslip(basic=M("200,000"), frequency=P.MONTHLY,
                              rules=PR.rules_for(db))
    assert r.paye == M("13,000")
    assert r.net_pay == M("177,000")


def test_the_last_band_takes_everything_above_it(client):
    """A width typed into the last row must not cap the top of the tax table."""
    client.post("/payroll/settings",
                data=scheme_form(band_width=["1,000,000", "1,000,000", "5,000,000"]),
                follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert PR.rules_for(db).bands[-1][0] is None


def test_a_known_answer_can_be_added_and_is_checked_at_once(client):
    clear_checks(client)
    client.post("/payroll/settings", data=scheme_form(), follow_redirects=True)
    r = client.post("/payroll/checks/save", data={
        "name": "The worked example", "frequency": "MONTHLY",
        "basic": "200,000", "pension_enrolled": "1",
        "expected_gross": "200,000", "expected_tax": "13,000",
        "expected_net": "177,000", "tolerance": "1",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "reproduces it" in r.text

    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert PC.is_verified(db) is True


def test_a_wrong_scheme_is_caught_before_anybody_is_paid(client):
    clear_checks(client)
    client.post("/payroll/settings", data=scheme_form(), follow_redirects=True)
    r = client.post("/payroll/checks/save", data={
        "name": "A case that will not match", "frequency": "MONTHLY",
        "basic": "200,000", "pension_enrolled": "1",
        "expected_net": "150,000", "tolerance": "1",
    }, follow_redirects=True)
    assert "does not reproduce it" in r.text

    page = client.get("/payroll/settings", follow_redirects=True).text
    assert "do not come out right" in page

    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert PC.is_verified(db) is False


def clear_checks(client):
    """Each screen test starts from no checks at all."""
    with dbmod.session_scope_for(registry.default_slug()) as db:
        for row in db.scalars(select(PayrollCheck)):
            db.delete(row)
        db.flush()
        db.commit()


def test_changing_a_rate_re_runs_every_check_by_itself(client):
    clear_checks(client)
    client.post("/payroll/settings", data=scheme_form(), follow_redirects=True)
    client.post("/payroll/checks/save", data={
        "name": "The worked example", "frequency": "MONTHLY",
        "basic": "200,000", "pension_enrolled": "1",
        "expected_net": "177,000", "tolerance": "1",
    }, follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert PC.is_verified(db)

    # Somebody changes the pension rate. Nobody re-checks anything by hand.
    client.post("/payroll/settings", data=scheme_form(pension_employee="9"),
                follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert PC.is_verified(db) is False
    assert "do not come out right" in client.get("/payroll/settings",
                                                 follow_redirects=True).text
