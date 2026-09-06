"""How many people may sign in, and how many already can.

A licence is bought for a number of users. This module is the only place that
decides what that means, because the answer is less obvious than it looks and
getting it wrong in either direction is expensive.

**What counts as a user.** An active account somebody can sign in with, counted
once across the whole installation. Not once per company: a bookkeeper who
keeps two companies' books has one account name in each and is one person, so
she is one seat. Counting her twice would charge a business for staff it does
not have, and no explanation afterwards makes that feel fair.

**What does not count.** An account that has been switched off. Somebody who
has left keeps their name on the audit trail — deleting them would falsify the
history — but they are not using a seat, and a business should not be paying
for their former staff.

**What happens when there are too many.** New accounts are refused, with the
number and a way to buy more. Existing people are never signed out, never
deactivated and never hidden, even when a licence with fewer seats is entered.
Locking a bookkeeper out of the ledger she is halfway through is not a
collection strategy; it is a way of turning a renewal conversation into an
emergency. The screen says plainly that there are more people than seats and
what to do about it, and that is where the pressure belongs.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from .. import companies as registry
from .. import db as dbmod
from .. import licensing
from ..models import User


@dataclass(frozen=True)
class Usage:
    """Seats in use, seats paid for, and what follows from the two."""

    used: int
    allowed: int              # 0 means no limit
    names: tuple[str, ...] = ()

    @property
    def unlimited(self) -> bool:
        return not self.allowed

    @property
    def left(self) -> int:
        return 0 if self.unlimited else max(0, self.allowed - self.used)

    @property
    def over(self) -> int:
        """How many more people there are than seats. Zero when within."""
        return 0 if self.unlimited else max(0, self.used - self.allowed)

    @property
    def room(self) -> bool:
        return self.unlimited or self.used < self.allowed

    @property
    def summary(self) -> str:
        if self.unlimited:
            return f"{self.used} {'person' if self.used == 1 else 'people'} — no limit"
        return f"{self.used} of {self.allowed} users"


def _active_usernames() -> set[str]:
    """Every name that can sign in anywhere in this installation.

    Reads each company file in turn. A company whose file cannot be opened —
    mid-restore, on a disconnected drive — is skipped rather than allowed to
    break the screen: an unreadable company is not evidence of extra staff.
    """
    found: set[str] = set()
    for ref in registry.all_companies(include_archived=True):
        try:
            dbmod.init_db(ref.slug)
            with dbmod.session_scope_for(ref.slug) as db:
                for name in db.scalars(
                    select(User.username).where(User.is_active.is_(True))
                ):
                    if name:
                        found.add(str(name).strip().lower())
        except Exception:                       # noqa: BLE001 — see docstring
            continue
    return found


def allowed(state: licensing.Status | None = None) -> int:
    """Seats this installation has paid for. 0 means no limit.

    A trial has no limit. That is deliberate: somebody trying the software
    should be setting it up the way they actually work, with their real staff,
    because a trial run by one person alone answers a question nobody asked.
    """
    state = state or licensing.status()
    if state.licence is None:
        return 0
    return int(getattr(state.licence, "users", 0) or 0)


def usage(state: licensing.Status | None = None) -> Usage:
    names = _active_usernames()
    return Usage(used=len(names), allowed=allowed(state), names=tuple(sorted(names)))


def room_for(username: str, state: licensing.Status | None = None) -> bool:
    """Whether this particular name may be switched on.

    Takes the name rather than a count, because somebody who already has an
    account in another company is already occupying their seat and must not be
    refused a second one.
    """
    wanted = str(username or "").strip().lower()
    if not wanted:
        return False
    limit = allowed(state)
    if not limit:
        return True
    names = _active_usernames()
    if wanted in names:
        return True
    return len(names) < limit


def refusal(username: str, state: licensing.Status | None = None) -> str:
    """What to tell somebody who has run out of seats. '' when they have not."""
    if room_for(username, state):
        return ""
    now = usage(state)
    return (
        f"Your licence covers {now.allowed} "
        f"{'user' if now.allowed == 1 else 'users'} and all "
        f"{now.allowed} are in use, so '{username}' was not added. "
        "You can add more users to your licence from Settings › Licence, or "
        "switch off somebody who has left — their name stays on the audit "
        "trail either way."
    )
