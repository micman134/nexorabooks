"""Reading a bank statement, matching it, and posting what a person confirmed.

Three layers, and the middle one is where the money is.

**Reading.** Banks export in every shape there is. These tests feed the reader
the shapes real banks actually produce — preamble rows above the header, debit
and credit as two columns, one signed column, European decimals, a separate
D/C marker, no header at all, OFX — and check it works out the same
transactions from each.

**Matching.** The test that matters most is
``test_a_payment_already_in_the_books_is_not_recorded_twice``. If the software
ever imports a receipt somebody already entered by hand, the customer is
credited twice and the bank balance doubles. Everything else here is
convenience; that one is correctness.

**Posting.** Nothing may reach the ledger without a person confirming that
line, every posting must leave the books balanced, and the same file imported
twice must add nothing the second time.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-bank-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ACTION_CLEAR,
    ACTION_IGNORE,
    ACTION_PAYMENT,
    ACTION_POST,
    ACTION_RECEIPT,
    CONFIRMED,
    DRAFT,
    IGNORED,
    Account,
    BankAccount,
    Bill,
    BillLine,
    Contact,
    Invoice,
    InvoiceLine,
    JournalLine,
    PayeeRule,
)
from app.money import fmt  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import bankimport as BI  # noqa: E402
from app.services import charts, documents, matching, reports, statements  # noqa: E402
from app.services.importer import ImportError_  # noqa: E402
from app.services.posting import (  # noqa: E402
    EntryDraft,
    account_net,
    next_number,
    post_entry,
    sys_account,
)


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-bank-")
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def acc(db, key):
    return sys_account(db, key)


def bank_of(db) -> BankAccount:
    return db.scalar(select(BankAccount))


def customer(db, name="Dangote Cement Plc"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def supplier(db, name="Lagos Supplies Ltd"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def invoice(db, cust, on, amount):
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=cust.id, date=on, due_date=on + timedelta(days=30),
                  status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Goods", qty=1000,
                       unit_price=amount, account_id=acc(db, "SALES").id))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    db.flush()
    return inv


def bill(db, vend, on, amount):
    b = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vend.id,
             date=on, due_date=on + timedelta(days=30), status=DRAFT)
    db.add(b)
    db.flush()
    db.add(BillLine(bill_id=b.id, line_no=1, description="Materials", qty=1000,
                    unit_price=amount, account_id=acc(db, "PURCHASES").id))
    db.flush()
    db.refresh(b)
    documents.recalc_bill(db, b)
    documents.post_bill(db, b)
    db.flush()
    return b


def line(when, description, amount, **kw):
    return statements.Line(row=1, date=when, description=description, amount=amount, **kw)


def books_balance(db) -> bool:
    _rows, td, tc = reports.trial_balance(db, None, date(2030, 1, 1))
    bs = reports.balance_sheet(db, date(2030, 1, 1))
    return td == tc and bs.difference == 0


def bring_in(db, csv: bytes):
    reading = statements.read(csv)
    outcome = BI.create(db, bank_of(db).id, reading, "test.csv")
    db.flush()
    BI.run_matching(db, outcome.batch)
    db.flush()
    return outcome


# --------------------------------------------------------------------------
# Reading: the shapes real banks export
# --------------------------------------------------------------------------

NIGERIAN = b"""Account Statement
Account Number: 0123456789

Trans Date,Value Date,Narration,Debit,Credit,Balance
01/07/2026,01/07/2026,BALANCE BROUGHT FORWARD,,,1500000.00
03/07/2026,03/07/2026,TRF FROM DANGOTE CEMENT PLC,,850000.00,2350000.00
05/07/2026,05/07/2026,POS PURCHASE SHOPRITE,45500.00,,2304500.00
"""


def test_a_typical_bank_export_reads_correctly():
    r = statements.read(NIGERIAN)
    assert len(r.lines) == 2
    assert [x.amount for x in r.lines] == [85_000_000, -4_550_000]
    assert r.opening_balance == 150_000_000
    assert r.closing_balance == 230_450_000
    assert r.opening_balance + r.net == r.closing_balance
    assert not r.problems


def test_the_value_date_column_is_not_mistaken_for_the_amount():
    """"valuedate" starts with "value", which is also an amount name.

    Reading it as the amount produces a statement where every figure is a
    date — and it looks perfectly plausible until somebody checks a total.
    """
    r = statements.read(NIGERIAN)
    assert r.columns["date"] == "Trans Date"
    assert "amount" not in r.columns          # this file has debit/credit instead
    assert r.columns["debit"] == "Debit"


def test_a_single_signed_column_with_currency_symbols():
    r = statements.read(b'''Date,Description,Amount,Running Balance
07/15/2026,"ACME CORP","$1,250.00","$3,250.00"
07/16/2026,"RENT","-$800.00","$2,450.00"
''')
    assert r.date_order == "mdy"
    assert [x.amount for x in r.lines] == [125_000, -80_000]


def test_european_decimals_and_semicolons():
    r = statements.read("""Datum;Beschreibung;Betrag;Saldo
