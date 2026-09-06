"""Requisitions: asking for money, and the trail of who agreed to it.

The route is fixed and it only ever moves one way.

    staff raises it
        -> their named line manager
        -> a director, if it is over the limit
        -> finance, who send the money to the person's own account
        -> the person retires it with receipts

A rejection at any stage carries a reason and goes straight back to the person
who raised it, where they can correct it and send it again. Nothing is ever
rejected silently: the reason is required, not optional.

Two rules are enforced here rather than left to the screens, because a screen
can be bypassed and an approval trail that can be bypassed is worth nothing:

* Nobody approves their own requisition, whatever their role.
* Only the named manager on the requisition — or an administrator standing in
  for them — can give the manager's approval. Being senior is not enough.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import clock
from ..models import (
    OPEN_REQUISITIONS,
    REQ_CANCELLED,
    REQ_DRAFT,
    REQ_PAID,
    REQ_REJECTED,
    REQ_RETIRED,
    REQ_WITH_DIRECTOR,
    REQ_WITH_FINANCE,
    REQ_WITH_MANAGER,
    Account,
    BankAccount,
    Company,
    JournalEntry,
    Requisition,
    RequisitionEvent,
    RequisitionLine,
    User,
)
from ..money import allocate
from ..security import P_ADMIN, can
from .posting import EntryDraft, PostingError, audit, next_number, post_entry, reverse_entry


class RequisitionError(PostingError):
    """Safe to show the user."""


# --------------------------------------------------------------------------
# Who may do what
# --------------------------------------------------------------------------


def limit(db: Session) -> int:
    company = db.get(Company, 1)
    return int(company.requisition_limit or 0) if company else 0


def needs_director(db: Session, req: Requisition) -> bool:
    cap = limit(db)
    return bool(cap) and req.total > cap


def can_approve_as_manager(db: Session, req: Requisition, user: User | None) -> bool:
    if user is None or req.status != REQ_WITH_MANAGER:
        return False
    if user.id == req.raised_by_id:
        return False          # never your own
    if req.manager_id and user.id == req.manager_id:
        return True
    # An administrator can stand in when the named manager is away or has left.
    return can(user, P_ADMIN)


def can_approve_as_director(db: Session, req: Requisition, user: User | None) -> bool:
    if user is None or req.status != REQ_WITH_DIRECTOR:
        return False
    if user.id == req.raised_by_id:
        return False
    return bool(user.approves_large_requisitions) or can(user, P_ADMIN)


def can_pay(db: Session, req: Requisition, user: User | None) -> bool:
    if user is None or req.status != REQ_WITH_FINANCE:
        return False
    if user.id == req.raised_by_id:
        return False          # nobody pays themselves
    return bool(user.pays_requisitions) or can(user, P_ADMIN)


def can_retire(db: Session, req: Requisition, user: User | None) -> bool:
    if user is None or req.status != REQ_PAID:
        return False
    return user.id == req.raised_by_id or can(user, P_ADMIN)


def waiting_for(db: Session, user: User | None) -> list[Requisition]:
    """Everything sitting on this person's desk right now."""
    if user is None:
        return []
    out = []
    for req in db.scalars(
        select(Requisition)
        .where(Requisition.status.in_(OPEN_REQUISITIONS))
        .order_by(Requisition.date, Requisition.id)
    ):
        if (can_approve_as_manager(db, req, user)
                or can_approve_as_director(db, req, user)
                or can_pay(db, req, user)):
            out.append(req)
    return out


def sent_back_to(db: Session, user: User | None) -> list[Requisition]:
    """Rejected requisitions this person raised and has not dealt with."""
    if user is None:
        return []
    return list(
        db.scalars(
            select(Requisition)
            .where(Requisition.raised_by_id == user.id,
                   Requisition.status == REQ_REJECTED)
            .order_by(Requisition.rejected_at.desc())
        )
    )


def unretired(db: Session, user: User | None = None) -> list[Requisition]:
    """Money that has gone out and not been accounted for.

    Deliberately easy to reach. Because the cost is recorded when the money
    leaves, an unretired requisition is an expense sitting in the accounts
    with no receipt behind it — which is exactly what an auditor asks about.
    """
    stmt = select(Requisition).where(Requisition.status == REQ_PAID)
    if user is not None:
        stmt = stmt.where(Requisition.raised_by_id == user.id)
    return list(db.scalars(stmt.order_by(Requisition.paid_on)))


