"""Reading a bank statement, whatever shape the bank exported it in.

There is no standard for a bank CSV. Every bank invents its own columns, its
own date format, its own way of showing a debit, and its own idea of how many
lines of preamble to put above the header. A person who has just downloaded
their statement should not have to care about any of that, and should never be
asked "which column is the amount?" when the answer is obvious from the file.

So this module works it out. It finds the header row wherever it is, matches
the columns by name against the many things banks call the same thing, and
falls back to looking at the *data* when the names are no help — a column of
things that parse as dates is a date column whatever it is labelled.

Two ambiguities cannot be resolved by cleverness and are handled by asking:

  * **Day-first or month-first.** 03/04/2026 is two different days. If any row
    in the file has a value above twelve in one position, that settles it for
    the whole file. If none does — a statement covering only the first twelve
    days of a month — the reader says so rather than guessing, because a
    silently wrong date puts transactions in the wrong period.

  * **Which way a debit points.** Most banks write money out as a negative
    amount; some use two columns; a few use a positive amount with a separate
    D/C marker. All three are detected, and the preview shows the resulting
    balance so a person can see at a glance whether it is upside down.

OFX and QFX — what most banks offer as "Quicken" or "Money" format — are also
read, and they are unambiguous, so nothing has to be guessed for those.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .importer import ImportError_, decode, normalise


# --------------------------------------------------------------------------
# One line of a statement
# --------------------------------------------------------------------------


@dataclass
class Line:
    """A single movement on the account, as the bank reported it."""

    row: int
    date: Date
    description: str
    amount: int          # minor units, positive in, negative out
    balance: int | None = None
    reference: str = ""
    payee: str = ""
    kind: str = ""       # whatever the bank called it: TRANSFER, POS, CHQ...
    raw: dict = field(default_factory=dict)

    @property
    def is_money_in(self) -> bool:
        return self.amount > 0

    @property
    def fingerprint(self) -> str:
        """What makes this line the same line if the file is imported twice.

        Deliberately not the description alone: two identical ₦5,000 card
        payments to the same shop on the same day are a real thing, and the
        bank's own reference is what separates them when it gives one.
        """
        return "|".join([
            self.date.isoformat(),
            str(self.amount),
            (self.reference or "").strip().lower(),
            " ".join((self.description or "").lower().split())[:80],
        ])


@dataclass
class Reading:
    """What came out of the file, and everything that had to be assumed."""

    lines: list[Line] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)
    date_order: str = ""          # "dmy", "mdy", "ymd"
    date_ambiguous: bool = False
    #: A balance the bank stated on a line of its own, with no amount against
    #: it — the "brought forward" row nearly every statement opens with.
    stated_opening: int | None = None
    format: str = "csv"
    problems: list[str] = field(default_factory=list)
    skipped: int = 0

    @property
    def total_in(self) -> int:
        return sum(line.amount for line in self.lines if line.amount > 0)

    @property
    def total_out(self) -> int:
        return sum(-line.amount for line in self.lines if line.amount < 0)

    @property
    def net(self) -> int:
        return sum(line.amount for line in self.lines)

    @property
    def first_date(self) -> Date | None:
        return min((line.date for line in self.lines), default=None)

    @property
    def last_date(self) -> Date | None:
        return max((line.date for line in self.lines), default=None)

    @property
    def closing_balance(self) -> int | None:
        """The balance on the last line the bank gave one for."""
        for line in sorted(self.lines, key=lambda x: (x.date, x.row), reverse=True):
            if line.balance is not None:
                return line.balance
        return None

    @property
    def opening_balance(self) -> int | None:
        """What was in the account before the first line.

        Taken from the bank's own brought-forward row when it gave one, and
        otherwise worked backwards from the first line that carries a balance.
        """
        if self.stated_opening is not None:
            return self.stated_opening
        ordered = sorted(self.lines, key=lambda x: (x.date, x.row))
        for line in ordered:
            if line.balance is not None:
                return line.balance - line.amount
        return None


# --------------------------------------------------------------------------
# What banks call things
# --------------------------------------------------------------------------

DATE_NAMES = {
    "date", "transactiondate", "valuedate", "postingdate", "posteddate",
    "bookingdate", "trandate", "datetime", "effectivedate", "entrydate",
    "transdate", "dateposted", "valuedt", "txndate",
    # Not English: this software is sold worldwide and a German or Spanish
    # bank's export should not fall back to guessing from the data.
    "datum", "buchungstag", "wertstellung", "fecha", "fechaoperacion",
    "fechavalor", "dataoperazione", "data", "dato", "datatransactie",
}
DESCRIPTION_NAMES = {
    "description", "narration", "details", "particulars", "transactiondetails",
    "remarks", "narrative", "memo", "transactiondescription", "reference1",
    "detail", "transactionnarration", "descriptionoftransaction", "text",
    "beschreibung", "verwendungszweck", "buchungstext", "descripcion",
    "concepto", "libelle", "libelle", "descricao", "historico", "causale",
    "omschrijving",
}
AMOUNT_NAMES = {
    "amount", "transactionamount", "value", "amountngn", "amt", "signedamount",
    "netamount", "transactionamt",
    "betrag", "importe", "montant", "valor", "monto", "importo", "bedrag",
}
DEBIT_NAMES = {
    "debit", "withdrawal", "withdrawals", "moneyout", "paidout", "dr",
    "debitamount", "withdrawalamount", "outflow", "payments", "spent",
    "soll", "debito", "debe", "cargo", "uscite", "af",
}
CREDIT_NAMES = {
    "credit", "deposit", "deposits", "moneyin", "paidin", "cr",
    "creditamount", "depositamount", "inflow", "receipts", "received",
    "haben", "credito", "haber", "abono", "entrate", "bij",
}
BALANCE_NAMES = {
    "balance", "runningbalance", "closingbalance", "availablebalance",
    "ledgerbalance", "balanceafter", "runningtotal", "bal",
    "saldo", "solde", "kontostand", "saldoconta",
}
REFERENCE_NAMES = {
    "reference", "ref", "transactionref", "transactionreference", "chequeno",
    "cheque", "chequenumber", "instrumentno", "trnref", "sessionid", "fitid",
    "transactionid", "documentnumber",
}
PAYEE_NAMES = {
    "payee", "counterparty", "beneficiary", "name", "merchant", "sendername",
    "beneficiaryname", "originator", "payeename",
}
TYPE_NAMES = {
    "type", "transactiontype", "trantype", "drcr", "debitcredit", "indicator",
    "crdr", "sign", "trtype",
}


def _match_column(header: str, names: set[str]) -> bool:
    key = normalise(header)
    if key in names:
        return True
    # "Transaction Date (Value)" and similar: match on the leading part too.
    return any(key.startswith(name) and len(key) - len(name) <= 6 for name in names)


# --------------------------------------------------------------------------
# Reading values
# --------------------------------------------------------------------------

_DATE_PATTERNS = [
    ("%Y-%m-%d", "ymd"), ("%Y/%m/%d", "ymd"), ("%Y.%m.%d", "ymd"),
    ("%d-%b-%Y", "dmy"), ("%d %b %Y", "dmy"), ("%d-%B-%Y", "dmy"),
    ("%d %B %Y", "dmy"), ("%b %d, %Y", "mdy"), ("%B %d, %Y", "mdy"),
    ("%d-%b-%y", "dmy"), ("%d %b %y", "dmy"),
    ("%Y%m%d", "ymd"),
]

_NUMERIC_DATE = re.compile(r"^\s*(\d{1,4})[/\-. ](\d{1,2})[/\-. ](\d{1,4})")


def _clean_cell(value) -> str:
    return " ".join(str(value or "").split())


def parse_date(text: str, order: str = "dmy") -> Date | None:
    """One date cell, in whichever way the bank wrote it."""
    text = _clean_cell(text)
    if not text:
        return None
    # Strip a time part; statements often carry one and it is never needed.
    text = re.split(r"[T ]\d{1,2}:\d{2}", text)[0].strip()

    for pattern, _order in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    match = _NUMERIC_DATE.match(text)
    if not match:
        return None
    a, b, c = (int(part) for part in match.groups())
    try:
        if len(match.group(1)) == 4:
            return Date(a, b, c)
        year = c if c > 99 else (2000 + c if c < 70 else 1900 + c)
        if order == "mdy":
            return Date(year, a, b)
        return Date(year, b, a)
    except ValueError:
        return None


def _looks_like_a_date(text: str) -> bool:
    return parse_date(text) is not None


_AMOUNT_JUNK = re.compile(r"[^\d,.\-+()]")


def parse_amount(text: str) -> int | None:
    """One money cell, in minor units. ``None`` when the cell holds no number.

    Handles what banks actually put in these columns: currency symbols and
    codes, thousands separators either way round, a trailing minus, brackets
    for negatives, and the letters DR or CR stuck on the end.
    """
    raw = _clean_cell(text)
    if not raw:
        return None

    upper = raw.upper()
    trailing_debit = bool(re.search(r"\b(DR|DB)\b\s*$", upper))
    trailing_credit = bool(re.search(r"\b(CR)\b\s*$", upper))

    body = _AMOUNT_JUNK.sub("", raw)
    if not body or not any(ch.isdigit() for ch in body):
        return None

    negative = body.startswith("(") and body.endswith(")")
    body = body.strip("()")
    if body.endswith("-"):
        negative = True
        body = body[:-1]
    if body.startswith("-"):
        negative = True
        body = body[1:]
    body = body.lstrip("+")

    body = _normalise_separators(body)
    try:
        value = Decimal(body)
    except (InvalidOperation, ValueError):
        return None

    if negative or trailing_debit:
        value = -abs(value)
    elif trailing_credit:
        value = abs(value)
    return int((value * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _normalise_separators(body: str) -> str:
    """Decide which of . and , is the decimal point.

    The same rules the money parser uses, and for the same reason: 1.234,56 and
    1,234.56 are the same amount written by different countries, and reading
    one as the other is out by a factor of a thousand.
    """
    has_dot, has_comma = "." in body, "," in body
    if has_dot and has_comma:
        # Whichever comes last is the decimal point.
        return (body.replace(".", "").replace(",", ".")
                if body.rfind(",") > body.rfind(".")
                else body.replace(",", ""))
    if has_comma:
        parts = body.split(",")
        # One comma with two digits after it is a decimal comma; anything else
        # (or several commas) is grouping.
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            return body.replace(",", ".")
        return body.replace(",", "")
    if has_dot:
        parts = body.split(".")
        if len(parts) > 2:
            return body.replace(".", "")
        if len(parts[1]) == 3 and len(parts[0]) <= 3:
            # 1.234 — grouping, unless the whole file says otherwise. Treated
            # as a decimal here because a lone "1.234" is far more often 1.234
            # than 1234 in a statement; the preview shows the total either way.
            return body
    return body


# --------------------------------------------------------------------------
# Finding the header
# --------------------------------------------------------------------------


def _rows(text: str) -> list[list[str]]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        raise ImportError_("That file is empty.")
    first = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t", "|"), key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return [row for row in rows if any(_clean_cell(cell) for cell in row)]


def _find_header(rows: list[list[str]]) -> int:
    """Which row is the header.

    Banks put the account number, the customer's name and a date range above
    the actual table, so it is rarely row one. The header is the first row
    that names a date column and either an amount or a debit/credit pair.
    """
    for index, row in enumerate(rows[:30]):
        has_date = any(_match_column(cell, DATE_NAMES) for cell in row)
        has_money = any(
            _match_column(cell, AMOUNT_NAMES | DEBIT_NAMES | CREDIT_NAMES) for cell in row
        )
        if has_date and has_money:
            return index
    return -1


#: The categories, in the order they get to claim a column. More specific
#: first, so a file with both "Debit" and "Amount" does not read the debit
#: column as the amount.
_CATEGORIES: tuple[tuple[str, set[str]], ...] = (
    ("date", DATE_NAMES),
    ("debit", DEBIT_NAMES),
    ("credit", CREDIT_NAMES),
    ("balance", BALANCE_NAMES),
    ("amount", AMOUNT_NAMES),
    ("description", DESCRIPTION_NAMES),
    ("reference", REFERENCE_NAMES),
    ("payee", PAYEE_NAMES),
    ("type", TYPE_NAMES),
)


def _map_columns(header: list[str]) -> dict[str, int]:
    """Column name to index, by what the bank called it.

    Exact matches are settled before any prefix guessing, and a column whose
    name is exactly something else's is never claimed by a prefix. Without
    that rule a statement with both "Trans Date" and "Value Date" reads the
    second date column as the amount, because "valuedate" starts with "value"
    — which produces a statement where every figure is a date.
    """
    keys = [normalise(cell) for cell in header]
    found: dict[str, int] = {}
    used: set[int] = set()

    for key, names in _CATEGORIES:
        for index, name in enumerate(keys):
            if index in used or key in found:
                continue
            if name in names:
                found[key] = index
                used.add(index)
                break

    named_exactly = {
        index for index, name in enumerate(keys)
        if any(name in names for _key, names in _CATEGORIES)
    }
    for key, names in _CATEGORIES:
        if key in found:
            continue
        for index, name in enumerate(keys):
            if index in used or index in named_exactly:
                continue
            if any(name.startswith(other) and len(name) - len(other) <= 6
                   for other in names):
                found[key] = index
                used.add(index)
                break
    return found


def _guess_columns_from_data(rows: list[list[str]]) -> dict[str, int]:
    """When the headers say nothing useful, look at what is in the columns."""
    width = max(len(row) for row in rows)
    sample = rows[:40]
    found: dict[str, int] = {}

    def column(index: int) -> list[str]:
        return [row[index] for row in sample if index < len(row)]

    dates = [i for i in range(width)
             if sum(_looks_like_a_date(v) for v in column(i)) >= max(1, len(sample) * 0.6)]
    if dates:
        found["date"] = dates[0]

    money = [i for i in range(width)
             if i not in found.values()
             and sum(parse_amount(v) is not None for v in column(i))
             >= max(1, len(sample) * 0.6)]
    if len(money) >= 2:
        # The last money column of several is nearly always the balance.
        found["amount"], found["balance"] = money[0], money[-1]
    elif money:
        found["amount"] = money[0]

    text = [i for i in range(width)
            if i not in found.values()
            and sum(len(_clean_cell(v)) > 6 for v in column(i)) >= max(1, len(sample) * 0.5)]
    if text:
        found["description"] = text[0]
    return found


def _detect_date_order(rows: list[list[str]], index: int) -> tuple[str, bool]:
    """Day-first or month-first, decided by the file rather than assumed."""
    first_over_twelve = second_over_twelve = False
    four_digit_year_first = False
    for row in rows:
        if index >= len(row):
            continue
        match = _NUMERIC_DATE.match(_clean_cell(row[index]))
        if not match:
            continue
        if len(match.group(1)) == 4:
            four_digit_year_first = True
            continue
        a, b = int(match.group(1)), int(match.group(2))
        first_over_twelve |= a > 12
        second_over_twelve |= b > 12

    if four_digit_year_first and not (first_over_twelve or second_over_twelve):
        return "ymd", False
    if first_over_twelve and not second_over_twelve:
        return "dmy", False
    if second_over_twelve and not first_over_twelve:
        return "mdy", False
    if first_over_twelve and second_over_twelve:
        # Both columns exceed twelve somewhere: the file is inconsistent.
        return "dmy", True
    # Nothing above twelve anywhere — genuinely undecidable from the data.
    return "dmy", True


# --------------------------------------------------------------------------
# Reading the whole file
# --------------------------------------------------------------------------


#: What a file actually is, by its first few bytes rather than by its name.
#: Somebody who renames statement.pdf to statement.csv has changed nothing
#: about the file, and telling them "no date column" would send them looking
#: for a column instead of for a different download.
def identify(raw: bytes) -> str:
    head = raw[:8]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        # A zip. Excel workbooks are zips with a known member inside.
        try:
            import zipfile

            with zipfile.ZipFile(io.BytesIO(raw)) as bundle:
                names = bundle.namelist()
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("word/") for n in names):
                return "docx"
        except Exception:
            return "zip"
        return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"                       # the old binary Excel format
    if head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8")):
        return "image"
    return ""


#: What to say about a file this cannot read, and what to do instead. Each one
#: names the format, says plainly that it is not readable, and gives the one
#: action that fixes it — because "unsupported file type" tells somebody
#: nothing they can act on.
CANNOT_READ = {
    "docx": (
        "That is a Word document, not a bank statement file. Download the "
        "statement from your internet banking as CSV or Excel instead."
    ),
    "image": (
        "That is a picture — a screenshot or a photograph of a statement. "
        "Numbers read out of a picture cannot be trusted into a ledger. "
        "Download the statement itself from your internet banking as CSV or "
        "Excel."
    ),
    "zip": (
        "That is a zip file. Open it, and import the statement inside it."
    ),
    "xls": (
        "That is an old-style Excel file (.xls). Open it in Excel and use "
        "File \u203a Save As to save it as CSV or as a modern .xlsx workbook, "
        "then import that."
    ),
}


def read(raw: bytes, date_order: str = "", flip: bool = False) -> Reading:
    """Read a statement file. ``date_order`` overrides what was detected."""
    kind = identify(raw)
    if kind in CANNOT_READ:
        raise ImportError_(CANNOT_READ[kind])
    if kind == "pdf":
        return read_pdf(raw, date_order=date_order, flip=flip)
    if kind == "xlsx":
        return read_xlsx(raw, date_order=date_order, flip=flip)
    if _looks_like_ofx(raw):
        return read_ofx(raw)
    return read_csv(raw, date_order=date_order, flip=flip)


def read_csv(raw: bytes, date_order: str = "", flip: bool = False) -> Reading:
    return _from_rows(_rows(decode(raw)), "csv", date_order=date_order, flip=flip)


def _from_rows(rows: list[list[str]], kind: str, date_order: str = "",
               flip: bool = False) -> Reading:
    """Everything after the file has been turned into rows of text.

    A spreadsheet and a CSV differ only in how the rows are got out of the
    file. Once they are rows, the same heading-finding, column-mapping,
    date-order and running-balance work applies to both — so it lives here and
    neither format gets a second-class version of it.
    """
    reading = Reading(format=kind)

    header_at = _find_header(rows)
    if header_at >= 0:
        header = [_clean_cell(cell) for cell in rows[header_at]]
        columns = _map_columns(header)
        body = rows[header_at + 1:]
        reading.columns = {key: header[index] for key, index in columns.items()}
    else:
        # No recognisable header: treat every row as data and read the shape.
        header, body = [], rows
        columns = _guess_columns_from_data(rows)
        reading.columns = {key: f"column {index + 1}" for key, index in columns.items()}
        if columns:
            reading.problems.append(
                "This file has no column headings that could be recognised, so the "
                "columns below were worked out from what is in them. Check the first "
                "few rows carefully before going on."
            )

    if "date" not in columns:
        raise ImportError_(
            "No date column could be found in that file. A bank statement needs "
            "one column of dates and one of amounts."
        )
    if not ({"amount", "debit", "credit"} & set(columns)):
        raise ImportError_(
            "No amount column could be found in that file. It needs either an "
            "'Amount' column, or a 'Debit' and 'Credit' pair."
        )

    detected, ambiguous = _detect_date_order(body, columns["date"])
    reading.date_order = date_order or detected
    reading.date_ambiguous = ambiguous and not date_order

    for offset, row in enumerate(body):
        line = _read_row(row, columns, reading.date_order, header_at + offset + 2)
        if line is None:
            # A row with a date and a balance but no amount is the brought
            # forward line. It is not a transaction, but its figure is the one
            # thing that lets the whole statement be proved against the ledger.
            stated = _brought_forward(row, columns, reading.date_order)
            if stated is not None and reading.stated_opening is None:
                reading.stated_opening = stated
            else:
                reading.skipped += 1
            continue
        reading.lines.append(line)

    if not reading.lines:
        raise ImportError_(
            "No transactions could be read out of that file. Check that it is the "
            "statement itself rather than a summary page."
        )

    if flip:
        for line in reading.lines:
            line.amount = -line.amount

    _check_running_balance(reading)
    return reading


def _read_row(row: list[str], columns: dict[str, int], order: str, number: int) -> Line | None:
    def cell(key: str) -> str:
        index = columns.get(key)
        return _clean_cell(row[index]) if index is not None and index < len(row) else ""

    when = parse_date(cell("date"), order)
    if when is None:
        return None

    amount: int | None = None
    if "amount" in columns:
        amount = parse_amount(cell("amount"))
        marker = cell("type").upper()
        if amount is not None and amount > 0 and marker:
            # A separate D/C column, with the amount always positive.
            if re.search(r"\b(DR|DB|DEBIT|WITHDRAWAL|OUT)\b", marker):
                amount = -amount
    if amount is None and ("debit" in columns or "credit" in columns):
        debit = parse_amount(cell("debit")) or 0
        credit = parse_amount(cell("credit")) or 0
        # Some banks put a positive number in the debit column, some negative.
        amount = abs(credit) - abs(debit)
    if amount is None or amount == 0:
        return None

    description = cell("description") or cell("payee") or cell("reference")
    return Line(
        row=number,
        date=when,
        description=description,
        amount=amount,
        balance=parse_amount(cell("balance")) if "balance" in columns else None,
        reference=cell("reference"),
        payee=cell("payee"),
        kind=cell("type"),
    )


def _brought_forward(row: list[str], columns: dict[str, int], order: str) -> int | None:
    """The opening balance off a row that has one but no movement."""
    if "balance" not in columns:
        return None
    index = columns["balance"]
    if index >= len(row):
        return None
    if parse_date(_clean_cell(row[columns["date"]]) if columns["date"] < len(row) else "",
                  order) is None:
        return None
    return parse_amount(row[index])


def _check_running_balance(reading: Reading) -> None:
    """If the bank gave a running balance, use it to prove the reading.

    This is the single most valuable check in the whole module: when the
    balances march correctly from one line to the next, the amounts and their
    signs have been read right. When they do not, the file was almost
    certainly read upside down, and saying so is far more use than a silent
    import that puts every debit in as a credit.
    """
    ordered = sorted(reading.lines, key=lambda line: (line.date, line.row))
    withs = [line for line in ordered if line.balance is not None]
    if len(withs) < 2:
        return

    def drift(sign: int) -> int:
        wrong = 0
        for previous, current in zip(withs, withs[1:]):
            if previous.balance + sign * current.amount != current.balance:
                wrong += 1
        return wrong

    forward, reverse = drift(1), drift(-1)
    if forward == 0:
        return
    if reverse == 0:
        for line in reading.lines:
            line.amount = -line.amount
        reading.problems.append(
            "The debits and credits in that file were the other way round from "
            "what the column headings suggested. The running balance proved it, "
            "so they have been turned round and now add up correctly."
        )
        return
    reading.problems.append(
        f"The running balance in the file does not follow from the amounts on "
        f"{forward} of {len(withs) - 1} lines. Something has been read wrongly, or "
        "the statement itself has a gap in it. Check the figures below before going on."
    )


# --------------------------------------------------------------------------
# OFX and QFX
# --------------------------------------------------------------------------


def _looks_like_ofx(raw: bytes) -> bool:
    head = raw[:2048].upper()
    return b"<OFX" in head or b"OFXHEADER" in head


_TAG = re.compile(r"<([A-Z0-9.]+)>([^<\r\n]*)", re.I)


def read_ofx(raw: bytes) -> Reading:
    """OFX/QFX — what most banks call "Quicken" or "Microsoft Money" format.

    Nothing has to be guessed here: the format states the sign, the currency
    and the date explicitly. It is worth offering for exactly that reason —
    when a bank's CSV is a mess, its OFX rarely is.
    """
    text = decode(raw)
    reading = Reading(format="ofx")
    reading.date_order = "ymd"

    blocks = re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.S | re.I)
    if not blocks:
        raise ImportError_("That file says it is OFX but contains no transactions.")

    for index, block in enumerate(blocks, start=1):
        fields = {name.upper(): value.strip() for name, value in _TAG.findall(block)}
        when = _ofx_date(fields.get("DTPOSTED", "") or fields.get("DTUSER", ""))
        amount = parse_amount(fields.get("TRNAMT", ""))
        if when is None or amount is None or amount == 0:
            reading.skipped += 1
            continue
        name = fields.get("NAME", "")
        memo = fields.get("MEMO", "")
        reading.lines.append(
            Line(
                row=index,
                date=when,
                description=" — ".join(x for x in (name, memo) if x) or "(no description)",
                amount=amount,
                reference=fields.get("FITID", "") or fields.get("CHECKNUM", ""),
                payee=name,
                kind=fields.get("TRNTYPE", ""),
            )
        )

    if not reading.lines:
        raise ImportError_("No transactions could be read out of that OFX file.")

    ledger = re.search(r"<LEDGERBAL>.*?<BALAMT>([^<\r\n]*)", text, re.S | re.I)
    if ledger:
        closing = parse_amount(ledger.group(1))
        if closing is not None and reading.lines:
            last = max(reading.lines, key=lambda line: (line.date, line.row))
            last.balance = closing
    return reading


def _ofx_date(value: str) -> Date | None:
    digits = re.sub(r"\D", "", value or "")[:8]
    if len(digits) != 8:
        return None
    try:
        return Date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Excel workbooks
# --------------------------------------------------------------------------
#
# Banks that offer a spreadsheet as well as a PDF are common, and asking a
# customer to open Excel and re-save as CSV is asking them to do work the
# software can do. An .xlsx file is a zip of XML, so this needs nothing
# installed: the standard library opens the zip and reads the XML.
#
# The one genuinely awkward part is dates. A spreadsheet stores 26 August 2026
# as the number 46260, and whether that is a date or a quantity of cement is
# recorded not on the cell but in a numbering format the cell points at. So
# the formats have to be read too — otherwise every date in the statement
# arrives as a five-digit number.

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
       "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}

#: Excel's own built-in formats that mean "this is a date".
_DATE_FORMAT_IDS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}


def _column_index(reference: str) -> int:
    """"C7" -> 2. Cells can be missing from a row, so position is not order."""
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch.upper()) - 64)
    return max(0, index - 1)


def _excel_date(serial: float, epoch_1904: bool = False) -> Date | None:
    """Turn a spreadsheet's day-number into a date.

    Counting starts on 30 December 1899, not the 31st, because Excel believes
    1900 was a leap year and everybody has had to agree with it ever since.
    """
    from datetime import timedelta

    if serial <= 0 or serial > 400000:
        return None
    base = Date(1904, 1, 1) if epoch_1904 else Date(1899, 12, 30)
    try:
        return base + timedelta(days=int(serial))
    except (OverflowError, ValueError):
        return None


def _date_styles(bundle) -> set[int]:
    """Which cell styles mean the number in the cell is a date."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(bundle.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()

    custom = {}
    for entry in root.iterfind(".//m:numFmts/m:numFmt", _NS):
        code = (entry.get("formatCode") or "").lower()
        # A format with day, month or year in it and no scientific notation.
        if any(ch in code for ch in "dmy") and "e+" not in code:
            custom[int(entry.get("numFmtId", -1))] = True

    styles = set()
    formats = root.find("m:cellXfs", _NS)
    # "formats or []" reads naturally and is wrong: an XML element with no
    # children is falsy today and will be truthy in a future Python, so this
    # would quietly start reading a spreadsheet's date columns as numbers.
    for position, entry in enumerate(formats if formats is not None else []):
        number_format = int(entry.get("numFmtId", 0) or 0)
        if number_format in _DATE_FORMAT_IDS or custom.get(number_format):
            styles.add(position)
    return styles


