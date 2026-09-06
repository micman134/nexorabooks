"""The fixed asset register, depreciation and disposals.

Depreciation here is monthly and whole-month: an asset put into service on any
day of a month is charged for that whole month, and an asset sold mid-month is
charged up to and including the month before the sale. That is the convention
most small Nigerian companies use, and it is the one an auditor can check by
counting months on a calendar.

Two rules keep the register honest:

* An asset is never charged twice for the same month. ``last_depreciated_period``
  records the last month charged, and a run refuses to go backwards.
* Depreciation never takes an asset below its residual value. The final month's
  charge is trimmed to whatever is left, so the closing net book value lands
  exactly on the residual — not a kobo under.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    ASSET_ACTIVE,
    ASSET_DISPOSED,
    ASSET_WRITTEN_OFF,
    DRAFT,
    NO_DEPRECIATION,
    POSTED,
    REDUCING_BALANCE,
    STRAIGHT_LINE,
    VOID,
    Account,
    AssetCategory,
    DepreciationLine,
    DepreciationRun,
    FixedAsset,
    JournalEntry,
    User,
)
from .posting import (
    EntryDraft,
    PostingError,
    audit,
    next_number,
    post_entry,
    reverse_entry,
    sys_account,
)


class AssetError(PostingError):
    """Safe to show the user."""


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def period_of(on: date) -> int:
    """A month as YYYYMM — sortable, comparable, and readable in the database."""
    return on.year * 100 + on.month


def period_end(period: int) -> date:
    y, m = divmod(period, 100)
    return date(y, m, monthrange(y, m)[1])


def period_start(period: int) -> date:
    y, m = divmod(period, 100)
    return date(y, m, 1)


def next_period(period: int) -> int:
    y, m = divmod(period, 100)
    return (y + 1) * 100 + 1 if m == 12 else y * 100 + m + 1


def months_between(first: int, last: int) -> int:
    """How many months from ``first`` to ``last`` inclusive. Never negative."""
    if last < first:
        return 0
    fy, fm = divmod(first, 100)
    ly, lm = divmod(last, 100)
    return (ly - fy) * 12 + (lm - fm) + 1


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


@dataclass
class Charge:
    """One month's depreciation on one asset."""

    amount: int
    nbv_before: int
    nbv_after: int
    months: int = 1
    memo: str = ""


def monthly_charge(
    *,
    cost: int,
    residual: int,
    accumulated: int,
    method: str,
    useful_life_months: int,
    rate_pct: str | Decimal = "0",
) -> int:
    """One month's depreciation, in kobo, before any trimming.

    Straight line spreads cost less residual evenly over the life. Reducing
    balance takes an annual percentage of what is left, charged a twelfth at a
    time, which is what "25% reducing balance" means in practice.
    """
    if method == NO_DEPRECIATION:
        return 0

    depreciable = max(0, cost - residual)
    if depreciable <= 0:
        return 0

    if method == REDUCING_BALANCE:
        rate = Decimal(str(rate_pct or 0))
        if rate <= 0:
            return 0
        nbv = cost - accumulated
        if nbv <= residual:
            return 0
        return int((Decimal(nbv) * rate / Decimal(1200)).to_integral_value())

    # Straight line
    if useful_life_months <= 0:
        return 0
    return int((Decimal(depreciable) / Decimal(useful_life_months)).to_integral_value())