# --------------------------------------------------------------------------
# Building one
# --------------------------------------------------------------------------


def recalc(db: Session, req: Requisition) -> Requisition:
    lines = list(
        db.scalars(
            select(RequisitionLine)
            .where(RequisitionLine.requisition_id == req.id)
            .order_by(RequisitionLine.line_no)
        )
    )
    for line in lines:
        line.amount = round(line.qty * line.unit_price / 1000)
    req.total = sum(line.amount for line in lines)
    db.flush()
    db.refresh(req)
    return req


def log(db: Session, req: Requisition, action: str, user: User | None,
        note: str = "", amount: int = 0) -> RequisitionEvent:
    event = RequisitionEvent(
        requisition_id=req.id,
        action=action,
        by_id=user.id if user else None,
        by_name=user.display_name if user else "",
        note=note,
        amount=amount,
    )
    db.add(event)
    db.flush()
    return event


def create(db: Session, user: User, on: date | None = None) -> Requisition:
    req = Requisition(
        number=next_number(db, "REQUISITION"),
        date=on or date.today(),
        raised_by_id=user.id,
        manager_id=user.manager_id,
        department=user.department or "",
        status=REQ_DRAFT,
    )
    db.add(req)
    db.flush()
    return req


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def submit(db: Session, req: Requisition, user: User) -> Requisition:
    """Send it to the manager."""
    if req.status not in (REQ_DRAFT, REQ_REJECTED):
        raise RequisitionError(f"{req.number} has already been sent for approval.")
    if user.id != req.raised_by_id and not can(user, P_ADMIN):
        raise RequisitionError("Only the person who raised a requisition can send it.")

    recalc(db, req)
    if req.total <= 0:
        raise RequisitionError(
            "Add at least one line with an amount before sending this for approval."
        )
    if not any(l.account_id for l in req.lines):
        raise RequisitionError(
            "Say what each line is for — finance needs an account to charge it to."
        )

    raiser = db.get(User, req.raised_by_id)
    if not req.manager_id:
        # Refuse rather than quietly routing it to nobody: a requisition with
        # no approver would sit unseen forever.
        raise RequisitionError(
            f"{raiser.display_name} has no manager set, so there is nobody to approve "
            "this. An administrator can set one under Settings › Users."
        )
    if req.manager_id == req.raised_by_id:
        raise RequisitionError(
            "You cannot be your own manager. Ask an administrator to set your "
            "manager under Settings › Users."
        )

    was_rejected = req.status == REQ_REJECTED
    req.status = REQ_WITH_MANAGER
    req.submitted_at = clock.now()
    # Clear the old rejection so the record shows where it is now, while the
    # events below keep the history of it having been sent back.
    req.rejected_by_id = None
    req.rejected_at = None
    req.rejected_stage = ""
    req.rejection_reason = ""
    db.flush()
    log(db, req, "RESUBMIT" if was_rejected else "SUBMIT", user,
        note=f"Sent to {req.manager.display_name}" if req.manager else "",
        amount=req.total)
    audit(db, user, "SUBMIT", "Requisition", req.id,
          detail=f"{req.number} — {req.total}")
    return req


def approve(db: Session, req: Requisition, user: User, note: str = "") -> Requisition:
    """Approve at whatever stage it is at, and move it on."""
    if can_approve_as_manager(db, req, user):
        req.manager_approved_by_id = user.id
        req.manager_approved_at = clock.now()
        req.manager_note = note
        if needs_director(db, req):
            req.status = REQ_WITH_DIRECTOR
            where_next = "a director, because it is over the approval limit"
        else:
            req.status = REQ_WITH_FINANCE
            where_next = "finance"
        db.flush()
        log(db, req, "MANAGER_OK", user, note=note or f"Approved — now with {where_next}")
        audit(db, user, "APPROVE", "Requisition", req.id, detail=req.number)
        return req

    if can_approve_as_director(db, req, user):
        req.director_approved_by_id = user.id
        req.director_approved_at = clock.now()
        req.director_note = note
        req.status = REQ_WITH_FINANCE
        db.flush()
        log(db, req, "DIRECTOR_OK", user, note=note or "Approved — now with finance")
        audit(db, user, "APPROVE", "Requisition", req.id, detail=req.number)
        return req

    if user.id == req.raised_by_id:
        raise RequisitionError("You cannot approve your own requisition.")
    raise RequisitionError(
        f"{req.number} is not waiting for you. It is with "
        f"{req.awaiting or 'somebody else'}."
    )


