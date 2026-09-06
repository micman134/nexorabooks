"""Sending an invoice to the customer, over the business's own mail account.

An invoice that has to be printed, scanned and attached by hand gets sent late,
which means it gets paid late. So the software sends it — through the
customer's own mail provider, using their own address, so the invoice arrives
from the business the customer already knows rather than from a service they
have never heard of.

Three things this deliberately does not do.

It does not relay through anybody else. There is no account to sign up for and
no third party holding the mail: the settings are the same ones the owner
already typed into Outlook or their phone.

It does not lose a document when the mail fails. The PDF is generated,
attached, and if the server refuses it the failure is written down with the
reason, and the invoice is exactly where it was. Sending is never the only copy
of anything.

It does not pretend an unknown failure is understood. A mail server's own words
are passed through, next to a plain-English explanation where the cause is one
of the handful that are always the cause.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from .. import config, fileguard

NONE, STARTTLS, SSL = "NONE", "STARTTLS", "SSL"

SECURITY_LABELS = {
    STARTTLS: "STARTTLS — usual for port 587",
    SSL: "SSL/TLS — usual for port 465",
    NONE: "None — only on a mail server inside your own office",
}

#: What the common providers want, so nobody has to look it up.
PRESETS = {
    "Gmail / Google Workspace": ("smtp.gmail.com", 587, STARTTLS),
    "Microsoft 365 / Outlook": ("smtp.office365.com", 587, STARTTLS),
    "Yahoo Mail": ("smtp.mail.yahoo.com", 465, SSL),
    "Zoho Mail": ("smtp.zoho.com", 587, STARTTLS),
    "cPanel or your own domain": ("mail.yourdomain.com", 587, STARTTLS),
}

TIMEOUT = 30


class MailError(Exception):
    """Something went wrong that the person can be told about plainly."""


@dataclass
class Settings:
    host: str = ""
    port: int = 587
    security: str = STARTTLS
    username: str = ""
    password: str = ""
    from_name: str = ""
    from_email: str = ""
    reply_to: str = ""
    signature: str = ""
    last_error: str = ""
    last_ok: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.host and self.from_email)

    @property
    def sender(self) -> str:
        name = self.from_name or self.from_email
        return formataddr((name, self.from_email))


def _file() -> Path:
    return config.data_dir() / "email.json"


def load() -> Settings:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    known = set(Settings().__dataclass_fields__)
    return Settings(**{k: v for k, v in data.items() if k in known})


def save(settings: Settings) -> None:
    path = _file()
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    # The mail password lives in here. Nobody else on the machine needs it.
    fileguard.restrict_to_owner(path)


def looks_like_an_address(value: str) -> bool:
    """A cheap check that catches the mistakes people actually make.

    Deliberately not a full RFC 5322 parser: the point is to stop a typo or a
    half-filled box before the mail server is troubled, and to say so in words,
    rather than to adjudicate the exotic addresses the standard permits.
    """
    text = (value or "").strip()
    if not text or any(ch.isspace() for ch in text) or text.count("@") != 1:
        return False
    local, _, domain = text.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") \
        and not domain.endswith(".")


# --------------------------------------------------------------------------
# Saying what went wrong in words somebody can act on
# --------------------------------------------------------------------------


def explain(exc: Exception) -> str:
    """A mail server's refusal, translated where the cause is a known one."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "The mail server would not accept that username and password. "
            "If you use Gmail or Microsoft 365 with two-step verification, your "
            "ordinary password will not work here — create an app password in "
            "your mail account's security settings and use that instead."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the address you are sending to."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return ("The mail server refused the address you are sending from. It "
                "usually has to be the same account you signed in with.")
    if isinstance(exc, ssl.SSLError):
        return ("The secure connection failed. Try the other security setting — "
                "port 587 usually wants STARTTLS and port 465 wants SSL/TLS.")
    if isinstance(exc, (TimeoutError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return ("Could not reach the mail server. Check the server name and port, "
                "and that this computer is online.")
    if isinstance(exc, smtplib.SMTPException):
        return f"The mail server refused it: {exc}"
    return str(exc)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def build(settings: Settings, to: str, subject: str, body: str,
          attachments: list[tuple[str, bytes, str]] | None = None,
          cc: str = "") -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    if settings.reply_to:
        message["Reply-To"] = settings.reply_to
    message["Subject"] = subject
    text = body.rstrip()
    if settings.signature:
        text += "\n\n" + settings.signature
    message.set_content(text + "\n")
    for filename, data, mime in attachments or []:
        major, _, minor = mime.partition("/")
        message.add_attachment(data, maintype=major, subtype=minor or "octet-stream",
                               filename=filename)
    return message


def send(to: str, subject: str, body: str,
         attachments: list[tuple[str, bytes, str]] | None = None,
         *, cc: str = "", settings: Settings | None = None) -> None:
    """Send it, or raise MailError with something worth reading."""
    settings = settings or load()
    if not settings.ready:
        raise MailError(
            "Email has not been set up yet. Settings › Email takes a minute.")
    if not looks_like_an_address(to):
        raise MailError(f"{to or 'That'} does not look like an email address.")

    message = build(settings, to, subject, body, attachments, cc)
    try:
        if settings.security == SSL:
            server = smtplib.SMTP_SSL(settings.host, settings.port,
                                      timeout=TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.host, settings.port, timeout=TIMEOUT)
        with server:
            server.ehlo()
            if settings.security == STARTTLS:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            if settings.username:
                server.login(settings.username, settings.password)
            server.send_message(message)
    except Exception as exc:                       # noqa: BLE001 — all reported
        settings.last_error = explain(exc)
        save(settings)
        raise MailError(settings.last_error) from exc

    settings.last_error = ""
    from datetime import datetime

    settings.last_ok = datetime.now().strftime("%d %b %Y, %H:%M")
    save(settings)


# --------------------------------------------------------------------------
# What to say in the covering message
# --------------------------------------------------------------------------


#: What a company may put in its own wording, and what each one becomes.
#: Shown on the settings screen exactly as written here, so the explanations
#: are the documentation.
PLACEHOLDERS: list[tuple[str, str]] = [
    ("{customer}", "the customer's name, as it is on the invoice"),
    ("{first_name}", "just their first name — 'Chinedu' rather than 'Chinedu Okafor'"),
    ("{number}", "the document number, e.g. INV-0042"),
    ("{document}", "'invoice', 'quotation' or 'credit note', whichever it is"),
    ("{amount}", "the amount owing, with the currency"),
    ("{due_date}", "when payment is due"),
    ("{date}", "the date on the document"),
    ("{item}", "what the first line says — the course, product or service"),
    ("{company}", "your company's name"),
    ("{phone}", "your company's phone number"),
    ("{email}", "your company's email address"),
]

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def fill(template: str, values: dict[str, str]) -> str:
    """Put the real figures into somebody's wording.

    Two rules, both there because of how this goes wrong in practice.

    An unknown placeholder is left exactly as it was typed. Somebody who writes
    ``{customer_name}`` should see ``{customer_name}`` in the preview and
    understand what to fix; a silent gap teaches them nothing and an exception
    loses them the invoice.

    A line that mentions something this document does not have is left out
    altogether. A credit note has no due date, so "Please pay by {due_date}."
    would otherwise arrive as "Please pay by ." — the line is dropped instead,
    and the rest of the message is untouched. It also means one piece of
    wording can serve invoices, quotations and credit notes without anybody
    maintaining three of them.
    """
    def swap(match: re.Match) -> str:
        name = match.group(1)
        return values[name] if name in values else match.group(0)

    kept: list[str] = []
    for line in (template or "").splitlines():
        mentioned = [name for name in _PLACEHOLDER.findall(line) if name in values]
        if mentioned and not all(str(values[name]).strip() for name in mentioned):
            continue                                # this document has no such thing
        kept.append(_PLACEHOLDER.sub(swap, line))
    return "\n".join(kept)


def unknown_placeholders(text: str) -> list[str]:
    """Anything in braces that this cannot fill in, in the order written.

    Worth telling somebody about at the moment they save, rather than letting
    ``{customer_name}`` go out to a customer looking exactly like that.
    """
    known = {token.strip("{}") for token, _ in PLACEHOLDERS}
    seen: list[str] = []
    for name in _PLACEHOLDER.findall(text or ""):
        if name not in known and f"{{{name}}}" not in seen:
            seen.append(f"{{{name}}}")
    return seen


def _first_line_of(doc) -> str:
    """What the document is for, in the words already on it."""
    try:
        for line in (doc.lines or []):
            text = (line.description or "").strip()
            if text:
                return text
    except Exception:                               # noqa: BLE001 — wording, not accounting
        pass
    return ""


def document_values(company, doc, label: str, amount: str, due: str) -> dict[str, str]:
    """Everything a company's own wording is allowed to refer to."""
    from .. import prefs

    customer = getattr(getattr(doc, "contact", None), "name", "") or ""
    return {
        "customer": customer,
        "first_name": customer.split()[0] if customer.split() else customer,
        "number": doc.number or "",
        "document": (label or "Invoice").lower(),
        "amount": amount or "",
        "due_date": due or "",
        "date": prefs.strftime(getattr(doc, "date", None), prefs.date_format()),
        "item": _first_line_of(doc),
        "company": (company.name if company else "") or "",
        "phone": (getattr(company, "phone", "") or "") if company else "",
        "email": (getattr(company, "email", "") or "") if company else "",
    }


def invoice_subject(company, doc, label: str) -> str:
    name = company.name if company else ""
    own = (getattr(company, "invoice_email_subject", "") or "").strip() if company else ""
    if own:
        # A subject is one line, so the drop-a-line rule would empty it; the
        # amount and the due date are left out of a subject anyway.
        written = fill(own, document_values(company, doc, label, "-", "-")).strip()
        if written:
            return written
    return f"{label} {doc.number} from {name}".strip()


def invoice_body(company, doc, label: str, amount: str, due: str) -> str:
    """A short, plain covering note. Nobody reads a long one.

    Unless this company has written its own, in which case theirs is used and
    nothing here second-guesses it. A training business sending the same
    "thank you for registering" note with every invoice should not have to
    retype it, or worse, forget a line of it on the one that matters.
    """
    name = company.name if company else "us"
    own = (getattr(company, "invoice_email_body", "") or "").strip() if company else ""
    if own:
        written = fill(own, document_values(company, doc, label, amount, due)).strip()
        if written:
            return written
        # Every line referred to something this document does not have. Rather
        # than send a blank message, fall through to the plain wording below.
    lines = [f"Dear {doc.contact.name},", ""]
    if label.lower() == "quotation":
        lines.append(f"Please find attached our quotation {doc.number} for {amount}.")
        if due:
            lines.append(f"It is valid until {due}.")
    elif label.lower() == "credit note":
        lines.append(f"Please find attached credit note {doc.number} for {amount}.")
    else:
        lines.append(f"Please find attached invoice {doc.number} for {amount}.")
        if due:
            lines.append(f"Payment is due by {due}.")
    lines += ["", "The details are on the attached PDF. Please let us know if "
                  "anything needs correcting.", "", "With thanks,", name]
    return "\n".join(lines)


def statement_subject(company, contact) -> str:
    return f"Statement of account from {company.name if company else ''}".strip()


def statement_body(company, contact, closing: str) -> str:
    name = company.name if company else "us"
    return "\n".join([
        f"Dear {contact.name},", "",
        "Please find attached your statement of account.",
        f"The balance outstanding is {closing}.", "",
        "If any of it does not agree with your records, tell us and we will "
        "look into it.", "",
        "With thanks,", name,
    ])


def payslip_period(run) -> str:
    """The period a payslip covers, written the way a person would say it."""
    if run is None:
        return ""
    start, end = run.period_start, run.period_end
    if start and end and (start.year, start.month) == (end.year, end.month):
        return start.strftime("%B %Y")
    if start and end:
        return f"{start:%d %b} to {end:%d %b %Y}"
    return run.number or ""


def payslip_subject(company, slip) -> str:
    period = payslip_period(slip.run if slip else None)
    name = company.name if company else ""
    return " — ".join(part for part in
                      [f"Payslip {period}".strip(), name] if part)


def payslip_body(company, slip, period: str, net: str) -> str:
    """A payslip note says four things and stops.

    Who it is from, which period it covers, what the net was, and who to ask if
    it looks wrong. Anything longer and the one line that matters — "tell us if
    this is wrong" — gets skimmed past.
    """
    name = company.name if company else "us"
    first = (slip.employee_name or "").split()[0] if slip.employee_name else ""
    return "\n".join([
        f"Dear {first or slip.employee_name},", "",
        f"Please find attached your payslip for {period}.",
        f"The net amount paid to you is {net}.", "",
        "The attachment shows how that figure was arrived at, including the "
        "tax and pension working. If anything on it does not look right, "
        "reply to this message and we will check it.", "",
        "Kind regards,", name,
    ])
