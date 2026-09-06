"""Licences: a trial, a key tied to one computer, and what happens when neither.

Two principles decide everything in this module.

The first is that a licence must be checkable with no internet connection, on a
machine that may never have one, and must not be forgeable by the person
holding the copy. So a licence is a short signed message: the seller signs it
with a private key that never leaves the seller, and every copy of the
application carries only the public half, which can check a signature but never
produce one.

The second is that **nobody is ever locked out of their own accounts.** When a
trial runs out, or a licence expires, or a licence file is carried to a
different computer, this application keeps opening the books, keeps printing
reports, keeps making backups and keeps letting the owner export everything they
have. What it stops is *writing new entries to the ledger*, and it says plainly
why. Holding somebody's own bookkeeping hostage would be wrong, and a business
that cannot get its records out of a system it has stopped paying for is a
business that should never have started using it.

The machine code shown to the customer is a hash. It carries no name, no serial
number and nothing identifying — it is a fingerprint of the installation, and
the same computer produces the same one every time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config
from .rsa_lite import verify

#: How long a fresh installation runs before it needs a key.
TRIAL_DAYS = 30

#: The seller's public key. Replace both numbers with your own before you sell
#: anything — see make_licence_keys.py, which prints the lines to paste here.
#: The matching private key must never be shipped, and never leave your own
#: computer: anybody holding it can issue licences in your name.
PUBLIC_KEY_N = int(
    "2048107621579541296601390502007257012659451207000720388514410066875675"
    "3461553729506427981517333095465987947936529317601305257794132678322107"
    "9041068601937387303304954512144603295089882013029149310216907609112278"
    "4455259556269829002193195183077981794562508834203137918033173849151289"
    "9341604096325763606563951087051501070004994278327318276551790904986653"
    "1431112481403278224058596339760650483547461207366811162975277365323941"
    "8263682246542074569513762032818658930603834494455498712319916363332927"
    "9143121321014613147811011065264753402981516436973600673771824303370018"
    "883531453071558369370628666933803505337905867433979600567"
)
PUBLIC_KEY_E = 65537

#: This build carries a working keypair, so licensing runs out of the box. The
#: private half is in seller/private-key.json — keep it off every machine you
#: ship to, and run make_licence_keys.py to mint your own before you sell
#: anything, because a private key that has ever been anywhere else is a private
#: key somebody else can issue licences with.
KEY_IS_PLACEHOLDER = False

TRIAL, LICENSED, TRIAL_OVER, EXPIRED, WRONG_MACHINE, UNREADABLE = (
    "TRIAL", "LICENSED", "TRIAL_OVER", "EXPIRED", "WRONG_MACHINE", "UNREADABLE"
)


# --------------------------------------------------------------------------
# Which computer this is
# --------------------------------------------------------------------------


def _machine_id() -> str:
    """A value that stays the same on this computer and differs on another.

    Deliberately not the hostname: people rename computers, and a licence that
    stopped working because somebody tidied up their network names would be a
    support call and a very annoyed customer.
    """
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                if value:
                    return f"winguid:{value}"
        except OSError:
            pass
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
            if value:
                return f"machineid:{value}"
        except OSError:
            continue
    # Last resort: the network card. Stable enough, and better than nothing.
    return f"mac:{uuid.getnode()}:{platform.system()}"


def machine_code() -> str:
    """What the customer reads out or emails when buying a licence.

    Twenty characters of a hash, in groups of four. It identifies the
    installation and nothing else — there is no way back from it to a name, a
    serial number or a person.
    """
    digest = hashlib.sha256(_machine_id().encode()).hexdigest().upper()
    short = digest[:20]
    return "-".join(short[i:i + 4] for i in range(0, 20, 4))


# --------------------------------------------------------------------------
# The licence itself
# --------------------------------------------------------------------------


@dataclass
class Licence:
    name: str = ""
    machine: str = ""
    issued: date | None = None
    expires: date | None = None
    companies: int = 0            # 0 means no limit
    #: How many people may sign in. 0 means no limit — and, importantly, that
    #: is what an older licence issued before seats existed reads as, so
    #: nobody who has already paid finds themselves limited to nothing by an
    #: update. A limit has to be granted deliberately, never inferred.
    users: int = 0
    edition: str = "Standard"
    note: str = ""

    @property
    def unlimited_users(self) -> bool:
        return not self.users

    @property
    def perpetual(self) -> bool:
        return self.expires is None

    def days_left(self, today: date | None = None) -> int | None:
        if self.expires is None:
            return None
        return (self.expires - (today or date.today())).days


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def build(payload: dict, signature: bytes) -> str:
    """The licence as the customer sees it: two blocks and a dot, wrapped."""
    body = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    raw = f"{body}.{_b64(signature)}"
    return "\n".join(raw[i:i + 64] for i in range(0, len(raw), 64))


def parse(text: str) -> tuple[dict, bytes] | None:
    """Split a pasted licence, forgiving whatever whitespace came with it."""
    cleaned = "".join((text or "").split())
    if cleaned.count(".") != 1:
        return None
    body, sig = cleaned.split(".")
    try:
        payload = json.loads(_unb64(body))
        signature = _unb64(sig)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload, signature


def _signed_message(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def read(text: str) -> Licence | None:
    """Check the signature and return what the licence says, or None.

    None means it is not a licence this seller issued — a typo, a truncated
    paste, or something somebody made up. It says nothing about which computer
    it is for; that is checked separately, so the customer can be told the
    difference between "this is not a licence" and "this is a licence for your
    other computer".
    """
    parsed = parse(text)
    if parsed is None:
        return None
    payload, signature = parsed
    if not verify(_signed_message(payload), signature, PUBLIC_KEY_N, PUBLIC_KEY_E):
        return None

    def as_date(value):
        try:
            return date.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    return Licence(
        name=str(payload.get("name", "")),
        machine=str(payload.get("machine", "")),
        issued=as_date(payload.get("issued")),
        expires=as_date(payload.get("expires")),
        companies=int(payload.get("companies") or 0),
        users=int(payload.get("users") or 0),
        edition=str(payload.get("edition") or "Standard"),
        note=str(payload.get("note") or ""),
    )


# --------------------------------------------------------------------------
# Where it is kept, and when the trial started
# --------------------------------------------------------------------------


def licence_file() -> Path:
    return config.data_dir() / "licence.key"


def installed_text() -> str:
    try:
        return licence_file().read_text(encoding="utf-8")
    except OSError:
        return ""


def install(text: str) -> Licence | None:
    """Save a licence, but only one that verifies and is for this computer."""
    licence = read(text)
    if licence is None or licence.machine != machine_code():
        return None
    licence_file().write_text(text.strip(), encoding="utf-8")
    forget_cached()
    return licence


def remove() -> None:
    try:
        licence_file().unlink()
    except OSError:
        pass
    forget_cached()


def _trial_file() -> Path:
    return config.data_dir() / "started.txt"


def trial_started() -> date:
    """The day this installation first ran.

    Written once, on the first start. Somebody who deletes it gets another
    thirty days, and that is a deliberate trade: the alternative is hiding
    marks around a customer's computer, which is what malware does. An honest
    customer is served properly and a determined one was never going to pay.
    """
    path = _trial_file()
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        today = date.today()
        try:
            path.write_text(today.isoformat(), encoding="utf-8")
        except OSError:
            pass
        return today


def trial_ends() -> date:
    return trial_started() + timedelta(days=TRIAL_DAYS)


# --------------------------------------------------------------------------
# What state this installation is in
# --------------------------------------------------------------------------


@dataclass
class Status:
    kind: str
    licence: Licence | None = None
    days_left: int = 0

    @property
    def can_post(self) -> bool:
        """Whether new entries may be written to the ledger.

        Everything else — opening the books, every report, every export, every
        backup — works in all states. This is the only thing a lapsed licence
        stops.
        """
        return self.kind in (TRIAL, LICENSED)

    @property
    def is_licensed(self) -> bool:
        return self.kind == LICENSED

    @property
    def headline(self) -> str:
        return {
            TRIAL: f"Trial — {self.days_left} "
                   f"{'day' if self.days_left == 1 else 'days'} left",
            LICENSED: f"Licensed to {self.licence.name}" if self.licence else "Licensed",
            TRIAL_OVER: "Your trial has finished",
            EXPIRED: "Your licence has expired",
            WRONG_MACHINE: "This licence is for a different computer",
            UNREADABLE: "This licence could not be read",
        }.get(self.kind, self.kind)

    @property
    def explanation(self) -> str:
        if self.can_post:
            return ""
        return (
            "Your books are still here and nothing has been touched. You can open "
            "every screen, print every report, export your data and take a backup. "
            "What is paused is writing new entries — invoices, bills, payments, "
            "payroll and journals — until a licence is entered."
        )


#: Every page render asks what state we are in. The answer changes at most once
#: a day, so it is worked out once and kept, and thrown away whenever a licence
#: is entered or removed.
_cached: tuple[date, Status] | None = None


def forget_cached() -> None:
    global _cached
    _cached = None


def status(today: date | None = None) -> Status:
    global _cached
    if today is None and _cached is not None and _cached[0] == date.today():
        return _cached[1]
    fresh = _status(today)
    if today is None:
        _cached = (date.today(), fresh)
    return fresh


def _status(today: date | None = None) -> Status:
    today = today or date.today()
    text = installed_text()
    if text:
        licence = read(text)
        if licence is None:
            return Status(UNREADABLE)
        if licence.machine != machine_code():
            return Status(WRONG_MACHINE, licence)
        if licence.expires and licence.expires < today:
            return Status(EXPIRED, licence)
        return Status(LICENSED, licence,
                      days_left=licence.days_left(today) or 0)

    left = (trial_ends() - today).days
    if left < 0:
        return Status(TRIAL_OVER, days_left=0)
    return Status(TRIAL, days_left=left)


def can_post(today: date | None = None) -> bool:
    return status(today).can_post
