"""What you need when a customer says "it stopped working".

Selling software means answering that sentence, usually by phone, usually from
somebody who is busy and cannot tell you what they clicked. Two things make the
difference between a five-minute fix and a lost afternoon.

**Every unhandled error is written down**, with the page it happened on, who
was signed in, and the traceback — because the one thing a customer can never
reproduce is the thing that just happened.

**All of it goes into one file they can send you.** Version, operating system,
how big the books are, whether backups are running, whether the licence is
valid, and the last errors — collected by one button, so the answer to "what
does it say?" is a file rather than a description.

The report is written for a customer to read before they send it. Nothing in it
is obscured, because asking somebody to email you something they cannot see the
inside of is not a reasonable thing to ask.
"""
from __future__ import annotations

import os
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .. import companies as registry
from .. import config, licensing

#: Errors are kept until the file passes this, then rolled once. Two files of
#: a quarter of a megabyte is plenty to diagnose anything and small enough to
#: email.
MAX_LOG = 256 * 1024
SEPARATOR = "\n" + "-" * 78 + "\n"


def log_dir() -> Path:
    folder = config.data_dir() / "logs"
    folder.mkdir(exist_ok=True)
    return folder


def error_log() -> Path:
    return log_dir() / "errors.log"


def _roll() -> None:
    path = error_log()
    try:
        if path.exists() and path.stat().st_size > MAX_LOG:
            previous = log_dir() / "errors-previous.log"
            if previous.exists():
                previous.unlink()
            path.rename(previous)
    except OSError:
        pass


def record(exc: BaseException, *, where: str = "", who: str = "",
           company: str = "") -> str:
    """Write one failure down. Returns a short reference to show the person.

    The reference is only a timestamp, but it turns "it crashed this morning"
    into a line somebody can search for.
    """
    stamp = datetime.now()
    reference = stamp.strftime("%d%H%M%S")
    _roll()
    entry = [
        SEPARATOR,
        f"{stamp:%Y-%m-%d %H:%M:%S}  reference {reference}",
        f"  page:    {where}",
        f"  user:    {who or '(not signed in)'}",
        f"  company: {company}",
        f"  version: {config.APP_NAME} {config.APP_VERSION} on "
        f"{platform.system()} {platform.release()}",
        "",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    ]
    try:
        with error_log().open("a", encoding="utf-8") as handle:
            handle.write("\n".join(entry))
    except OSError:
        pass
    return reference


def recent(limit: int = 20) -> list[str]:
    """The most recent failures, newest first, as blocks of text."""
    text = ""
    for path in (log_dir() / "errors-previous.log", error_log()):
        try:
            text += path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    blocks = [b.strip() for b in text.split(SEPARATOR.strip()) if b.strip()]
    return list(reversed(blocks))[:limit]


def clear() -> None:
    for name in ("errors.log", "errors-previous.log"):
        try:
            (log_dir() / name).unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def _size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "missing"
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return ""


def company_facts() -> list[str]:
    from sqlalchemy import func, select

    from .. import db as dbmod
    from ..models import Contact, Invoice, JournalEntry

    lines = []
    for ref in registry.all_companies(include_archived=True):
        path = registry.company_db(ref.slug)
        lines.append(f"  {ref.name} ({ref.slug})"
                     f"{'  [archived]' if ref.is_archived else ''}")
        lines.append(f"      file:     {_size(path)}")
        try:
            dbmod.init_db(ref.slug)
            with dbmod.session_scope_for(ref.slug) as db:
                counts = {
                    "journal entries": db.scalar(select(func.count(JournalEntry.id))),
                    "invoices": db.scalar(select(func.count(Invoice.id))),
                    "contacts": db.scalar(select(func.count(Contact.id))),
                }
            lines.append("      contains: " +
                         ", ".join(f"{v:,} {k}" for k, v in counts.items()))
        except Exception as exc:                    # noqa: BLE001
            lines.append(f"      could not be opened: {exc}")
        backups = registry.list_backups(ref.slug)
        if backups:
            name, _bytes, when = backups[0]
            lines.append(f"      last backup: {when:%d %b %Y, %H:%M} ({name})")
        else:
            lines.append("      last backup: none")
    return lines


