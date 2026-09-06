"""First-run setup: chart of accounts, tax codes, bank accounts, admin user."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    ASSET,
    EQUITY,
    EXPENSE,
    INCOME,
    LIABILITY,
    ROLE_ADMIN,
    Account,
    AssetCategory,
    BankAccount,
    Company,
    FiscalYear,
    PayrollSetting,
    User,
)
from .security import hash_password
from .services.tax import seed_tax_codes

# code, name, type, subtype, system_key, cashflow_class, is_bank
CHART_OF_ACCOUNTS: list[tuple] = [
    # ---------------- ASSETS ----------------
    ("1000", "Cash and Bank", ASSET, "BANK", "", "OPERATING", False),
    ("1010", "Cash on Hand", ASSET, "CASH", "CASH", "OPERATING", True),
    ("1020", "Main Current Account", ASSET, "BANK", "DEFAULT_BANK", "OPERATING", True),
    ("1030", "Savings Account", ASSET, "BANK", "", "OPERATING", True),
    ("1040", "Domiciliary Account (USD)", ASSET, "BANK", "", "OPERATING", True),
    ("1050", "Petty Cash", ASSET, "CASH", "", "OPERATING", True),
    ("1060", "Mobile Money / POS Settlement", ASSET, "BANK", "", "OPERATING", True),
    ("1100", "Accounts Receivable (Trade Debtors)", ASSET, "RECEIVABLE", "AR", "OPERATING", False),
    ("1110", "Allowance for Doubtful Debts", ASSET, "RECEIVABLE", "BAD_DEBT_PROV", "OPERATING", False),
    ("1200", "Inventory / Stock", ASSET, "INVENTORY", "INVENTORY", "OPERATING", False),
    ("1210", "Goods in Transit", ASSET, "INVENTORY", "", "OPERATING", False),
    ("1300", "Prepaid Expenses", ASSET, "CURRENT_ASSET", "", "OPERATING", False),
    ("1310", "Staff Advances and Loans", ASSET, "CURRENT_ASSET", "", "OPERATING", False),
    ("1320", "Deposits and Refundables", ASSET, "CURRENT_ASSET", "", "OPERATING", False),
    ("1400", "Input VAT Recoverable", ASSET, "CURRENT_ASSET", "VAT_INPUT", "OPERATING", False),
    ("1410", "WHT Credit Receivable", ASSET, "CURRENT_ASSET", "WHT_RECEIVABLE", "OPERATING", False),
    ("1415", "VAT Withheld at Source", ASSET, "CURRENT_ASSET", "VAT_WITHHELD", "OPERATING", False),
    ("1420", "Suspense Account", ASSET, "CURRENT_ASSET", "SUSPENSE", "OPERATING", False),
    ("1500", "Land and Buildings", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1510", "Motor Vehicles", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1520", "Plant and Machinery", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1530", "Furniture and Fittings", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1540", "Computer and Office Equipment", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1550", "Generators and Power Equipment", ASSET, "FIXED_ASSET", "", "INVESTING", False),
    ("1600", "Accumulated Depreciation — Buildings", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1610", "Accumulated Depreciation — Motor Vehicles", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1620", "Accumulated Depreciation — Plant and Machinery", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1630", "Accumulated Depreciation — Furniture and Fittings", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1640", "Accumulated Depreciation — Computer Equipment", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1650", "Accumulated Depreciation — Generators", ASSET, "ACCUM_DEP", "", "INVESTING", False),
    ("1700", "Intangible Assets", ASSET, "OTHER_ASSET", "", "INVESTING", False),
    ("1800", "Long-term Investments", ASSET, "OTHER_ASSET", "", "INVESTING", False),

    # ---------------- LIABILITIES ----------------
    ("2000", "Accounts Payable (Trade Creditors)", LIABILITY, "PAYABLE", "AP", "OPERATING", False),
    ("2100", "Accruals and Other Payables", LIABILITY, "CURRENT_LIABILITY", "", "OPERATING", False),
    ("2110", "Customer Deposits and Advances", LIABILITY, "CURRENT_LIABILITY", "", "OPERATING", False),
    ("2200", "Output VAT Payable", LIABILITY, "TAX_PAYABLE", "VAT_OUTPUT", "OPERATING", False),
    ("2210", "Withholding Tax Payable", LIABILITY, "TAX_PAYABLE", "WHT_PAYABLE", "OPERATING", False),
    ("2220", "PAYE Payable", LIABILITY, "TAX_PAYABLE", "PAYE_PAYABLE", "OPERATING", False),
    ("2230", "Company Income Tax Payable", LIABILITY, "TAX_PAYABLE", "CIT_PAYABLE", "OPERATING", False),
    ("2240", "Development Levy Payable", LIABILITY, "TAX_PAYABLE", "", "OPERATING", False),
    ("2250", "Pension Payable (PENCOM)", LIABILITY, "CURRENT_LIABILITY", "PENSION_PAYABLE", "OPERATING", False),
    ("2260", "NHF Payable", LIABILITY, "CURRENT_LIABILITY", "NHF_PAYABLE", "OPERATING", False),
    ("2262", "NSITF Payable", LIABILITY, "CURRENT_LIABILITY", "NSITF_PAYABLE", "OPERATING", False),
    ("2264", "ITF Payable", LIABILITY, "CURRENT_LIABILITY", "ITF_PAYABLE", "OPERATING", False),
    ("2266", "NHIS Payable", LIABILITY, "CURRENT_LIABILITY", "NHIS_PAYABLE", "OPERATING", False),
    ("2268", "Staff Deductions Payable", LIABILITY, "CURRENT_LIABILITY", "STAFF_DEDUCTIONS", "OPERATING", False),
    ("2270", "Salaries and Wages Payable", LIABILITY, "CURRENT_LIABILITY", "WAGES_PAYABLE", "OPERATING", False),
    ("2300", "Bank Overdraft", LIABILITY, "CURRENT_LIABILITY", "", "FINANCING", False),
    ("2400", "Short-term Loans", LIABILITY, "LOAN", "", "FINANCING", False),
    ("2500", "Long-term Loans", LIABILITY, "OTHER_LIABILITY", "", "FINANCING", False),
    ("2600", "Directors' Current Account", LIABILITY, "OTHER_LIABILITY", "", "FINANCING", False),

    # ---------------- EQUITY ----------------
    ("3000", "Share Capital", EQUITY, "CAPITAL", "", "FINANCING", False),
    ("3010", "Share Premium", EQUITY, "CAPITAL", "", "FINANCING", False),
    ("3100", "Retained Earnings", EQUITY, "RETAINED_EARNINGS", "RETAINED_EARNINGS", "FINANCING", False),
    ("3200", "Owner's Equity / Capital Introduced", EQUITY, "CAPITAL", "", "FINANCING", False),
    ("3300", "Drawings / Dividends Paid", EQUITY, "DRAWINGS", "DRAWINGS", "FINANCING", False),
    ("3900", "Opening Balance Equity", EQUITY, "CAPITAL", "OPENING_EQUITY", "FINANCING", False),

    # ---------------- INCOME ----------------
    ("4000", "Sales — Goods", INCOME, "SALES", "SALES", "OPERATING", False),
    ("4010", "Sales — Services", INCOME, "SALES", "SALES_SERVICES", "OPERATING", False),
    ("4100", "Sales Returns and Allowances", INCOME, "SALES", "SALES_RETURNS", "OPERATING", False),
    ("4200", "Discounts Allowed", INCOME, "SALES", "DISCOUNT_ALLOWED", "OPERATING", False),
    ("4900", "Other Operating Income", INCOME, "OTHER_INCOME", "OTHER_INCOME", "OPERATING", False),
    ("4910", "Interest Income", INCOME, "OTHER_INCOME", "INTEREST_INCOME", "OPERATING", False),
    ("4920", "Foreign Exchange Gain", INCOME, "OTHER_INCOME", "FX_GAIN", "OPERATING", False),
    ("4930", "Gain on Disposal of Assets", INCOME, "OTHER_INCOME", "DISPOSAL_GAIN", "INVESTING", False),
    ("4940", "Stock Adjustment Gain", INCOME, "OTHER_INCOME", "STOCK_GAIN", "OPERATING", False),

    # ---------------- COST OF SALES ----------------
    ("5000", "Cost of Goods Sold", EXPENSE, "COGS", "COGS", "OPERATING", False),
    ("5010", "Purchases", EXPENSE, "COGS", "PURCHASES", "OPERATING", False),
    ("5020", "Direct Labour", EXPENSE, "COGS", "", "OPERATING", False),
    ("5030", "Freight and Clearing In", EXPENSE, "COGS", "", "OPERATING", False),
    ("5040", "Import Duty and Customs", EXPENSE, "COGS", "", "OPERATING", False),
    ("5050", "Stock Adjustment / Shrinkage", EXPENSE, "COGS", "STOCK_LOSS", "OPERATING", False),
    ("5060", "Discounts Received", EXPENSE, "COGS", "DISCOUNT_RECEIVED", "OPERATING", False),

    # ---------------- OPERATING EXPENSES ----------------
    ("6000", "Salaries and Wages", EXPENSE, "PAYROLL", "SALARIES", "OPERATING", False),
    ("6010", "Staff Pension (Employer)", EXPENSE, "PAYROLL", "PENSION_EXPENSE", "OPERATING", False),
    ("6015", "NSITF — Employee Compensation", EXPENSE, "PAYROLL", "NSITF_EXPENSE", "OPERATING", False),
    ("6016", "Industrial Training Fund (ITF)", EXPENSE, "PAYROLL", "ITF_EXPENSE", "OPERATING", False),
    ("6017", "NHIS (Employer)", EXPENSE, "PAYROLL", "NHIS_EXPENSE", "OPERATING", False),
    ("6020", "Staff Welfare and Medical", EXPENSE, "PAYROLL", "", "OPERATING", False),
    ("6030", "Staff Training", EXPENSE, "PAYROLL", "", "OPERATING", False),
    ("6100", "Rent and Rates", EXPENSE, "OPERATING_EXPENSE", "RENT", "OPERATING", False),
    ("6110", "Electricity and Utilities", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6120", "Diesel, Fuel and Generator Running", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6130", "Security Services", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6140", "Repairs and Maintenance", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6150", "Cleaning and Sanitation", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6200", "Telephone and Internet", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6210", "Printing and Stationery", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6220", "Postage and Courier", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6230", "Software and Subscriptions", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6300", "Transport and Travelling", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6310", "Motor Vehicle Running Costs", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6320", "Freight and Delivery Out", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6400", "Advertising and Marketing", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6410", "Entertainment and Hospitality", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6500", "Professional and Consultancy Fees", EXPENSE, "OPERATING_EXPENSE", "PROF_FEES", "OPERATING", False),
    ("6510", "Audit Fees", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6520", "Legal Fees", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6530", "Statutory and Regulatory Fees", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6600", "Insurance", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6700", "Bank Charges and Commission", EXPENSE, "OPERATING_EXPENSE", "BANK_CHARGES", "OPERATING", False),
    ("6710", "Interest Expense", EXPENSE, "FINANCE_COST", "INTEREST_EXPENSE", "FINANCING", False),
    ("6720", "Foreign Exchange Loss", EXPENSE, "FINANCE_COST", "FX_LOSS", "OPERATING", False),
    ("6800", "Bad Debts Written Off", EXPENSE, "OPERATING_EXPENSE", "BAD_DEBTS", "OPERATING", False),
    ("6810", "Donations and Levies", EXPENSE, "OPERATING_EXPENSE", "", "OPERATING", False),
    ("6820", "Loss on Disposal of Assets", EXPENSE, "OTHER_EXPENSE", "DISPOSAL_LOSS", "INVESTING", False),
    ("6900", "Depreciation", EXPENSE, "DEPRECIATION", "DEPRECIATION", "OPERATING", False),
    ("6910", "Amortisation", EXPENSE, "DEPRECIATION", "", "OPERATING", False),
    ("6950", "Rounding Differences", EXPENSE, "OTHER_EXPENSE", "ROUNDING", "OPERATING", False),
    ("6960", "Till Differences (Over and Short)", EXPENSE, "OPERATING_EXPENSE", "TILL_DIFF", "OPERATING", False),
    ("6990", "Sundry Expenses", EXPENSE, "OTHER_EXPENSE", "", "OPERATING", False),

    # ---------------- TAX ----------------
    ("7000", "Company Income Tax", EXPENSE, "TAX_EXPENSE", "CIT_EXPENSE", "OPERATING", False),
    ("7010", "Education Tax / Development Levy", EXPENSE, "TAX_EXPENSE", "", "OPERATING", False),
    ("7020", "Irrecoverable VAT", EXPENSE, "TAX_EXPENSE", "VAT_IRRECOVERABLE", "OPERATING", False),
]

DEFAULT_BANKS = [
    ("Main Current Account", "1020", "CURRENT", True),
    ("Cash on Hand", "1010", "CASH", False),
    ("Petty Cash", "1050", "CASH", False),
]


def seed_accounts(db: Session) -> int:
    created = 0
    for code, name, type_, subtype, key, cf, is_bank in CHART_OF_ACCOUNTS:
        if db.scalar(select(Account).where(Account.code == code)):
            continue
        db.add(
            Account(
                code=code,
                name=name,
                type=type_,
                subtype=subtype,
                system_key=key,
                is_system=bool(key),
                cashflow_class=cf,
                is_bank=is_bank,
            )
        )
        created += 1
    db.flush()
    return created


def attach_system_keys(db: Session) -> int:
    """Fill in system keys on accounts that predate them.

    A company created before payroll existed already has account 2260, but
    without the ``NHF_PAYABLE`` key the posting engine looks for. This runs on
    every start and quietly brings an older company file up to date.
    """
    fixed = 0
    for code, name, _t, _s, key, _cf, _b in CHART_OF_ACCOUNTS:
        if not key:
            continue
        acc = db.scalar(select(Account).where(Account.code == code))
        if acc is not None and not acc.system_key:
            acc.system_key = key
            acc.is_system = True
            # The old 2260 covered three schemes that now have accounts of
            # their own; rename it so the books are not misleading.
            if code == "2260" and "NSITF" in acc.name:
                acc.name = name
            fixed += 1
    if fixed:
        db.flush()
    return fixed


# name, method, life in months, reducing-balance rate, asset a/c, accum dep a/c, expense a/c
ASSET_CATEGORIES: list[tuple] = [
    ("Land", "NONE", 0, "0", "1500", "1600", "6900"),
    ("Buildings", "STRAIGHT", 600, "2", "1500", "1600", "6900"),
    ("Motor Vehicles", "STRAIGHT", 48, "25", "1510", "1610", "6900"),
    ("Plant and Machinery", "STRAIGHT", 60, "20", "1520", "1620", "6900"),
    ("Furniture and Fittings", "STRAIGHT", 60, "20", "1530", "1630", "6900"),
    ("Computer and Office Equipment", "STRAIGHT", 36, "33.33", "1540", "1640", "6900"),
    ("Generators and Power Equipment", "STRAIGHT", 60, "20", "1550", "1650", "6900"),
]


def seed_asset_categories(db: Session) -> int:
    """The asset classes a Nigerian trading company actually holds.

    Lives here are book depreciation, which is what the accounts show. Capital
    allowances for tax are a separate calculation the accountant makes at
    year end, and deliberately are not mixed in with these.
    """
    created = 0
    codes = {a.code: a for a in db.scalars(select(Account))}
    for i, (name, method, months, rate, asset_c, accum_c, exp_c) in enumerate(ASSET_CATEGORIES):
        if db.scalar(select(AssetCategory).where(AssetCategory.name == name)):
            continue
        asset_a, accum_a, exp_a = codes.get(asset_c), codes.get(accum_c), codes.get(exp_c)
        db.add(
            AssetCategory(
                name=name,
                method=method,
                useful_life_months=months,
                rate_pct=rate,
                asset_account_id=asset_a.id if asset_a else None,
                accum_dep_account_id=accum_a.id if accum_a else None,
                expense_account_id=exp_a.id if exp_a else None,
                sort=i,
            )
        )
        created += 1
    db.flush()
    return created


def seed_bank_accounts(db: Session) -> None:
    for i, (name, code, kind, is_default) in enumerate(DEFAULT_BANKS):
        acc = db.scalar(select(Account).where(Account.code == code))
        if acc is None:
            continue
        if db.scalar(select(BankAccount).where(BankAccount.account_id == acc.id)):
            continue
        db.add(
            BankAccount(
                name=name,
                account_id=acc.id,
                account_type=kind,
                is_default=is_default,
                sort=i,
            )
        )
    db.flush()


def ensure_fiscal_year(db: Session, on: date, start_month: int = 1) -> FiscalYear:
    """Return the fiscal year containing ``on``, creating it if needed."""
    year = on.year if on.month >= start_month else on.year - 1
    start = date(year, start_month, 1)
    end = date(year + 1, start_month, 1) - __import__("datetime").timedelta(days=1)
    fy = db.scalar(select(FiscalYear).where(FiscalYear.start_date == start))
    if fy is None:
        label = str(year) if start_month == 1 else f"{year}/{str(year + 1)[-2:]}"
        fy = FiscalYear(start_date=start, end_date=end, name=label)
        db.add(fy)
        db.flush()
    return fy


def bootstrap(db: Session, admin_password: str = "admin123") -> Company:
    """Create everything a brand-new installation needs."""
    company = db.get(Company, 1)
    if company is None:
        company = Company(id=1)
        db.add(company)
        db.flush()

    seed_accounts(db)
    attach_system_keys(db)
    seed_tax_codes(db)
    seed_bank_accounts(db)
    seed_asset_categories(db)
    from .services.costing import ensure_default_location

    ensure_default_location(db)
    ensure_fiscal_year(db, date.today(), company.fiscal_year_start_month)

    if db.get(PayrollSetting, 1) is None:
        db.add(PayrollSetting(id=1))
        db.flush()

    if db.scalar(select(User).limit(1)) is None:
        db.add(
            User(
                username="admin",
                full_name="Administrator",
                role=ROLE_ADMIN,
                password_hash=hash_password(admin_password),
                must_change_password=True,
                is_super_admin=True,
            )
        )
    db.flush()

    # An installation must never be left with nobody able to grant the flag.
    # This also carries books made before super administrators existed: the
    # first administrator in the file becomes one, which is the person who
    # already had every other power anyway.
    if db.scalar(select(User).where(User.is_super_admin.is_(True)).limit(1)) is None:
        first = db.scalar(
            select(User).where(User.role == ROLE_ADMIN, User.is_active.is_(True))
            .order_by(User.id))
        if first is not None:
            first.is_super_admin = True
    db.flush()
    return company