def reject(db: Session, req: Requisition, user: User, reason: str) -> Requisition:
    """Send it back, with the reason attached.

    The reason is not optional. A requisition that comes back saying only
    "rejected" tells the person nothing and they will simply raise it again.
    """
    reason = (reason or "").strip()
    if not reason:
        raise RequisitionError(
            "Say why you are sending it back. The person who raised it needs to "
            "know what to change."
        )
    if len(reason) < 5:
        raise RequisitionError(
            "Give a little more detail than that — the person who raised it has "
            "to act on what you write."
        )

    if can_approve_as_manager(db, req, user):
        stage = "MANAGER"
    elif can_approve_as_director(db, req, user):
        stage = "DIRECTOR"
    elif can_pay(db, req, user):
        stage = "FINANCE"
    elif user.id == req.raised_by_id:
        raise RequisitionError("You cannot reject your own requisition — withdraw it instead.")
    else:
        raise RequisitionError(
            f"{req.number} is not waiting for you. It is with "
            f"{req.awaiting or 'somebody else'}."
        )

    req.status = REQ_REJECTED
    req.rejected_by_id = user.id
    req.rejected_at = clock.now()
    req.rejected_stage = stage
    req.rejection_reason = reason
    db.flush()
    log(db, req, "REJECT", user, note=reason)
    audit(db, user, "REJECT", "Requisition", req.id, detail=f"{req.number}: {reason}")
    return req


def withdraw(db: Session, req: Requisition, user: User, reason: str = "") -> Requisition:
    if req.status in (REQ_PAID, REQ_RETIRED):
        raise RequisitionError(
            f"{req.number} has already been paid and cannot be withdrawn."
        )
    if user.id != req.raised_by_id and not can(user, P_ADMIN):
        raise RequisitionError("Only the person who raised it can withdraw it.")
    req.status = REQ_CANCELLED
    db.flush()
    log(db, req, "WITHDRAW", user, note=reason)
    audit(db, user, "CANCEL", "Requisition", req.id, detail=req.number)
    return req


# --------------------------------------------------------------------------
# Paying it
# --------------------------------------------------------------------------


def pay(
    db: Session,
    req: Requisition,
    user: User,
    *,
    bank_account_id: int,
    on: date | None = None,
    amount: int | None = None,
    reference: str = "",
) -> JournalEntry:
    """Send the money, and record the cost.

    The expense is recognised here, at what was actually paid out. Retiring
    the requisition afterwards corrects that figure to what the receipts show.
    """
    if not can_pay(db, req, user):
        if req.status != REQ_WITH_FINANCE:
            raise RequisitionError(
                f"{req.number} is not ready to be paid — it is with "
                f"{req.awaiting or 'somebody else'}."
            )
        if user.id == req.raised_by_id:
            raise RequisitionError("You cannot pay your own requisition.")
        raise RequisitionError(
            "You are not set up to release money. An administrator can tick "
            "'can pay requisitions' on your user under Settings › Users."
        )

    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        raise RequisitionError("Choose the account the money is going out of.")

    amount = req.total if amount is None else amount
    if amount <= 0:
        raise RequisitionError("The amount paid must be more than zero.")
    if amount > req.total:
        from ..money import fmt

        raise RequisitionError(
            f"{req.number} was approved for {fmt(req.total)}. You cannot pay "
            f"{fmt(amount)} against it — send it back for a fresh approval instead."
        )

    raiser = db.get(User, req.raised_by_id)
    on = on or date.today()

    # Spread what is actually paid across the lines, so the expense accounts
    # carry the right share even when finance pays less than was asked for.
    lines = list(req.lines)
    shares = allocate(amount, [l.amount for l in lines]) if lines else []

    draft = EntryDraft(
        date=on,
        memo=f"Requisition {req.number} — {raiser.display_name}: "
             f"{req.purpose[:120] or 'staff requisition'}",
        reference=reference or req.number,
        source="REQUISITION",
        source_id=req.id,
    )
    for line, share in zip(lines, shares):
        if not share:
            continue
        if not line.account_id:
            raise RequisitionError(
                f"Line {line.line_no} has no account to charge it to. Send it back "
                "so the account can be filled in."
            )
        draft.debit(line.account_id, share,
                    line.description[:255] or req.purpose[:255],
                    contact_id=line.vendor_id)
    draft.credit(bank.account_id, amount,
                 f"Requisition {req.number} — {raiser.display_name}")

    entry = post_entry(db, draft, user=user)

    req.status = REQ_PAID
    req.paid_by_id = user.id
    req.paid_at = clock.now()
    req.paid_on = on
    req.paid_amount = amount
    req.bank_account_id = bank.id
    req.payment_reference = reference[:60]
    req.paid_to_bank = raiser.bank_name
    req.paid_to_account_no = raiser.bank_account_no
    req.paid_to_account_name = raiser.bank_account_name or raiser.display_name
    req.payment_entry_id = entry.id
    db.flush()

    log(db, req, "PAID", user,
        note=f"Sent to {req.paid_to_bank} {req.paid_to_account_no}".strip(),
        amount=amount)
    audit(db, user, "PAY", "Requisition", req.id, detail=f"{req.number} — {amount}")
    return entry


