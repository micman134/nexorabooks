"""Working out what each line of a bank statement actually is.

This is the part of the bank import that saves the afternoon, and it is worth
being clear about what it does and does not do.

It **suggests**. Every line comes back with a proposal, the evidence behind it
in plain words, and a score. Nothing is posted, allocated or reconciled until a
person confirms it. That is not timidity: a wrongly matched receipt puts a
customer's account out and a wrongly categorised payment puts the accounts out,
and both are far more expensive to unpick than to check.

The matching itself is ordinary detective work over the ledger, done in a fixed
order, because the first question is always the same one:

  1. **Is this already in the books?** Somebody may have entered the receipt on
     the day it arrived. Then the statement line is not a new transaction at
     all — it is confirmation, and confirming it ticks the entry off for bank
     reconciliation. Matching a statement line against an entry that is already
     there is the single most important check here, because getting it wrong
     records the same money twice.

  2. **Does it settle an invoice or a bill?** Exact amounts first, then
     invoice numbers quoted in the description, then the customer's name, then
     combinations of invoices adding to the amount, then a part payment.

  3. **Have we seen this payee before?** If the last three times a line said
     "MTN NIGERIA" it was posted to telephone costs, say so. That rule was
     written down when a person chose it, not guessed.

  4. **Otherwise, say so plainly** and let the person decide.

Scores run 0 to 100 and are a **rules score, not a probability**. Nothing here
is statistical, and calling it a confidence would imply a mathematical basis it
does not have. What matters to the person is the list of reasons, which is why
every suggestion carries one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import timedelta
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ACTION_CLEAR,
    ACTION_PAYMENT,
    ACTION_POST,
    ACTION_RECEIPT,
    Account,
    Bill,
    Contact,
    Invoice,
    JournalEntry,
    JournalLine,
    PayeeRule,
)

#: How many days either side of the statement date a ledger entry may sit and
#: still be the same transaction. Cheques clear late; transfers post next day.
NEARBY_DAYS = 5

#: The widest window for matching an invoice to a payment. A customer paying an
#: invoice four months late is entirely normal.
INVOICE_WINDOW_DAYS = 400

#: Above this, the software will offer to confirm everything in one go.
STRONG = 80


@dataclass
class Suggestion:
    """What one statement line probably is, and why."""

    action: str = ""
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    contact_id: int | None = None
    account_id: int | None = None
    document_ids: list[int] = field(default_factory=list)
    journal_line_id: int | None = None
    label: str = ""

    @property
    def strong(self) -> bool:
        return self.score >= STRONG

    @property
    def why(self) -> str:
        return " ".join(self.reasons)


# --------------------------------------------------------------------------
# Reading what the bank wrote
# --------------------------------------------------------------------------

#: Words banks add to every line that say nothing about who was paid.
NOISE = {
    "trf", "transfer", "trsf", "nip", "neft", "rtgs", "ach", "pos", "atm",
    "web", "mobile", "app", "purchase", "payment", "pmt", "from", "to", "ref",
    "value", "charge", "charges", "fee", "fees", "vat", "commission", "comm",
    "card", "debit", "credit", "txn", "transaction", "bank", "plc", "ltd",
    "limited", "nigeria", "ng", "inward", "outward", "instant", "online",
    "the", "and", "for", "via", "at", "on", "of", "no", "int", "intl",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z&'\-]{1,}")
_DOCNO = re.compile(r"\b([A-Z]{2,6}[-/]?\d{3,10})\b", re.I)


def words(text: str) -> list[str]:
    """The words in a statement description that carry any meaning."""
    out = []
    for found in _WORD.findall(text or ""):
        word = found.lower().strip("-'&")
        if word and word not in NOISE and len(word) > 2:
            out.append(word)
    return out


def normalise_payee(text: str) -> str:
    """A stable key for "this is the same payee as last time"."""
    return " ".join(words(text))[:120]


def document_numbers(text: str) -> list[str]:
    """Anything in the description shaped like a document number."""
    return [match.group(1).upper().replace("/", "-") for match in _DOCNO.finditer(text or "")]


def _name_score(description: str, name: str) -> float:
    """How much of a contact's name appears in the description, 0 to 1.

    Word overlap rather than a string distance, because bank descriptions
    reorder and truncate names: "PAYMENT FROM DANGOTE CEM PLC/INV12" should
    still find "Dangote Cement Plc".
    """
    wanted = set(words(name))
    if not wanted:
        return 0.0
    got = set(words(description))
    if not got:
        return 0.0
    hits = sum(
        1 for want in wanted
        if any(part.startswith(want[:5]) or want.startswith(part[:5]) for part in got)
    )
    return hits / len(wanted)


# --------------------------------------------------------------------------
# 1. Is it already in the books?
# --------------------------------------------------------------------------


def _already_recorded(
    db: Session, bank_account_id: int, when: Date, amount: int, description: str
) -> Suggestion | None:
    """An unreconciled ledger line on this account for the same money.

    This has to run first and has to be right. If somebody entered the receipt
    on the day and the statement line is then imported as a new receipt, the
    customer is credited twice and the bank balance doubles.
    """
    from ..models import BankAccount

    bank = db.get(BankAccount, bank_account_id)
    if bank is None:
        return None

    rows = db.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .where(
            JournalEntry.is_posted.is_(True),
            JournalLine.account_id == bank.account_id,
            JournalLine.cleared.is_(False),
            JournalEntry.date >= when - timedelta(days=NEARBY_DAYS),
            JournalEntry.date <= when + timedelta(days=NEARBY_DAYS),
        )
    ).all()

    best: Suggestion | None = None
    for line, entry in rows:
        movement = line.debit - line.credit
        if movement != amount:
            continue
        gap = abs((entry.date - when).days)
        score = 96 - gap * 3
        reasons = [
            f"Already in your books: {entry.number} on {entry.date:%d %b %Y} "
            f"for exactly this amount."
        ]
        if gap:
            reasons.append(f"Dated {gap} day{'s' if gap != 1 else ''} away from the statement.")
        text = " ".join(filter(None, [entry.memo, entry.reference]))
        if text and _name_score(description, text) > 0.3:
            score += 3
            reasons.append("The wording matches too.")
        reasons.append("Confirming it will tick it off as cleared, not record it again.")
        if best is None or score > best.score:
            best = Suggestion(
                action=ACTION_CLEAR,
                score=min(99, score),
                reasons=reasons,
                journal_line_id=line.id,
                label=f"{entry.number} — {entry.memo or 'already recorded'}",
            )
    return best


# --------------------------------------------------------------------------
# 2. Does it settle an invoice or a bill?
# --------------------------------------------------------------------------


def _open_documents(db: Session, money_in: bool, when: Date) -> list:
    model = Invoice if money_in else Bill
    doc_type = "INVOICE" if money_in else "BILL"
    return list(
        db.scalars(
            select(model)
            .where(
                model.status.in_(("POSTED", "PART_PAID")),
                model.doc_type == doc_type,
                model.date <= when + timedelta(days=NEARBY_DAYS),
                model.date >= when - timedelta(days=INVOICE_WINDOW_DAYS),
            )
            .order_by(model.date)
        )
    )


def _settles_documents(
    db: Session, when: Date, amount: int, description: str, reference: str
) -> Suggestion | None:
    money_in = amount > 0
    value = abs(amount)
    docs = [d for d in _open_documents(db, money_in, when) if d.balance_due > 0]
    if not docs:
        return None

    quoted = set(document_numbers(f"{description} {reference}"))
    action = ACTION_RECEIPT if money_in else ACTION_PAYMENT

    def by_number(doc) -> bool:
        number = (doc.number or "").upper().replace("/", "-")
        return bool(quoted) and any(
            number == q or number.endswith(q) or q.endswith(number) for q in quoted
        )

    # -- a single document, exactly ---------------------------------------
    best: Suggestion | None = None
    for doc in docs:
        if doc.balance_due != value:
            continue
        name = doc.contact.name if doc.contact else ""
        score = 70
        reasons = [f"Exactly settles {doc.number} ({name}), outstanding {value / 100:,.2f}."]
        if by_number(doc):
            score += 25
            reasons.append(f"Its number is quoted on the statement line.")
        hit = _name_score(description, name)
        if hit >= 0.5:
            score += 15
            reasons.append(f"The name on the statement is {name}.")
        elif hit > 0:
            score += 5
        gap = abs((doc.date - when).days)
        if gap <= 45:
            score += 3
        if best is None or score > best.score:
            best = Suggestion(
                action=action, score=min(99, score), reasons=reasons,
                contact_id=doc.contact_id, document_ids=[doc.id],
                label=f"{doc.number} — {name}",
            )
    if best and best.strong:
        return best

    # -- several documents adding up to it ---------------------------------
    # Only within one contact, and only over a handful, because "some subset of
    # forty invoices happens to add to this" is arithmetic, not evidence.
    by_contact: dict[int, list] = {}
    for doc in docs:
        by_contact.setdefault(doc.contact_id, []).append(doc)
    for contact_id, group in by_contact.items():
        if len(group) < 2 or len(group) > 12:
            continue
        name = group[0].contact.name if group[0].contact else ""
        hit = _name_score(description, name)
        for size in (2, 3, 4):
            if len(group) < size:
                break
            for combo in combinations(group, size):
                if sum(d.balance_due for d in combo) != value:
                    continue
                score = 55 + (20 if hit >= 0.5 else 0) + (10 if quoted else 0)
                if hit < 0.5 and not quoted:
                    score = 45      # arithmetic alone is weak evidence
                numbers = ", ".join(d.number for d in combo)
                reasons = [
                    f"Adds up to {size} outstanding documents for {name}: {numbers}."
                ]
                if hit >= 0.5:
                    reasons.append("The name on the statement matches.")
                else:
                    reasons.append(
                        "Only the total matches — check this one before confirming."
                    )
                candidate = Suggestion(
                    action=action, score=min(90, score), reasons=reasons,
                    contact_id=contact_id, document_ids=[d.id for d in combo],
                    label=f"{size} documents — {name}",
                )
                if best is None or candidate.score > best.score:
                    best = candidate
    if best and best.strong:
        return best

    # -- a part payment, where the name or number is unmistakable ----------
    for doc in docs:
        if doc.balance_due <= value:
            continue
        name = doc.contact.name if doc.contact else ""
        hit = _name_score(description, name)
        if not by_number(doc) and hit < 0.6:
            continue
        score = 60 if by_number(doc) else 52
        reasons = [
            f"Part payment of {doc.number} ({name}), which has "
            f"{doc.balance_due / 100:,.2f} outstanding.",
        ]
        reasons.append("Its number is quoted." if by_number(doc)
                       else "The name on the statement matches.")
        if best is None or score > best.score:
            best = Suggestion(
                action=action, score=score, reasons=reasons,
                contact_id=doc.contact_id, document_ids=[doc.id],
                label=f"Part of {doc.number} — {name}",
            )
    return best


# --------------------------------------------------------------------------
# 3. Have we seen this payee before?
# --------------------------------------------------------------------------


def _learned(db: Session, description: str, amount: int) -> Suggestion | None:
    key = normalise_payee(description)
    if not key:
        return None
    direction = "IN" if amount > 0 else "OUT"

    best: Suggestion | None = None
    for rule in db.scalars(select(PayeeRule)):
        if rule.direction not in ("BOTH", direction):
            continue
        if not rule.pattern:
            continue
        overlap = _pattern_overlap(rule.pattern, key)
        if overlap < 0.6:
            continue
        account = rule.account
        score = int(45 + overlap * 25 + min(rule.times_used, 5) * 3)
        reasons = [
            f"Last time a line like this came through you posted it to "
            f"{account.name if account else 'this account'}."
        ]
        if rule.times_used > 1:
            reasons.append(f"You have done that {rule.times_used} times.")
        if best is None or score > best.score:
            best = Suggestion(
                action=ACTION_POST, score=min(88, score), reasons=reasons,
                account_id=rule.account_id, contact_id=rule.contact_id,
                label=account.name if account else "",
            )
    return best


def _pattern_overlap(pattern: str, key: str) -> float:
    wanted, got = set(pattern.split()), set(key.split())
    if not wanted:
        return 0.0
    return len(wanted & got) / len(wanted)


# --------------------------------------------------------------------------
# 4. A contact we know, even without a document
# --------------------------------------------------------------------------


def _known_contact(db: Session, description: str, amount: int) -> Suggestion | None:
    """The name is one of ours, but nothing is outstanding to settle."""
    best_contact, best_hit = None, 0.0
    for contact in db.scalars(select(Contact).where(Contact.is_active.is_(True))):
        hit = _name_score(description, contact.name)
        if hit > best_hit:
            best_contact, best_hit = contact, hit
    if best_contact is None or best_hit < 0.6:
        return None
    return Suggestion(
        action="",
        score=35,
        reasons=[
            f"The name on the statement looks like {best_contact.name}, but nothing "
            "of theirs is outstanding for this amount. Choose what it is."
        ],
        contact_id=best_contact.id,
        label=best_contact.name,
    )


# --------------------------------------------------------------------------
# Putting it together
# --------------------------------------------------------------------------


def suggest(
    db: Session,
    bank_account_id: int,
    when: Date,
    amount: int,
    description: str,
    reference: str = "",
) -> Suggestion:
    """The best explanation for one statement line."""
    found = _already_recorded(db, bank_account_id, when, amount, description)
    if found is not None and found.strong:
        return found

    candidates = [found]
    candidates.append(_settles_documents(db, when, amount, description, reference))
    candidates.append(_learned(db, description, amount))
    real = [c for c in candidates if c is not None]
    if real:
        best = max(real, key=lambda c: c.score)
        if best.score >= 40:
            return best

    known = _known_contact(db, description, amount)
    if known is not None:
        return known
    return Suggestion(
        score=0,
        reasons=["Nothing in your books matches this. Tell it what this was."],
    )


def suggest_all(db: Session, bank_account_id: int, lines) -> list[Suggestion]:
    """Every line in one pass.

    Documents already claimed by a stronger line are not offered again, so one
    invoice cannot be settled twice by two different statement lines — which is
    exactly what happens when a customer's payment appears on the statement
    alongside a refund of the same amount.
    """
    scored = []
    for index, line in enumerate(lines):
        scored.append((index, suggest(db, bank_account_id, line.date, line.amount,
                                      line.description, getattr(line, "reference", ""))))

    taken_docs: set[int] = set()
    taken_lines: set[int] = set()
    out: list[Suggestion | None] = [None] * len(scored)
    for index, suggestion in sorted(scored, key=lambda pair: -pair[1].score):
        clash = (
            any(doc in taken_docs for doc in suggestion.document_ids)
            or (suggestion.journal_line_id is not None
                and suggestion.journal_line_id in taken_lines)
        )
        if clash:
            out[index] = Suggestion(
                score=0,
                reasons=["Another line on this statement is the better match for the "
                         "same invoice, so this one needs a decision."],
            )
            continue
        taken_docs.update(suggestion.document_ids)
        if suggestion.journal_line_id is not None:
            taken_lines.add(suggestion.journal_line_id)
        out[index] = suggestion
    return [s or Suggestion() for s in out]


# --------------------------------------------------------------------------
# Learning from what the person actually chose
# --------------------------------------------------------------------------


def remember(db: Session, description: str, amount: int, account_id: int | None,
             contact_id: int | None = None, when: Date | None = None) -> PayeeRule | None:
    """Write down what a person decided, so next month it is suggested.

    Only ever called after somebody confirms a line themselves. Nothing the
    software worked out on its own is learned from — otherwise one wrong guess
    accepted in a hurry becomes the rule for ever.
    """
    if account_id is None:
        return None
    key = normalise_payee(description)
    if not key or len(key.split()) < 1:
        return None
    direction = "IN" if amount > 0 else "OUT"

    for rule in db.scalars(select(PayeeRule).where(PayeeRule.direction == direction)):
        if _pattern_overlap(rule.pattern, key) >= 0.8:
            rule.account_id = account_id
            if contact_id:
                rule.contact_id = contact_id
            rule.times_used += 1
            rule.last_used = when
            return rule

    rule = PayeeRule(
        pattern=key, account_id=account_id, contact_id=contact_id,
        direction=direction, times_used=1, last_used=when,
    )
    db.add(rule)
    return rule