#: What a PDF might have to print, and a word or two of each to test with.
SCRIPTS = (
    ("Chinese", "上海貿易"),
    ("Japanese", "東京商事"),
    ("Korean", "서울무역"),
    ("Cyrillic", "Восток"),
    ("Greek", "Ελληνικά"),
    ("Arabic", "شركة"),
    ("Hebrew", "מרכז"),
    ("Devanagari", "मुंबई"),
    ("Thai", "กรุงเทพ"),
    ("naira sign", "₦"),
)


def font_facts() -> list[str]:
    """Which alphabets this computer can put on a PDF, and which it cannot.

    "My invoices print as boxes" is otherwise a long conversation. This turns
    it into one line of a report: the customer's computer has no font for that
    script, and installing one — or dropping a .ttf into the fonts folder —
    fixes it.
    """
    from .. import fonts as fontfinder

    lines = [f"  fonts folder: {config.data_dir() / 'fonts'} "
             f"({'has files' if (config.data_dir() / 'fonts').is_dir() else 'empty'})"]
    for name, sample in SCRIPTS:
        try:
            found = fontfinder.find(sample)
        except Exception:                      # a font search must never fail
            found = None
        if found is None:
            lines.append(f"  {name:<12} no font here can print this")
        else:
            lines.append(f"  {name:<12} {found.font.name} "
                         f"({os.path.basename(found.path)})")
    return lines


def report(include_errors: int = 5) -> str:
    """Everything worth knowing about this installation, as plain text."""
    from .autobackup import load as backup_settings
    from .mailer import load as mail_settings

    state = licensing.status()
    backup = backup_settings()
    mail = mail_settings()

    lines = [
        f"{config.APP_NAME} diagnostic report",
        f"Taken {datetime.now():%d %B %Y at %H:%M}",
        "",
        "THIS INSTALLATION",
        f"  version:     {config.APP_VERSION}",
        f"  running as:  {'packaged application' if config.is_frozen() else 'from source'}",
        f"  started from:{' ' + str(config.program_dir())}",
        f"  python:      {sys.version.split()[0]}",
        f"  system:      {platform.system()} {platform.release()} ({platform.machine()})",
        f"  data folder: {config.data_dir()}",
        f"  machine code:{' ' + licensing.machine_code()}",
        "",
        "LICENCE",
        f"  {state.headline}",
        f"  can post new entries: {'yes' if state.can_post else 'no'}",
        "",
        "BACKUPS",
        f"  schedule:    {backup.when}",
        f"  last run:    {backup.last_run or 'never'} {backup.last_result}",
        f"  second copy: {backup.copy_to or 'not set — everything is on this disk only'}",
    ]
    if backup.last_error:
        lines.append(f"  last problem: {backup.last_error}")

    lines += [
        "",
        "EMAIL",
        f"  configured:  {'yes, ' + mail.host if mail.ready else 'no'}",
        f"  sends from:  {mail.from_email or '-'}",
        f"  last sent:   {mail.last_ok or 'never'}",
    ]
    if mail.last_error:
        lines.append(f"  last problem: {mail.last_error}")

    lines += ["", "PRINTING OTHER ALPHABETS"] + font_facts()
    lines += ["", "COMPANIES"] + company_facts()

    failures = recent(include_errors)
    lines += ["", f"RECENT ERRORS ({len(failures)} of the most recent shown)"]
    if not failures:
        lines.append("  none recorded")
    for block in failures:
        lines.append("")
        lines.extend("  " + line for line in block.splitlines())

    lines += [
        "",
        "-" * 78,
        "This file describes the installation, not the books: no invoice figures,",
        "no customer list and no passwords are copied into it. The error section",
        "is the exception — a traceback can occasionally quote a value that was",
        "being worked on when something failed, so read it before you send it.",
    ]
    return "\n".join(lines) + "\n"


def report_filename() -> str:
    return f"nexora-diagnostics-{datetime.now():%Y%m%d-%H%M}.txt"
