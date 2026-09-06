"""The brief, the board pack and the screens that carry them.

The engine's arithmetic is proved in test_variance.py. What is checked here is
the layer a person actually meets: does the brief raise the right things and
stay quiet when there is nothing to raise, does the board pack tie to the
reports it claims to summarise, and does the PDF come out readable.

One test in here matters more than the rest: the pack must never print a
currency symbol the built-in PDF fonts cannot draw, because that comes out as a
row of boxes in front of a board.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import zlib
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-ins-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import currency, db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    Company,
    Contact,
    Invoice,
    InvoiceLine,
)
from app.seed import bootstrap  # noqa: E402
from app.services import boardpdf, brief, documents, pdfdocs  # noqa: E402
from app.services import reports  # noqa: E402
from app.services import variance as V  # noqa: E402
from app.services.posting import EntryDraft, next_number, post_entry, sys_account  # noqa: E402
from tests.pdftext import text_of  # noqa: E402

JULY = V.Period("July", date(2026, 7, 1), date(2026, 7, 31))
JUNE = V.Period("June", date(2026, 6, 1), date(2026, 6, 30))


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-ins-")
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


def customer(db, name="Acme Ltd"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def invoice(db, cust, on, amount, post=True, due=None):
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=cust.id, date=on, due_date=due or on + timedelta(days=30),
                  status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Services",
                       qty=1000, unit_price=amount, account_id=acc(db, "SALES").id))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    if post:
        documents.post_invoice(db, inv)
    db.flush()
    return inv


def spend(db, on, amount, key="RENT"):
    draft = EntryDraft(date=on, memo="Rent")
    draft.debit(acc(db, key), amount)
    draft.credit(acc(db, "CASH"), amount)
    return post_entry(db, draft)





def titles(b) -> str:
    return " | ".join(p.title for p in b.needs_you)


# --------------------------------------------------------------------------
# The brief: quiet when it should be
# --------------------------------------------------------------------------


def test_empty_books_raise_nothing(db):
    b = brief.build(db, date(2026, 7, 31))
    assert b.quiet
    assert b.headline == "Nothing needs a decision today."


def test_a_paid_up_business_raises_nothing(db):
    """Nothing overdue, nothing in draft: the brief must not invent work."""
    a = customer(db)
    inv = invoice(db, a, date(2026, 7, 1), 500_000)
    from app.models import RECEIPT, BankAccount, Payment
    from app.services import cash

    bank = db.scalar(select(BankAccount))
    pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                  contact_id=a.id, date=date(2026, 7, 2), amount=inv.total,
                  bank_account_id=bank.id)
    db.add(pay)
    db.flush()
    cash.auto_allocate(db, pay)
    cash.post_payment(db, pay)
    db.commit()

    b = brief.build(db, date(2026, 7, 10))
    assert "past its due date" not in titles(b)
    assert "still in draft" not in titles(b)


# --------------------------------------------------------------------------
# The brief: raises what it should
# --------------------------------------------------------------------------


def test_an_overdue_invoice_is_raised_with_the_right_amount(db):
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 5, 1), 800_000, due=date(2026, 5, 15))
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    late = [p for p in b.needs_you if "past its due date" in p.title]
    assert late and late[0].amount == 800_000
    assert late[0].kind == brief.URGENT


def test_the_worst_debtor_is_named(db):
    small = customer(db, "Small Ltd")
    big = customer(db, "Big Debtor Plc")
    invoice(db, small, date(2026, 5, 1), 100_000, due=date(2026, 5, 2))
    invoice(db, big, date(2026, 5, 1), 9_000_000, due=date(2026, 5, 2))
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    assert any("Big Debtor Plc is the one to chase" == p.title for p in b.needs_you)


def test_a_draft_invoice_is_raised(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 1), 250_000, post=False)
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    drafts = [p for p in b.needs_you if "in draft" in p.title]
    assert drafts and drafts[0].amount == 250_000


def test_an_overdrawn_bank_account_is_raised(db):
    from app.models import Account, BankAccount

    bank = db.scalar(select(BankAccount))
    assert bank is not None, "the seeded chart should always have a bank account"
    draft = EntryDraft(date=date(2026, 7, 3), memo="Paid a supplier")
    draft.debit(acc(db, "RENT"), 5_000_000)
    draft.credit(db.get(Account, bank.account_id), 5_000_000)
    post_entry(db, draft)
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    assert any("overdrawn" in p.title for p in b.needs_you)


# --------------------------------------------------------------------------
# The brief: ranking and the headline
# --------------------------------------------------------------------------


def test_urgent_comes_first_then_largest(db):
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 5, 1), 9_000_000, due=date(2026, 5, 2))
    invoice(db, a, date(2026, 7, 1), 100_000, post=False)
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    order = {brief.URGENT: 0, brief.WATCH: 1, brief.INFO: 2, brief.GOOD: 3}
    kinds = [order[p.kind] for p in b.needs_you]
    assert kinds == sorted(kinds)
    urgent = [p.at_stake for p in b.needs_you if p.kind == brief.URGENT]
    assert urgent == sorted(urgent, reverse=True)


def test_the_headline_counts_every_row_it_shows(db):
    """The count in the heading has to match the list underneath it."""
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 5, 1), 9_000_000, due=date(2026, 5, 2))
    invoice(db, a, date(2026, 7, 1), 100_000, post=False)
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    numbers = [int(n) for n in re.findall(r"\b(\d+)\b", b.headline)]
    assert sum(numbers) == len(b.needs_you)


def test_one_broken_check_does_not_blank_the_page(db, monkeypatch):
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 5, 1), 800_000, due=date(2026, 5, 2))
    db.commit()

    def explode(*_args, **_kwargs):
        raise RuntimeError("something went wrong in here")

    monkeypatch.setattr(brief, "_due_this_week", explode)
    b = brief.build(db, date(2026, 7, 31))
    assert any("past its due date" in p.title for p in b.needs_you)


def test_every_point_that_shows_an_amount_can_be_opened(db):
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 5, 1), 800_000, due=date(2026, 5, 2))
    invoice(db, a, date(2026, 7, 1), 100_000, post=False)
    db.commit()

    b = brief.build(db, date(2026, 7, 31))
    for p in b.needs_you:
        assert p.link, f"{p.title} has nowhere to go"
        assert p.link.startswith("/")


def test_the_brief_reports_cash_and_what_is_owed(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 1), 640_000)
    db.commit()

    on = date(2026, 7, 31)
    b = brief.build(db, on)
    _, _, owed = reports.aging(db, on, receivable=True)
    assert b.receivables == owed
    assert b.cash == reports._cash_balance(db, reports._bank_account_ids(db), on)


# --------------------------------------------------------------------------
# The board pack ties to the reports it summarises
# --------------------------------------------------------------------------


def test_the_pack_reports_the_same_profit_as_the_profit_and_loss(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    invoice(db, a, date(2026, 6, 4), 1_000_000)
    spend(db, date(2026, 7, 6), 400_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE)
    pl = reports.profit_and_loss(db, JULY.start, JULY.end)
    assert pack.pl.net_profit == pl.net_profit
    assert pack.bs.balances_ok


def test_the_pack_cash_figures_agree_with_each_other(db):
    """The cash KPI and the end of the cash flow statement are the same number.

    They come from the same helper, and this test is what keeps that true if
    somebody ever computes one of them a different way.
    """
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    spend(db, date(2026, 7, 6), 400_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    cash_kpi = next(v for name, v, _ in pack.kpis if name == "Cash at bank")
    assert cash_kpi == pdfdocs.money(pack.cf.closing_cash)


def test_movers_are_ranked_by_effect_on_profit(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    invoice(db, a, date(2026, 6, 4), 500_000)
    spend(db, date(2026, 7, 6), 900_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE)
    sizes = [abs(m.profit_effect) for m in pack.movers]
    assert sizes == sorted(sizes, reverse=True)


def test_every_question_has_an_answer_with_a_figure_in_it(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000, due=date(2026, 7, 5))
    invoice(db, a, date(2026, 6, 4), 1_000_000)
    spend(db, date(2026, 7, 6), 400_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE)
    assert pack.questions
    for q in pack.questions:
        assert q.question.endswith("?")
        assert q.answer.strip()
        assert any(ch.isdigit() for ch in q.answer)


def test_a_pack_on_empty_books_still_builds(db):
    pack = brief.board_pack(db, JULY, JUNE)
    assert pack.kpis
    assert pack.bs.balances_ok


def test_risks_are_taken_at_the_period_end_not_today(db):
    """A pack for July must describe July, not the day it was printed."""
    a = customer(db, "Late Payer Ltd")
    invoice(db, a, date(2026, 8, 20), 5_000_000, due=date(2026, 8, 21))
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE)
    assert not any("past its due date" in p.title for p in pack.risks)


# --------------------------------------------------------------------------
# The PDF
# --------------------------------------------------------------------------


def test_the_pack_is_a_real_pdf(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    data = boardpdf.render(pack, db.get(Company, 1))
    assert data.startswith(b"%PDF-1.")
    assert data.rstrip().endswith(b"%%EOF")
    # One page per numbered section, plus the cover
    kids = re.search(rb"/Type /Pages /Kids \[(.*?)\]", data)
    assert kids and len(kids.group(1).split(b" R")) - 1 >= 7


def test_every_section_heading_reaches_the_page(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    text = text_of(boardpdf.render(pack, db.get(Company, 1)))
    for heading in ("WHERE THE BUSINESS STANDS", "WHAT MOVED, AND BY HOW MUCH",
                    "PROFIT AND LOSS", "CASH FLOW", "WHAT TO RAISE",
                    "QUESTIONS YOU WILL BE ASKED"):
        assert heading in text, f"{heading} is missing from the pack"
    assert "BALANCE SHEET AT" in text


def test_no_unprintable_currency_symbol_reaches_the_pack(db):
    """Every figure in the pack must actually appear, in front of a board.

    The naira sign is the case that matters: the built-in PDF fonts do not
    have it, so it is either drawn from a font found on this computer or
    written as "NGN". What it must never be is a box or a question mark.
    """
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    text = text_of(boardpdf.render(pack, db.get(Company, 1)))
    assert "�" not in text
    assert "?3" not in text and "? 3" not in text
    assert "₦" in text or "NGN" in text
    assert "30,000.00" in text


def test_the_pack_footer_carries_the_period_and_the_page_numbers(db):
    a = customer(db)
    invoice(db, a, date(2026, 7, 4), 3_000_000)
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    text = text_of(boardpdf.render(pack, db.get(Company, 1)))
    assert "board pack" in text
    assert "Page 1 of" in text


def test_a_long_account_name_does_not_run_into_the_figures(db):
    """Truncation, not overlap: the number column is the one that must survive."""
    from app.models import Account

    long_name = "Provision for doubtful debts and other assorted sundry matters " * 2
    target = db.scalar(select(Account).where(Account.subtype == "OPERATING_EXPENSE"))
    target.name = long_name
    db.flush()
    spend(db, date(2026, 7, 6), 400_000, "RENT")
    db.commit()

    pack = brief.board_pack(db, JULY, JUNE, fmt=pdfdocs.money)
    data = boardpdf.render(pack, db.get(Company, 1))
    assert data.startswith(b"%PDF-1.")
    assert long_name.strip() not in text_of(data)  # it was cut to fit


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-insweb-")
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
            "fiscal_year_start_month": "1", "is_vat_registered": "1", "vat_rate": "7.5",
            "default_payment_terms_days": "30",
        }, follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def ok(client, url):
    r = client.get(url, follow_redirects=True)
    assert r.status_code == 200, f"{url} returned {r.status_code}"
    assert "Internal Server Error" not in r.text
    return r


def test_the_three_screens_open(client):
    assert "Today" in ok(client, "/insights/brief").text
    assert "Why did that change" in ok(client, "/insights/why").text
    assert "Board" in ok(client, "/insights/board-pack").text


def test_every_comparison_option_works(client):
    for key, _cur, _prior in V.compare_choices():
        ok(client, f"/insights/why?compare={key}")


def test_custom_dates_work_and_survive_being_the_wrong_way_round(client):
    r = ok(client, "/insights/why?compare=custom&cs=2026-07-31&ce=2026-07-01"
                   "&ps=2026-06-30&pe=2026-06-01")
    assert "01 Jul 2026" in r.text or "2026-07-01" in r.text


def test_a_nonsense_path_does_not_error(client):
    ok(client, "/insights/why?p=nonsense")
    ok(client, "/insights/why?p=revenue&p=99999")
    ok(client, "/insights/why?p=revenue&p=abc&p=def&p=ghi&p=jkl&p=mno")
    ok(client, "/insights/why?compare=not-a-real-choice")


def test_the_board_pack_downloads_as_a_pdf(client):
    r = client.get("/insights/board-pack?format=pdf", follow_redirects=True)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF-1.")
    assert "board-pack-" in r.headers.get("content-disposition", "")


def test_the_insight_screens_are_linked_from_the_menu(client):
    text = ok(client, "/").text
    assert "/insights/brief" in text
    assert "/insights/why" in text
