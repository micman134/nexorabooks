"""Moving a business in from whatever it was using before.

Nobody adopts accounting software by typing four hundred customers into it. If
the books cannot be brought across in an afternoon they are not brought across
at all, and the software is never used — which makes this module the difference
between a demonstration and a product.

Three commitments shape it.

**Nothing is written until the whole file has been read and shown.** Every
upload is parsed, checked row by row and displayed as it will be applied —
which rows are new, which update something that already exists, which are wrong
and why. Only then, on a second click, is anything saved. An import that half
succeeded and stopped in the middle would be worse than no import at all.

**A header is matched by what it means, not by what it says.** Real
spreadsheets say "Customer Name", "CLIENT NAME", "name" and "Company". They all
mean the same column, so they are all accepted, and any column nobody
recognises is reported rather than silently dropped.

**Opening balances go in as accounting, not as numbers.** Unpaid invoices are
brought in as real invoices, so the ageing and the statements are right from
day one, with the other side going to Opening Balances rather than to income —
because the sale happened in the old system and counting it again would inflate
this year's turnover.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    ACCOUNT_TYPES,
    DRAFT,
    SERVICE_ITEM,
    STOCK_ITEM,
    Account,
    Bill,
    BillLine,
    Contact,
    Employee,
    Invoice,
    InvoiceLine,
    Item,
    TaxCode,
)
from ..money import fmt, to_minor
from .posting import EntryDraft, PostingError, next_number, post_entry, sys_account

CREATE, UPDATE, SKIP, ERROR = "CREATE", "UPDATE", "SKIP", "ERROR"


class ImportError_(Exception):
    """Something wrong with the file as a whole, rather than with one row."""


# --------------------------------------------------------------------------
# Reading whatever file arrived
# --------------------------------------------------------------------------


def decode(raw: bytes) -> str:
    """Text out of a file a spreadsheet wrote, whatever it decided to use.

    Excel on a Windows machine writes UTF-8 with a byte-order mark, or the
    local code page, depending on which "Save as CSV" was chosen. All of them
    have to open, because the person exporting the file does not know which one
    they picked and should not have to.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def sniff(text: str) -> list[list[str]]:
    """Rows out of the text, guessing the separator.

    Commas normally, semicolons where the decimal point is a comma, tabs when
    somebody pasted straight out of a spreadsheet. Guessed from the header line
    rather than configured, because being asked "what is your delimiter?" is
    not a question anybody should be asked.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        raise ImportError_("That file is empty.")
    first = text.split("\n", 1)[0]
    delimiter = max((",", ";", "\t", "|"), key=first.count)
    if first.count(delimiter) == 0:
        delimiter = ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter)]
    return [r for r in rows if any((c or "").strip() for c in r)]


def normalise(header: str) -> str:
    """'Customer Name *' and 'customer_name' become the same thing."""
    return "".join(c for c in (header or "").lower() if c.isalnum())


# --------------------------------------------------------------------------
# What a column is
# --------------------------------------------------------------------------

TEXT, MONEY, QTY, WHOLE, BOOL, DATE, CHOICE, ACCOUNT, CONTACT = (
    "TEXT", "MONEY", "QTY", "WHOLE", "BOOL", "DATE", "CHOICE", "ACCOUNT", "CONTACT"
)

TRUE_WORDS = {"1", "y", "yes", "true", "t", "x", "on", "✓", "tick", "ticked"}
FALSE_WORDS = {"", "0", "n", "no", "false", "f", "off", "-"}

DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%Y/%m/%d", "%d/%m/%y", "%m/%d/%y",
)


@dataclass
class Column:
    key: str
    labels: tuple[str, ...]
    kind: str = TEXT
    required: bool = False
    choices: dict[str, str] = field(default_factory=dict)
    note: str = ""
    example: str = ""

    @property
    def heading(self) -> str:
        return self.labels[0]

    def accepts(self, header: str) -> bool:
        n = normalise(header)
        return any(normalise(l) == n for l in self.labels)


def _money(value: str) -> int:
    return to_minor(value)


def _qty(value: str) -> int:
    try:
        return int((Decimal(str(value).replace(",", "").strip()) * 1000).to_integral_value())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{value!r} is not a quantity")


def _date(value: str) -> date:
    text = str(value).strip()
    for f in DATE_FORMATS:
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    raise ValueError(f"{value!r} is not a date this understands")


def _bool(value: str) -> bool:
    text = str(value).strip().lower()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    raise ValueError(f"{value!r} — write yes or no")


# --------------------------------------------------------------------------
# A sheet is one kind of thing being brought in
# --------------------------------------------------------------------------


@dataclass
class Sheet:
    key: str
    title: str
    what: str
    columns: list[Column]
    order: int = 0
    warning: str = ""

    def column(self, key: str) -> Column | None:
        for c in self.columns:
            if c.key == key:
                return c
        return None

    def template(self) -> str:
        """A CSV with the headings and one worked example row."""
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([c.heading for c in self.columns])
        writer.writerow([c.example for c in self.columns])
        return out.getvalue()


@dataclass
class Row:
    number: int                       # the line in their file, for pointing at
    values: dict = field(default_factory=dict)
    action: str = CREATE
    errors: list[str] = field(default_factory=list)
    note: str = ""
    raw: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class Preview:
    sheet: Sheet
    rows: list[Row] = field(default_factory=list)
    #: Only the columns this file actually had. The preview shows these and no
    #: others — a table of twelve mostly-empty columns is harder to check than
    #: the four the customer really sent.
    matched_columns: list[Column] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)

    @property
    def good(self) -> list[Row]:
        return [r for r in self.rows if r.ok]

    @property
    def bad(self) -> list[Row]:
        return [r for r in self.rows if not r.ok]

    @property
    def creating(self) -> int:
        return sum(1 for r in self.good if r.action == CREATE)

    @property
    def updating(self) -> int:
        return sum(1 for r in self.good if r.action == UPDATE)

    @property
    def can_apply(self) -> bool:
        return bool(self.good) and not self.missing_columns


# --------------------------------------------------------------------------
# The sheets themselves
# --------------------------------------------------------------------------


def _sheets() -> list[Sheet]:
    account_types = {t.lower(): t for t in ACCOUNT_TYPES}
    account_types.update({
        "asset": "ASSET", "assets": "ASSET",
        "liability": "LIABILITY", "liabilities": "LIABILITY",
        "equity": "EQUITY", "capital": "EQUITY",
        "income": "INCOME", "revenue": "INCOME", "sales": "INCOME",
        "expense": "EXPENSE", "expenses": "EXPENSE", "cost": "EXPENSE",
    })

    return [
        Sheet(
            key="accounts", order=1,
            title="Chart of accounts",
            what="Extra accounts of your own, on top of the ones already here. "
                 "An account whose code already exists is renamed, never retyped.",
            columns=[
                Column("code", ("Code", "Account code", "No", "Number"), TEXT,
                       required=True, example="6250"),
                Column("name", ("Name", "Account name", "Description"), TEXT,
                       required=True, example="Generator diesel"),
                Column("type", ("Type", "Account type", "Class"), CHOICE,
                       required=True, choices=account_types,
                       note="Asset, Liability, Equity, Income or Expense",
                       example="Expense"),
                Column("description", ("Notes", "Description", "Detail"), TEXT,
                       example="Fuel for the office generator"),
            ],
        ),
        Sheet(
            key="customers", order=2,
            title="Customers",
            what="People and businesses you invoice.",
            columns=[
                Column("name", ("Name", "Customer", "Customer name", "Company",
                                "Client", "Client name", "Account name"), TEXT,
                       required=True, example="Zenith Construction Ltd"),
                Column("code", ("Code", "Customer code", "Account no", "Reference"),
                       TEXT, note="Left empty, one is made for you",
                       example="C-0001"),
                Column("contact_person", ("Contact", "Contact person", "Attention"),
                       TEXT, example="Mrs Bello"),
                Column("email", ("Email", "Email address", "E-mail"), TEXT,
                       example="accounts@zenith.example"),
                Column("phone", ("Phone", "Telephone", "Mobile", "Tel"), TEXT,
                       example="+234 801 000 0000"),
                Column("address", ("Address", "Street", "Address line"), TEXT,
                       example="4 Marina Road"),
                Column("city", ("City", "Town"), TEXT, example="Lagos"),
                Column("state", ("State", "Region", "Province", "County"), TEXT,
                       example="Lagos"),
                Column("tin", ("Tax ID", "TIN", "VAT number", "GST number",
                               "Tax number"), TEXT, example="01234567-0001"),
                Column("payment_terms_days", ("Payment terms", "Terms", "Days",
                                              "Credit days"), WHOLE,
                       note="Days. Empty means your company default.", example="30"),
                Column("credit_limit", ("Credit limit", "Limit"), MONEY,
                       example="5,000,000"),
                Column("notes", ("Notes", "Comment", "Remarks"), TEXT, example=""),
            ],
        ),
        Sheet(
            key="suppliers", order=3,
            title="Suppliers",
            what="People and businesses who invoice you.",
            columns=[
                Column("name", ("Name", "Supplier", "Supplier name", "Vendor",
                                "Vendor name", "Company"), TEXT,
                       required=True, example="Dangote Cement Plc"),
                Column("code", ("Code", "Supplier code", "Account no", "Reference"),
                       TEXT, example="S-0001"),
                Column("contact_person", ("Contact", "Contact person"), TEXT,
                       example="Sales desk"),
                Column("email", ("Email", "Email address", "E-mail"), TEXT,
                       example="orders@dangote.example"),
                Column("phone", ("Phone", "Telephone", "Mobile", "Tel"), TEXT,
                       example="+234 802 000 0000"),
                Column("address", ("Address", "Street"), TEXT, example="Union Marble House"),
                Column("city", ("City", "Town"), TEXT, example="Lagos"),
                Column("state", ("State", "Region", "Province"), TEXT, example="Lagos"),
                Column("tin", ("Tax ID", "TIN", "VAT number", "Tax number"), TEXT,
                       example="09876543-0001"),
                Column("payment_terms_days", ("Payment terms", "Terms", "Days"), WHOLE,
                       example="30"),
                Column("notes", ("Notes", "Comment", "Remarks"), TEXT, example=""),
            ],
        ),
        Sheet(
            key="items", order=4,
            title="Products and services",
            what="What you sell. Stock quantities come in with the opening "
                 "stock sheet, not here.",
            columns=[
                Column("code", ("Code", "Item code", "SKU", "Product code", "Part no"),
                       TEXT, required=True, example="CEM-50"),
                Column("name", ("Name", "Item", "Description", "Product", "Item name"),
                       TEXT, required=True, example="Dangote cement 50kg"),
                Column("item_type", ("Type", "Item type", "Kind"), CHOICE,
                       choices={"stock": STOCK_ITEM, "goods": STOCK_ITEM,
                                "product": STOCK_ITEM, "inventory": STOCK_ITEM,
                                "service": SERVICE_ITEM, "labour": SERVICE_ITEM,
                                "labor": SERVICE_ITEM, "": STOCK_ITEM},
                       note="Stock or service. Empty means stock.", example="Stock"),
                Column("unit", ("Unit", "UOM", "Units", "Measure"), TEXT,
                       example="bag"),
                Column("category", ("Category", "Group", "Class"), TEXT,
                       example="Cement"),
                Column("sale_price", ("Sale price", "Price", "Selling price",
                                      "Unit price"), MONEY, example="9,500"),
                Column("purchase_price", ("Cost", "Cost price", "Purchase price",
                                          "Buy price"), MONEY, example="8,200"),
                Column("reorder_level", ("Reorder level", "Minimum", "Min stock",
                                         "Reorder point"), QTY, example="100"),
                Column("barcode", ("Barcode", "EAN", "UPC"), TEXT, example=""),
            ],
        ),
        Sheet(
            key="trial_balance", order=5,
            title="Opening balances",
            what="Your closing trial balance from the old system, on the day you "
                 "switch over. Leave out customer and supplier balances — those "
                 "come in as unpaid invoices and bills below.",
            warning="This posts one journal dated as you choose. Running it again "
                    "replaces the previous one, so a corrected file can simply be "
                    "uploaded over the top.",
            columns=[
                Column("account", ("Account", "Account code", "Code", "Ledger"),
                       ACCOUNT, required=True,
                       note="The account code, or its name",
                       example="1020"),
                Column("debit", ("Debit", "Dr", "Debits"), MONEY, example="2,400,000"),
                Column("credit", ("Credit", "Cr", "Credits"), MONEY, example=""),
            ],
        ),
        Sheet(
            key="open_invoices", order=6,
            title="Unpaid customer invoices",
            what="Invoices your customers still owe on the switch-over day. They "
                 "come in as real invoices, so ageing and statements are right "
                 "from the first day.",
            warning="The income side goes to Opening Balances, not to sales — the "
                    "sale was already counted in your old system and counting it "
                    "twice would inflate this year's turnover.",
            columns=[
                Column("contact", ("Customer", "Customer name", "Client", "Name",
                                   "Account"), CONTACT, required=True,
                       example="Zenith Construction Ltd"),
                Column("number", ("Invoice no", "Invoice number", "Number",
                                  "Reference", "Doc no"), TEXT,
                       note="Keep your old numbers so people recognise them",
                       example="INV-2025-0413"),
                Column("date", ("Date", "Invoice date", "Issued"), DATE,
                       required=True, example="2026-06-14"),
                Column("due_date", ("Due date", "Due", "Payment due"), DATE,
                       example="2026-07-14"),
                Column("amount", ("Amount", "Balance", "Outstanding", "Amount due",
                                  "Total", "Balance due"), MONEY, required=True,
                       note="What is still owed, including tax",
                       example="1,075,000"),
                Column("description", ("Description", "Details", "Memo", "Narration"),
                       TEXT, example="Balance brought forward"),
            ],
        ),
        Sheet(
            key="open_bills", order=7,
            title="Unpaid supplier bills",
            what="What you still owe your suppliers on the switch-over day.",
            warning="The expense side goes to Opening Balances rather than to a cost "
                    "account, for the same reason as the invoices above.",
            columns=[
                Column("contact", ("Supplier", "Supplier name", "Vendor", "Name"),
                       CONTACT, required=True, example="Dangote Cement Plc"),
                Column("number", ("Bill no", "Invoice no", "Number", "Reference"),
                       TEXT, example="DCP-88214"),
                Column("date", ("Date", "Bill date", "Invoice date"), DATE,
                       required=True, example="2026-06-20"),
                Column("due_date", ("Due date", "Due"), DATE, example="2026-07-20"),
                Column("amount", ("Amount", "Balance", "Outstanding", "Total"),
                       MONEY, required=True, example="4,300,000"),
                Column("description", ("Description", "Details", "Memo"), TEXT,
                       example="Balance brought forward"),
            ],
        ),
        Sheet(
            key="employees", order=8,
            title="Employees",
            what="Your staff and their pay. Set their manager and their scheme "
                 "membership afterwards on each employee card.",
            columns=[
                Column("staff_no", ("Staff no", "Employee no", "Staff number",
                                    "Payroll no", "ID"), TEXT, required=True,
                       example="EMP-001"),
                Column("first_name", ("First name", "Forename", "Given name"), TEXT,
                       required=True, example="Adaeze"),
                Column("last_name", ("Last name", "Surname", "Family name"), TEXT,
                       required=True, example="Okafor"),
                Column("job_title", ("Job title", "Position", "Role", "Designation"),
                       TEXT, example="Accountant"),
                Column("department", ("Department", "Section", "Unit"), TEXT,
                       example="Finance"),
                Column("hire_date", ("Start date", "Hire date", "Date joined",
                                     "Employed from"), DATE, example="2024-03-01"),
                Column("email", ("Email", "Email address"), TEXT,
                       example="adaeze@example.com"),
                Column("phone", ("Phone", "Mobile", "Telephone"), TEXT,
                       example="+234 803 000 0000"),
                Column("basic", ("Basic", "Basic salary", "Basic pay", "Salary"),
                       MONEY, example="450,000"),
                Column("housing", ("Housing", "Housing allowance", "Accommodation"),
                       MONEY, example="150,000"),
                Column("transport", ("Transport", "Transport allowance"), MONEY,
                       example="75,000"),
                Column("frequency", ("Paid", "Frequency", "Pay frequency", "Cycle"),
                       CHOICE,
                       choices={"monthly": "MONTHLY", "month": "MONTHLY",
                                "fortnightly": "FORTNIGHTLY", "biweekly": "FORTNIGHTLY",
                                "weekly": "WEEKLY", "week": "WEEKLY", "": "MONTHLY"},
                       example="Monthly"),
                Column("bank_name", ("Bank", "Bank name"), TEXT, example="GTBank"),
                Column("bank_account_no", ("Account no", "Bank account",
                                           "Account number"), TEXT,
                       example="0123456789"),
                Column("tin", ("Tax ID", "TIN", "Tax number"), TEXT, example=""),
            ],
        ),
    ]


SHEETS: dict[str, Sheet] = {s.key: s for s in _sheets()}


def sheets() -> list[Sheet]:
    return sorted(SHEETS.values(), key=lambda s: s.order)


def sheet(key: str) -> Sheet | None:
    return SHEETS.get(key)


# --------------------------------------------------------------------------
# Reading a file against a sheet
# --------------------------------------------------------------------------


def read(db: Session, sheet_key: str, raw: bytes) -> Preview:
    """Parse and check, touching nothing. The result is what will happen."""
    s = sheet(sheet_key)
    if s is None:
        raise ImportError_("That is not something this can bring in.")

    rows = sniff(decode(raw))
    header = rows[0]
    body = rows[1:]
    if not body:
        raise ImportError_(
            "That file has a heading row and nothing under it. Check you exported "
            "the data and not just the column names."
        )

    mapping: dict[int, Column] = {}
    unknown: list[str] = []
    for index, cell in enumerate(header):
        match = next((c for c in s.columns if c.accepts(cell)), None)
        if match is None:
            if (cell or "").strip():
                unknown.append(cell.strip())
        elif match.key not in {c.key for c in mapping.values()}:
            mapping[index] = match

    found = {c.key for c in mapping.values()}
    missing = [c.heading for c in s.columns if c.required and c.key not in found]

    preview = Preview(
        sheet=s,
        matched_columns=[c for c in s.columns if c.key in found],
        unknown_columns=unknown,
        missing_columns=missing,
    )
    if missing:
        return preview

    seen_keys: set[str] = set()
    for offset, raw_row in enumerate(body):
        row = Row(number=offset + 2, raw=list(raw_row))
        for index, column in mapping.items():
            text = (raw_row[index] if index < len(raw_row) else "") or ""
            text = text.strip()
            if not text:
                if column.required:
                    row.errors.append(f"{column.heading} is needed and is empty")
                continue
            try:
                row.values[column.key] = _coerce(db, column, text)
            except ValueError as exc:
                row.errors.append(f"{column.heading}: {exc}")
        if row.ok:
            _decide(db, s, row, seen_keys)
        else:
            row.action = ERROR
        preview.rows.append(row)

    return preview


def _coerce(db: Session, column: Column, text: str):
    if column.kind == MONEY:
        return _money(text)
    if column.kind == QTY:
        return _qty(text)
    if column.kind == WHOLE:
        try:
            return int(Decimal(text.replace(",", "")))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{text!r} is not a whole number")
    if column.kind == BOOL:
        return _bool(text)
    if column.kind == DATE:
        return _date(text)
    if column.kind == CHOICE:
        key = text.strip().lower()
        if key in column.choices:
            return column.choices[key]
        allowed = sorted({v for k, v in column.choices.items() if k})
        raise ValueError(f"{text!r} is not one of {', '.join(allowed)}")
    if column.kind == ACCOUNT:
        account = _find_account(db, text)
        if account is None:
            raise ValueError(
                f"no account called {text!r} — add it to the chart of accounts first"
            )
        return account.id
    if column.kind == CONTACT:
        return text                     # matched or created when applied
    return text


def _find_account(db: Session, text: str) -> Account | None:
    text = text.strip()
    found = db.scalar(select(Account).where(Account.code == text))
    if found is not None:
        return found
    return db.scalar(
        select(Account).where(func.lower(Account.name) == text.lower())
    )


def _decide(db: Session, s: Sheet, row: Row, seen: set[str]) -> None:
    """Is this row new, an update, or a duplicate of one higher up the file?"""
    if s.key == "accounts":
        key = str(row.values.get("code", ""))
        existing = db.scalar(select(Account).where(Account.code == key))
        if existing is not None and existing.is_system:
            row.action = SKIP
            row.note = "already here as a built-in account, left alone"
            return
        row.action = UPDATE if existing else CREATE
    elif s.key in ("customers", "suppliers"):
        key = str(row.values.get("code") or row.values.get("name", "")).lower()
        existing = _find_contact(db, row.values.get("code"), row.values.get("name"))
        row.action = UPDATE if existing else CREATE
    elif s.key == "items":
        key = str(row.values.get("code", "")).lower()
        existing = db.scalar(select(Item).where(func.lower(Item.code) == key))
        row.action = UPDATE if existing else CREATE
    elif s.key == "employees":
        key = str(row.values.get("staff_no", "")).lower()
        existing = db.scalar(
            select(Employee).where(func.lower(Employee.staff_no) == key))
        row.action = UPDATE if existing else CREATE
    elif s.key == "trial_balance":
        key = f"acct-{row.values.get('account')}"
        if not (row.values.get("debit") or row.values.get("credit")):
            row.action = SKIP
            row.note = "no figure on this line"
            return
        row.action = CREATE
    else:                                    # open invoices and bills
        key = f"{row.values.get('contact','')}|{row.values.get('number','')}".lower()
        if not row.values.get("amount"):
            row.action = SKIP
            row.note = "nothing outstanding on this line"
            return
        row.action = CREATE

    if key and key in seen:
        row.action = SKIP
        row.note = "the same thing appears earlier in this file"
    elif key:
        seen.add(key)


def _find_contact(db: Session, code, name) -> Contact | None:
    if code:
        found = db.scalar(select(Contact).where(Contact.code == str(code)))
        if found is not None:
            return found
    if name:
        return db.scalar(
            select(Contact).where(func.lower(Contact.name) == str(name).lower()))
    return None


# --------------------------------------------------------------------------
# Applying it
# --------------------------------------------------------------------------


@dataclass
class Result:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return self.created + self.updated

    def summary(self, s: Sheet) -> str:
        bits = []
        if self.created:
            bits.append(f"{self.created} added")
        if self.updated:
            bits.append(f"{self.updated} updated")
        if self.skipped:
            bits.append(f"{self.skipped} skipped")
        if self.failed:
            bits.append(f"{self.failed} could not be brought in")
        return f"{s.title}: " + (", ".join(bits) if bits else "nothing to do")


def apply(db: Session, preview: Preview, user=None, *, on: date | None = None) -> Result:
    """Write the rows that passed. The bad ones are left for the person to fix."""
    s = preview.sheet
    result = Result(failed=len(preview.bad))
    handler = {
        "accounts": _apply_accounts,
        "customers": lambda *a: _apply_contacts(*a, customer=True),
        "suppliers": lambda *a: _apply_contacts(*a, customer=False),
        "items": _apply_items,
        "employees": _apply_employees,
        "trial_balance": lambda d, p, r, u: _apply_trial_balance(d, p, r, u, on),
        "open_invoices": lambda d, p, r, u: _apply_open_documents(d, p, r, u, sales=True),
        "open_bills": lambda d, p, r, u: _apply_open_documents(d, p, r, u, sales=False),
    }[s.key]
    handler(db, preview, result, user)
    return result


def _apply_accounts(db: Session, preview: Preview, result: Result, user) -> None:
    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        code = row.values["code"]
        account = db.scalar(select(Account).where(Account.code == code))
        if account is None:
            account = Account(code=code, name=row.values["name"],
                              type=row.values["type"])
            db.add(account)
            result.created += 1
        else:
            # A live account's type is never changed by an import: entries are
            # already sitting in it, and moving it between asset and expense
            # would silently rewrite the accounts.
            if account.type != row.values["type"]:
                result.messages.append(
                    f"Line {row.number}: {code} is already an "
                    f"{account.type.lower()} account, so its type was left alone."
                )
            account.name = row.values["name"]
            result.updated += 1
        if row.values.get("description"):
            account.description = row.values["description"]
    db.flush()


def _apply_contacts(db: Session, preview: Preview, result: Result, user,
                    *, customer: bool) -> None:
    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        contact = _find_contact(db, row.values.get("code"), row.values.get("name"))
        if contact is None:
            contact = Contact(
                code=row.values.get("code") or next_number(db, "CONTACT"),
                name=row.values["name"],
            )
            db.add(contact)
            result.created += 1
        else:
            result.updated += 1
        # A business can be both. Importing a customer list must never stop
        # somebody also being a supplier.
        if customer:
            contact.is_customer = True
        else:
            contact.is_vendor = True
        for key in ("contact_person", "email", "phone", "address", "city",
                    "state", "tin", "notes"):
            if row.values.get(key):
                setattr(contact, key, row.values[key])
        if row.values.get("payment_terms_days") is not None:
            contact.payment_terms_days = row.values["payment_terms_days"]
        if row.values.get("credit_limit"):
            contact.credit_limit = row.values["credit_limit"]
    db.flush()


def _apply_items(db: Session, preview: Preview, result: Result, user) -> None:
    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        code = row.values["code"]
        item = db.scalar(select(Item).where(func.lower(Item.code) == code.lower()))
        if item is None:
            item = Item(code=code, name=row.values["name"])
            db.add(item)
            result.created += 1
        else:
            result.updated += 1
        item.name = row.values["name"]
        kind = row.values.get("item_type") or STOCK_ITEM
        item.item_type = kind
        item.track_stock = kind == STOCK_ITEM
        for key in ("unit", "category", "barcode"):
            if row.values.get(key):
                setattr(item, key, row.values[key])
        for key in ("sale_price", "purchase_price", "reorder_level"):
            if row.values.get(key):
                setattr(item, key, row.values[key])
    db.flush()


def _apply_employees(db: Session, preview: Preview, result: Result, user) -> None:
    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        staff_no = row.values["staff_no"]
        person = db.scalar(
            select(Employee).where(func.lower(Employee.staff_no) == staff_no.lower()))
        if person is None:
            person = Employee(staff_no=staff_no,
                              first_name=row.values["first_name"],
                              last_name=row.values["last_name"])
            db.add(person)
            result.created += 1
        else:
            result.updated += 1
        for key in ("first_name", "last_name", "job_title", "department", "email",
                    "phone", "bank_name", "bank_account_no", "tin", "hire_date"):
            if row.values.get(key):
                setattr(person, key, row.values[key])
        for key in ("basic", "housing", "transport"):
            if row.values.get(key):
                setattr(person, key, row.values[key])
        if row.values.get("frequency"):
            person.frequency = row.values["frequency"]
        if not person.bank_account_name:
            person.bank_account_name = f"{person.first_name} {person.last_name}".strip()
    db.flush()


def _apply_trial_balance(db: Session, preview: Preview, result: Result, user,
                         on: date | None) -> None:
    """One journal for the whole trial balance, replacing any earlier one.

    Whatever the file does not balance by goes to Opening Balances, which is
    exactly where an accountant would put it — and the amount is reported, so
    nobody has to wonder whether the file was complete.
    """
    from ..models import JournalEntry
    from .posting import reverse_entry

    on = on or date.today()
    previous = db.scalar(select(JournalEntry).where(
        JournalEntry.source == "OPENING", JournalEntry.is_void.is_(False)))
    if previous is not None:
        reverse_entry(db, previous, on=on, user=user,
                      memo="Replaced by an imported trial balance")

    draft = EntryDraft(date=on, memo="Opening balances (imported)",
                       source="OPENING", reference="OPENING")
    debits = credits = 0
    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        account = db.get(Account, row.values["account"])
        debit = row.values.get("debit") or 0
        credit = row.values.get("credit") or 0
        if debit:
            draft.debit(account, debit)
            debits += debit
        if credit:
            draft.credit(account, credit)
            credits += credit
        result.created += 1

    difference = debits - credits
    if difference:
        opening = sys_account(db, "OPENING_EQUITY")
        if difference > 0:
            draft.credit(opening, difference)
        else:
            draft.debit(opening, -difference)
        result.messages.append(
            f"The file was out by {fmt(abs(difference))}, so that much went to "
            f"{opening.code} {opening.name}. If your old trial balance did "
            "balance, a line is missing from the file."
        )

    if not draft.lines:
        result.messages.append("Nothing on that file had a figure against it.")
        return
    post_entry(db, draft, user=user)


def _apply_open_documents(db: Session, preview: Preview, result: Result, user,
                          *, sales: bool) -> None:
    """Unpaid invoices or bills, posted against Opening Balances."""
    opening = sys_account(db, "OPENING_EQUITY")

    for row in preview.good:
        if row.action == SKIP:
            result.skipped += 1
            continue
        name = str(row.values["contact"]).strip()
        contact = _find_contact(db, None, name)
        if contact is None:
            contact = Contact(code=next_number(db, "CONTACT"), name=name)
            db.add(contact)
            db.flush()
            result.messages.append(
                f"Line {row.number}: {name} was not on file, so a "
                f"{'customer' if sales else 'supplier'} record was made.")
        if sales:
            contact.is_customer = True
        else:
            contact.is_vendor = True

        on = row.values["date"]
        due = row.values.get("due_date") or on
        amount = row.values["amount"]
        note = row.values.get("description") or "Balance brought forward"

        if sales:
            doc = Invoice(
                number=row.values.get("number") or next_number(db, "INVOICE"),
                doc_type="INVOICE", contact_id=contact.id, date=on, due_date=due,
                status=DRAFT, memo="Brought in from your previous system",
            )
            db.add(doc)
            db.flush()
            db.add(InvoiceLine(invoice_id=doc.id, line_no=1, description=note,
                               qty=1000, unit_price=amount,
                               account_id=opening.id, tax_code_id=None))
            db.flush()
            db.refresh(doc)
            from . import documents

            documents.recalc_invoice(db, doc)
            documents.post_invoice(db, doc, user=user)
        else:
            doc = Bill(
                number=row.values.get("number") or next_number(db, "BILL"),
                doc_type="BILL", contact_id=contact.id, date=on, due_date=due,
                status=DRAFT, memo="Brought in from your previous system",
            )
            db.add(doc)
            db.flush()
            db.add(BillLine(bill_id=doc.id, line_no=1, description=note,
                            qty=1000, unit_price=amount,
                            account_id=opening.id, tax_code_id=None))
            db.flush()
            db.refresh(doc)
            from . import documents

            documents.recalc_bill(db, doc)
            documents.post_bill(db, doc, user=user)
        result.created += 1
    db.flush()