def charge_for(asset: FixedAsset, period: int) -> Charge | None:
    """What ``asset`` should be charged for ``period``, or None if nothing.

    Returns None — rather than a zero charge — when the asset is not in service
    yet, is finished, has already been charged for the month, or has left the
    register. A caller can treat None as "leave this one alone".
    """
    if asset.status != ASSET_ACTIVE:
        return None
    if asset.method == NO_DEPRECIATION:
        return None
    if period_of(asset.in_service_date) > period:
        return None
    if asset.last_depreciated_period >= period:
        return None

    nbv_before = asset.cost - asset.accumulated_depreciation
    remaining = nbv_before - asset.residual_value
    if remaining <= 0:
        return None

    # An asset added late — or a month somebody forgot to run — catches up.
    start = max(period_of(asset.in_service_date), next_period(asset.last_depreciated_period)
                if asset.last_depreciated_period else period_of(asset.in_service_date))
    months = months_between(start, period)
    if months <= 0:
        return None

    amount = 0
    accumulated = asset.accumulated_depreciation
    for _ in range(months):
        step = monthly_charge(
            cost=asset.cost,
            residual=asset.residual_value,
            accumulated=accumulated,
            method=asset.method,
            useful_life_months=asset.useful_life_months,
            rate_pct=asset.rate_pct,
        )
        left = asset.cost - accumulated - asset.residual_value
        step = min(step, max(0, left))
        if step <= 0:
            break
        amount += step
        accumulated += step

    if amount <= 0:
        return None

    memo = f"{asset.number} {asset.name}"
    if months > 1:
        memo += f" ({months} months)"
    return Charge(
        amount=amount,
        nbv_before=nbv_before,
        nbv_after=nbv_before - amount,
        months=months,
        memo=memo,
    )


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


def _accounts_for(db: Session, asset: FixedAsset) -> tuple[Account, Account, Account]:
    """(asset account, accumulated depreciation, depreciation expense)."""
    cat = asset.category
    if cat is None:
        raise AssetError(
            f"{asset.number} {asset.name} has no category, so there is nothing to "
            "tell the books which accounts to use. Give it one first."
        )
    missing = [
        label
        for label, acc in (
            ("an asset account", cat.asset_account_id),
            ("an accumulated depreciation account", cat.accum_dep_account_id),
            ("a depreciation expense account", cat.expense_account_id),
        )
        if not acc
    ]
    if missing:
        raise AssetError(
            f"The category '{cat.name}' is missing {', '.join(missing)}. "
            "Set it in Assets › Categories."
        )
    return cat.asset_account, cat.accum_dep_account, cat.expense_account


# --------------------------------------------------------------------------
# Creating an asset
# --------------------------------------------------------------------------


def apply_category_defaults(asset: FixedAsset, category: AssetCategory) -> None:
    asset.method = category.method
    asset.useful_life_months = category.useful_life_months
    asset.rate_pct = category.rate_pct
    if category.residual_pct and Decimal(str(category.residual_pct)) > 0 and asset.cost:
        pct = Decimal(str(category.residual_pct))
        asset.residual_value = int((Decimal(asset.cost) * pct / 100).to_integral_value())


def capitalise(
    db: Session,
    asset: FixedAsset,
    *,
    paid_from_account: Account | int,
    user: User | None = None,
) -> JournalEntry:
    """Post the purchase of an asset that was not already in the books.

    Dr the asset account, Cr wherever the money came from — a bank account, or
    the supplier's payable account if it was bought on credit.
    """
    if asset.acquisition_entry_id:
        raise AssetError(f"{asset.number} has already been capitalised.")
    if asset.cost <= 0:
        raise AssetError("An asset needs a cost before it can be capitalised.")

    asset_acc, _accum, _exp = _accounts_for(db, asset)
    draft = EntryDraft(
        date=asset.purchase_date,
        memo=f"Purchase of {asset.name}",
        reference=asset.number,
        source="ASSET",
        source_id=asset.id,
    )
    draft.debit(asset_acc, asset.cost, asset.name, contact_id=asset.supplier_id)
    draft.credit(paid_from_account, asset.cost, asset.name, contact_id=asset.supplier_id)
    entry = post_entry(db, draft, user=user)
    asset.acquisition_entry_id = entry.id
    db.flush()
    return entry


# --------------------------------------------------------------------------
# The monthly run
# --------------------------------------------------------------------------


