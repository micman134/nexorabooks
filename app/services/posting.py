"""The double-entry posting engine.

Every financial event in the application ends up here.  Nothing writes to
``journal_lines`` except :func:`post_entry`, and :func:`post_entry` refuses to
write an unbalanced entry.  That single choke point is what guarantees the
trial balance is always zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Account, AuditLog, JournalEntry, JournalLine, NumberSequence, User


def _assert_licensed() -> None:
    from .. import licensing

    state = licensing.status()
    if state.can_post:
        return
    raise UnlicensedError(
        f"{state.headline}. New entries cannot be posted until a licence is "
        f"entered under Settings › Licence. {state.explanation}"
    )


class PostingError(Exception):
    """Raised when an entry cannot be posted. Always safe to show the user."""


class PeriodLockedError(PostingError):
    pass


class UnlicensedError(PostingError):
    """Raised when the trial or the licence has run out.

    Deliberately a PostingError, so that every screen which already knows how to
    show a posting problem shows this one too, rolls back, and changes nothing.
    Reading, reporting, exporting and backing up are all untouched — this stops
    new entries reaching the ledger and nothing else.
    """


# --------------------------------------------------------------------------
# Document numbering
# --------------------------------------------------------------------------

SEQUENCE_DEFAULTS = {
    "JOURNAL": ("JV-", 1, 5),
    "INVOICE": ("INV-", 1, 5),
    "CREDIT_NOTE": ("CN-", 1, 5),
    "QUOTE": ("QTE-", 1, 5),
    "BILL": ("BILL-", 1, 5),
    "DEBIT_NOTE": ("DN-", 1, 5),
    "PO": ("PO-", 1, 5),
    "RECEIPT": ("RCT-", 1, 5),
    "PAYMENT": ("PMT-", 1, 5),
    "TRANSFER": ("TRF-", 1, 5),
    "CONTACT": ("C", 1001, 4),
    "ITEM": ("ITM-", 1, 4),
    "EMPLOYEE": ("EMP-", 1, 4),
    "PAYROLL": ("PAY-", 1, 4),
    "ASSET": ("FA-", 1, 4),
    "DEPRECIATION": ("DEP-", 1, 4),
    "LANDED": ("LC-", 1, 4),
    "REQUISITION": ("REQ-", 1, 4),
    "PROJECT": ("JOB-", 1, 4),
    "TILL": ("TILL-", 1, 4),
}


def next_number(db: Session, key: str) -> str:
    """Allocate the next document number for ``key`` (atomically within the txn)."""
    seq = db.get(NumberSequence, key)
    if seq is None:
        prefix, start, pad = SEQUENCE_DEFAULTS.get(key, (key[:3] + "-", 1, 5))
        seq = NumberSequence(key=key, prefix=prefix, next_number=start, padding=pad)
        db.add(seq)
        db.flush()
    number = f"{seq.prefix}{str(seq.next_number).zfill(seq.padding)}"
    seq.next_number += 1
    db.flush()
    return number


# --------------------------------------------------------------------------
# System accounts
# --------------------------------------------------------------------------


def sys_account(db: Session, key: str) -> Account:
    acc = db.scalar(select(Account).where(Account.system_key == key))
    if acc is None:
        raise PostingError(
            f"The system account '{key}' is missing from the chart of accounts. "
            "Go to Settings › Chart of Accounts to restore it."
        )
    return acc


def account_by_code(db: Session, code: str) -> Account | None:
    return db.scalar(select(Account).where(Account.code == code))


# --------------------------------------------------------------------------
# Period locking
# --------------------------------------------------------------------------


def assert_period_open(db: Session, on: date) -> None:
    from ..models import Company

    company = db.get(Company, 1)
    if company and company.lock_date and on <= company.lock_date:
        raise PeriodLockedError(
            f"The books are locked up to {company.lock_date:%d %b %Y}. "
            f"An entry cannot be dated {on:%d %b %Y}. "
            "An administrator can change the lock date in Settings."
        )


# --------------------------------------------------------------------------
# Entry construction
# --------------------------------------------------------------------------


@dataclass
class Line:
    """One side of a journal entry. Give it a debit **or** a credit, never both."""

    account: Account | int
    debit: int = 0
    credit: int = 0
    memo: str = ""
    contact_id: int | None = None
    item_id: int | None = None
    project_id: int | None = None
    tax_code_id: int | None = None
    tax_base: int = 0

    @property
    def account_id(self) -> int:
        return self.account if isinstance(self.account, int) else self.account.id


@dataclass
class EntryDraft:
    """Accumulates lines, then posts them as one balanced entry."""

    date: date
    memo: str = ""
    reference: str = ""
    source: str = "MANUAL"
    source_id: int | None = None
    lines: list[Line] = field(default_factory=list)

    def debit(self, account, amount: int, memo: str = "", **kw) -> "EntryDraft":
        if amount:
            self._add(account, amount, 0, memo, **kw)
        return self

    def credit(self, account, amount: int, memo: str = "", **kw) -> "EntryDraft":
        if amount:
            self._add(account, 0, amount, memo, **kw)
        return self

    def signed(self, account, amount: int, memo: str = "", **kw) -> "EntryDraft":
        """Debit a positive amount, credit a negative one. Handy for credit notes."""
        if amount > 0:
            return self.debit(account, amount, memo, **kw)
        if amount < 0:
            return self.credit(account, -amount, memo, **kw)
        return self

    def _add(self, account, dr: int, cr: int, memo: str, **kw) -> None:
        # A negative debit is a credit; normalise so no line is ever negative.
        if dr < 0:
            dr, cr = 0, -dr
        if cr < 0:
            cr, dr = 0, -cr
        self.lines.append(Line(account=account, debit=dr, credit=cr, memo=memo, **kw))

    @property
    def total_debit(self) -> int:
        return sum(l.debit for l in self.lines)

    @property
    def total_credit(self) -> int:
        return sum(l.credit for l in self.lines)

    @property
    def is_balanced(self) -> bool:
        return self.total_debit == self.total_credit


def post_entry(
    db: Session,
    draft: EntryDraft,
    user: User | None = None,
    allow_locked: bool = False,
) -> JournalEntry:
    """Validate and write a journal entry. This is the only writer of the ledger.

    Being the only writer makes this the one honest place to ask whether this
    installation is still allowed to write. Every other screen stays open.
    """
    _assert_licensed()

    if not draft.lines:
        raise PostingError("An entry needs at least one line.")

    if not allow_locked:
        assert_period_open(db, draft.date)

    # Drop zero lines (they carry no information and clutter the ledger)
    lines = [l for l in draft.lines if l.debit or l.credit]
    if not lines:
        raise PostingError("Every line on this entry is zero — nothing to post.")

    for l in lines:
        if l.debit and l.credit:
            raise PostingError("A single line cannot carry both a debit and a credit.")

    td = sum(l.debit for l in lines)
    tc = sum(l.credit for l in lines)
    if td != tc:
        from ..money import fmt

        raise PostingError(
            "This entry does not balance. "
            f"Debits {fmt(td)} vs credits {fmt(tc)} — a difference of {fmt(abs(td - tc))}."
        )
    if td == 0:
        raise PostingError("An entry cannot be for zero.")

    # Guard against posting to a header/parent or inactive account
    for l in lines:
        acc = l.account if isinstance(l.account, Account) else db.get(Account, l.account)
        if acc is None:
            raise PostingError("An entry line refers to an account that no longer exists.")
        if not acc.is_active:
            raise PostingError(f"Account {acc.code} — {acc.name} is archived and cannot be posted to.")

    entry = JournalEntry(
        number=next_number(db, "JOURNAL"),
        date=draft.date,
        memo=draft.memo,
        reference=draft.reference,
        source=draft.source,
        source_id=draft.source_id,
        is_posted=True,
        total_debit=td,
        total_credit=tc,
        created_by_id=user.id if user else None,
    )
    db.add(entry)
    db.flush()

    for i, l in enumerate(lines, start=1):
        db.add(
            JournalLine(
                entry_id=entry.id,
                line_no=i,
                account_id=l.account_id,
                debit=l.debit,
                credit=l.credit,
                memo=l.memo[:255],
                contact_id=l.contact_id,
                item_id=l.item_id,
                project_id=l.project_id,
                tax_code_id=l.tax_code_id,
                tax_base=l.tax_base,
            )
        )
    db.flush()
    return entry


def reverse_entry(
    db: Session,
    entry: JournalEntry,
    on: date | None = None,
    user: User | None = None,
    memo: str = "",
) -> JournalEntry:
    """Post the mirror image of ``entry``.

    Posted entries are never edited or deleted — they are reversed, so the
    audit trail stays intact. This is what an auditor expects to see.
    """
    if entry.is_void:
        raise PostingError(f"Journal {entry.number} has already been reversed.")

    on = on or entry.date
    assert_period_open(db, on)

    draft = EntryDraft(
        date=on,
        memo=memo or f"Reversal of {entry.number}: {entry.memo}",
        reference=entry.number,
        source="REVERSAL",
        source_id=entry.id,
    )
    for l in entry.lines:
        draft.lines.append(
            Line(
                account=l.account_id,
                debit=l.credit,
                credit=l.debit,
                memo=f"Reversal — {l.memo}",
                contact_id=l.contact_id,
                item_id=l.item_id,
                project_id=l.project_id,
                tax_code_id=l.tax_code_id,
                tax_base=-l.tax_base,
            )
        )
    rev = post_entry(db, draft, user=user)
    rev.reverses_id = entry.id
    entry.is_void = True
    db.flush()
    return rev


# --------------------------------------------------------------------------
# Balances
# --------------------------------------------------------------------------


def account_balance(
    db: Session,
    account_id: int,
    start: date | None = None,
    end: date | None = None,
) -> tuple[int, int]:
    """Return ``(total_debit, total_credit)`` for an account over a date range.

    A reversed entry and its reversal both count — they cancel each other out,
    which is exactly how a reversal is supposed to work.
    """
    from sqlalchemy import func

    q = (
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(JournalLine.account_id == account_id, JournalEntry.is_posted.is_(True))
    )
    if start:
        q = q.where(JournalEntry.date >= start)
    if end:
        q = q.where(JournalEntry.date <= end)
    row = db.execute(q).one()
    return int(row[0]), int(row[1])


def account_net(db: Session, account_id: int, start=None, end=None) -> int:
    """Balance in the account's natural direction (positive = normal balance)."""
    acc = db.get(Account, account_id)
    dr, cr = account_balance(db, account_id, start, end)
    return acc.signed(dr, cr)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def audit(
    db: Session,
    user: User | None,
    action: str,
    entity: str = "",
    entity_id: int | None = None,
    detail: str = "",
    ip: str = "",
) -> None:
    db.add(
        AuditLog(
            at=clock.now(),
            user_id=user.id if user else None,
            username=user.username if user else "system",
            action=action,
            entity=entity,
            entity_id=entity_id,
            detail=detail[:2000],
            ip=ip,
        )
    )