01.07.2026;Miete;-1.250,00;3.750,00
15.07.2026;Zahlung;2.400,50;6.150,50
""".encode())
    assert [x.amount for x in r.lines] == [-125_000, 240_050]
    assert r.columns["amount"] == "Betrag"


def test_a_separate_debit_credit_marker():
    r = statements.read(b"""Date,Narration,Type,Amount
03/07/2026,Inflow,CR,850000.00
05/07/2026,Card,DR,45500.00
""")
    assert [x.amount for x in r.lines] == [85_000_000, -4_550_000]


def test_a_file_with_no_headings_is_read_from_its_contents():
    r = statements.read(b"""03/07/2026,TRF FROM ACME,850000.00,2350000.00
05/07/2026,POS SHOPRITE,-45500.00,2304500.00
""")
    assert len(r.lines) == 2
    assert r.problems and "no column headings" in r.problems[0]


def test_ofx_is_read_without_guessing_anything():
    r = statements.read(b"""OFXHEADER:100
<OFX><BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260703120000<TRNAMT>850000.00<FITID>X1<NAME>DANGOTE</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260705<TRNAMT>-45500.00<FITID>X2<NAME>SHOPRITE</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>2304500.00</LEDGERBAL></OFX>""")
    assert r.format == "ofx"
    assert not r.date_ambiguous
    assert [x.amount for x in r.lines] == [85_000_000, -4_550_000]
    assert r.lines[0].reference == "X1"


def test_signs_the_wrong_way_round_are_caught_by_the_running_balance():
    """The most valuable check in the reader.

    A file whose columns are labelled the other way round reads as a business
    that spent everything it earned. The running balance proves what happened.
    """
    r = statements.read(b"""Date,Description,Amount,Balance
05/07/2026,Rent paid,500.00,4500.00
09/07/2026,Customer paid,-1000.00,5500.00
""")
    assert [x.amount for x in r.lines] == [-50_000, 100_000]
    assert r.problems and "other way round" in r.problems[0]


def test_dates_that_could_be_read_either_way_are_declared_not_guessed():
    r = statements.read(b"""Date,Description,Amount
01/02/2026,Payment,100.00
03/04/2026,Rent,-50.00
""")
    assert r.date_ambiguous
    forced = statements.read(b"""Date,Description,Amount
01/02/2026,Payment,100.00
""", date_order="mdy")
    assert forced.lines[0].date == date(2026, 1, 2)


def test_a_day_over_twelve_settles_the_order_for_the_whole_file():
    r = statements.read(b"""Date,Description,Amount
