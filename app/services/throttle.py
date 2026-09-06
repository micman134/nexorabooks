"""Slowing down somebody guessing passwords.

Two-factor sign-in has had a limit since it was built. The password itself never
did, because the software was only ever meant to be reached from inside one
office, where the person at the keyboard is somebody you employ.

That assumption stops holding the moment a member of staff in another state
needs to sign in. So: a limit on wrong passwords, and a limit on wrong
passwords from one place, whether or not the username exists.

Three decisions worth explaining.

**Both the name and the address are counted.** Counting only the username lets
somebody try one password against every account in turn and never trip
anything. Counting only the address lets an office behind one connection lock
itself out because a colleague fat-fingered their password twice. Counting both,
separately, catches the attack without punishing the office.

**A username that does not exist is counted too.** Otherwise the timing tells an
attacker which names are real, which is the first thing they want to know.

**It waits rather than locking.** After a handful of wrong tries the answer is
"try again in a few minutes", not "this account is closed". A permanent lock is
a way for a stranger to take a bookkeeper's account away from her at will, and
the delay costs an attacker far more than it costs her.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: Wrong passwords for one username before that username waits.
PER_USER = 8

#: Wrong passwords from one address before that address waits — higher, because
#: a whole office can share one, and lower than infinity because an attacker
#: usually has one too.
PER_ADDRESS = 20

#: How long the wait is, and how long a run of failures is remembered. Five
#: minutes turns a million guesses into a lifetime's work while costing a
#: person who mistyped their password one cup of tea.
WAIT = 5 * 60
WINDOW = 15 * 60


@dataclass
class _Run:
    count: int = 0
    first: float = 0.0
    until: float = 0.0


_lock = threading.Lock()
_runs: dict[str, _Run] = {}


def reset(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _runs.clear()
        else:
            _runs.pop(key, None)


def _wait_for(key: str, limit: int, now: float) -> int:
    run = _runs.get(key)
    if run is None:
        return 0
    if run.until > now:
        return int(run.until - now) + 1
    if now - run.first > WINDOW:
        _runs.pop(key, None)
    return 0


def wait_needed(username: str, address: str, now: float | None = None) -> int:
    """Seconds this attempt must wait. Zero when it may go ahead."""
    now = now if now is not None else time.time()
    with _lock:
        return max(_wait_for(f"u:{_tidy(username)}", PER_USER, now),
                   _wait_for(f"a:{_tidy(address)}", PER_ADDRESS, now))


def failed(username: str, address: str, now: float | None = None) -> None:
    """Remember a wrong password, and start a wait when there have been enough."""
    now = now if now is not None else time.time()
    with _lock:
        _count(f"u:{_tidy(username)}", PER_USER, now)
        _count(f"a:{_tidy(address)}", PER_ADDRESS, now)


def succeeded(username: str, address: str) -> None:
    """A right password clears the count for both — they are evidently who they say."""
    with _lock:
        _runs.pop(f"u:{_tidy(username)}", None)
        _runs.pop(f"a:{_tidy(address)}", None)


def _count(key: str, limit: int, now: float) -> None:
    run = _runs.get(key)
    if run is None or now - run.first > WINDOW:
        run = _Run(first=now)
        _runs[key] = run
    run.count += 1
    if run.count >= limit:
        run.until = now + WAIT
        run.count = 0
        run.first = now


def _tidy(value: str) -> str:
    return str(value or "").strip().lower() or "-"


def in_words(seconds: int) -> str:
    if seconds <= 90:
        return f"{max(1, seconds)} seconds"
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