def open_run(db: Session, period: int, user: User | None = None) -> DepreciationRun:
    """Build (but do not post) the depreciation for one month."""
    existing = db.scalar(
        select(DepreciationRun).where(
            DepreciationRun.period == period, DepreciationRun.status != VOID
        )
    )
    if existing is not None:
        raise AssetError(
            f"Depreciation for {existing.period_label} already exists as "
            f"{existing.number}. Open it, or void it and start again."
        )

    on = period_end(period)
    run = DepreciationRun(
        number=next_number(db, "DEPRECIATION"),
        period=period,
        date=on,
        status=DRAFT,
        created_by_id=user.id if user else None,
    )
    db.add(run)
    db.flush()

    assets = list(
        db.scalars(
            select(FixedAsset)
            .where(FixedAsset.status == ASSET_ACTIVE)
            .order_by(FixedAsset.number)
        )
    )
    total = 0
    for asset in assets:
        charge = charge_for(asset, period)
        if charge is None:
            continue
        db.add(
            DepreciationLine(
                run_id=run.id,
                asset_id=asset.id,
                amount=charge.amount,
                nbv_before=charge.nbv_before,
                nbv_after=charge.nbv_after,
                months_charged=charge.months,
                memo=charge.memo,
            )
        )
        total += charge.amount

    run.total = total
    db.flush()
    return run


def post_run(db: Session, run: DepreciationRun, user: User | None = None) -> JournalEntry:
    """Write the month's depreciation to the ledger.

    One journal entry: the expense split by category so the profit and loss
    reads sensibly, and the accumulated depreciation credited to the matching
    account for each class of asset.
    """
    if run.status == POSTED:
        raise AssetError(f"{run.number} has already been posted.")
    if run.status == VOID:
        raise AssetError(f"{run.number} was voided and cannot be posted.")
    if not run.lines:
        raise AssetError(
            f"There is nothing to depreciate for {run.period_label}. Every asset is "
            "either not yet in service, fully depreciated, or already charged."
        )

    by_expense: dict[int, int] = {}
    by_accum: dict[int, int] = {}
    for line in run.lines:
        _asset_acc, accum, expense = _accounts_for(db, line.asset)
        by_expense[expense.id] = by_expense.get(expense.id, 0) + line.amount
        by_accum[accum.id] = by_accum.get(accum.id, 0) + line.amount

    draft = EntryDraft(
        date=run.date,
        memo=f"Depreciation for {run.period_label}",
        reference=run.number,
        source="DEPRECIATION",
        source_id=run.id,
    )
    for account_id, amount in by_expense.items():
        draft.debit(account_id, amount, f"Depreciation — {run.period_label}")
    for account_id, amount in by_accum.items():
        draft.credit(account_id, amount, f"Depreciation — {run.period_label}")

    entry = post_entry(db, draft, user=user)

    for line in run.lines:
        asset = line.asset
        asset.accumulated_depreciation += line.amount
        asset.last_depreciated_period = run.period

    run.status = POSTED
    run.journal_entry_id = entry.id
    from datetime import datetime

    run.posted_at = clock.now()
    db.flush()
    audit(db, user, "POST", "DepreciationRun", run.id,
          detail=f"{run.number} — {run.period_label}")
    return entry


def void_run(db: Session, run: DepreciationRun, user: User | None = None) -> None:
    """Reverse a posted run and give every asset its accumulated figure back."""
    if run.status == VOID:
        raise AssetError(f"{run.number} is already void.")

    if run.status == POSTED and run.journal_entry_id:
        entry = db.get(JournalEntry, run.journal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, user=user,
                          memo=f"Reversal of depreciation for {run.period_label}")

    for line in run.lines:
        asset = line.asset
        asset.accumulated_depreciation = max(0, asset.accumulated_depreciation - line.amount)
        # Hand the asset back the month, so a corrected run can charge it again.
        if asset.last_depreciated_period == run.period:
            prior = _previous_period_charged(db, asset, run.period)
            asset.last_depreciated_period = prior

    run.status = VOID
    db.flush()
    audit(db, user, "VOID", "DepreciationRun", run.id, detail=run.number)


def _previous_period_charged(db: Session, asset: FixedAsset, before: int) -> int:
    """The last month this asset was charged for, ignoring voided runs."""
    row = db.execute(
        select(func.max(DepreciationRun.period))
        .join(DepreciationLine, DepreciationLine.run_id == DepreciationRun.id)
        .where(
            DepreciationLine.asset_id == asset.id,
            DepreciationRun.period < before,
            DepreciationRun.status == POSTED,
        )
    ).scalar()
    return int(row or 0)


