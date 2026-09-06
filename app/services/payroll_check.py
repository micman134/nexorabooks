"""Proving a payroll scheme against a salary whose answer is already known.

Letting an employer type their own tax bands and contribution rates is the only
way to make payroll work outside Nigeria. It is also the fastest way to get
payroll wrong, and a wrong payslip is somebody's rent.

So the scheme can be pointed at a case the employer already has the right
answer for — last month's payslip from whatever they used before, or the tax
office's own worked example — and asked to reproduce it. If it cannot, that
shows up here, on a screen nobody has been paid from, rather than in a payslip.

Nothing in this module writes to the ledger. It computes, compares and records
the verdict. Checks are re-run whenever the rates change, so a scheme that was
right in March cannot quietly stop being right in April.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PayrollCheck, PayrollSetting
from ..money import fmt
from . import payroll
from .payroll_run import rules_for, settings

PASS, FAIL, UNTESTED = "PASS", "FAIL", ""


@dataclass
class Comparison:
    """One figure the employer said they knew, next to what the scheme made of it."""

    label: str
    expected: int
    actual: int

    @property
    def difference(self) -> int:
        return self.actual - self.expected

    def ok(self, tolerance: int) -> bool:
        return abs(self.difference) <= max(0, tolerance)


@dataclass
class Outcome:
    check: PayrollCheck
    result: payroll.PayslipResult
    comparisons: list[Comparison] = field(default_factory=list)

    @property
    def tested(self) -> bool:
        """False when the employer has not yet said what the answer should be."""
        return bool(self.comparisons)

    @property
    def passed(self) -> bool:
        return self.tested and all(c.ok(self.check.tolerance) for c in self.comparisons)

    @property
    def verdict(self) -> str:
        if not self.tested:
            return UNTESTED
        return PASS if self.passed else FAIL

    @property
    def detail(self) -> str:
        """A one-line summary, kept on the row so the list can show it."""
        if not self.tested:
            return "No expected figures entered yet"
        bad = [c for c in self.comparisons if not c.ok(self.check.tolerance)]
        if not bad:
            return "Matches on " + ", ".join(c.label.lower() for c in self.comparisons)
        return "; ".join(
            f"{c.label} is out by {fmt(abs(c.difference))} "
            f"({'too much' if c.difference > 0 else 'too little'})"
            for c in bad
        )


def compute(db: Session, check: PayrollCheck) -> payroll.PayslipResult:
    """Run the company's current scheme over this check's salary."""
    return payroll.compute_payslip(
        basic=check.basic,
        housing=check.housing,
        transport=check.transport,
        frequency=check.frequency or payroll.MONTHLY,
        annual_rent_paid=check.annual_rent_paid,
        pension_enrolled=check.pension_enrolled,
        nhf_enrolled=check.nhf_enrolled,
        nhis_enrolled=check.nhis_enrolled,
        nsitf_applies=True,
        itf_applies=False,
        rules=rules_for(db),
    )


def run(db: Session, check: PayrollCheck, *, record: bool = True) -> Outcome:
    """Check one known salary and, unless asked not to, record the verdict."""
    result = compute(db, check)
    rules = rules_for(db)

    comparisons = []
    if check.expected_gross:
        comparisons.append(Comparison("Gross pay", check.expected_gross, result.gross))
    if check.expected_tax:
        comparisons.append(Comparison(rules.tax_name, check.expected_tax, result.paye))
    if check.expected_net:
        comparisons.append(Comparison("Net pay", check.expected_net, result.net_pay))

    outcome = Outcome(check=check, result=result, comparisons=comparisons)
    if record:
        check.last_run_at = datetime.now()
        check.last_result = outcome.verdict
        check.last_detail = outcome.detail[:500]
    return outcome


def all_checks(db: Session) -> list[PayrollCheck]:
    return list(db.scalars(select(PayrollCheck).order_by(PayrollCheck.id)))


def run_all(db: Session) -> list[Outcome]:
    """Re-run every check. Called after any change to the rates."""
    outcomes = [run(db, c) for c in all_checks(db)]
    mark(db, outcomes)
    return outcomes


def mark(db: Session, outcomes: list[Outcome]) -> bool:
    """Record on the settings whether this scheme currently stands up.

    A scheme counts as verified only when there is at least one real check —
    one with expected figures in it — and every such check matches. An employer
    who has entered no expectations has verified nothing, and the screen says so
    rather than showing a reassuring tick.
    """
    tested = [o for o in outcomes if o.tested]
    verified = bool(tested) and all(o.passed for o in tested)
    s = settings(db)
    s.scheme_verified = verified
    return verified


def is_verified(db: Session) -> bool:
    s = db.get(PayrollSetting, 1)
    return bool(s and s.scheme_verified)


def summary(db: Session) -> tuple[int, int, int]:
    """(checks, passing, failing) as last recorded — cheap enough for a banner."""
    rows = all_checks(db)
    passing = sum(1 for c in rows if c.last_result == PASS)
    failing = sum(1 for c in rows if c.last_result == FAIL)
    return len(rows), passing, failing


def starter(db: Session) -> PayrollCheck:
    """A blank check with a plausible salary in it, ready to be filled in.

    The expected figures are deliberately left empty. Filling them in from this
    application's own answer would prove nothing at all — the whole point is
    that the number comes from somewhere else.
    """
    check = PayrollCheck(
        name="Last month's payslip for one member of staff",
        note=("Type in one person's pay exactly as it was, then put the gross, "
              "tax and net from the payslip you already have into the expected "
              "figures. If the scheme reproduces them, the rates are right."),
        basic=0, housing=0, transport=0,
        frequency=payroll.MONTHLY,
        tolerance=100,
    )
    db.add(check)
    db.flush()
    return check
