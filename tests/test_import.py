"""Moving a business in from whatever it used before.

The failure that matters here is not a crash — it is an import that half worked,
or that silently dropped a column, or that brought a year's sales in twice. So
these tests care less about the happy path and more about what the machinery
refuses to do.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-imp-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Account,
    Bill,
    Contact,
    Employee,
    Invoice,
    Item,
    JournalEntry,
)
from app.money import to_minor as M  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import importer as I  # noqa: E402
from app.services import reports as R  # noqa: E402
from app.services.posting import account_by_code  # noqa: E402


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-imp-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    with dbmod.session_scope_for(ref.slug) as session:
        yield session
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def bring_in(db, key: str, text: str, *, on=None) -> I.Result:
    preview = I.read(db, key, text.encode())
    return I.apply(db, preview, on=on)


# --------------------------------------------------------------------------
# Reading whatever file turned up
# --------------------------------------------------------------------------


def test_a_plain_comma_file_is_read():
    rows = I.sniff("a,b,c\n1,2,3\n")
    assert rows == [["a", "b", "c"], ["1", "2", "3"]]


def test_semicolons_and_tabs_are_read_too():
    """Excel writes semicolons wherever the decimal point is a comma."""
    assert I.sniff("a;b;c\n1;2;3")[1] == ["1", "2", "3"]
    assert I.sniff("a\tb\tc\n1\t2\t3")[1] == ["1", "2", "3"]


def test_the_byte_order_mark_excel_adds_is_not_part_of_the_heading():
    text = I.decode("Name,Email\nZenith,z@example.com".encode("utf-8-sig"))
    assert I.sniff(text)[0][0] == "Name"


def test_a_file_saved_in_the_windows_code_page_still_opens():
    assert I.decode("Adé Trading".encode("cp1252")) == "Adé Trading"


def test_blank_lines_in_the_middle_are_ignored():
    assert len(I.sniff("a,b\n1,2\n\n\n3,4\n")) == 3


def test_an_empty_file_says_so():
    with pytest.raises(I.ImportError_):
        I.sniff("   ")


def test_headings_only_is_refused_rather_than_silently_doing_nothing(db):
    with pytest.raises(I.ImportError_) as caught:
        I.read(db, "customers", b"Name,Email\n")
    assert "heading row and nothing under it" in str(caught.value)


# --------------------------------------------------------------------------
# Matching headings by what they mean
# --------------------------------------------------------------------------


def test_a_heading_is_matched_however_it_is_written(db):
    for heading in ("Name", "name", "CUSTOMER NAME", "Customer_Name", "Client Name"):
        preview = I.read(db, "customers", f"{heading}\nZenith Ltd".encode())
        assert not preview.missing_columns, heading
        assert preview.rows[0].values["name"] == "Zenith Ltd"


def test_a_column_nobody_recognises_is_reported_not_dropped_in_silence(db):
    preview = I.read(db, "customers", b"Name,Favourite biscuit\nZenith Ltd,Digestive")
    assert preview.unknown_columns == ["Favourite biscuit"]


def test_a_missing_needed_column_stops_the_whole_thing(db):
    preview = I.read(db, "customers", b"Email\nz@example.com")
    assert preview.missing_columns == ["Name"]
    assert preview.can_apply is False


def test_columns_you_do_not_have_can_simply_be_left_out(db):
    preview = I.read(db, "customers", b"Name\nZenith Ltd")
    assert preview.can_apply
    assert preview.creating == 1


# --------------------------------------------------------------------------
# Reading the values
# --------------------------------------------------------------------------


def test_money_is_read_the_way_the_company_writes_it(db):
    preview = I.read(db, "customers",
                     b"Name,Credit limit\nZenith Ltd,\"5,000,000\"")
    assert preview.rows[0].values["credit_limit"] == M("5,000,000")


def test_dates_are_read_in_whichever_order_the_file_used(db):
    for text in ("2026-06-14", "14/06/2026", "14 Jun 2026", "14.06.2026"):
        preview = I.read(db, "open_invoices",
                         f"Customer,Date,Amount\nZenith,{text},1000".encode())
        assert preview.rows[0].values["date"] == date(2026, 6, 14), text


def test_a_date_nobody_can_read_is_an_error_on_that_line_only(db):
    preview = I.read(
        db, "open_invoices",
        b"Customer,Date,Amount\nZenith,not a date,1000\nKwame,2026-06-14,2000")
    assert len(preview.bad) == 1
    assert len(preview.good) == 1
    assert "Date" in preview.bad[0].errors[0]


def test_a_choice_says_what_it_would_have_accepted(db):
    preview = I.read(db, "accounts", b"Code,Name,Type\n6250,Diesel,Vegetable")
    assert "EXPENSE" in preview.bad[0].errors[0]


def test_the_words_people_actually_use_for_a_type_are_understood(db):
    for word in ("Expense", "expenses", "COST"):
        preview = I.read(db, "accounts", f"Code,Name,Type\n6250,Diesel,{word}".encode())
        assert preview.rows[0].values["type"] == "EXPENSE", word


# --------------------------------------------------------------------------
# Nothing is written until it is confirmed
# --------------------------------------------------------------------------


def test_reading_a_file_writes_nothing(db):
    before = db.scalar(select(func.count(Contact.id)))
    I.read(db, "customers", b"Name\nZenith Ltd\nKwame Motors")
    assert db.scalar(select(func.count(Contact.id))) == before


def test_what_the_preview_promised_is_what_happens(db):
    preview = I.read(db, "customers", b"Name\nZenith Ltd\nKwame Motors")
    assert (preview.creating, preview.updating) == (2, 0)
    result = I.apply(db, preview)
    assert (result.created, result.updated) == (2, 0)


# --------------------------------------------------------------------------
# Customers and suppliers
# --------------------------------------------------------------------------


def test_customers_come_in_with_their_details(db):
    bring_in(db, "customers",
             "Name,Email,Phone,City,Payment terms\n"
             "Zenith Construction Ltd,ap@zenith.example,+234 801,Lagos,45\n")
    c = db.scalar(select(Contact).where(Contact.name == "Zenith Construction Ltd"))
    assert c.is_customer and c.email == "ap@zenith.example"
    assert c.payment_terms_days == 45
    assert c.code, "a code is made when the file has none"


def test_running_the_same_file_twice_updates_rather_than_duplicates(db):
    bring_in(db, "customers", "Name,Phone\nZenith Ltd,111\n")
    result = bring_in(db, "customers", "Name,Phone\nZenith Ltd,222\n")
    assert (result.created, result.updated) == (0, 1)
    assert db.scalar(select(func.count(Contact.id)).where(
        Contact.name == "Zenith Ltd")) == 1
    assert db.scalar(select(Contact).where(Contact.name == "Zenith Ltd")).phone == "222"


def test_the_same_name_twice_in_one_file_is_brought_in_once(db):
    preview = I.read(db, "customers", b"Name\nZenith Ltd\nZenith Ltd")
    assert preview.rows[1].action == I.SKIP
    assert "appears earlier" in preview.rows[1].note
    result = I.apply(db, preview)
    assert result.created == 1 and result.skipped == 1


def test_a_business_that_is_both_customer_and_supplier_stays_both(db):
    """Importing a supplier list must not stop somebody being a customer."""
    bring_in(db, "customers", "Name\nAdeyemi Ltd\n")
    bring_in(db, "suppliers", "Name\nAdeyemi Ltd\n")
    c = db.scalar(select(Contact).where(Contact.name == "Adeyemi Ltd"))
    assert c.is_customer and c.is_vendor


def test_an_empty_optional_cell_does_not_wipe_what_is_already_there(db):
    bring_in(db, "customers", "Name,Phone\nZenith Ltd,08011111111\n")
    bring_in(db, "customers", "Name,Phone\nZenith Ltd,\n")
    assert db.scalar(select(Contact).where(
        Contact.name == "Zenith Ltd")).phone == "08011111111"


# --------------------------------------------------------------------------
# The chart of accounts
# --------------------------------------------------------------------------


def test_a_new_account_is_added(db):
    bring_in(db, "accounts", "Code,Name,Type\n6250,Generator diesel,Expense\n")
    account = account_by_code(db, "6250")
    assert account.name == "Generator diesel" and account.type == "EXPENSE"


def test_a_built_in_account_is_left_completely_alone(db):
    """Renaming 1020 Bank from a spreadsheet would break the system keys."""
    preview = I.read(db, "accounts", b"Code,Name,Type\n1020,Something else,Asset")
    assert preview.rows[0].action == I.SKIP
    I.apply(db, preview)
    assert account_by_code(db, "1020").name != "Something else"


def test_a_live_accounts_type_is_never_changed_by_a_spreadsheet(db):
    """Entries are already sitting in it. Moving it would rewrite the accounts."""
    bring_in(db, "accounts", "Code,Name,Type\n6250,Diesel,Expense\n")
    result = bring_in(db, "accounts", "Code,Name,Type\n6250,Diesel,Asset\n")
    assert account_by_code(db, "6250").type == "EXPENSE"
    assert any("left alone" in m for m in result.messages)


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


def test_items_come_in_with_their_prices(db):
    bring_in(db, "items",
             "SKU,Description,Type,Unit,Price,Cost\n"
             "CEM-50,Dangote cement 50kg,Stock,bag,\"9,500\",\"8,200\"\n")
    item = db.scalar(select(Item).where(Item.code == "CEM-50"))
    assert item.sale_price == M("9,500") and item.purchase_price == M("8,200")
    assert item.track_stock is True


def test_a_service_is_not_given_a_stock_balance(db):
    bring_in(db, "items", "Code,Name,Type\nDEL,Delivery,Service\n")
    assert db.scalar(select(Item).where(Item.code == "DEL")).track_stock is False


# --------------------------------------------------------------------------
# Opening balances
# --------------------------------------------------------------------------


def test_a_trial_balance_is_posted_as_one_journal(db):
    bring_in(db, "trial_balance",
             "Account,Debit,Credit\n"
             "1020,\"2,400,000\",\n"
             "3000,,\"2,400,000\"\n", on=date(2026, 1, 1))
    rows, debits, credits = R.trial_balance(db, None, date(2026, 12, 31))
    assert debits == credits == M("2,400,000")


def test_a_trial_balance_that_does_not_balance_is_squared_off_and_said_so(db):
    result = bring_in(db, "trial_balance",
                      "Account,Debit,Credit\n1020,\"2,400,000\",\n",
                      on=date(2026, 1, 1))
    assert any("out by" in m for m in result.messages)
    _rows, debits, credits = R.trial_balance(db, None, date(2026, 12, 31))
    assert debits == credits


def test_uploading_a_corrected_trial_balance_replaces_the_first_one(db):
    bring_in(db, "trial_balance",
             "Account,Debit,Credit\n1020,\"2,400,000\",\n3000,,\"2,400,000\"\n",
             on=date(2026, 1, 1))
    bring_in(db, "trial_balance",
             "Account,Debit,Credit\n1020,\"1,000,000\",\n3000,,\"1,000,000\"\n",
             on=date(2026, 1, 1))
    bank = account_by_code(db, "1020")
    rows, _d, _c = R.trial_balance(db, None, date(2026, 12, 31))
    by_id = {r.account.id: r.debit - r.credit for r in rows}
    assert by_id[bank.id] == M("1,000,000")


def test_an_account_the_file_names_that_does_not_exist_is_an_error_not_a_guess(db):
    preview = I.read(db, "trial_balance", b"Account,Debit\n9999,1000")
    assert preview.bad and "no account" in preview.bad[0].errors[0]


def test_an_account_can_be_named_instead_of_coded(db):
    account = account_by_code(db, "1020")
    preview = I.read(db, "trial_balance",
                     f"Account,Debit\n{account.name},1000".encode())
    assert preview.good[0].values["account"] == account.id


# --------------------------------------------------------------------------
# Unpaid invoices and bills
# --------------------------------------------------------------------------


def test_unpaid_invoices_come_in_as_real_invoices(db):
    bring_in(db, "open_invoices",
             "Customer,Invoice no,Date,Due date,Amount\n"
             "Zenith Ltd,INV-2025-0413,2026-06-14,2026-07-14,\"1,075,000\"\n")
    inv = db.scalar(select(Invoice).where(Invoice.number == "INV-2025-0413"))
    assert inv.total == M("1,075,000")
    assert inv.balance_due == M("1,075,000")


def test_they_show_up_in_the_ageing_from_the_first_day(db):
    bring_in(db, "open_invoices",
             "Customer,Date,Amount\nZenith Ltd,2026-06-14,\"1,000,000\"\n")
    rows, buckets, total = R.aging(db, date(2026, 8, 31), receivable=True)
    assert total == M("1,000,000")
    assert rows[0].contact.name == "Zenith Ltd"


def test_bringing_in_last_years_invoices_does_not_inflate_this_years_sales(db):
    """The sale was counted in the old system. Counting it again is a lie."""
    bring_in(db, "open_invoices",
             "Customer,Date,Amount\nZenith Ltd,2026-06-14,\"1,000,000\"\n")
    pl = R.profit_and_loss(db, date(2026, 1, 1), date(2026, 12, 31))
    assert pl.revenue.total == 0
    assert pl.net_profit == 0


def test_the_other_side_lands_in_opening_balances(db):
    bring_in(db, "open_invoices",
             "Customer,Date,Amount\nZenith Ltd,2026-06-14,\"1,000,000\"\n")
    opening = db.scalar(select(Account).where(Account.system_key == "OPENING_EQUITY"))
    from app.services.posting import account_net

    # Equity's natural side is credit, so a positive net here is the credit
    # the invoice put there rather than income for this year.
    assert account_net(db, opening.id) == M("1,000,000")


def test_a_customer_not_yet_on_file_is_created_and_reported(db):
    result = bring_in(db, "open_invoices",
                      "Customer,Date,Amount\nBrand New Ltd,2026-06-14,1000\n")
    assert db.scalar(select(Contact).where(Contact.name == "Brand New Ltd")) is not None
    assert any("was not on file" in m for m in result.messages)


def test_unpaid_bills_come_in_the_same_way(db):
    bring_in(db, "open_bills",
             "Supplier,Bill no,Date,Amount\n"
             "Dangote Cement,DCP-88214,2026-06-20,\"4,300,000\"\n")
    bill = db.scalar(select(Bill).where(Bill.number == "DCP-88214"))
    assert bill.total == M("4,300,000")
    assert db.scalar(select(Contact).where(
        Contact.name == "Dangote Cement")).is_vendor


def test_the_books_balance_after_a_whole_move_in(db):
    """The test that matters: bring everything in, and the accounts still add up."""
    bring_in(db, "accounts", "Code,Name,Type\n6250,Generator diesel,Expense\n")
    bring_in(db, "customers", "Name\nZenith Ltd\nKwame Motors\n")
    bring_in(db, "suppliers", "Name\nDangote Cement\n")
    bring_in(db, "items", "Code,Name,Price\nCEM-50,Cement,\"9,500\"\n")
    bring_in(db, "trial_balance",
             "Account,Debit,Credit\n1020,\"5,000,000\",\n3000,,\"5,000,000\"\n",
             on=date(2026, 1, 1))
    bring_in(db, "open_invoices",
             "Customer,Date,Amount\nZenith Ltd,2026-01-05,\"1,000,000\"\n")
    bring_in(db, "open_bills",
             "Supplier,Date,Amount\nDangote Cement,2026-01-06,\"400,000\"\n")

    _rows, debits, credits = R.trial_balance(db, None, date(2026, 12, 31))
    assert debits == credits
    assert R.balance_sheet(db, date(2026, 12, 31)).difference == 0


# --------------------------------------------------------------------------
# Employees
# --------------------------------------------------------------------------


def test_employees_come_in_with_their_pay(db):
    bring_in(db, "employees",
             "Staff no,First name,Last name,Basic,Housing,Paid\n"
             "EMP-001,Adaeze,Okafor,\"450,000\",\"150,000\",Monthly\n")
    person = db.scalar(select(Employee).where(Employee.staff_no == "EMP-001"))
    assert person.basic == M("450,000") and person.frequency == "MONTHLY"
    assert person.bank_account_name == "Adaeze Okafor"


# --------------------------------------------------------------------------
# The templates offered for download
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(I.SHEETS))
def test_every_blank_template_can_be_read_back_by_its_own_importer(db, key):
    """A template that its own reader rejects would be a cruel joke."""
    preview = I.read(db, key, I.sheet(key).template().encode())
    assert not preview.missing_columns, key
    assert not preview.unknown_columns, key


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def test_the_import_page_lists_everything_in_order(client):
    page = client.get("/import", follow_redirects=True).text
    assert "Chart of accounts" in page
    assert "Unpaid customer invoices" in page


def test_a_template_can_be_downloaded(client):
    r = client.get("/import/customers/template", follow_redirects=True)
    assert r.status_code == 200
    assert "Name" in r.text
    assert "attachment" in r.headers.get("content-disposition", "")


def test_uploading_shows_a_preview_and_saves_nothing(client):
    r = client.post("/import/customers", files={
        "file": ("customers.csv", b"Name,Email\nZenith Ltd,z@example.com", "text/csv")},
        follow_redirects=True)
    assert r.status_code == 200
    assert "what this would do" in r.text.lower()
    with dbmod.session_scope_for(registry.default_slug()) as s:
        assert s.scalar(select(Contact).where(Contact.name == "Zenith Ltd")) is None


def test_confirming_brings_it_in(client):
    import re

    r = client.post("/import/customers", files={
        "file": ("customers.csv", b"Name\nZenith Ltd", "text/csv")},
        follow_redirects=True)
    token = re.search(r'name="token" value="([0-9a-f]{32})"', r.text).group(1)

    r = client.post("/import/customers", data={"token": token},
                    follow_redirects=True)   # wrong URL on purpose is not this test
    r = client.post("/import/customers/apply", data={"token": token},
                    follow_redirects=True)
    assert "1 added" in r.text
    with dbmod.session_scope_for(registry.default_slug()) as s:
        assert s.scalar(select(Contact).where(Contact.name == "Zenith Ltd")) is not None


def test_a_made_up_token_cannot_reach_outside_the_scratch_folder(client):
    for token in ("../../../etc/passwd", "..%2f..%2fsecret", "zz" * 16, ""):
        r = client.post("/import/customers/apply", data={"token": token},
                        follow_redirects=True)
        assert r.status_code == 200
        assert "expired" in r.text.lower()


def test_uploading_nothing_says_so_rather_than_failing(client):
    r = client.post("/import/customers",
                    files={"file": ("empty.csv", b"", "text/csv")},
                    follow_redirects=True)
    assert "Choose a file first" in r.text