# --------------------------------------------------------------------------
# Disposal
# --------------------------------------------------------------------------


def dispose(
    db: Session,
    asset: FixedAsset,
    *,
    on: date,
    proceeds: int,
    proceeds_account: Account | int | None,
    note: str = "",
    written_off: bool = False,
    user: User | None = None,
) -> JournalEntry:
    """Take an asset off the register.

    The entry removes the cost and the depreciation charged against it, brings
    in whatever was received, and puts the difference to gain or loss. The
    asset's own record keeps the date, the proceeds and the entry, so the
    schedule can still show it in the year it left.
    """
    if asset.status != ASSET_ACTIVE:
        raise AssetError(f"{asset.number} has already left the register.")
    if on < asset.purchase_date:
        raise AssetError("An asset cannot be disposed of before it was bought.")
    if proceeds and proceeds_account is None:
        raise AssetError("Say where the sale proceeds were paid in.")

    asset_acc, accum_acc, _expense = _accounts_for(db, asset)
    accumulated = asset.accumulated_depreciation
    nbv = asset.cost - accumulated
    result = proceeds - nbv  # positive is a gain

    what = "Write-off" if written_off else "Disposal"
    draft = EntryDraft(
        date=on,
        memo=f"{what} of {asset.name}",
        reference=asset.number,
        source="ASSET_DISPOSAL",
        source_id=asset.id,
    )
    # Clear the asset out of the books
    draft.debit(accum_acc, accumulated, f"Depreciation to date on {asset.number}")
    draft.credit(asset_acc, asset.cost, f"Cost of {asset.number}")
    if proceeds:
        draft.debit(proceeds_account, proceeds, f"Proceeds on {asset.number}")
    if result > 0:
        draft.credit(sys_account(db, "DISPOSAL_GAIN"), result, f"Gain on {asset.number}")
    elif result < 0:
        draft.debit(sys_account(db, "DISPOSAL_LOSS"), -result, f"Loss on {asset.number}")

    entry = post_entry(db, draft, user=user)

    asset.status = ASSET_WRITTEN_OFF if written_off else ASSET_DISPOSED
    asset.disposal_date = on
    asset.disposal_proceeds = proceeds
    asset.disposal_note = note[:255]
    asset.disposal_entry_id = entry.id
    db.flush()
    audit(db, user, "DISPOSE", "FixedAsset", asset.id,
          detail=f"{asset.number} {asset.name} — {what.lower()}")
    return entry


def undo_disposal(db: Session, asset: FixedAsset, user: User | None = None) -> None:
    """Put an asset back on the register, reversing the disposal entry."""
    if asset.status == ASSET_ACTIVE:
        raise AssetError(f"{asset.number} is already on the register.")
    if asset.disposal_entry_id:
        entry = db.get(JournalEntry, asset.disposal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, user=user, memo=f"Reversal of disposal of {asset.name}")
    asset.status = ASSET_ACTIVE
    asset.disposal_date = None
    asset.disposal_proceeds = 0
    asset.disposal_entry_id = None
    db.flush()
    audit(db, user, "RESTORE", "FixedAsset", asset.id, detail=asset.number)


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------


@dataclass
class ScheduleRow:
    """One category's movement over a period — the note to the accounts."""

    category: str
    cost_open: int = 0
    additions: int = 0
    disposals_cost: int = 0
    cost_close: int = 0
    dep_open: int = 0
    charge: int = 0
    disposals_dep: int = 0
    dep_close: int = 0

    @property
    def nbv_open(self) -> int:
        return self.cost_open - self.dep_open

    @property
    def nbv_close(self) -> int:
        return self.cost_close - self.dep_close