25/03/2026,A,100.00
01/02/2026,B,100.00
""")
    assert r.date_order == "dmy" and not r.date_ambiguous
    assert r.lines[1].date == date(2026, 2, 1)


def test_files_that_are_not_statements_are_refused_clearly():
    for bad, message in [(b"", "empty"),
                         (b"Hello,World\nnot,a,statement\n", "date column")]:
        with pytest.raises(ImportError_, match=message):
            statements.read(bad)


def test_amount_parsing_handles_what_banks_write():
    for text, want in [("1,250.50", 125_050), ("1.250,50", 125_050),
                       ("(800.00)", -80_000), ("800.00-", -80_000),
                       ("₦1,250.00", 125_000), ("500.00 DR", -50_000),
                       ("500.00 CR", 50_000), ("", None), ("n/a", None)]:
        assert statements.parse_amount(text) == want, text


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def test_an_exact_invoice_with_the_name_on_it_is_recognised(db):
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 6, 10), 85_000_000)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), inv.total,
                           "TRF FROM DANGOTE CEMENT PLC")
    assert got.action == ACTION_RECEIPT
    assert got.document_ids == [inv.id]
    assert got.strong
    assert "Exactly settles" in got.why


def test_the_invoice_number_quoted_on_the_line_raises_the_score(db):
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 6, 10), 85_000_000)
    db.commit()

    without = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), inv.total, "TRF INFLOW")
    with_number = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), inv.total,
                                   f"TRF INFLOW {inv.number}")
    assert with_number.score > without.score
    assert "number is quoted" in with_number.why


def test_one_payment_covering_two_invoices_is_found(db):
    cust = customer(db)
    one = invoice(db, cust, date(2026, 6, 10), 30_000_000)
    two = invoice(db, cust, date(2026, 6, 20), 20_000_000)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3),
                           one.total + two.total, "PAYMENT DANGOTE CEMENT PLC")
    assert got.action == ACTION_RECEIPT
    assert set(got.document_ids) == {one.id, two.id}


def test_arithmetic_alone_is_treated_as_weak_evidence(db):
    """Some subset happening to add up is not proof of anything."""
    cust = customer(db, "Quiet Ltd")
    one = invoice(db, cust, date(2026, 6, 10), 30_000_000)
    two = invoice(db, cust, date(2026, 6, 20), 20_000_000)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3),
                           one.total + two.total, "INFLOW 99887766")
    assert not got.strong
    assert "check this one" in got.why


def test_a_bill_being_paid_is_recognised(db):
    vend = supplier(db)
    b = bill(db, vend, date(2026, 6, 10), 40_000_000)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), -b.total,
                           "PAYMENT TO LAGOS SUPPLIES LTD")
    assert got.action == ACTION_PAYMENT
    assert got.document_ids == [b.id]


def test_an_invoice_not_yet_raised_is_never_matched(db):
    """You cannot pay an invoice that does not exist yet on the statement date."""
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 8, 20), 85_000_000)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), inv.total,
                           "TRF FROM DANGOTE CEMENT PLC")
    assert got.action != ACTION_RECEIPT


def test_a_payment_already_in_the_books_is_recognised_rather_than_repeated(db):
    """The check that stops the same money being recorded twice."""
    entry = None
    draft = EntryDraft(date=date(2026, 7, 5), memo="Office rent July")
    draft.debit(acc(db, "RENT"), 45_000_000)
    draft.credit(db.get(Account, bank_of(db).account_id), 45_000_000)
    entry = post_entry(db, draft)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 6),
                           -45_000_000, "OFFICE RENT JULY")
    assert got.action == ACTION_CLEAR
    assert got.strong
    assert "Already in your books" in got.why
    assert "not record it again" in got.why


def test_an_entry_already_ticked_off_is_not_offered_again(db):
    draft = EntryDraft(date=date(2026, 7, 5), memo="Rent")
    draft.debit(acc(db, "RENT"), 45_000_000)
    draft.credit(db.get(Account, bank_of(db).account_id), 45_000_000)
    entry = post_entry(db, draft)
    for row in db.scalars(select(JournalLine).where(JournalLine.entry_id == entry.id)):
        if row.account_id == bank_of(db).account_id:
            row.cleared = True
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 6), -45_000_000, "RENT")
    assert got.action != ACTION_CLEAR


def test_two_statement_lines_cannot_claim_the_same_invoice(db):
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 6, 10), 85_000_000)
    db.commit()

    lines = [line(date(2026, 7, 3), "TRF FROM DANGOTE CEMENT PLC", inv.total),
             line(date(2026, 7, 9), "TRF FROM DANGOTE CEMENT PLC", inv.total)]
    got = matching.suggest_all(db, bank_of(db).id, lines)
    claimed = [s for s in got if inv.id in s.document_ids]
    assert len(claimed) == 1
    other = [s for s in got if inv.id not in s.document_ids][0]
    assert "better match" in other.why


def test_an_unknown_line_says_so_plainly(db):
    got = matching.suggest(db, bank_of(db).id, date(2026, 7, 3), -2_500_000,
                           "SOMETHING NOBODY HAS EVER SEEN")
    assert not got.action
    assert got.score == 0
    assert "Tell it what this was" in got.why


def test_the_wording_of_a_bank_line_is_reduced_to_what_matters():
    assert matching.words("TRF FROM DANGOTE CEMENT PLC/REF 998877") == ["dangote", "cement"]
    assert matching.document_numbers("PAYMENT INV-0012 REF") == ["INV-0012"]
    assert matching._name_score("PAYMENT FROM DANGOTE CEM PLC", "Dangote Cement Plc") == 1.0
    assert matching._name_score("SOMETHING ELSE", "Dangote Cement Plc") == 0.0


# --------------------------------------------------------------------------
# Posting what was confirmed
# --------------------------------------------------------------------------


def test_confirming_a_receipt_settles_the_invoice_and_balances(db):
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 6, 10), 85_000_000)
    db.commit()
    csv = f"""Date,Description,Amount
03/07/2026,TRF FROM DANGOTE CEMENT PLC,{inv.total / 100:.2f}
""".encode()
    outcome = bring_in(db, csv)
    db.commit()

    row = outcome.batch.lines[0]
    BI.apply(db, row, ACTION_RECEIPT)
    db.commit()

    db.refresh(inv)
    assert inv.balance_due == 0
    assert inv.status == "PAID"
    assert row.status == CONFIRMED and row.payment_id
    assert books_balance(db)


def test_confirming_an_already_recorded_line_posts_nothing_new(db):
    """The one that matters: the rent must appear once, not twice."""
    draft = EntryDraft(date=date(2026, 7, 5), memo="Office rent July")
    draft.debit(acc(db, "RENT"), 45_000_000)
    draft.credit(db.get(Account, bank_of(db).account_id), 45_000_000)
    post_entry(db, draft)
    db.commit()
    before = account_net(db, acc(db, "RENT").id, None, date(2030, 1, 1))

    outcome = bring_in(db, b"""Date,Description,Amount
