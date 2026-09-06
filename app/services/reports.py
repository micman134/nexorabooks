"""Financial reporting.

Every figure here is derived from the general ledger, never from a cached
total, so a report can always be traced line by line back to a journal entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    ASSET,
    DEBIT_TYPES,
    EQUITY,
    EXPENSE,
    INCOME,
    LIABILITY,
    VAT,
    WHT,
    Account,
    BankAccount,
    Bill,
    Company,
    Contact,
    Invoice,
    Item,
    JournalEntry,
    JournalLine,
    Payment,
    TaxCode,
)


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


def _live_entries():
    """Which entries a report sees.

    A reversed entry and its reversal both stay in the ledger and cancel each
    other out — that is what an auditor expects, and it keeps the trial balance
    honest. ``is_void`` therefore means "has been reversed", and is only used
    for display and to stop an entry being reversed twice; it is never a
    reason to hide an entry from a report.
    """
    return JournalEntry.is_posted.is_(True)


def balances(
    db: Session, start: Date | None = None, end: Date | None = None
) -> dict[int, tuple[int, int]]:
    """``{account_id: (debit, credit)}`` over the given range."""
    q = (
        select(
            JournalLine.account_id,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(_live_entries())
        .group_by(JournalLine.account_id)
    )
    if start:
        q = q.where(JournalEntry.date >= start)
    if end:
        q = q.where(JournalEntry.date <= end)
    return {r[0]: (int(r[1]), int(r[2])) for r in db.execute(q)}


def fiscal_year_bounds(db: Session, on: Date) -> tuple[Date, Date]:
    company = db.get(Company, 1)
    m = company.fiscal_year_start_month if company else 1
    year = on.year if on.month >= m else on.year - 1
    start = Date(year, m, 1)
    end = Date(year + 1, m, 1) - timedelta(days=1)
    return start, end


# --------------------------------------------------------------------------
# Trial balance
# --------------------------------------------------------------------------


@dataclass
class TBRow:
    account: Account
    debit: int
    credit: int

    @property
    def net_debit(self) -> int:
        return max(self.debit - self.credit, 0)

    @property
    def net_credit(self) -> int:
        return max(self.credit - self.debit, 0)


def trial_balance(db: Session, start: Date | None, end: Date) -> tuple[list[TBRow], int, int]:
    bals = balances(db, start, end)
    rows: list[TBRow] = []
    for acc in db.scalars(select(Account).order_by(Account.code)):
        dr, cr = bals.get(acc.id, (0, 0))
        if dr or cr:
            rows.append(TBRow(acc, dr, cr))
    return rows, sum(r.net_debit for r in rows), sum(r.net_credit for r in rows)


# --------------------------------------------------------------------------
# Profit and loss
# --------------------------------------------------------------------------


@dataclass
class PLSection:
    title: str
    rows: list[tuple[Account, int, int]] = field(default_factory=list)
    total: int = 0
    total_prior: int = 0


@dataclass
class ProfitAndLoss:
    start: Date
    end: Date
    revenue: PLSection
    cogs: PLSection
    expenses: PLSection
    other_income: PLSection
    tax: PLSection
    gross_profit: int = 0
    gross_profit_prior: int = 0
    operating_profit: int = 0
    operating_profit_prior: int = 0
    profit_before_tax: int = 0
    profit_before_tax_prior: int = 0
    net_profit: int = 0
    net_profit_prior: int = 0
    compare_label: str = ""


def _section(db, title, accounts, cur, prior) -> PLSection:
    sec = PLSection(title)
    for acc in accounts:
        d, c = cur.get(acc.id, (0, 0))
        pd, pc = prior.get(acc.id, (0, 0))
        v, pv = acc.signed(d, c), acc.signed(pd, pc)
        if v or pv:
            sec.rows.append((acc, v, pv))
            sec.total += v
            sec.total_prior += pv
    return sec


def profit_and_loss(
    db: Session,
    start: Date,
    end: Date,
    prior_start: Date | None = None,
    prior_end: Date | None = None,
    compare_label: str = "",
) -> ProfitAndLoss:
    cur = balances(db, start, end)
    prior = balances(db, prior_start, prior_end) if prior_start else {}

    accounts = list(db.scalars(select(Account).order_by(Account.code)))
    by = lambda *subs: [a for a in accounts if a.subtype in subs]  # noqa: E731

    revenue = _section(db, "Revenue", by("SALES"), cur, prior)
    cogs = _section(db, "Cost of sales", by("COGS"), cur, prior)
    expenses = _section(
        db, "Operating expenses",
        by("OPERATING_EXPENSE", "PAYROLL", "DEPRECIATION", "FINANCE_COST", "OTHER_EXPENSE"),
        cur, prior,
    )
    other_income = _section(db, "Other income", by("OTHER_INCOME"), cur, prior)
    taxsec = _section(db, "Taxation", by("TAX_EXPENSE"), cur, prior)

    pl = ProfitAndLoss(start, end, revenue, cogs, expenses, other_income, taxsec,
                       compare_label=compare_label)
    pl.gross_profit = revenue.total - cogs.total
    pl.gross_profit_prior = revenue.total_prior - cogs.total_prior
    pl.operating_profit = pl.gross_profit - expenses.total
    pl.operating_profit_prior = pl.gross_profit_prior - expenses.total_prior
    pl.profit_before_tax = pl.operating_profit + other_income.total
    pl.profit_before_tax_prior = pl.operating_profit_prior + other_income.total_prior
    pl.net_profit = pl.profit_before_tax - taxsec.total
    pl.net_profit_prior = pl.profit_before_tax_prior - taxsec.total_prior
    return pl


def net_profit_between(db: Session, start: Date | None, end: Date) -> int:
    bals = balances(db, start, end)
    total = 0
    for acc in db.scalars(select(Account).where(Account.type.in_([INCOME, EXPENSE]))):
        d, c = bals.get(acc.id, (0, 0))
        v = acc.signed(d, c)
        total += v if acc.type == INCOME else -v
    return total


# --------------------------------------------------------------------------
# Balance sheet
# --------------------------------------------------------------------------


@dataclass
class BSSection:
    title: str
    rows: list[tuple[Account, int]] = field(default_factory=list)
    total: int = 0


@dataclass
class BalanceSheet:
    as_of: Date
    current_assets: BSSection
    fixed_assets: BSSection
    current_liabilities: BSSection
    long_term_liabilities: BSSection
    equity: BSSection
    retained_brought_forward: int = 0
    current_earnings: int = 0
    total_assets: int = 0
    total_liabilities: int = 0
    total_equity: int = 0
    difference: int = 0

    @property
    def balances_ok(self) -> bool:
        return self.difference == 0


def balance_sheet(db: Session, as_of: Date) -> BalanceSheet:
    bals = balances(db, None, as_of)
    accounts = list(db.scalars(select(Account).order_by(Account.code)))

    def sect(title, subs) -> BSSection:
        s = BSSection(title)
        for acc in accounts:
            if acc.subtype not in subs:
                continue
            d, c = bals.get(acc.id, (0, 0))
            v = acc.signed(d, c)
            if v:
                s.rows.append((acc, v))
                s.total += v
        return s

    ca = sect("Current assets", {"BANK", "CASH", "RECEIVABLE", "INVENTORY", "CURRENT_ASSET"})
    fa = sect("Non-current assets", {"FIXED_ASSET", "ACCUM_DEP", "OTHER_ASSET"})
    cl = sect("Current liabilities", {"PAYABLE", "TAX_PAYABLE", "CURRENT_LIABILITY"})
    ltl = sect("Non-current liabilities", {"LOAN", "OTHER_LIABILITY"})
    eq = sect("Equity", {"CAPITAL", "DRAWINGS", "RESERVE", "RETAINED_EARNINGS"})

    fy_start, _ = fiscal_year_bounds(db, as_of)
    prior_profit = net_profit_between(db, None, fy_start - timedelta(days=1))
    current_profit = net_profit_between(db, fy_start, as_of)

    bs = BalanceSheet(as_of, ca, fa, cl, ltl, eq,
                      retained_brought_forward=prior_profit,
                      current_earnings=current_profit)
    bs.total_assets = ca.total + fa.total
    bs.total_liabilities = cl.total + ltl.total
    bs.total_equity = eq.total + prior_profit + current_profit
    bs.difference = bs.total_assets - (bs.total_liabilities + bs.total_equity)
    return bs


# --------------------------------------------------------------------------
# Cash flow (direct method, derived from actual bank and cash movements)
# --------------------------------------------------------------------------


@dataclass
class CashFlow:
    start: Date
    end: Date
    opening_cash: int = 0
    closing_cash: int = 0
    operating: list[tuple[str, int]] = field(default_factory=list)
    investing: list[tuple[str, int]] = field(default_factory=list)
    financing: list[tuple[str, int]] = field(default_factory=list)
    operating_total: int = 0
    investing_total: int = 0
    financing_total: int = 0
    net_movement: int = 0
    difference: int = 0


def _bank_account_ids(db: Session) -> set[int]:
    ids = {b.account_id for b in db.scalars(select(BankAccount))}
    ids |= {a.id for a in db.scalars(select(Account).where(Account.is_bank.is_(True)))}
    return ids


def _cash_balance(db: Session, bank_ids: set[int], end: Date) -> int:
    if not bank_ids:
        return 0
    row = db.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0)
            - func.coalesce(func.sum(JournalLine.credit), 0)
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(_live_entries(), JournalLine.account_id.in_(bank_ids), JournalEntry.date <= end)
    ).scalar()
    return int(row or 0)


CASHFLOW_LABELS = {
    "RECEIVABLE": "Received from customers",
    "PAYABLE": "Paid to suppliers",
    "PAYROLL": "Salaries, wages and staff costs",
    "TAX_PAYABLE": "Taxes paid / collected",
    "TAX_EXPENSE": "Income tax paid",
    "SALES": "Cash sales",
    "COGS": "Direct costs paid",
    "INVENTORY": "Inventory purchased for cash",
    "OPERATING_EXPENSE": "Operating expenses paid",
    "OTHER_INCOME": "Other income received",
    "OTHER_EXPENSE": "Other expenses paid",
    "CURRENT_ASSET": "Other working capital movements",
    "CURRENT_LIABILITY": "Other working capital movements",
    "FIXED_ASSET": "Purchase of fixed assets",
    "ACCUM_DEP": "Fixed asset movements",
    "OTHER_ASSET": "Investments",
    "LOAN": "Loans received / repaid",
    "OTHER_LIABILITY": "Long-term financing",
    "CAPITAL": "Capital introduced",
    "DRAWINGS": "Drawings and dividends paid",
    "RESERVE": "Reserve movements",
    "RETAINED_EARNINGS": "Equity movements",
    "FINANCE_COST": "Interest and finance costs paid",
    "DEPRECIATION": "Non-cash adjustments",
}


def cash_flow(db: Session, start: Date, end: Date) -> CashFlow:
    """Direct-method cash flow statement.

    Each journal entry that moves cash is classified by the accounts on its
    other side, prorated by value.  Because every naira of cash movement is
    allocated, the three sections always add back to the change in the bank.
    """
    from ..money import allocate

    bank_ids = _bank_account_ids(db)
    cf = CashFlow(start, end)
    if not bank_ids:
        return cf

    cf.opening_cash = _cash_balance(db, bank_ids, start - timedelta(days=1))
    cf.closing_cash = _cash_balance(db, bank_ids, end)
    cf.net_movement = cf.closing_cash - cf.opening_cash

    entry_ids = [
        r[0]
        for r in db.execute(
            select(JournalLine.entry_id)
            .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
            .where(
                _live_entries(),
                JournalLine.account_id.in_(bank_ids),
                JournalEntry.date >= start,
                JournalEntry.date <= end,
            )
            .distinct()
        )
    ]

    buckets: dict[tuple[str, str], int] = {}
    accounts = {a.id: a for a in db.scalars(select(Account))}

    for eid in entry_ids:
        lines = list(db.scalars(select(JournalLine).where(JournalLine.entry_id == eid)))
        cash_delta = sum(l.debit - l.credit for l in lines if l.account_id in bank_ids)
        if cash_delta == 0:
            continue  # a pure bank-to-bank transfer nets to nothing
        others = [l for l in lines if l.account_id not in bank_ids]
        if not others:
            continue
        weights = [abs(l.debit - l.credit) for l in others]
        if sum(weights) == 0:
            continue
        parts = allocate(abs(cash_delta), weights)
        sign = 1 if cash_delta > 0 else -1
        for l, part in zip(others, parts):
            acc = accounts.get(l.account_id)
            if acc is None or part == 0:
                continue
            cls = acc.cashflow_class or "OPERATING"
            label = CASHFLOW_LABELS.get(acc.subtype, acc.name)
            buckets[(cls, label)] = buckets.get((cls, label), 0) + sign * part

    for (cls, label), amount in sorted(buckets.items(), key=lambda kv: (kv[0][0], -abs(kv[1]))):
        if amount == 0:
            continue
        if cls == "INVESTING":
            cf.investing.append((label, amount))
            cf.investing_total += amount
        elif cls == "FINANCING":
            cf.financing.append((label, amount))
            cf.financing_total += amount
        else:
            cf.operating.append((label, amount))
            cf.operating_total += amount

    total = cf.operating_total + cf.investing_total + cf.financing_total
    cf.difference = cf.net_movement - total
    return cf


# --------------------------------------------------------------------------
# General ledger
# --------------------------------------------------------------------------


@dataclass
class LedgerRow:
    entry: JournalEntry
    line: JournalLine
    running: int


def general_ledger(db: Session, account_id: int, start: Date, end: Date):
    acc = db.get(Account, account_id)
    o_dr, o_cr = 0, 0
    row = db.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(_live_entries(), JournalLine.account_id == account_id, JournalEntry.date < start)
    ).one()
    o_dr, o_cr = int(row[0]), int(row[1])
    opening = acc.signed(o_dr, o_cr)

    lines = db.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            _live_entries(),
            JournalLine.account_id == account_id,
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .order_by(JournalEntry.date, JournalEntry.id, JournalLine.line_no)
    ).all()

    rows, running = [], opening
    for line, entry in lines:
        running += acc.signed(line.debit, line.credit)
        rows.append(LedgerRow(entry, line, running))
    return acc, opening, rows, running


# --------------------------------------------------------------------------
# Ageing
# --------------------------------------------------------------------------

AGE_BUCKETS = ["Current", "1–30 days", "31–60 days", "61–90 days", "Over 90 days"]


def _bucket(days: int) -> int:
    if days <= 0:
        return 0
    if days <= 30:
        return 1
    if days <= 60:
        return 2
    if days <= 90:
        return 3
    return 4


@dataclass
class AgeRow:
    contact: Contact
    buckets: list[int]
    total: int
    docs: list = field(default_factory=list)


def aging(db: Session, as_of: Date, receivable: bool = True) -> tuple[list[AgeRow], list[int], int]:
    model = Invoice if receivable else Bill
    doc_type = "INVOICE" if receivable else "BILL"
    credit_type = "CREDIT_NOTE" if receivable else "DEBIT_NOTE"

    docs = db.scalars(
        select(model)
        .where(
            model.status.in_(("POSTED", "PART_PAID")),
            model.date <= as_of,
            model.doc_type.in_((doc_type, credit_type)),
        )
        .order_by(model.date)
    )

    by_contact: dict[int, AgeRow] = {}
    for doc in docs:
        outstanding = doc.balance_due
        if outstanding == 0:
            continue
        sign = -1 if doc.doc_type == credit_type else 1
        ref = doc.due_date or doc.date
        idx = _bucket((as_of - ref).days)
        row = by_contact.get(doc.contact_id)
        if row is None:
            row = AgeRow(doc.contact, [0] * 5, 0)
            by_contact[doc.contact_id] = row
        amt = abs(outstanding) * (1 if outstanding > 0 else -1)
        row.buckets[idx] += amt
        row.total += amt
        row.docs.append((doc, idx, amt))

    rows = sorted((r for r in by_contact.values() if r.total), key=lambda r: -abs(r.total))
    totals = [sum(r.buckets[i] for r in rows) for i in range(5)]
    return rows, totals, sum(totals)


def statement(db: Session, contact_id: int, start: Date, end: Date):
    """Customer or supplier statement: opening balance then every movement."""
    contact = db.get(Contact, contact_id)
    opening_row = db.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            _live_entries(),
            JournalLine.contact_id == contact_id,
            Account.subtype.in_(("RECEIVABLE", "PAYABLE")),
            JournalEntry.date < start,
        )
    ).one()
    opening = int(opening_row[0]) - int(opening_row[1])

    lines = db.execute(
        select(JournalLine, JournalEntry, Account)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            _live_entries(),
            JournalLine.contact_id == contact_id,
            Account.subtype.in_(("RECEIVABLE", "PAYABLE")),
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .order_by(JournalEntry.date, JournalEntry.id)
    ).all()

    rows, running = [], opening
    for line, entry, _acc in lines:
        running += line.debit - line.credit
        rows.append((entry, line, running))
    return contact, opening, rows, running


# --------------------------------------------------------------------------
# Tax reports
# --------------------------------------------------------------------------


@dataclass
class VATReturn:
    start: Date
    end: Date
    standard_sales: int = 0
    zero_rated_sales: int = 0
    exempt_sales: int = 0
    output_vat: int = 0
    standard_purchases: int = 0
    input_vat: int = 0
    vat_withheld: int = 0
    net_payable: int = 0
    opening_balance: int = 0
    paid_in_period: int = 0
    closing_balance: int = 0
    due_date: Date | None = None


def vat_return(db: Session, start: Date, end: Date) -> VATReturn:
    from .. import config

    r = VATReturn(start, end)

    rows = db.execute(
        select(Account.type, TaxCode.code, TaxCode.is_exempt, TaxCode.is_zero_rated,
               func.coalesce(func.sum(JournalLine.tax_base), 0))
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .join(TaxCode, JournalLine.tax_code_id == TaxCode.id)
        .where(
            _live_entries(),
            TaxCode.kind == VAT,
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .group_by(Account.type, TaxCode.code, TaxCode.is_exempt, TaxCode.is_zero_rated)
    ).all()

    for acc_type, code, is_exempt, is_zero, base in rows:
        base = int(base)
        if acc_type == INCOME:
            if is_zero:
                r.zero_rated_sales += base
            elif is_exempt:
                r.exempt_sales += base
            else:
                r.standard_sales += base
        else:
            if not is_exempt and not is_zero:
                r.standard_purchases += base

    out_acc = db.scalar(select(Account).where(Account.system_key == "VAT_OUTPUT"))
    in_acc = db.scalar(select(Account).where(Account.system_key == "VAT_INPUT"))
    period = balances(db, start, end)
    if out_acc:
        d, c = period.get(out_acc.id, (0, 0))
        r.output_vat = c - d
        opening = balances(db, None, start - timedelta(days=1))
        od, oc = opening.get(out_acc.id, (0, 0))
        r.opening_balance = oc - od
        r.paid_in_period = d
    if in_acc:
        d, c = period.get(in_acc.id, (0, 0))
        r.input_vat = d - c

    # VAT that government and oil-and-gas customers kept back and paid to the
    # NRS on your behalf. It has already reached the Service, so it comes off
    # what you owe — otherwise you would pay it twice.
    wh_acc = db.scalar(select(Account).where(Account.system_key == "VAT_WITHHELD"))
    if wh_acc:
        d, c = period.get(wh_acc.id, (0, 0))
        r.vat_withheld = d - c

    r.net_payable = r.output_vat - r.input_vat - r.vat_withheld
    r.closing_balance = r.opening_balance + r.net_payable - r.paid_in_period
    nxt = (end.replace(day=1) + timedelta(days=32)).replace(day=1)
    r.due_date = nxt.replace(day=config.VAT_FILING_DAY)
    return r


def wht_schedule(db: Session, start: Date, end: Date, payable: bool = True):
    """WHT deducted from suppliers (payable) or suffered on our sales (credit)."""
    key = "WHT_PAYABLE" if payable else "WHT_RECEIVABLE"
    acc = db.scalar(select(Account).where(Account.system_key == key))
    if acc is None:
        return [], 0
    rows = db.execute(
        select(JournalLine, JournalEntry, Contact)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .outerjoin(Contact, JournalLine.contact_id == Contact.id)
        .where(
            _live_entries(),
            JournalLine.account_id == acc.id,
            JournalEntry.date >= start,
            JournalEntry.date <= end,
        )
        .order_by(JournalEntry.date, JournalEntry.id)
    ).all()
    out = []
    total = 0
    for line, entry, contact in rows:
        amount = (line.credit - line.debit) if payable else (line.debit - line.credit)
        if amount == 0:
            continue
        out.append((entry, line, contact, amount))
        total += amount
    return out, total


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


def inventory_valuation(db: Session):
    items = list(
        db.scalars(
            select(Item)
            .where(Item.item_type == "STOCK", Item.track_stock.is_(True))
            .order_by(Item.code)
        )
    )
    total_value = sum(i.stock_value for i in items)
    return items, total_value


def low_stock(db: Session):
    return list(
        db.scalars(
            select(Item)
            .where(
                Item.item_type == "STOCK",
                Item.track_stock.is_(True),
                Item.is_active.is_(True),
                Item.reorder_level > 0,
                Item.qty_on_hand <= Item.reorder_level,
            )
            .order_by(Item.name)
        )
    )


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


def dashboard(db: Session, on: Date | None = None) -> dict:
    on = on or Date.today()
    fy_start, fy_end = fiscal_year_bounds(db, on)
    month_start = on.replace(day=1)

    bank_ids = _bank_account_ids(db)
    cash_now = _cash_balance(db, bank_ids, on)

    ar_rows, _, ar_total = aging(db, on, receivable=True)
    ap_rows, _, ap_total = aging(db, on, receivable=False)

    pl_month = profit_and_loss(db, month_start, on)
    pl_year = profit_and_loss(db, fy_start, on)

    overdue_ar = sum(sum(r.buckets[1:]) for r in ar_rows)
    overdue_ap = sum(sum(r.buckets[1:]) for r in ap_rows)

    vat_acc = db.scalar(select(Account).where(Account.system_key == "VAT_OUTPUT"))
    vat_in_acc = db.scalar(select(Account).where(Account.system_key == "VAT_INPUT"))
    wht_acc = db.scalar(select(Account).where(Account.system_key == "WHT_PAYABLE"))
    all_bal = balances(db, None, on)

    def bal(acc):
        if acc is None:
            return 0
        d, c = all_bal.get(acc.id, (0, 0))
        return acc.signed(d, c)

    # Twelve-month revenue and expense trend for the dashboard chart
    trend = []
    cursor = (month_start - timedelta(days=1)).replace(day=1)
    months = []
    m = month_start
    for _ in range(12):
        months.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    for ms in reversed(months):
        me = (ms + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        p = profit_and_loss(db, ms, min(me, on))
        trend.append(
            {
                "label": ms.strftime("%b %y"),
                "revenue": p.revenue.total + p.other_income.total,
                "expense": p.cogs.total + p.expenses.total + p.tax.total,
                "profit": p.net_profit,
            }
        )

    return {
        "as_of": on,
        "cash": cash_now,
        "ar_total": ar_total,
        "ap_total": ap_total,
        "overdue_ar": overdue_ar,
        "overdue_ap": overdue_ap,
        "revenue_month": pl_month.revenue.total,
        "revenue_year": pl_year.revenue.total,
        "profit_month": pl_month.net_profit,
        "profit_year": pl_year.net_profit,
        "gross_margin": (
            round(pl_year.gross_profit * 100 / pl_year.revenue.total, 1)
            if pl_year.revenue.total
            else 0
        ),
        "vat_due": bal(vat_acc) - bal(vat_in_acc),
        "wht_due": bal(wht_acc),
        "fy_start": fy_start,
        "fy_end": fy_end,
        "trend": trend,
        "low_stock": low_stock(db),
        "top_debtors": ar_rows[:5],
        "top_creditors": ap_rows[:5],
    }
