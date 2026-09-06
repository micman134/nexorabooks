"""Two-factor sign-in: the rules around the six digits.

``app/totp.py`` does the arithmetic. This module decides what the application
does with it — when a code is asked for, what happens when it is wrong, and
what happens when somebody has lost their phone.

Three decisions here are worth explaining, because each of them is a choice
between security and somebody being locked out of their own accounts on a
Friday afternoon:

  * **Attempts are limited.** Six digits is a million combinations, but a
    script can try a million things quickly. Five wrong codes and that person's
    second step is shut for fifteen minutes. The lock is per user, held in
    memory, and clears when the application restarts — which is the right
    trade for software running on one office computer, where the alternative
    is a table that has to be migrated and pruned.

  * **A half-finished setup never blocks anybody.** A secret can exist without
    ``totp_enabled`` being set. Only the flag decides.

  * **Recovery codes are single use and are shown once.** Losing a phone is
    common; being permanently locked out of your own ledger is not acceptable.
    An administrator can also clear another person's second factor, which is
    the way back when the recovery codes are gone too.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .. import clock
from .. import totp
from ..models import Company, User

#: Wrong codes allowed before the second step closes for a while.
MAX_ATTEMPTS = 5

#: How long it stays closed, in seconds.
LOCKOUT = 15 * 60

#: How long a half-finished setup keeps the same key. Long enough that opening
#: the screen, scanning, being interrupted, and coming back after lunch still
#: works; short enough that a key shown on a screen last week is not still the
#: live one. See ``setup_secret``.
SETUP_WINDOW = timedelta(hours=12)


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


#: user id -> attempts. In memory on purpose; see the module docstring.
_attempts: dict[int, _Attempts] = {}


def reset_attempts(user_id: int | None = None) -> None:
    if user_id is None:
        _attempts.clear()
    else:
        _attempts.pop(user_id, None)


def locked_for(user_id: int, now: float | None = None) -> int:
    """Seconds until this person may try again. Zero when they may try now."""
    record = _attempts.get(user_id)
    if record is None:
        return 0
    remaining = record.locked_until - (now if now is not None else time.time())
    return int(remaining) + 1 if remaining > 0 else 0


def _record_failure(user_id: int, now: float | None = None) -> None:
    now = now if now is not None else time.time()
    record = _attempts.setdefault(user_id, _Attempts())
    record.count += 1
    if record.count >= MAX_ATTEMPTS:
        record.locked_until = now + LOCKOUT
        record.count = 0


# --------------------------------------------------------------------------
# Is a second step needed?
# --------------------------------------------------------------------------


def is_on(user: User | None) -> bool:
    return bool(user is not None and user.totp_enabled and user.totp_secret)


def required_by_company(company: Company | None) -> bool:
    return bool(company is not None and getattr(company, "require_two_factor", False))


def must_set_up(user: User | None, company: Company | None) -> bool:
    """True when the company insists and this person has not done it yet."""
    return bool(user is not None and required_by_company(company) and not is_on(user))


# --------------------------------------------------------------------------
# Checking what was typed
# --------------------------------------------------------------------------


@dataclass
class Result:
    ok: bool = False
    used_recovery: bool = False
    message: str = ""
    codes_left: int = 0
    locked_seconds: int = 0

    @property
    def locked(self) -> bool:
        return self.locked_seconds > 0


def _minutes(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def check(db: Session, user: User, supplied: str, when: float | None = None) -> Result:
    """Accept a six-digit code or a recovery code, and spend whichever it was."""
    locked = locked_for(user.id, when)
    if locked:
        return Result(
            message="Too many wrong codes. Try again in " + _minutes(locked) + ".",
            locked_seconds=locked,
        )

    typed = str(supplied or "").strip()
    if not typed:
        return Result(message="Enter the six-digit code from your authenticator app.")

    # A recovery code first, because it is unmistakably not six digits.
    normalised = totp.normalise(typed)
    if len(normalised) == 10 and not normalised.isdigit():
        stored = user.recovery_codes
        matched = totp.check_recovery(typed, stored)
        if matched is None:
            _record_failure(user.id, when)
            return Result(message="That recovery code is not one of yours.")
        remaining = [code for code in stored if code != matched]
        user.recovery_codes = remaining
        reset_attempts(user.id)
        return Result(
            ok=True,
            used_recovery=True,
            codes_left=len(remaining),
            message=(
                f"Signed in with a recovery code. {len(remaining)} left."
                if remaining
                else "That was your last recovery code. Make new ones now."
            ),
        )

    spent = user.totp_last_counter or None
    stored_offset = int(getattr(user, "totp_offset", 0) or 0)

    # True time first, so a clock that has since been put right heals itself
    # and the recorded offset is dropped rather than kept for ever.
    counter = totp.verify(user.totp_secret, typed, when=when, used_counter=spent)
    if counter is not None:
        stored_offset = 0
    elif stored_offset:
        counter = totp.verify(
            user.totp_secret, typed, when=when, used_counter=spent, offset=stored_offset
        )

    if counter is None:
        _record_failure(user.id, when)
        left = MAX_ATTEMPTS - _attempts.get(user.id, _Attempts()).count
        locked = locked_for(user.id, when)
        if locked:
            return Result(
                message="Too many wrong codes. Try again in " + _minutes(locked) + ".",
                locked_seconds=locked,
            )
        reason = why_it_failed(user, typed, when=when)
        return Result(
            message=(reason or "That code is not right. It changes every thirty "
                               "seconds — wait for the next one and type that.")
            + (f" {left} attempt{'s' if left != 1 else ''} left." if left <= 2 else "")
        )

    user.totp_last_counter = counter
    user.totp_offset = stored_offset
    reset_attempts(user.id)
    return Result(ok=True)


# --------------------------------------------------------------------------
# Turning it on and off
# --------------------------------------------------------------------------


def begin_setup(user: User) -> str:
    """A fresh secret, stored but not yet in force. Returns it."""
    user.totp_secret = totp.new_secret()
    user.totp_enabled = False
    user.totp_confirmed_at = None
    user.totp_last_counter = 0
    user.totp_offset = 0
    user.totp_started_at = clock.now()
    return user.totp_secret


def setup_secret(user: User, restart: bool = False, now: datetime | None = None) -> str:
    """The key to show on the setup screen — the same one as last time if there is one.

    This function exists because of a bug that was worth writing down. The
    setup screen used to mint a new secret on every single page load, on the
    reasoning that an abandoned key should not stay live for ever. What that
    actually did: somebody scans the QR code, then reloads the page or comes
    back to it from the menu, and the software quietly throws away the key
    their phone is now holding. Every code the phone offers from then on is
    refused, no matter how many times they try, and nothing on the screen says
    why. The security worry was real but small; the failure was total.

    So the pending key is kept while setup is in progress, for
    ``SETUP_WINDOW``, and there is a button to deliberately start again. An
    enabled account is never touched — turning it off is a separate, deliberate
    act that asks for the password.
    """
    now = now or clock.now()
    started = user.totp_started_at
    fresh = (
        restart
        or not user.totp_secret
        or user.totp_enabled
        or not totp.looks_like_a_secret(user.totp_secret)
        or started is None
        or now - started > SETUP_WINDOW
    )
    if fresh:
        return begin_setup(user)
    return user.totp_secret


def confirm_setup(user: User, supplied: str, when: float | None = None) -> tuple[bool, list[str]]:
    """Prove the phone works before switching it on. Returns recovery codes.

    Confirming with a real code is the whole point: switching two-factor on
    without checking the phone actually produces the right digits is how people
    lock themselves out on the same day they set it up.

    A code that is right for the secret but wrong for this computer's clock is
    accepted here, and the difference is measured and kept — see
    ``totp.verify``. The phone is not wrong in that situation and neither is
    the person; the computer is, and refusing them entry teaches them nothing.
    ``user.totp_offset`` afterwards is how the caller knows to say so.
    """
    if not user.totp_secret:
        return False, []
    offset = 0
    counter = totp.verify(user.totp_secret, supplied, when=when)
    if counter is None:
        found = totp.find_offset(user.totp_secret, supplied, when=when)
        if found is None:
            return False, []
        offset = found
        counter = totp.verify(user.totp_secret, supplied, when=when, offset=offset)
        if counter is None:                                     # pragma: no cover
            return False, []
    codes = totp.new_recovery_codes()
    user.totp_enabled = True
    user.totp_confirmed_at = clock.now()
    user.totp_last_counter = counter
    user.totp_offset = offset
    user.recovery_codes = [totp.hash_recovery(code) for code in codes]
    reset_attempts(user.id)
    return True, codes


def clock_note(user: User) -> str:
    """What to tell somebody whose computer's clock is out. '' when it is fine."""
    offset = int(getattr(user, "totp_offset", 0) or 0)
    if not offset:
        return ""
    return (
        "One thing to fix: this computer's clock is about "
        + totp.minutes_out(offset)
        + ". Your codes will still work, but please set the time correctly — on "
        "Windows, Settings › Time & language › Date & time, and turn on 'Set time "
        "automatically'. Dates and times on your invoices and audit trail come "
        "from this same clock."
    )


