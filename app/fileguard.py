"""Making a file readable only by the person it belongs to — on any computer.

A handful of files this software writes are secrets in the ordinary sense:
the customer's real email password, the key that signs sessions, the private
half of the certificate. Anybody who can read those files can send mail as the
business, or sign in as anybody.

On Linux and macOS the answer is one line — ``chmod 600`` — and that is what
this software did everywhere. On Windows that line does almost nothing.
``os.chmod`` there can only turn the read-only flag on and off; the file is
left at 0o666 and every other account on the machine can still open it. The
test that was meant to catch this **skipped itself on Windows**, so for a long
time the reassuring green tick meant nothing at all on the one operating
system every customer actually runs.

Windows does have real permissions, they are just not POSIX modes. They are
access-control lists, and ``icacls`` is the tool that edits them. So:

* everywhere else, ``chmod 600`` as before;
* on Windows, strip the inherited permissions off the file and grant full
  control to exactly one account — the one running the program.

Both functions return a plain ``True``/``False`` rather than raising. A file
whose permissions could not be tightened is a problem worth reporting, but it
is never worth losing the customer's settings over, so callers save the file
either way and it is the caller's business what to say about it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Long enough for a tool that only edits a few bytes of metadata; short
#: enough that a wedged ``icacls`` cannot hang the settings screen.
PATIENCE = 15

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW, so nothing flashes on screen


def _on_windows() -> bool:
    """Asked through a function so a test can pretend, without pretending
    hard enough to break pathlib underneath itself."""
    return os.name == "nt"


def _one_real_file(path: Path | str) -> Path | None:
    """The file at this path, or None — and nothing else, ever.

    This guard is the whole reason this function exists separately, and it was
    written after the alternative did real damage. The first version asked
    ``path.exists()``. An empty string became ``Path("")``, which is ``Path(".")``,
    which exists — so a call meant for one small secret file was pointed at the
    **current working directory**, and stripping the inherited permissions off a
    folder takes them off everything inside it. It locked a customer out of the
    folder the software was being built in.

    So: it must be a file, it must already be there, and a blank path is not a
    path. A directory is refused even when it exists, because nothing here has
    any business tightening a folder.
    """
    if isinstance(path, str) and not path.strip():
        return None
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    return candidate


def restrict_to_owner(path: Path | str) -> bool:
    """Take everybody else's access to this file away. True if it worked."""
    path = _one_real_file(path)
    if path is None:
        return False
    if not _on_windows():
        try:
            os.chmod(path, 0o600)
        except OSError:
            return False
        return True
    return _only_me(path)


def only_owner_can_read(path: Path | str) -> bool:
    """Is this file now closed to everybody but its owner?

    Asked separately from setting it, because "we tried" and "it is true" are
    different claims and the tests are entitled to the second one.
    """
    path = _one_real_file(path)
    if path is None:
        return False
    if not _on_windows():
        return not (path.stat().st_mode & 0o077)
    listed = _icacls([str(path)])
    if listed is None:
        return False
    allowed = {name.lower() for name in _my_names()}
    people = _people_in(listed, path)
    return bool(people) and all(person.lower() in allowed for person in people)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def _icacls(arguments: list[str]) -> str | None:
    """Run icacls. None if it is missing, refused, or took too long."""
    try:
        done = subprocess.run(
            ["icacls", *arguments],
            capture_output=True, text=True, timeout=PATIENCE,
            creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout or ""


def _my_sid() -> str:
    """The current account's SID, which is the same word in every language.

    Granting to a name breaks on a Windows installed in another language and on
    a machine joined to a domain. The SID does not.
    """
    try:
        done = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, text=True, timeout=PATIENCE,
                              creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    for field in reversed((done.stdout or "").strip().split(",")):
        field = field.strip().strip('"')
        if field.upper().startswith("S-1-"):
            return field
    return ""


def _my_names() -> set[str]:
    """Everything this account might be called in an icacls listing."""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    names = {n for n in (user, f"{domain}\\{user}" if domain and user else "") if n}
    sid = _my_sid()
    if sid:
        names.add(sid)
    if not names:
        try:
            import getpass

            names.add(getpass.getuser())
        except Exception:                                  # noqa: BLE001
            pass
    return names


def _only_me(path: Path) -> bool:
    # Checked once more here rather than trusted from the caller. This is the
    # line that actually edits permissions, and the cost of it running against
    # a folder is somebody locked out of their own computer's files.
    if not path.is_file():
        return False
    sid = _my_sid()
    who = f"*{sid}" if sid else (os.environ.get("USERNAME") or "")
    if not who:
        return False
    # /inheritance:r drops what the file inherited from its folder — which is
    # where "Users can read this" comes from. /grant:r then replaces, rather
    # than adds to, whatever is left.
    if _icacls([str(path), "/inheritance:r", "/grant:r", f"{who}:(F)", "/q"]) is None:
        return False
    return only_owner_can_read(path)


def _people_in(listing: str, path: Path) -> list[str]:
    """The accounts named in an icacls listing, in the order they appear.

    The first line carries the filename before the account; the rest are
    indented continuations. The trailing "Successfully processed" line and any
    blank lines are not permissions and are dropped.
    """
    people: list[str] = []
    for line in listing.splitlines():
        if ":(" not in line:
            continue
        entry = line.strip()
        if entry.lower().startswith(str(path).lower()):
            entry = entry[len(str(path)):].strip()
        who = entry.split(":(", 1)[0].strip()
        if who:
            people.append(who)
    return people