06/07/2026,OFFICE RENT JULY,-450000.00
""")
    db.commit()
    row = outcome.batch.lines[0]
    assert row.action == ACTION_CLEAR

    BI.apply(db, row, ACTION_CLEAR)
    db.commit()

    assert account_net(db, acc(db, "RENT").id, None, date(2030, 1, 1)) == before
    assert db.get(JournalLine, row.journal_line_id).cleared
    assert books_balance(db)


def test_clearing_refuses_an_entry_for_a_different_amount(db):
    draft = EntryDraft(date=date(2026, 7, 5), memo="Rent")
    draft.debit(acc(db, "RENT"), 45_000_000)
    draft.credit(db.get(Account, bank_of(db).account_id), 45_000_000)
    entry = post_entry(db, draft)
    bank_line = next(r for r in db.scalars(
        select(JournalLine).where(JournalLine.entry_id == entry.id))
        if r.account_id == bank_of(db).account_id)
    db.commit()

    outcome = bring_in(db, b"""Date,Description,Amount
06/07/2026,SOMETHING ELSE,-999999.00
""")
    db.commit()
    with pytest.raises(BI.ImportProblem, match="not for the same amount"):
        BI.apply(db, outcome.batch.lines[0], ACTION_CLEAR,
                 journal_line_id=bank_line.id)


def test_posting_a_cost_straight_to_an_account_balances(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,MTN NIGERIA DATA BUNDLE,-25000.00
""")
    db.commit()
    row = outcome.batch.lines[0]
    BI.apply(db, row, ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)
    db.commit()

    assert row.status == CONFIRMED and row.entry_id
    assert account_net(db, acc(db, "BANK_CHARGES").id, None, date(2030, 1, 1)) == 2_500_000
    assert books_balance(db)


def test_a_posted_line_is_ticked_off_for_reconciliation(db):
    """Otherwise every imported transaction sits on the next reconciliation."""
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,MTN NIGERIA DATA BUNDLE,-25000.00
""")
    db.commit()
    row = outcome.batch.lines[0]
    BI.apply(db, row, ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)
    db.commit()
    assert db.get(JournalLine, row.journal_line_id).cleared


def test_posting_to_the_bank_account_itself_is_refused(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,SOMETHING,-25000.00
""")
    db.commit()
    with pytest.raises(BI.ImportProblem, match="bank account itself"):
        BI.apply(db, outcome.batch.lines[0], ACTION_POST,
                 account_id=bank_of(db).account_id)


def test_a_receipt_cannot_be_made_from_money_going_out(db):
    cust = customer(db)
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,PAYMENT OUT,-25000.00
""")
    db.commit()
    with pytest.raises(BI.ImportProblem, match="cannot be a receipt"):
        BI.apply(db, outcome.batch.lines[0], ACTION_RECEIPT, contact_id=cust.id)


def test_leaving_a_line_out_posts_nothing(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,TRANSFER TO MY OTHER ACCOUNT,-25000.00
""")
    db.commit()
    row = outcome.batch.lines[0]
    BI.apply(db, row, ACTION_IGNORE)
    db.commit()
    assert row.status == IGNORED
    assert row.entry_id is None and row.payment_id is None
    assert books_balance(db)


def test_a_line_cannot_be_confirmed_twice(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,MTN DATA,-25000.00
""")
    db.commit()
    row = outcome.batch.lines[0]
    BI.apply(db, row, ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)
    db.commit()
    with pytest.raises(BI.ImportProblem, match="already been dealt with"):
        BI.apply(db, row, ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)


def test_confirming_without_saying_what_it_was_is_refused(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,MYSTERY,-25000.00
""")
    db.commit()
    with pytest.raises(BI.ImportProblem, match="Choose what this line was"):
        BI.apply(db, outcome.batch.lines[0], "")


# --------------------------------------------------------------------------
# The bulk action
# --------------------------------------------------------------------------


def test_confirm_strong_posts_only_what_it_was_sure_about(db):
    cust = customer(db)
    inv = invoice(db, cust, date(2026, 6, 10), 85_000_000)
    db.commit()
    csv = f"""Date,Description,Amount
03/07/2026,TRF FROM DANGOTE CEMENT PLC {inv.number},{inv.total / 100:.2f}
12/07/2026,SOMETHING NOBODY KNOWS,-25000.00
""".encode()
    outcome = bring_in(db, csv)
    db.commit()

    result = BI.confirm_strong(db, outcome.batch)
    db.commit()
    assert result["done"] == 1 and result["failed"] == 0

    rows = list(outcome.batch.lines)
    assert rows[0].status == CONFIRMED
    assert rows[1].status != CONFIRMED       # left for a person
    assert books_balance(db)


def test_the_bulk_action_does_not_learn_from_its_own_guesses(db):
    """A rule written from a guess turns one wrong match into next month's rule."""
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,MTN NIGERIA DATA,-25000.00
""")
    db.commit()
    BI.confirm_strong(db, outcome.batch)
    db.commit()
    assert not list(db.scalars(select(PayeeRule)))


# --------------------------------------------------------------------------
# Learning from what a person chose
# --------------------------------------------------------------------------


def test_what_a_person_chose_is_suggested_next_time(db):
    first = bring_in(db, b"""Date,Description,Amount