# --------------------------------------------------------------------------
# Retiring it
# --------------------------------------------------------------------------


def retire(
    db: Session,
    req: Requisition,
    user: User,
    *,
    spent: dict[int, int],
    on: date | None = None,
    note: str = "",
    settle_bank_account_id: int | None = None,
) -> JournalEntry | None:
    """Account for the money with receipts, and correct the expense.

    ``spent`` is ``{requisition line id: amount actually spent}``. Anything not
    spent comes back into the bank and comes off the expense; anything spent
    over goes on to it. If the figures match what was paid, nothing needs to be
    posted at all.
    """
    if not can_retire(db, req, user):
        if req.status == REQ_RETIRED:
            raise RequisitionError(f"{req.number} has already been retired.")
        if req.status != REQ_PAID:
            raise RequisitionError(f"{req.number} has not been paid yet.")
        raise RequisitionError(
            f"Only {req.raised_by.display_name} can retire {req.number}."
        )

    on = on or date.today()
    lines = list(req.lines)
    total_spent = 0
    for line in lines:
        line.spent = max(0, int(spent.get(line.id, 0)))
        total_spent += line.spent

    if total_spent < 0:
        raise RequisitionError("What was spent cannot be negative.")

    difference = req.paid_amount - total_spent      # positive: money to return
    entry = None

    if difference:
        bank = db.get(BankAccount, settle_bank_account_id) if settle_bank_account_id \
            else db.get(BankAccount, req.bank_account_id)
        if bank is None:
            raise RequisitionError(
                "Say which account the balance is going back into." if difference > 0
                else "Say which account the extra is being paid out of."
            )

        raiser = db.get(User, req.raised_by_id)
        draft = EntryDraft(
            date=on,
            memo=(f"Requisition {req.number} retired — "
                  f"{'balance returned by' if difference > 0 else 'extra reimbursed to'} "
                  f"{raiser.display_name}"),
            reference=req.number,
            source="REQUISITION",
            source_id=req.id,
        )
        # Correct each line's expense by what it was actually short or over.
        for line in lines:
            paid_share = _paid_share(req, line)
            gap = paid_share - line.spent
            if not gap:
                continue
            if not line.account_id:
                continue
            # gap positive: less was spent, so credit the expense back
            draft.signed(line.account_id, -gap,
                         f"Retirement of {req.number} — {line.description[:200]}")
        draft.signed(bank.account_id, difference,
                     f"Requisition {req.number} — "
                     f"{'balance returned' if difference > 0 else 'extra reimbursed'}")
        entry = post_entry(db, draft, user=user)
        req.retirement_entry_id = entry.id

    req.status = REQ_RETIRED
    req.retired_at = clock.now()
    req.retired_on = on
    req.amount_spent = total_spent
    req.retirement_note = note
    db.flush()

    if difference > 0:
        detail = f"{req.number} retired — {difference} returned"
    elif difference < 0:
        detail = f"{req.number} retired — {-difference} reimbursed"
    else:
        detail = f"{req.number} retired in full"
    log(db, req, "RETIRED", user, note=note or detail, amount=total_spent)
    audit(db, user, "RETIRE", "Requisition", req.id, detail=detail)
    return entry


