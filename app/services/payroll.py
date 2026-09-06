"""Nigerian payroll: PAYE and statutory deductions.

Everything here is pure arithmetic on integer kobo — no database, no session —
so it can be tested exhaustively and read by an accountant checking the maths.

The rules implemented, all in force from 1 January 2026:

PAYE — Nigeria Tax Act 2025
    Annual chargeable income is taxed in bands:

        First      ₦800,000    0%
        Next     ₦2,200,000   15%   (to ₦3,000,000)
        Next     ₦9,000,000   18%   (to ₦12,000,000)
        Next    ₦13,000,000   21%   (to ₦25,000,000)
        Next    ₦25,000,000   23%   (to ₦50,000,000)
        Above   ₦50,000,000   25%

    The Consolidated Relief Allowance is abolished. So is the 1% minimum tax.
    Chargeable income is gross pay less: the employee's 8% pension, NHF, NHIS,
    life assurance premiums, and rent relief — the lower of 20% of annual rent
    actually paid or ₦500,000.

    An employee earning no more than the national minimum wage (₦70,000 a
    month) is not liable to PAYE at all.

Statutory contributions
    Pension   8% employee / 10% employer, on basic + housing + transport.
              Remit within 7 working days of payday.
    NHF       2.5% of basic salary, deducted from the employee.
    NSITF     1% of total payroll, employer only.
    ITF       1% of annual payroll, employer, where there are 5 or more
              employees or turnover reaches ₦50m.
    NHIS      5% employee / 10% employer of basic, where offered.

Rates change. They are all editable in Settings; these are the defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from ..money import fmt, pct_of

# --------------------------------------------------------------------------
# Statutory defaults
# --------------------------------------------------------------------------

NATIONAL_MINIMUM_WAGE = 70_000_00      # per month, in kobo
RENT_RELIEF_RATE = "20"                # percent of annual rent paid
RENT_RELIEF_CAP = 500_000_00           # per annum, in kobo

PENSION_EMPLOYEE = "8"
PENSION_EMPLOYER = "10"
NHF_RATE = "2.5"
NSITF_RATE = "1"
ITF_RATE = "1"
NHIS_EMPLOYEE = "5"
NHIS_EMPLOYER = "10"

ITF_EMPLOYEE_THRESHOLD = 5
ITF_TURNOVER_THRESHOLD = 50_000_000_00

# (width of the band in kobo, rate) — a width of None means "everything above"
PAYE_BANDS: list[tuple[int | None, str]] = [
    (800_000_00, "0"),
    (2_200_000_00, "15"),
    (9_000_000_00, "18"),
    (13_000_000_00, "21"),
    (25_000_000_00, "23"),
    (None, "25"),
]

# How often someone is paid. This drives annualisation and nothing else.
MONTHLY, FORTNIGHTLY, WEEKLY = "MONTHLY", "FORTNIGHTLY", "WEEKLY"

PERIODS_PER_YEAR = {
    MONTHLY: 12,
    FORTNIGHTLY: 26,
    WEEKLY: 52,
}

FREQUENCY_LABELS = {
    MONTHLY: "Monthly",
    FORTNIGHTLY: "Fortnightly",
    WEEKLY: "Weekly",
}

# How the pay is worked out — kept separate from how often it is paid, because
# a site labourer on a daily rate is usually still paid weekly or monthly.
FIXED, DAILY_RATE, HOURLY_RATE = "FIXED", "DAILY_RATE", "HOURLY_RATE"

PAY_BASIS_LABELS = {
    FIXED: "A fixed amount each period",
    DAILY_RATE: "A daily rate — paid for days worked",
    HOURLY_RATE: "An hourly rate — paid for hours worked",
}

UNIT_LABELS = {FIXED: "periods", DAILY_RATE: "days", HOURLY_RATE: "hours"}

STANDARD_WORKING_DAYS_PER_MONTH = 22

# Remittance deadlines, for the reminders on the payroll dashboard
REMITTANCE_RULES = {
    "PAYE": ("PAYE to the State Internal Revenue Service", "by the 10th of the following month"),
    "PENSION": ("Pension to the employees' PFAs", "within 7 working days of payday"),
    "NHF": ("NHF to the Federal Mortgage Bank", "within 1 month of deduction"),
    "NSITF": ("NSITF employee compensation contribution", "by the 16th of the following month"),
    "ITF": ("Industrial Training Fund contribution", "annually, by 1 April"),
    "NHIS": ("NHIS contributions", "monthly, with the scheme"),
}


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


#: The five contribution slots, in the order they appear on a payslip.
#: The keys are fixed because the ledger accounts and the payslip columns are
#: named after them. Everything a user sees — the name, what it is charged on,
#: the rates — is theirs to set.
SLOT_KEYS = ("PENSION", "NHF", "NHIS", "NSITF", "ITF")

#: Slots the employee contributes to at all. The last two are employer levies.
EMPLOYEE_SLOTS = ("PENSION", "NHF", "NHIS")


@dataclass
class Slot:
    """One contribution: what it is called, what it is charged on, and at what rate."""

    key: str
    name: str
    base: str = "GROSS"              # PENSIONABLE | BASIC | GROSS | TAXABLE
    employee_rate: str = "0"
    employer_rate: str = "0"
    employee_cap: int = 0            # per period; 0 means no cap
    employer_cap: int = 0
    reduces_tax: bool = False        # employee's share comes off chargeable income
    active: bool = True

    def charge(self, bases: dict[str, int]) -> tuple[int, int]:
        """(employee, employer) for this period, capped if a cap is set."""
        if not self.active:
            return 0, 0
        amount = bases.get(self.base, 0)
        if amount <= 0:
            return 0, 0
        employee = pct_of(amount, self.employee_rate or "0")
        employer = pct_of(amount, self.employer_rate or "0")
        if self.employee_cap:
            employee = min(employee, self.employee_cap)
        if self.employer_cap:
            employer = min(employer, self.employer_cap)
        return employee, employer


def nigerian_slots() -> list[Slot]:
    """The statutory Nigerian scheme, expressed in the generic vocabulary."""
    return [
        Slot("PENSION", "Pension", "PENSIONABLE", PENSION_EMPLOYEE, PENSION_EMPLOYER,
             reduces_tax=True),
        Slot("NHF", "NHF", "BASIC", NHF_RATE, "0", reduces_tax=True),
        Slot("NHIS", "NHIS", "BASIC", NHIS_EMPLOYEE, NHIS_EMPLOYER, reduces_tax=True),
        Slot("NSITF", "NSITF", "GROSS", "0", NSITF_RATE),
        Slot("ITF", "ITF", "GROSS", "0", ITF_RATE),
    ]


class PayrollRules:
    """The scheme in force: the tax table, the reliefs and the contributions.

    Nigeria's rates are the defaults, but nothing here is Nigerian in shape.
    An employer anywhere types their own bands, renames the contributions their
    country actually has and switches off the ones it does not — and the same
    arithmetic runs.

    The individual Nigerian rates can still be passed by name — ``pension_employee``,
    ``nhf_rate`` and so on — which is how the older tests and the Nigerian
    settings screen speak. They are written straight into the matching slot.
    """

    _RATE_KEYWORDS = {
        "pension_employee": ("PENSION", "employee_rate"),
        "pension_employer": ("PENSION", "employer_rate"),
        "nhf_rate": ("NHF", "employee_rate"),
        "nhis_employee": ("NHIS", "employee_rate"),
        "nhis_employer": ("NHIS", "employer_rate"),
        "nsitf_rate": ("NSITF", "employer_rate"),
        "itf_rate": ("ITF", "employer_rate"),
    }

    def __init__(
        self,
        minimum_wage: int = NATIONAL_MINIMUM_WAGE,
        rent_relief_rate: str = RENT_RELIEF_RATE,
        rent_relief_cap: int = RENT_RELIEF_CAP,
        relief_name: str = "Rent relief",
        tax_name: str = "PAYE",
        threshold_name: str = "the national minimum wage",
        bands: list[tuple[int | None, str]] | None = None,
        slots: list[Slot] | None = None,
        **rates: str,
    ):
        self.minimum_wage = minimum_wage
        self.rent_relief_rate = rent_relief_rate
        self.rent_relief_cap = rent_relief_cap
        self.relief_name = relief_name
        self.tax_name = tax_name
        self.threshold_name = threshold_name
        self.bands = list(bands) if bands else list(PAYE_BANDS)
        self.slots = list(slots) if slots else nigerian_slots()

        for name, value in rates.items():
            if name not in self._RATE_KEYWORDS:
                raise TypeError(f"PayrollRules got an unexpected keyword: {name}")
            key, attr = self._RATE_KEYWORDS[name]
            setattr(self.slot(key), attr, value)

    def __repr__(self) -> str:
        return (f"PayrollRules(tax_name={self.tax_name!r}, bands={len(self.bands)}, "
                f"slots={[s.key for s in self.slots if s.active]})")

    def slot(self, key: str) -> Slot:
        for s in self.slots:
            if s.key == key:
                return s
        missing = Slot(key, key, active=False)
        self.slots.append(missing)
        return missing

    # The five rates by their old names, so that anything reading the Nigerian
    # scheme directly — a report heading, a template — still reads correctly.
    @property
    def pension_employee(self) -> str:
        return self.slot("PENSION").employee_rate

    @property
    def pension_employer(self) -> str:
        return self.slot("PENSION").employer_rate

    @property
    def nhf_rate(self) -> str:
        return self.slot("NHF").employee_rate

    @property
    def nhis_employee(self) -> str:
        return self.slot("NHIS").employee_rate

    @property
    def nhis_employer(self) -> str:
        return self.slot("NHIS").employer_rate

    @property
    def nsitf_rate(self) -> str:
        return self.slot("NSITF").employer_rate

    @property
    def itf_rate(self) -> str:
        return self.slot("ITF").employer_rate

    @classmethod
    def from_settings(cls, s, bands=None) -> "PayrollRules":
        """Build from a PayrollSetting row, falling back to the Nigerian defaults.

        ``bands`` is the employer's own tax table, as (width, rate) pairs with
        the last width None. Passing None keeps the built-in Nigerian bands —
        which is what an unconfigured company gets, and what every existing set
        of books already has.
        """
        if s is None:
            return cls()

        def flag(name, default=True):
            return bool(getattr(s, name, default))

        slots = [
            Slot("PENSION", getattr(s, "pension_name", "") or "Pension",
                 getattr(s, "pension_base", "") or "PENSIONABLE",
                 s.pension_employee or PENSION_EMPLOYEE,
                 s.pension_employer or PENSION_EMPLOYER,
                 getattr(s, "pension_employee_cap", 0) or 0,
                 getattr(s, "pension_employer_cap", 0) or 0,
                 flag("pension_reduces_tax"), flag("operates_pension")),
            Slot("NHF", getattr(s, "nhf_name", "") or "NHF",
                 getattr(s, "nhf_base", "") or "BASIC",
                 s.nhf_rate or NHF_RATE, "0",
                 getattr(s, "nhf_cap", 0) or 0, 0,
                 flag("nhf_reduces_tax"), flag("operates_nhf")),
            Slot("NHIS", getattr(s, "nhis_name", "") or "NHIS",
                 getattr(s, "nhis_base", "") or "BASIC",
                 s.nhis_employee or NHIS_EMPLOYEE, s.nhis_employer or NHIS_EMPLOYER,
                 getattr(s, "nhis_employee_cap", 0) or 0,
                 getattr(s, "nhis_employer_cap", 0) or 0,
                 flag("nhis_reduces_tax"), flag("operates_nhis", False)),
            Slot("NSITF", getattr(s, "nsitf_name", "") or "NSITF",
                 getattr(s, "nsitf_base", "") or "GROSS",
                 "0", s.nsitf_rate or NSITF_RATE,
                 0, getattr(s, "nsitf_cap", 0) or 0,
                 False, flag("operates_nsitf")),
            Slot("ITF", getattr(s, "itf_name", "") or "ITF",
                 getattr(s, "itf_base", "") or "GROSS",
                 "0", s.itf_rate or ITF_RATE,
                 0, getattr(s, "itf_cap", 0) or 0,
                 False, flag("operates_itf", False)),
        ]
        return cls(
            minimum_wage=s.minimum_wage or 0,
            rent_relief_rate=s.rent_relief_rate or RENT_RELIEF_RATE,
            rent_relief_cap=s.rent_relief_cap if s.rent_relief_cap is not None else RENT_RELIEF_CAP,
            relief_name=getattr(s, "relief_name", "") or "Rent relief",
            tax_name=getattr(s, "tax_name", "") or "PAYE",
            threshold_name=getattr(s, "threshold_name", "")
            or "the national minimum wage",
            bands=list(bands) if bands else list(PAYE_BANDS),
            slots=slots,
        )


# --------------------------------------------------------------------------
# PAYE
# --------------------------------------------------------------------------


@dataclass
class BandCharge:
    """One slice of the tax computation, for showing the working."""

    label: str
    rate: str
    amount: int      # income falling in this band
    tax: int


def annual_paye(chargeable: int, bands=None) -> tuple[int, list[BandCharge]]:
    """Annual PAYE on annual chargeable income, with the band-by-band working."""
    bands = bands or PAYE_BANDS
    if chargeable <= 0:
        return 0, []

    remaining = chargeable
    lower = 0
    total = 0
    working: list[BandCharge] = []

    for width, rate in bands:
        if remaining <= 0:
            break
        slice_ = remaining if width is None else min(remaining, width)
        tax = pct_of(slice_, rate)
        upper = lower + slice_
        label = (
            f"Above {fmt(lower)}" if width is None
            else f"{fmt(lower + (100 if lower else 0))} – {fmt(upper)}"
        )
        working.append(BandCharge(label=label, rate=rate, amount=slice_, tax=tax))
        total += tax
        remaining -= slice_
        lower = upper

    return total, working


def rent_relief(annual_rent_paid: int, rules: PayrollRules | None = None) -> int:
    """The lower of 20% of annual rent actually paid, or ₦500,000."""
    rules = rules or PayrollRules()
    if annual_rent_paid <= 0:
        return 0
    return min(pct_of(annual_rent_paid, rules.rent_relief_rate), rules.rent_relief_cap)


# --------------------------------------------------------------------------
# A single payslip
# --------------------------------------------------------------------------


@dataclass
class Earning:
    name: str
    amount: int
    taxable: bool = True
    pensionable: bool = False


@dataclass
class Deduction:
    name: str
    amount: int
    # A statutory deduction reduces chargeable income; a voluntary one does not
    reduces_tax: bool = False


@dataclass
class SlotAmount:
    """What one contribution came to this period."""

    key: str
    name: str
    employee: int = 0
    employer: int = 0
    reduces_tax: bool = False


@dataclass
class PayslipResult:
    # Earnings
    basic: int = 0
    housing: int = 0
    transport: int = 0
    earnings: list[Earning] = field(default_factory=list)
    gross: int = 0
    taxable_gross: int = 0
    pensionable: int = 0

    # Every contribution this scheme charges, named as the employer named them
    contributions: list[SlotAmount] = field(default_factory=list)

    # Statutory deductions from the employee
    pension_employee: int = 0
    nhf: int = 0
    nhis_employee: int = 0

    # Employer's own cost
    pension_employer: int = 0
    nhis_employer: int = 0
    nsitf: int = 0
    itf: int = 0

    # Tax
    annual_gross: int = 0
    annual_reliefs: int = 0
    rent_relief: int = 0
    annual_chargeable: int = 0
    annual_paye: int = 0
    paye: int = 0
    paye_exempt_reason: str = ""
    bands: list[BandCharge] = field(default_factory=list)

    # Other deductions
    other_deductions: list[Deduction] = field(default_factory=list)
    loan_repayment: int = 0
    other_deductions_total: int = 0

    # Result
    total_deductions: int = 0
    net_pay: int = 0
    employer_cost: int = 0

    @property
    def statutory_employee(self) -> int:
        return sum(c.employee for c in self.contributions)

    @property
    def statutory_employer(self) -> int:
        return sum(c.employer for c in self.contributions)

    @property
    def tax_reducing_contributions(self) -> int:
        return sum(c.employee for c in self.contributions if c.reduces_tax)

    def slot_amounts(self, key: str) -> tuple[int, int]:
        for c in self.contributions:
            if c.key == key:
                return c.employee, c.employer
        return 0, 0


def compute_payslip(
    *,
    basic: int,
    housing: int = 0,
    transport: int = 0,
    earnings: list[Earning] | None = None,
    deductions: list[Deduction] | None = None,
    loan_repayment: int = 0,
    frequency: str = MONTHLY,
    units: Decimal | int | str = 1,
    annual_rent_paid: int = 0,
    pension_enrolled: bool = True,
    nhf_enrolled: bool = True,
    nhis_enrolled: bool = False,
    paye_exempt: bool = False,
    itf_applies: bool = False,
    nsitf_applies: bool = True,
    rules: PayrollRules | None = None,
) -> PayslipResult:
    """Work out one employee's pay for one period.

    ``frequency`` is how often this person is paid, and is the only thing that
    drives annualisation. ``units`` multiplies the pay elements within that
    period — days worked for someone on a daily rate, hours for an hourly one,
    and 1 for salaried staff. The two are independent: a labourer on a daily
    rate is usually still paid monthly.

    PAYE is worked out by annualising this period's pay, taxing it, then
    dividing back down. For someone whose hours vary, that makes each period's
    deduction provisional — which is how PAYE is meant to work, with the year
    reconciled at the end.
    """
    rules = rules or PayrollRules()
    earnings = list(earnings or [])
    deductions = list(deductions or [])
    periods = PERIODS_PER_YEAR.get(frequency, 12)
    mult = Decimal(str(units))

    r = PayslipResult()
    r.basic = _round(Decimal(basic) * mult)
    r.housing = _round(Decimal(housing) * mult)
    r.transport = _round(Decimal(transport) * mult)

    # Basic, housing and transport are the pensionable core
    r.earnings = [
        Earning("Basic salary", r.basic, taxable=True, pensionable=True),
        *([Earning("Housing allowance", r.housing, True, True)] if r.housing else []),
        *([Earning("Transport allowance", r.transport, True, True)] if r.transport else []),
    ]
    for e in earnings:
        scaled = Earning(e.name, _round(Decimal(e.amount) * mult), e.taxable, e.pensionable)
        if scaled.amount:
            r.earnings.append(scaled)

    r.gross = sum(e.amount for e in r.earnings)
    r.taxable_gross = sum(e.amount for e in r.earnings if e.taxable)
    r.pensionable = sum(e.amount for e in r.earnings if e.pensionable)

    # ---- Contributions -----------------------------------------------------
    # Whatever this country calls them. Each slot says what it is charged on;
    # the employee has to be enrolled in it and the employer has to operate it.
    bases = {
        "PENSIONABLE": r.pensionable,
        "BASIC": r.basic,
        "GROSS": r.gross,
        "TAXABLE": r.taxable_gross,
    }
    enrolled = {
        "PENSION": pension_enrolled,
        "NHF": nhf_enrolled,
        "NHIS": nhis_enrolled,
        "NSITF": nsitf_applies,
        "ITF": itf_applies,
    }
    for slot in rules.slots:
        if not enrolled.get(slot.key, True):
            continue
        employee, employer = slot.charge(bases)
        if not (employee or employer):
            continue
        r.contributions.append(
            SlotAmount(slot.key, slot.name, employee, employer, slot.reduces_tax)
        )

    r.pension_employee, r.pension_employer = r.slot_amounts("PENSION")
    r.nhf, _ = r.slot_amounts("NHF")
    r.nhis_employee, r.nhis_employer = r.slot_amounts("NHIS")
    _, r.nsitf = r.slot_amounts("NSITF")
    _, r.itf = r.slot_amounts("ITF")

    # ---- PAYE -------------------------------------------------------------
    # Everything is annualised, taxed, then divided back down — which is how
    # the bands are defined and how the tax office expects it to be done.
    r.annual_gross = r.taxable_gross * periods

    monthly_equivalent = _round(Decimal(r.gross) * periods / 12)
    if paye_exempt:
        r.paye_exempt_reason = "Marked exempt from PAYE on the employee record"
    elif rules.minimum_wage and monthly_equivalent <= rules.minimum_wage:
        r.paye_exempt_reason = (
            f"Earns no more than {rules.threshold_name} "
            f"({fmt(rules.minimum_wage)} a month), so no {rules.tax_name} is due"
        )

    statutory_annual = r.tax_reducing_contributions * periods
    tax_reducing = sum(d.amount for d in deductions if d.reduces_tax) * periods
    r.rent_relief = rent_relief(annual_rent_paid, rules)
    r.annual_reliefs = statutory_annual + tax_reducing + r.rent_relief
    r.annual_chargeable = max(0, r.annual_gross - r.annual_reliefs)

    if r.paye_exempt_reason:
        r.annual_paye, r.bands, r.paye = 0, [], 0
    else:
        r.annual_paye, r.bands = annual_paye(r.annual_chargeable, rules.bands)
        r.paye = _round(Decimal(r.annual_paye) / periods)

    # ---- Other deductions --------------------------------------------------
    r.other_deductions = [
        Deduction(d.name, _round(Decimal(d.amount) * mult), d.reduces_tax)
        for d in deductions
        if d.amount
    ]
    r.loan_repayment = loan_repayment
    r.other_deductions_total = sum(d.amount for d in r.other_deductions) + r.loan_repayment

    # ---- Result ------------------------------------------------------------
    r.total_deductions = r.statutory_employee + r.paye + r.other_deductions_total
    r.net_pay = r.gross - r.total_deductions
    r.employer_cost = r.gross + r.statutory_employer
    return r


def itf_applies_to(employee_count: int, annual_turnover: int) -> bool:
    """ITF is due from employers with 5+ staff or turnover of ₦50m or more."""
    return (
        employee_count >= ITF_EMPLOYEE_THRESHOLD
        or annual_turnover >= ITF_TURNOVER_THRESHOLD
    )