12/07/2026,MTN NIGERIA DATA BUNDLE RENEWAL,-25000.00
""")
    db.commit()
    BI.apply(db, first.batch.lines[0], ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)
    db.commit()

    got = matching.suggest(db, bank_of(db).id, date(2026, 8, 12), -2_500_000,
                           "MTN NIGERIA DATA BUNDLE RENEWAL")
    assert got.action == ACTION_POST
    assert got.account_id == acc(db, "BANK_CHARGES").id
    assert "Last time" in got.why


def test_a_rule_is_updated_rather_than_duplicated(db):
    for when in (date(2026, 7, 12), date(2026, 8, 12)):
        outcome = bring_in(db, f"""Date,Description,Amount
{when:%d/%m/%Y},MTN NIGERIA DATA BUNDLE,-25000.00
""".encode())
        db.commit()
        BI.apply(db, outcome.batch.lines[0], ACTION_POST,
                 account_id=acc(db, "BANK_CHARGES").id)
        db.commit()
    rules = list(db.scalars(select(PayeeRule)))
    assert len(rules) == 1 and rules[0].times_used == 2


def test_a_rule_only_applies_in_the_direction_it_was_learned(db):
    outcome = bring_in(db, b"""Date,Description,Amount
12/07/2026,ACME SERVICES,-25000.00
""")
    db.commit()
    BI.apply(db, outcome.batch.lines[0], ACTION_POST, account_id=acc(db, "BANK_CHARGES").id)
    db.commit()

    incoming = matching.suggest(db, bank_of(db).id, date(2026, 8, 1), 2_500_000,
                                "ACME SERVICES")
    assert incoming.account_id != acc(db, "BANK_CHARGES").id


# --------------------------------------------------------------------------
# Importing the same file twice
# --------------------------------------------------------------------------


def test_the_same_file_imported_twice_adds_nothing_the_second_time(db):
    first = bring_in(db, NIGERIAN)
    db.commit()
    assert first.added == 2 and first.duplicates == 0

    second = BI.create(db, bank_of(db).id, statements.read(NIGERIAN), "again.csv")
    db.commit()
    assert second.added == 0 and second.duplicates == 2


def test_two_identical_payments_on_one_day_are_both_kept(db):
    """A real thing: two £5 card payments to the same shop, same day."""
    outcome = bring_in(db, b"""Date,Description,Reference,Amount