def _paid_share(req: Requisition, line: RequisitionLine) -> int:
    """What of the money paid out was charged to this line."""
    lines = list(req.lines)
    if not lines:
        return 0
    shares = allocate(req.paid_amount, [l.amount for l in lines])
    for other, share in zip(lines, shares):
        if other.id == line.id:
            return share
    return 0


def paid_shares(req: Requisition) -> dict[int, int]:
    """``{line id: amount charged}`` — what the retirement screen compares against."""
    lines = list(req.lines)
    if not lines:
        return {}
    shares = allocate(req.paid_amount, [l.amount for l in lines])
    return {line.id: share for line, share in zip(lines, shares)}


def unretire(db: Session, req: Requisition, user: User) -> None:
    """Undo a retirement that was filed wrongly. Reverses the correcting entry."""
    if req.status != REQ_RETIRED:
        raise RequisitionError(f"{req.number} has not been retired.")
    if not can(user, P_ADMIN):
        raise RequisitionError("Only an administrator can reopen a retirement.")
    if req.retirement_entry_id:
        entry = db.get(JournalEntry, req.retirement_entry_id)
        if entry and not entry.is_void:
            reverse_entry(db, entry, user=user,
                          memo=f"Reversal of retirement of {req.number}")
    req.status = REQ_PAID
    req.retired_at = None
    req.retired_on = None
    req.amount_spent = 0
    req.retirement_entry_id = None
    for line in req.lines:
        line.spent = 0
    db.flush()
    log(db, req, "REOPENED", user, note="Retirement reopened for correction")
    audit(db, user, "REOPEN", "Requisition", req.id, detail=req.number)


# --------------------------------------------------------------------------
# Numbers for the dashboard and the listing
# --------------------------------------------------------------------------


@dataclass
class Summary:
    waiting_for_me: int = 0
    waiting_value: int = 0
    sent_back_to_me: int = 0
    my_unretired: int = 0
    my_unretired_value: int = 0
    all_unretired: int = 0
    all_unretired_value: int = 0


def summary(db: Session, user: User | None) -> Summary:
    s = Summary()
    if user is None:
        return s
    mine = waiting_for(db, user)
    s.waiting_for_me = len(mine)
    s.waiting_value = sum(r.total for r in mine)
    s.sent_back_to_me = len(sent_back_to(db, user))

    ours = unretired(db, user)
    s.my_unretired = len(ours)
    s.my_unretired_value = sum(r.paid_amount for r in ours)

    if can(user, P_ADMIN) or user.pays_requisitions:
        every = unretired(db)
        s.all_unretired = len(every)
        s.all_unretired_value = sum(r.paid_amount for r in every)
    return s


def outstanding_by_person(db: Session) -> list[tuple[User, int, int]]:
    """``(person, how many, how much)`` — who is holding company money."""
    rows: dict[int, list] = {}
    for req in unretired(db):
        entry = rows.setdefault(req.raised_by_id, [req.raised_by, 0, 0])
        entry[1] += 1
        entry[2] += req.paid_amount
    out = [(r[0], r[1], r[2]) for r in rows.values()]
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def visible_to(db: Session, user: User) -> list[Requisition]:
    """What this person is allowed to see.

    Their own, anything they are the manager for, and everything if they
    approve large ones, release money, or administer the system.
    """
    if can(user, P_ADMIN) or user.pays_requisitions or user.approves_large_requisitions:
        stmt = select(Requisition)
    else:
        reports_to_me = select(User.id).where(User.manager_id == user.id)
        stmt = select(Requisition).where(
            or_(Requisition.raised_by_id == user.id,
                Requisition.manager_id == user.id,
                Requisition.raised_by_id.in_(reports_to_me))
        )
    return list(db.scalars(stmt.order_by(Requisition.date.desc(), Requisition.id.desc())))