def _shared_strings(bundle) -> list[str]:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(bundle.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    out = []
    for item in root.iterfind("m:si", _NS):
        out.append("".join(node.text or "" for node in item.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    return out


def _first_sheet(bundle) -> str:
    names = [n for n in bundle.namelist()
             if n.startswith("xl/worksheets/") and n.endswith(".xml")]
    if not names:
        raise ImportError_("That workbook has no sheets in it.")
    return sorted(names)[0]


def xlsx_rows(raw: bytes) -> list[list[str]]:
    """The first sheet of a workbook, as rows of plain text."""
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        bundle = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ImportError_("That Excel file could not be opened — it may have "
                           "been damaged in transit. Download it again.") from None

    with bundle:
        strings = _shared_strings(bundle)
        dated = _date_styles(bundle)
        epoch_1904 = False
        try:
            book = ET.fromstring(bundle.read("xl/workbook.xml"))
            view = book.find("m:workbookPr", _NS)
            epoch_1904 = bool(view is not None and view.get("date1904") in ("1", "true"))
        except (KeyError, ET.ParseError):
            pass

        try:
            sheet = ET.fromstring(bundle.read(_first_sheet(bundle)))
        except ET.ParseError:
            raise ImportError_("That workbook could not be read.") from None

    rows: list[list[str]] = []
    for row in sheet.iterfind(".//m:sheetData/m:row", _NS):
        cells: list[str] = []
        for cell in row.iterfind("m:c", _NS):
            at = _column_index(cell.get("r") or "")
            while len(cells) <= at:
                cells.append("")
            cells[at] = _cell_text(cell, strings, dated, epoch_1904)
        rows.append(cells)
    return rows


def _cell_text(cell, strings: list[str], dated: set[int], epoch_1904: bool) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")).strip()

    value = cell.find("m:v", _NS)
    text = (value.text or "").strip() if value is not None else ""
    if not text:
        return ""
    if kind == "s":
        try:
            return strings[int(text)]
        except (ValueError, IndexError):
            return ""
    if kind in ("str", "e"):
        return text
    if kind == "b":
        return "TRUE" if text == "1" else "FALSE"

    # A number. It is a date if the cell's style says so.
    try:
        style = int(cell.get("s", -1))
    except ValueError:
        style = -1
    if style in dated:
        try:
            when = _excel_date(float(text), epoch_1904)
        except ValueError:
            when = None
        if when is not None:
            return when.strftime("%Y-%m-%d")
    return text


def read_xlsx(raw: bytes, date_order: str = "", flip: bool = False) -> Reading:
    rows = xlsx_rows(raw)
    if not rows:
        raise ImportError_("That workbook is empty.")
    return _from_rows(rows, "xlsx", date_order=date_order, flip=flip)


# --------------------------------------------------------------------------
# PDF statements
# --------------------------------------------------------------------------


def read_pdf(raw: bytes, date_order: str = "", flip: bool = False) -> Reading:
    """A statement out of the PDF the bank actually gave the customer.

    A PDF has no rows and no columns — only glyphs at points on a page — so
    ``app/pdfread.py`` reconstructs the table by looking at where everything
    sits, and from there it is read exactly like a spreadsheet.

    This is the least certain of the three readers, and the screen says so.
    What makes it safe enough to offer is that this import never posts
    anything by itself: every line is shown for a person to confirm, so a
    misreading is a wrong line in front of somebody rather than a wrong figure
    in a ledger.
    """
    from ..pdfread import PdfError
    from ..pdfread import read as read_pages

    try:
        rows = read_pages(raw)
    except PdfError as exc:
        raise ImportError_(str(exc)) from None

    if not rows:
        raise ImportError_(
            "Nothing could be read out of that PDF. Download the statement as "
            "CSV or Excel from your internet banking instead.")

    reading = _from_rows(rows, "pdf", date_order=date_order, flip=flip)
    reading.problems.insert(0, _PDF_WARNING)
    return reading


#: Said on every PDF import, at the top, before anything else.
_PDF_WARNING = (
    "This came out of a PDF, which is the least reliable thing to read a "
    "statement from — a PDF holds no columns, only text at positions, and the "
    "table below was reconstructed from where that text sat on the page. "
    "Check the dates and amounts against the statement before you confirm "
    "anything. Where your bank offers CSV or Excel, use that instead."
)
