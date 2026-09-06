"""Landed cost — getting freight, duty and clearing into the value of the goods.

A container of tiles is not worth what the supplier invoiced. It is worth that,
plus the shipping, plus the import duty, plus what the clearing agent charged at
Apapa. Until those are added to the stock, every sale out of that container
shows more profit than the business actually made, and the balance sheet
understates the inventory.

What this does is move cost sideways: out of the freight and duty expense
accounts, into the inventory account and onto the items themselves. It creates
no new cost and changes no total — the profit and loss is relieved by exactly
what the balance sheet takes on.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    BY_QUANTITY,
    BY_VALUE,
    BY_WEIGHT,
    DRAFT,
    POSTED,
    STOCK_ITEM,
    VOID,
    Account,
    Bill,
    BillLine,
    Item,
    JournalEntry,
    LandedCost,
    LandedCostCharge,
    LandedCostLine,
    User,
)
from ..money import allocate
from . import costing
from .posting import (
    EntryDraft,
    PostingError,
    audit,
    next_number,
    post_entry,
    reverse_entry,
    sys_account,
)


class LandedCostError(PostingError):
    """Safe to show the user."""


# --------------------------------------------------------------------------
# Building one
# --------------------------------------------------------------------------


def create(db: Session, on: date, basis: str = BY_VALUE, user: User | None = None) -> LandedCost:
    lc = LandedCost(
        number=next_number(db, "LANDED"),
        date=on,
        basis=basis,
        status=DRAFT,
        created_by_id=user.id if user else None,
    )
    db.add(lc)
    db.flush()
    return lc


def add_bill(db: Session, lc: LandedCost, bill: Bill) -> int:
    """Pull the stock lines off a purchase into the consignment.

    Only stock-tracked items: freight on a service line has nowhere to go.
    """
    if lc.status != DRAFT:
        raise LandedCostError(f"{lc.number} has been posted and cannot be changed.")
    if bill.status not in (POSTED, "PART_PAID", "PAID"):
        raise LandedCostError(
            f"{bill.number} has not been posted yet. Post the purchase first, "
            "then spread the freight over it."
        )

    already = {l.bill_line_id for l in lc.lines if l.bill_line_id}
    added = 0
    for line in bill.lines:
        if line.id in already:
            continue
        item = db.get(Item, line.item_id) if line.item_id else None
        if item is None or item.item_type != STOCK_ITEM or not item.track_stock:
            continue
        if line.qty <= 0:
            continue
        db.add(LandedCostLine(
            landed_cost_id=lc.id,
            bill_id=bill.id,
            bill_line_id=line.id,
            item_id=item.id,
            description=line.description or item.name,
            qty=line.qty,
            value=line.net,
            weight=0,
        ))
        added += 1
    db.flush()
    db.refresh(lc)
    if added == 0:
        raise LandedCostError(
            f"{bill.number} has no stock lines to spread freight over — "
            "every line on it is a service or an untracked item."
        )
    recalc(db, lc)
    return added


def add_charge(
    db: Session,
    lc: LandedCost,
    description: str,
    amount: int,
    account_id: int | None,
    contact_id: int | None = None,
    bill_id: int | None = None,
) -> LandedCostCharge:
    if lc.status != DRAFT:
        raise LandedCostError(f"{lc.number} has been posted and cannot be changed.")
    if amount <= 0:
        raise LandedCostError("A charge must be for a positive amount.")
    if not account_id:
        raise LandedCostError(
            "Say which account the charge is sitting in — the freight, duty or "
            "clearing account it was originally booked to."
        )
    charge = LandedCostCharge(
        landed_cost_id=lc.id, description=description[:200], amount=amount,
        account_id=account_id, contact_id=contact_id, bill_id=bill_id,
    )
    db.add(charge)
    db.flush()
    db.refresh(lc)
    recalc(db, lc)
    return charge


# --------------------------------------------------------------------------
# Spreading it
# --------------------------------------------------------------------------


def weights_for(lc: LandedCost, lines=None) -> list[int]:
    """What each line's share is proportional to."""
    lines = lc.lines if lines is None else lines
    if lc.basis == BY_QUANTITY:
        return [max(0, l.qty) for l in lines]
    if lc.basis == BY_WEIGHT:
        return [max(0, l.weight) for l in lines]
    return [max(0, l.value) for l in lines]


def _lines(db: Session, lc: LandedCost) -> list[LandedCostLine]:
    """Read from the table, not the relationship.

    Rows added earlier in the same transaction are not on the loaded
    relationship yet, and spreading a charge over a stale line list would
    quietly allocate nothing.
    """
    return list(
        db.scalars(
            select(LandedCostLine)
            .where(LandedCostLine.landed_cost_id == lc.id)
            .order_by(LandedCostLine.id)
        )
    )


def _charges(db: Session, lc: LandedCost) -> list[LandedCostCharge]:
    return list(
        db.scalars(
            select(LandedCostCharge)
            .where(LandedCostCharge.landed_cost_id == lc.id)
            .order_by(LandedCostCharge.id)
        )
    )


def total_charges(db: Session, lc: LandedCost) -> int:
    return sum(c.amount for c in _charges(db, lc))