def why_it_failed(user: User, supplied: str, when: float | None = None) -> str:
    """A sentence naming the actual reason a code was refused, where there is one.

    Empty when there is nothing useful to add. Never says anything that depends
    on knowing the code was nearly right — either the code belongs to the
    secret or it does not, and if it does the only remaining explanation is the
    clock.
    """
    typed = "".join(str(supplied or "").split()).replace("-", "")
    if typed.isdigit() and len(typed) != totp.DIGITS and typed:
        return f"That was {len(typed)} digits — the code is {totp.DIGITS}."
    if not user.totp_secret:
        return ""
    offset = totp.find_offset(user.totp_secret, supplied, when=when)
    if offset is None:
        return ""
    return (
        "The code itself is right — this computer's clock is about "
        + totp.minutes_out(offset)
        + ", which is why it was refused. Set this computer's time correctly and "
        "it will work: on Windows, Settings › Time & language › Date & time, then "
        "turn on 'Set time automatically'."
    )


def turn_off(user: User) -> None:
    user.totp_secret = ""
    user.totp_enabled = False
    user.totp_confirmed_at = None
    user.totp_last_counter = 0
    user.totp_recovery = ""
    user.totp_started_at = None
    user.totp_offset = 0
    reset_attempts(user.id)


def new_recovery_codes(user: User) -> list[str]:
    codes = totp.new_recovery_codes()
    user.recovery_codes = [totp.hash_recovery(code) for code in codes]
    return codes


def qr_svg(user: User, company: Company | None, secret: str | None = None) -> str:
    from .. import qrcode

    issuer = (company.name if company and company.name else "Nexora Books")
    uri = totp.provisioning_uri(secret or user.totp_secret, user.username, issuer)
    return qrcode.svg(uri, module=5, title="Scan this with your authenticator app")