def _dep_to(db: Session, asset_id: int, upto: date) -> int:
    """Depreciation posted against one asset up to and including ``upto``."""
    total = db.execute(
        select(func.coalesce(func.sum(DepreciationLine.amount), 0))
        .join(DepreciationRun, DepreciationLine.run_id == DepreciationRun.id)
        .where(
            DepreciationLine.asset_id == asset_id,
            DepreciationRun.status == POSTED,
            DepreciationRun.date <= upto,
        )
    ).scalar()
    return int(total or 0)


def schedule(db: Session, start: date, end: date) -> tuple[list[ScheduleRow], ScheduleRow]:
    """The fixed asset schedule for a period, by category.

    This is built from the assets and the posted depreciation runs, which is
    what makes it a check on the ledger rather than a copy of it: if the
    closing net book value here does not agree with the balance sheet,
    something has been posted to a fixed asset account by hand.
    """
    assets = list(db.scalars(select(FixedAsset).order_by(FixedAsset.number)))
    rows: dict[str, ScheduleRow] = {}
    day_before = date.fromordinal(start.toordinal() - 1)

    for asset in assets:
        name = asset.category.name if asset.category else "Uncategorised"
        row = rows.setdefault(name, ScheduleRow(category=name))

        bought_before = asset.purchase_date < start
        bought_within = start <= asset.purchase_date <= end
        gone_before = asset.disposal_date is not None and asset.disposal_date < start
        gone_within = asset.disposal_date is not None and start <= asset.disposal_date <= end

        if bought_before and not gone_before:
            row.cost_open += asset.cost
            row.dep_open += _dep_to(db, asset.id, day_before)
        if bought_within:
            row.additions += asset.cost
        if gone_within:
            row.disposals_cost += asset.cost
            row.disposals_dep += _dep_to(db, asset.id, asset.disposal_date)

        # The charge for the period, whether or not the asset survived it
        if not gone_before:
            row.charge += _dep_to(db, asset.id, end) - _dep_to(db, asset.id, day_before)

    ordered = []
    for row in rows.values():
        row.cost_close = row.cost_open + row.additions - row.disposals_cost
        row.dep_close = row.dep_open + row.charge - row.disposals_dep
        ordered.append(row)
    ordered.sort(key=lambda r: r.category)

    total = ScheduleRow(category="Total")
    for row in ordered:
        for field in ("cost_open", "additions", "disposals_cost", "cost_close",
                      "dep_open", "charge", "disposals_dep", "dep_close"):
            setattr(total, field, getattr(total, field) + getattr(row, field))
    return ordered, total


def register_totals(db: Session) -> dict:
    """Headline numbers for the register page."""
    assets = list(db.scalars(select(FixedAsset)))
    active = [a for a in assets if a.status == ASSET_ACTIVE]
    return {
        "count": len(active),
        "cost": sum(a.cost for a in active),
        "depreciation": sum(a.accumulated_depreciation for a in active),
        "nbv": sum(a.net_book_value for a in active),
        "disposed": len([a for a in assets if a.status != ASSET_ACTIVE]),
        "fully_depreciated": len([a for a in active if a.is_fully_depreciated]),
    }


def forecast(asset: FixedAsset, months: int = 12) -> list[tuple[int, int, int]]:
    """What the next few months will charge — (period, amount, closing NBV).

    Nothing is posted; this is the "when does this van come off the books"
    question, answered on screen.
    """
    out: list[tuple[int, int, int]] = []
    accumulated = asset.accumulated_depreciation
    period = max(
        period_of(asset.in_service_date),
        next_period(asset.last_depreciated_period) if asset.last_depreciated_period else 0,
    )
    if asset.status != ASSET_ACTIVE or asset.method == NO_DEPRECIATION:
        return out
    for _ in range(months):
        step = monthly_charge(
            cost=asset.cost,
            residual=asset.residual_value,
            accumulated=accumulated,
            method=asset.method,
            useful_life_months=asset.useful_life_months,
            rate_pct=asset.rate_pct,
        )
        step = min(step, max(0, asset.cost - accumulated - asset.residual_value))
        if step <= 0:
            break
        accumulated += step
        out.append((period, step, asset.cost - accumulated))
        period = next_period(period)
    return out
