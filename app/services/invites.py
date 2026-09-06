"""Inviting somebody to the books, without ever emailing them a password.

When an administrator adds a user, that person has to find out how to sign in.
The obvious way is to email them a temporary password — and it is the wrong
way, for a reason worth stating plainly: mail is not private. It sits in
inboxes and outboxes, on the mail provider's servers, in phone backups, and
in whatever the company forwards to whom. A password that has travelled by
email has been written down in a dozen places nobody controls, and it usually
turns out to be the same password the person then keeps for years.

So no password is ever sent. What is sent is a link that does one thing: it
lets the person set a password of their own choosing, once. Specifically —

  * the link carries a long random token, and only its **hash** is stored, so
    a copy of the company file does not give anybody a way in, exactly as with
    passwords themselves;
  * it works **once**. Setting the password destroys it, so a link sitting in
    an old inbox is worth nothing;
  * it **expires** after a week, whether it was used or not;
  * it is refused for anybody whose account has been switched off.

The address in the link is whatever address the administrator was using when
they sent it, which on an office network is the server's own address on that
network. That is right: a person who cannot reach the server cannot use the
software either, so a link that works only inside the office is a link that
works exactly where it is any use.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import User

#: Long enough that guessing is not a strategy.
TOKEN_BYTES = 32

#: How long an unused invitation stays good.
VALID_DAYS = 7


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def create(db: Session, user: User) -> str:
    """Give this person a fresh invitation and return the token, once.

    The plain token is returned here and never stored. This is the only moment
    it exists in a readable form; after this the database holds a hash and
    nothing else, so nobody — including an administrator reading the file —
    can recover the link.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    user.invite_hash = _hash(token)
    user.invite_expires = clock.now() + timedelta(days=VALID_DAYS)
    user.invite_sent_at = clock.now()
    db.flush()
    return token


def find(db: Session, token: str) -> User | None:
    """Whose invitation this is, if it is anybody's and still stands."""
    if not token or len(token) < 20:
        return None
    wanted = _hash(token)
    now = clock.now()
    for user in db.scalars(select(User).where(User.invite_hash != "")):
        if not hmac.compare_digest(user.invite_hash or "", wanted):
            continue
        if not user.is_active:
            return None
        if user.invite_expires and user.invite_expires < now:
            return None
        return user
    return None


def accept(db: Session, user: User, password: str) -> None:
    """Set the password they chose and burn the invitation."""
    from .. import security

    user.password_hash = security.hash_password(password)
    user.must_change_password = False
    revoke(db, user)


def revoke(db: Session, user: User) -> None:
    """Cancel an invitation — used up, withdrawn, or replaced by a new one."""
    user.invite_hash = ""
    user.invite_expires = None
    db.flush()


def outstanding(user: User) -> bool:
    """Whether this person has an invitation they have not used yet."""
    if not user.invite_hash:
        return False
    return not user.invite_expires or user.invite_expires >= clock.now()


def link(base_url: str, token: str) -> str:
    return f"{str(base_url).rstrip('/')}/invite/{token}"


def subject(company) -> str:
    name = company.name if company else ""
    return f"Your sign-in for {name}".strip() or "Your sign-in"


def body(company, user: User, url: str, invited_by: User | None = None) -> str:
    """What the person receives. Short, and honest about the one-week limit."""
    name = company.name if company else "the company"
    who = ""
    if invited_by is not None:
        who = f" by {invited_by.display_name or invited_by.username}"
    return "\n".join([
        f"Dear {user.full_name or user.username},", "",
        f"An account has been set up for you{who} on the accounting system "
        f"at {name}.", "",
        f"Your username is:  {user.username}", "",
        "To choose your password and sign in for the first time, open this "
        "link on a computer in the office:", "",
        f"    {url}", "",
        f"The link works once and stops working after {VALID_DAYS} days. "
        "No password has been sent to you — you choose your own, and nobody "
        "else ever sees it.", "",
        "If you were not expecting this, tell whoever looks after the "
        "accounts and ignore the link.", "",
        "Kind regards,", name,
    ])