12/07/2026,POS SHOPRITE,A1,-500.00
12/07/2026,POS SHOPRITE,A2,-500.00
""")
    db.commit()
    assert outcome.added == 2


# --------------------------------------------------------------------------
# The charts
# --------------------------------------------------------------------------


def test_buckets_add_up_to_the_statement():
    lines = [line(date(2026, 7, 1) + timedelta(days=i), f"line {i}",
                  1000 if i % 2 else -500) for i in range(10)]
    buckets = charts.bucket_lines(lines, opening=10_000)
    assert sum(b.money_in for b in buckets) == sum(x.amount for x in lines if x.amount > 0)
    assert sum(b.money_out for b in buckets) == -sum(x.amount for x in lines if x.amount < 0)
    assert buckets[-1].closing == 10_000 + sum(x.amount for x in lines)


def test_a_long_statement_is_grouped_rather_than_drawn_daily():
    lines = [line(date(2026, 1, 1) + timedelta(days=i), "x", 100) for i in range(200)]
    assert len(charts.bucket_lines(lines)) < 40


def test_the_chart_is_self_contained_svg():
    lines = [line(date(2026, 7, 1) + timedelta(days=i), "x", 100 * (1 if i % 2 else -1))
             for i in range(6)]
    svg = charts.cash_chart(charts.bucket_lines(lines, 5_000))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "var(--good)" in svg and "var(--danger)" in svg
    assert "http" not in svg.replace('xmlns="http://www.w3.org/2000/svg"', "")


def test_an_empty_statement_draws_nothing_rather_than_dividing_by_zero():
    assert charts.bucket_lines([]) == []
    assert charts.cash_chart([]) == ""
    assert charts.split_bar([]) == '<div class="split-bar"></div>'


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-bankweb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password",
               data={"new_password": "Lagos2026", "confirm_password": "Lagos2026"},
               follow_redirects=True)
        c.post("/settings/company", data={
            "name": "Adeyemi Trading Ltd", "currency_symbol": "₦", "currency_code": "NGN",
            "fiscal_year_start_month": "1", "vat_rate": "7.5",
            "default_payment_terms_days": "30",
        }, follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def upload(client, csv: bytes, name="july.csv"):
    return client.post("/banking/1/import", files={"file": (name, csv, "text/csv")},
                       follow_redirects=True)


def test_the_import_screens_open(client):
    for url in ("/banking/1/import", "/banking/imports"):
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, url
        assert "Internal Server Error" not in r.text


def test_the_preview_shows_the_reading_before_anything_is_saved(client):
    r = upload(client, NIGERIAN)
    assert r.status_code == 200
    assert "add up exactly" in r.text
    assert "nothing has been saved yet" in r.text
    # Nothing stored yet
    assert "No bank statements have been imported" in \
        client.get("/banking/imports", follow_redirects=True).text


def test_a_file_that_is_not_a_statement_is_refused_on_screen(client):
    r = upload(client, b"Hello,World\nnot,a,statement\n")
    assert r.status_code == 200
    assert "No date column could be found" in r.text


def test_uploading_nothing_asks_for_a_file(client):
    r = client.post("/banking/1/import", data={"date_order": "dmy"}, follow_redirects=True)
    assert "Choose the statement file" in r.text


def test_the_whole_flow_through_the_screens(client):
    upload(client, NIGERIAN)
    r = client.post("/banking/1/import/confirm", data={"date_order": "dmy"},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "The transactions" in r.text
    assert "2 lines brought in." in r.text
    assert "split-bar" in r.text and "cash-chart" in r.text

    # The line screen offers every way of dealing with it, worded for the
    # direction the money went: line 1 is money in, line 2 money out.
    money_in = client.get("/banking/import/1/line/1/choices", follow_redirects=True)
    assert money_in.status_code == 200
    for offer in ("Is a customer paying you?", "income of another kind", "leave it out"):
        assert offer.lower() in money_in.text.lower()

    money_out = client.get("/banking/import/1/line/2/choices", follow_redirects=True)
    assert money_out.status_code == 200
    for offer in ("Are you paying a supplier?", "is it a cost"):
        assert offer.lower() in money_out.text.lower()

    # Posting one line straight to an account
    r = client.post("/banking/import/1/line/1", data={
        "action": "POST", "account_id": "1", "show": "todo"}, follow_redirects=True)
    assert r.status_code == 200


def test_re_uploading_the_same_file_creates_no_empty_batch(client):
    upload(client, NIGERIAN)
    client.post("/banking/1/import/confirm", data={"date_order": "dmy"},
                follow_redirects=True)
    upload(client, NIGERIAN)
    r = client.post("/banking/1/import/confirm", data={"date_order": "dmy"},
                    follow_redirects=True)
    assert "had already been imported" in r.text
    # Still exactly one batch in the list
    assert r.text.count("august") == 0
    listing = client.get("/banking/imports", follow_redirects=True).text
    assert listing.count("/banking/import/") == 1


def test_confirming_without_an_upload_in_hand_says_so(client):
    r = client.post("/banking/1/import/confirm", data={"date_order": "dmy"},
                    follow_redirects=True)
    assert "expired" in r.text


def test_the_import_screens_are_linked_from_the_menu(client):
    text = client.get("/banking", follow_redirects=True).text
    assert "/banking/imports" in text


# --------------------------------------------------------------------------
# Files that are not statements, and files that are but are not CSV
# --------------------------------------------------------------------------
#
# A customer downloads the PDF because the PDF is what the bank puts in front
# of them. Telling them "no date column could be found" sends them looking for
# a column, when what they need is a different download.


def a_workbook(rows, styles_date_at=1) -> bytes:
    """A real .xlsx, written by hand so the test needs nothing installed."""
    import io
    import zipfile

    NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    body = [f'<?xml version="1.0"?><worksheet {NS}><sheetData>']
    for r, row in enumerate(rows, start=1):
        body.append(f'<row r="{r}">')
        for c, cell in enumerate(row):
            ref = chr(65 + c) + str(r)
            if isinstance(cell, tuple):          # (serial, "date")
                body.append(f'<c r="{ref}" s="{styles_date_at}"><v>{cell[0]}</v></c>')
            elif isinstance(cell, (int, float)):
                body.append(f'<c r="{ref}"><v>{cell}</v></c>')
            elif cell == "":
                continue
            else:
                body.append(f'<c r="{ref}" t="inlineStr"><is><t>{cell}</t></is></c>')
        body.append("</row>")
    body.append("</sheetData></worksheet>")

    styles = ('<?xml version="1.0"?><styleSheet xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main"><cellXfs count="2">'
              '<xf numFmtId="0"/><xf numFmtId="14" applyNumberFormat="1"/>'
              '</cellXfs></styleSheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns='
                   '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
        z.writestr("xl/styles.xml", styles)
        z.writestr("xl/worksheets/sheet1.xml", "".join(body))
    return buf.getvalue()


A_STATEMENT = [
    ["Providus Bank — Statement of Account"],
    ["Account: 1309209284"],
    [],
    ["Value Date", "Description", "Debit", "Credit", "Balance"],
    [(46236,), "Opening balance", "", "", 1250000],
    [(46237,), "TRF FROM ZENITH CONSTRUCTION LTD", "", 900000, 2150000],
    [(46239,), "POS PURCHASE SHOPRITE", 45500, "", 2104500],
]


def test_a_pdf_is_recognised_as_a_pdf():
    assert statements.identify(b"%PDF-1.7\n1 0 obj\ntrailer\n%%EOF") == "pdf"


def test_a_pdf_with_no_statement_in_it_says_something_useful():
    """Whatever goes wrong with a PDF, the answer is never "no date column" —
    that sends somebody hunting for a column instead of a better download."""
    with pytest.raises(ImportError_) as raised:
        statements.read(b"%PDF-1.7\n1 0 obj\ntrailer\n%%EOF")
    said = str(raised.value)
    assert "date column" not in said


def test_other_things_that_are_not_statements_say_what_they_are():
    assert statements.identify(b"\x89PNG\r\n\x1a\n") == "image"
    assert statements.identify(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") == "xls"
    with pytest.raises(ImportError_) as raised:
        statements.read(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    assert "Save As" in str(raised.value)


def test_renaming_a_pdf_to_csv_does_not_fool_it():
    """The name is not the file. Recognition is by what is inside."""
    with pytest.raises(ImportError_) as raised:
        statements.read(b"%PDF-1.4 pretending to be statement.csv")
    assert "PDF" in str(raised.value)


def test_an_excel_statement_is_read_like_any_other():
    reading = statements.read(a_workbook(A_STATEMENT))
    assert reading.format == "xlsx"
    assert len(reading.lines) == 2
    assert reading.lines[0].description.startswith("TRF FROM ZENITH")


def test_excel_day_numbers_become_real_dates():
    """A spreadsheet stores a date as a number, and without reading the cell
    formats every date arrives as five digits."""
    from datetime import date as D

    reading = statements.read(a_workbook(A_STATEMENT))
    assert reading.lines[0].date == D(2026, 8, 3)
    assert reading.lines[1].date == D(2026, 8, 5)


def test_an_excel_statement_gets_the_same_column_intelligence():
    """Including not mistaking "Value Date" for the amount."""
    reading = statements.read(a_workbook(A_STATEMENT))
    assert reading.columns["date"] == "Value Date"
    assert reading.columns["debit"] == "Debit"
    assert reading.columns["credit"] == "Credit"


def test_the_brought_forward_line_is_still_found_in_a_workbook():
    reading = statements.read(a_workbook(A_STATEMENT))
    # Amounts are kept in kobo, so 1,250,000 on the sheet is this many.
    assert reading.stated_opening == 125_000_000


def test_debits_and_credits_keep_their_signs_from_a_workbook():
    reading = statements.read(a_workbook(A_STATEMENT))
    assert reading.lines[0].amount > 0, "money in"
    assert reading.lines[1].amount < 0, "money out"


def test_a_damaged_workbook_is_refused_kindly():
    with pytest.raises(ImportError_) as raised:
        statements.read(b"PK\x03\x04" + b"\x00" * 60)
    assert "zip" in str(raised.value).lower() or "Excel" in str(raised.value)


def test_an_empty_workbook_says_so():
    with pytest.raises(ImportError_):
        statements.read(a_workbook([]))


# --------------------------------------------------------------------------
# Statements out of a PDF
# --------------------------------------------------------------------------
#
# The bank hands the customer a PDF because it looks like the paper statement
# they used to post. A PDF holds no rows and no columns — only glyphs at
# points on a page — so reading one means reconstructing the table from where
# things sit. That is less certain than a CSV, which is why the reading is
# shown line by line for confirmation and nothing is posted until it is.


def a_statement_pdf(rows=None, title="Providus Bank — Statement of Account"):
    """A PDF shaped like a bank statement, written with our own writer."""
    from app.pdfwriter import Canvas

    rows = rows or [
        ("01/08/2026", "OPENING BALANCE", "", "", "1,250,000.00"),
        ("03/08/2026", "TRF FROM ZENITH CONSTRUCTION LTD", "", "900,000.00",
         "2,150,000.00"),
        ("05/08/2026", "POS PURCHASE SHOPRITE IKEJA", "45,500.00", "",
         "2,104,500.00"),
        ("07/08/2026", "SALARY PAYMENT AUGUST", "860,000.00", "",
         "1,244,500.00"),
    ]
    c = Canvas()
    y = 60
    c.text(42, y, title, size=13, bold=True)
    y += 30
    for label, x, align in (("Value Date", 42, "left"), ("Description", 150, "left"),
                            ("Debit", 400, "right"), ("Credit", 470, "right"),
                            ("Balance", 553, "right")):
        c.text(x, y, label, size=9, bold=True, align=align)
    y += 18
    for date_text, description, debit, credit, balance in rows:
        c.text(42, y, date_text, size=9)
        c.text(150, y, description, size=9)
        c.text(400, y, debit, size=9, align="right")
        c.text(470, y, credit, size=9, align="right")
        c.text(553, y, balance, size=9, align="right")
        y += 16
    return c.output()


def test_a_pdf_statement_is_read():
    reading = statements.read(a_statement_pdf())
    assert reading.format == "pdf"
    assert len(reading.lines) == 3


def test_the_columns_are_worked_out_from_where_the_text_sits():
    """Right-aligned figures do not start in the same place, so the columns
    have to be found from the gaps between them rather than their starts."""
    reading = statements.read(a_statement_pdf())
    assert reading.columns["date"] == "Value Date"
    assert reading.columns["debit"] == "Debit"
    assert reading.columns["credit"] == "Credit"
    assert reading.columns["balance"] == "Balance"


def test_money_in_and_money_out_keep_their_sides():
    reading = statements.read(a_statement_pdf())
    by_description = {line.description: line.amount for line in reading.lines}
    assert by_description["TRF FROM ZENITH CONSTRUCTION LTD"] == 90_000_000
    assert by_description["POS PURCHASE SHOPRITE IKEJA"] == -4_550_000
    assert by_description["SALARY PAYMENT AUGUST"] == -86_000_000


def test_the_dates_survive_the_journey():
    from datetime import date as D

    reading = statements.read(a_statement_pdf())
    assert [line.date for line in reading.lines] == [
        D(2026, 8, 3), D(2026, 8, 5), D(2026, 8, 7)]


def test_the_brought_forward_line_is_found_in_a_pdf_too():
    assert statements.read(a_statement_pdf()).stated_opening == 125_000_000


def test_every_pdf_reading_carries_its_own_warning():
    """It is the least certain of the three readers and must say so."""
    reading = statements.read(a_statement_pdf())
    assert reading.problems
    first = reading.problems[0]
    assert "least reliable" in first
    assert "Check the dates and amounts" in first


def test_a_scanned_statement_is_refused_rather_than_read_as_empty():
    """A picture of a statement must not come back as a statement with no
    transactions in it — that reads as "the month was quiet"."""
    import zlib

    image = zlib.compress(b"\x00" * 300)
    body = bytearray(b"%PDF-1.4\n")

    def add(number, data):
        body.extend(f"{number} 0 obj\n".encode())
        body.extend(data)
        body.extend(b"\nendobj\n")

    add(1, b"<</Type/Catalog/Pages 2 0 R>>")
    add(2, b"<</Type/Pages/Kids[3 0 R]/Count 1>>")
    add(3, b"<</Type/Page/Parent 2 0 R/Resources<</XObject<</Im1 4 0 R>>>>"
           b"/Contents 5 0 R>>")
    add(4, b"<</Type/XObject/Subtype/Image/Width 10/Height 10"
           b"/Filter/FlateDecode/Length %d>>stream\n" % len(image)
           + image + b"\nendstream")
    add(5, b"<</Length 30>>stream\nq 100 0 0 100 0 0 cm /Im1 Do Q\nendstream")
    body.extend(b"trailer<</Root 1 0 R>>\n%%EOF")

    with pytest.raises(ImportError_) as raised:
        statements.read(bytes(body))
    said = str(raised.value)
    assert "scan or a photograph" in said
    assert "CSV or Excel" in said


def test_a_pdf_with_nothing_in_it_is_refused():
    with pytest.raises(ImportError_):
        statements.read(b"%PDF-1.4\n1 0 obj\n<</Type/Catalog>>\nendobj\n%%EOF")


def test_objects_hidden_inside_a_compressed_container_are_still_found():
    """Modern producers pack objects into an /ObjStm. A reader that only
    understands the plain layout sees an empty document."""
    import zlib

    from app.pdfread import Document

    inner = [b"<</Type/Catalog/Pages 2 0 R>>",
             b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
             b"<</Type/Page/Parent 2 0 R>>"]
    offsets, payload = [], b""
    for number, obj in enumerate(inner, start=1):
        offsets.append(f"{number} {len(payload)}")
        payload += obj + b" "
    header = (" ".join(offsets) + " ").encode()
    packed = zlib.compress(header + payload)

    body = bytearray(b"%PDF-1.5\n5 0 obj\n")
    body.extend(b"<</Type/ObjStm/N 3/First %d/Filter/FlateDecode/Length %d>>"
                b"stream\n" % (len(header), len(packed)))
    body.extend(packed)
    body.extend(b"\nendstream\nendobj\ntrailer<</Root 1 0 R>>\n%%EOF")

    doc = Document(bytes(body))
    assert 1 in doc.objects and 3 in doc.objects
    assert doc.get(doc.objects[1]).get("Type") == "Catalog"


def test_a_pdf_that_is_not_a_pdf_is_refused():
    from app.pdfread import Document, PdfError

    with pytest.raises(PdfError):
        Document(b"just some text, not a document at all")


def test_the_same_pdf_imported_twice_is_recognised(db):
    """The duplicate check works on what was read, so it must hold for PDFs."""
    raw = a_statement_pdf()
    first = BI.create(db, bank_of(db).id, statements.read(raw), "statement.pdf")
    db.flush()
    assert first.added == 3

    second = BI.create(db, bank_of(db).id, statements.read(raw), "statement.pdf")
    db.flush()
    assert second.added == 0 and second.duplicates == 3