def recalc(db: Session, lc: LandedCost) -> LandedCost:
    """Spread the charges across the lines. Nothing is lost to rounding."""
    lines = _lines(db, lc)
    if not lines:
        return lc

    weights = weights_for(lc, lines)
    if sum(weights) == 0:
        # Nothing to weigh by — spreading by weight when no weights were
        # entered, for instance. Fall back to an even split rather than
        # silently allocating nothing.
        weights = [1] * len(lines)

    shares = allocate(total_charges(db, lc), weights)
    for line, share in zip(lines, shares):
        line.allocated = share
    db.flush()
    db.refresh(lc)
    return lc


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------


def post(db: Session, lc: LandedCost, user: User | None = None) -> JournalEntry:
    """Move the charges out of expense and into the value of the stock."""
    if lc.status == POSTED:
        raise LandedCostError(f"{lc.number} has already been posted.")
    if lc.status == VOID:
        raise LandedCostError(f"{lc.number} was voided and cannot be posted.")
    charges = _charges(db, lc)
    lines = _lines(db, lc)
    if not charges:
        raise LandedCostError("Add at least one charge to spread.")
    if not lines:
        raise LandedCostError("Add the purchase the charges belong to.")

    recalc(db, lc)
    charges, lines = _charges(db, lc), _lines(db, lc)
    if sum(l.allocated for l in lines) != sum(c.amount for c in charges):
        from ..money import fmt

        raise LandedCostError(
            f"The charges add up to {fmt(sum(c.amount for c in charges))} but only "
            f"{fmt(sum(l.allocated for l in lines))} was allocated. Please report this."
        )

    draft = EntryDraft(
        date=lc.date,
        memo=f"Landed cost {lc.number} — freight and duty into stock",
        reference=lc.reference or lc.number,
        source="LANDED",
        source_id=lc.id,
    )

    # Out of the expense accounts the charges are sitting in
    for charge in charges:
        draft.credit(charge.account_id, charge.amount,
                     f"{charge.description} moved into stock",
                     contact_id=charge.contact_id)

    # Into inventory, and onto the items themselves
    by_account: dict[int, int] = {}
    for line in lines:
        if not line.allocated:
            continue
        item = db.get(Item, line.item_id)
        inv_acc = item.inventory_account_id or sys_account(db, "INVENTORY").id
        by_account[inv_acc] = by_account.get(inv_acc, 0) + line.allocated
        costing.add_cost(
            db, item, line.allocated, lc.date,
            doc_type="LANDED", doc_id=lc.id, doc_number=lc.number,
            memo=f"Landed cost {lc.number} — {lc.basis_label.lower()}",
        )
    for account_id, amount in by_account.items():
        draft.debit(account_id, amount, f"Landed cost {lc.number}")

    entry = post_entry(db, draft, user=user)
    lc.status = POSTED
    lc.journal_entry_id = entry.id
    lc.posted_at = clock.now()
    db.flush()
    from ..money import fmt as _fmt

    audit(db, user, "POST", "LandedCost", lc.id,
          detail=f"{lc.number} — {_fmt(sum(c.amount for c in charges))} "
                 f"spread over {len(lines)} lines")
    return entry


def void(db: Session, lc: LandedCost, user: User | None = None) -> None:
    """Reverse the entry and take the cost back off the stock."""
    if lc.status == VOID:
        raise LandedCostError(f"{lc.number} is already void.")

    if lc.status == POSTED and lc.journal_entry_id:
        entry = db.get(JournalEntry, lc.journal_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, user=user, memo=f"Reversal of landed cost {lc.number}")
        for line in _lines(db, lc):
            if not line.allocated:
                continue
            item = db.get(Item, line.item_id)
            costing.add_cost(
                db, item, -line.allocated, lc.date,
                doc_type="LANDED", doc_id=lc.id, doc_number=lc.number,
                memo=f"Reversal of landed cost {lc.number}",
            )

    lc.status = VOID
    db.flush()
    audit(db, user, "VOID", "LandedCost", lc.id, detail=lc.number)


# --------------------------------------------------------------------------
# Helping the user fill it in
# --------------------------------------------------------------------------


@dataclass
class Suggestion:
    """A bill that looks like freight, duty or clearing on a recent purchase."""

    bill: Bill
    account: Account | None
    amount: int


def charge_accounts(db: Session) -> list[Account]:
    """The accounts a landed cost normally comes out of."""
    wanted = ("5030", "5040", "6320", "6300")
    rows = list(
        db.scalars(
            select(Account)
            .where(Account.type == "EXPENSE", Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )
    rows.sort(key=lambda a: (a.code not in wanted, a.code))
    return rows


def recent_purchases(db: Session, limit: int = 40) -> list[Bill]:
    """Posted purchases with stock on them — what freight can be spread over."""
    out = []
    for bill in db.scalars(
        select(Bill)
        .where(Bill.doc_type == "BILL", Bill.status.in_((POSTED, "PART_PAID", "PAID")))
        .order_by(Bill.date.desc(), Bill.id.desc())
        .limit(200)
    ):
        if any(
            line.item_id and (db.get(Item, line.item_id).item_type == STOCK_ITEM)
            for line in bill.lines
        ):
            out.append(bill)
        if len(out) >= limit:
            break
    return out
