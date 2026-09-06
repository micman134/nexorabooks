"""Time-based one-time passwords — the six digits an authenticator app shows.

RFC 6238, on top of RFC 4226, using RFC 4648 base32 for the shared key. Written
here rather than installed because it is about eighty lines of standard library
and because a login that depends on a package somebody forgot to install is a
login that stops working.

The whole scheme is: a secret shared once, the clock rounded to a thirty-second
step, HMAC-SHA1 of that step number, and six digits pulled out of the result.
The server and the phone never speak to each other again after setup — which is
the reason this works with no internet connection, and the reason it fits a
piece of software that promises not to send anything anywhere.

Two details below are security, not style, and should not be tidied away:

  * codes are compared with ``hmac.compare_digest``, so how long the comparison
    takes tells an attacker nothing about how much of the code was right;
  * a code that has been used is refused for the rest of its window, so
    somebody reading it over a shoulder cannot use it a second time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from urllib.parse import quote

#: Seconds each code is valid for. Thirty is what every authenticator app uses.
STEP = 30

#: How many digits. Six is the universal default; more would confuse people.
DIGITS = 6

#: How many steps either side of now are accepted. One step means a code stays
#: usable for up to a minute and a half, which forgives a phone clock that is
#: half a minute out — common, and otherwise an unexplainable failure.
DRIFT = 1

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


# --------------------------------------------------------------------------
# The shared secret
# --------------------------------------------------------------------------


def new_secret(length: int = 20) -> str:
    """A fresh secret, base32 with no padding — what apps expect to be given.

    Twenty bytes is the RFC 4226 recommendation and matches the SHA-1 block
    size. It comes from ``os.urandom``, not ``random``.
    """
    return base64.b32encode(os.urandom(length)).decode("ascii").rstrip("=")


def _decode(secret: str) -> bytes:
    """Base32 back to bytes, forgiving how a person might have typed it.

    Somebody entering a key by hand will use lower case, put spaces in it, and
    leave the padding off. All three are accepted rather than rejected with a
    message they cannot act on.
    """
    cleaned = "".join(str(secret or "").split()).upper().replace("-", "").rstrip("=")
    if not cleaned or any(ch not in _ALPHABET for ch in cleaned):
        raise ValueError("That is not a valid key.")
    padding = "=" * (-len(cleaned) % 8)
    return base64.b32decode(cleaned + padding)


def looks_like_a_secret(secret: str) -> bool:
    try:
        _decode(secret)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Generating and checking a code
# --------------------------------------------------------------------------


def code_at(secret: str, counter: int, digits: int = DIGITS) -> str:
    """The HOTP value for one counter, zero-padded to ``digits``."""
    mac = hmac.new(_decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    # Dynamic truncation: the low nibble of the last byte says where to read.
    offset = mac[-1] & 0x0F
    chunk = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(chunk % (10 ** digits)).zfill(digits)


def counter_for(when: float | None = None) -> int:
    return int((when if when is not None else time.time()) // STEP)


def now(secret: str, when: float | None = None) -> str:
    """What the phone should be showing at this moment."""
    return code_at(secret, counter_for(when))


def verify(
    secret: str,
    supplied: str,
    when: float | None = None,
    drift: int = DRIFT,
    used_counter: int | None = None,
    offset: int = 0,
) -> int | None:
    """Check a code. Returns the counter it matched, or ``None``.

    The counter comes back rather than a plain True so the caller can store it
    and refuse that same code again — see ``used_counter``, which rejects
    anything at or before a counter already spent.

    ``offset`` shifts the centre of the window by that many thirty-second
    steps. It exists for one situation and no other: this computer's clock is
    known to be wrong by a measured amount, proved by a code off the phone when
    two-factor was set up. The window stays the same narrow three codes wide —
    it is moved, not widened.
    """
    typed = "".join(str(supplied or "").split()).replace("-", "")
    if not typed.isdigit() or len(typed) != DIGITS:
        return None
    centre = counter_for(when) + int(offset or 0)
    for step in range(-abs(drift), abs(drift) + 1):
        counter = centre + step
        if counter < 0:
            continue
        if used_counter is not None and counter <= used_counter:
            continue  # already spent: a replay, not a fresh code
        try:
            expected = code_at(secret, counter)
        except ValueError:
            return None
        if hmac.compare_digest(expected, typed):
            return counter
    return None


# --------------------------------------------------------------------------
# When the code is right but the clock is wrong
# --------------------------------------------------------------------------

#: How far either side of now to look when a code has already been refused and
#: we are trying to work out *why*. Two hours covers the two things that
#: actually happen: a computer that has never been told to set its time
#: automatically, and one sitting in the wrong time zone. It is a diagnosis,
#: not a wider door — nothing signs anybody in on the strength of it.
SEARCH_STEPS = 240


def find_offset(
    secret: str, supplied: str, when: float | None = None, window: int = SEARCH_STEPS
) -> int | None:
    """How many steps this computer's clock is out, judged by one good code.

    Returns the offset in thirty-second steps — positive when the phone is
    ahead of this computer, which is the same as saying this computer is slow —
    or ``None`` when the code is not one of this secret's codes at all.

    This is what turns "that code was not right" into "your computer's clock is
    four minutes behind", which is a sentence somebody can act on. The nearest
    match wins, so a small genuine drift is never reported as a large one.
    """
    typed = "".join(str(supplied or "").split()).replace("-", "")
    if not typed.isdigit() or len(typed) != DIGITS:
        return None
    centre = counter_for(when)
    try:
        for distance in range(0, abs(window) + 1):
            for step in ({-distance, distance} if distance else {0}):
                counter = centre + step
                if counter < 0:
                    continue
                if hmac.compare_digest(code_at(secret, counter), typed):
                    return step
    except ValueError:                      # the secret itself is not base32
        return None
    return None


def minutes_out(offset: int) -> str:
    """An offset in steps, said the way a person would say it."""
    seconds = abs(int(offset)) * STEP
    if seconds < 60:
        amount = f"{seconds} seconds"
    else:
        minutes = round(seconds / 60)
        hours, rest = divmod(minutes, 60)
        if hours and rest:
            amount = f"{hours} hour{'s' if hours != 1 else ''} and {rest} minute{'s' if rest != 1 else ''}"
        elif hours:
            amount = f"{hours} hour{'s' if hours != 1 else ''}"
        else:
            amount = f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"{amount} {'behind' if offset > 0 else 'ahead'}"


# --------------------------------------------------------------------------
# Handing the secret to the phone
# --------------------------------------------------------------------------


#: How much of the name is kept in the QR code. The issuer and account are
#: only what the authenticator app shows under the six digits — the secret is
#: what matters, and it is never shortened. A company called something very
#: long would otherwise push the URI past what a readable QR code holds, and a
#: code nobody can scan is worse than a slightly clipped label.
MAX_ISSUER = 40
MAX_ACCOUNT = 64


def _trim(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    """The ``otpauth://`` string an authenticator app reads from a QR code.

    Guaranteed to fit in a QR code this software can draw: the names are
    shortened if they have to be, and if even that is not enough the combined
    label is dropped in favour of the account on its own. The secret, the
    algorithm and the period are never touched.
    """
    from . import qrcode

    issuer = _trim(issuer, MAX_ISSUER)
    account = _trim(account, MAX_ACCOUNT)
    tail = (
        f"?secret={secret}&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={STEP}"
    )
    for label in (f"{issuer}:{account}", account, ""):
        uri = f"otpauth://totp/{quote(label, safe='')}{tail}"
        if qrcode.fits(uri):
            return uri
    # Nothing left to drop: hand back the plain form and let the caller show
    # the typed key instead. This is unreachable with the caps above.
    return f"otpauth://totp/{quote(account, safe='')}{tail}"  # pragma: no cover


def grouped(secret: str, size: int = 4) -> str:
    """The key in blocks, for somebody typing it in by hand off the screen."""
    return " ".join(secret[i:i + size] for i in range(0, len(secret), size))


# --------------------------------------------------------------------------
# Recovery codes — the way back in when the phone is gone
# --------------------------------------------------------------------------

#: Deliberately not the full alphabet: no O/0, no I/1/l. These get written on
#: paper and read back later, usually in a hurry.
_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

RECOVERY_COUNT = 10


def new_recovery_codes(count: int = RECOVERY_COUNT) -> list[str]:
    def one() -> str:
        body = "".join(secrets.choice(_CODE_CHARS) for _ in range(10))
        return f"{body[:5]}-{body[5:]}"

    return [one() for _ in range(count)]


def normalise(code: str) -> str:
    return "".join(str(code or "").split()).upper().replace("-", "")


def hash_recovery(code: str) -> str:
    """Recovery codes are stored hashed, like passwords.

    Plain SHA-256 rather than PBKDF2 on purpose: these are ten random
    characters from a 31-letter alphabet, so there is no dictionary to attack
    and no benefit in slowing a guess that already has to get one in 10^14.
    """
    return hashlib.sha256(normalise(code).encode("ascii")).hexdigest()


def check_recovery(code: str, stored: list[str]) -> str | None:
    """Return the stored hash that matched, so the caller can spend it."""
    wanted = hash_recovery(code)
    for candidate in stored:
        if hmac.compare_digest(candidate, wanted):
            return candidate
    return None
