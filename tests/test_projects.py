"""Job costing.

One rule is being defended here above all others: **a project's figures are
the ledger's figures, filtered**. Job costing that keeps its own running totals
drifts away from the accounts, and then two screens tell a person two different
things about the same work. So every test below checks that what a project
reports can be found in the general ledger, and that coding work to a job
changes nothing about the company's own profit.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-proj-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    DRAFT,
    PROJECT_DONE,
    PROJECT_OPEN,
    Account,
    Bill,
    BillLine,
    Contact,
    Invoice,
    InvoiceLine,
    JournalLine,
    Project,
)
from app.seed import bootstrap  # noqa: E402
from app.services import documents, reports  # noqa: E402
from app.services import projects as P  # noqa: E402
from app.services.posting import EntryDraft, next_number, post_entry, sys_account  # noqa: E402

TODAY = date(2026, 6, 15)


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-proj-")
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


def a_job(db, name="Ikoyi site", worth=1_000_000_000, budget=700_000_000, **kw):
    project = Project(code=next_number(db, "PROJECT"), name=name,
                      contract_value=worth, budget_cost=budget,
                      status=kw.pop("status", PROJECT_OPEN), **kw)
    db.add(project)
    db.flush()
    return project


def customer(db, name="Client Ltd"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def supplier(db, name="Supplier Ltd"):
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                payment_terms_days=30)
    db.add(c)
    db.flush()
    return c


def bill_customer(db, cust, amount, project=None, on=TODAY):
    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                  contact_id=cust.id, date=on, due_date=on + timedelta(days=30),
                  status=DRAFT)
    db.add(inv)
    db.flush()
    db.add(InvoiceLine(invoice_id=inv.id, line_no=1, description="Stage 1", qty=1000,
                       unit_price=amount, account_id=acc(db, "SALES").id,
                       project_id=project.id if project else None))
    db.flush()
    db.refresh(inv)
    documents.recalc_invoice(db, inv)
    documents.post_invoice(db, inv)
    db.flush()
    return inv


def spend_on(db, vend, amount, project=None, on=TODAY):
    b = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vend.id,
             date=on, due_date=on + timedelta(days=30), status=DRAFT)
    db.add(b)
    db.flush()
    db.add(BillLine(bill_id=b.id, line_no=1, description="Materials", qty=1000,
                    unit_price=amount, account_id=acc(db, "PURCHASES").id,
                    project_id=project.id if project else None))
    db.flush()
    db.refresh(b)
    documents.recalc_bill(db, b)
    documents.post_bill(db, b)
    db.flush()
    return b


def books_balance(db) -> bool:
    _rows, td, tc = reports.trial_balance(db, None, date(2030, 1, 1))
    return td == tc and reports.balance_sheet(db, date(2030, 1, 1)).difference == 0


# --------------------------------------------------------------------------
# The figures come out of the ledger
# --------------------------------------------------------------------------


def test_an_invoice_coded_to_a_job_shows_up_on_it(db):
    job = a_job(db)
    bill_customer(db, customer(db), 400_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    assert standing.figures.revenue == 400_000_000
    assert standing.figures.cost == 0


def test_a_bill_coded_to_a_job_shows_up_as_its_cost(db):
    job = a_job(db)
    spend_on(db, supplier(db), 600_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    assert standing.figures.cost == 600_000_000
    assert standing.figures.profit == -600_000_000


def test_the_project_id_reaches_the_journal_line(db):
    """Everything else reads that column, so it has to be written."""
    job = a_job(db)
    bill_customer(db, customer(db), 400_000_000, job)
    db.commit()

    coded = list(db.scalars(
        select(JournalLine).where(JournalLine.project_id == job.id)
    ))
    assert coded
    assert all(line.project_id == job.id for line in coded)


def test_a_projects_figures_can_be_traced_to_the_ledger(db):
    job = a_job(db)
    bill_customer(db, customer(db), 400_000_000, job)
    spend_on(db, supplier(db), 250_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    rows = P.ledger(db, job.id)
    revenue = sum(line.credit - line.debit for line, _e, a in rows if a.type == "INCOME")
    cost = sum(line.debit - line.credit for line, _e, a in rows if a.type == "EXPENSE")
    assert revenue == standing.figures.revenue
    assert cost == standing.figures.cost


def test_coding_work_to_a_job_does_not_change_the_company_profit(db):
    """A project is a label, not a second set of books."""
    cust, vend = customer(db), supplier(db)
    bill_customer(db, cust, 400_000_000)
    spend_on(db, vend, 250_000_000)
    db.commit()
    before = reports.profit_and_loss(db, date(2026, 1, 1), date(2026, 12, 31)).net_profit

    job = a_job(db)
    for line in db.scalars(select(JournalLine)):
        line.project_id = job.id
    db.commit()

    after = reports.profit_and_loss(db, date(2026, 1, 1), date(2026, 12, 31)).net_profit
    assert after == before
    assert books_balance(db)


def test_work_on_one_job_never_lands_on_another(db):
    one, two = a_job(db, "Site A"), a_job(db, "Site B")
    bill_customer(db, customer(db), 400_000_000, one)
    spend_on(db, supplier(db), 100_000_000, two)
    db.commit()

    assert P.one(db, one.id).figures.revenue == 400_000_000
    assert P.one(db, one.id).figures.cost == 0
    assert P.one(db, two.id).figures.cost == 100_000_000
    assert P.one(db, two.id).figures.revenue == 0


def test_the_period_filter_is_respected(db):
    job = a_job(db)
    bill_customer(db, customer(db), 400_000_000, job, on=date(2026, 3, 1))
    bill_customer(db, customer(db, "Other Ltd"), 100_000_000, job, on=date(2026, 9, 1))
    db.commit()

    whole = P.one(db, job.id).figures.revenue
    first_half = P.one(db, job.id, date(2026, 1, 1), date(2026, 6, 30)).figures.revenue
    assert whole == 500_000_000
    assert first_half == 400_000_000


# --------------------------------------------------------------------------
# What it notices
# --------------------------------------------------------------------------


def test_a_job_over_budget_is_flagged(db):
    job = a_job(db, budget=100_000_000)
    spend_on(db, supplier(db), 150_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    assert standing.over_budget
    assert any("over the budget" in c for c in standing.concerns)


def test_work_done_and_not_billed_is_flagged(db):
    """The Project Profitability Detective, in one check."""
    job = a_job(db, worth=1_000_000_000, budget=700_000_000)
    bill_customer(db, customer(db), 400_000_000, job)      # 40% billed
    spend_on(db, supplier(db), 600_000_000, job)           # 86% of the budget spent
    db.commit()

    standing = P.one(db, job.id)
    flagged = " ".join(standing.concerns)
    assert "has been incurred but only" in flagged
    assert "an invoice is owed" in flagged


def test_a_job_losing_money_says_so(db):
    job = a_job(db)
    bill_customer(db, customer(db), 100_000_000, job)
    spend_on(db, supplier(db), 300_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    assert standing.losing_money
    assert any("losing money" in c for c in standing.concerns)


def test_a_job_running_late_is_flagged(db):
    job = a_job(db, due_on=date.today() - timedelta(days=40))
    db.commit()
    assert any("due to finish" in c for c in P.one(db, job.id).concerns)


def test_a_healthy_job_is_left_alone(db):
    job = a_job(db, worth=1_000_000_000, budget=700_000_000,
                due_on=date.today() + timedelta(days=90))
    bill_customer(db, customer(db), 500_000_000, job)
    spend_on(db, supplier(db), 300_000_000, job)
    db.commit()

    standing = P.one(db, job.id)
    assert not standing.concerns
    assert not standing.needs_attention


def test_a_job_with_no_budget_is_not_nagged_about_one(db):
    job = a_job(db, worth=0, budget=0)
    spend_on(db, supplier(db), 300_000_000, job)
    db.commit()
    standing = P.one(db, job.id)
    assert not standing.over_budget
    assert standing.unbilled == 0


# --------------------------------------------------------------------------
# The list
# --------------------------------------------------------------------------


def test_the_jobs_needing_attention_come_first(db):
    fine = a_job(db, "Healthy", worth=1_000_000_000, budget=700_000_000)
    bill_customer(db, customer(db), 500_000_000, fine)
    spend_on(db, supplier(db), 300_000_000, fine)

    bad = a_job(db, "Runaway", worth=1_000_000_000, budget=100_000_000)
    spend_on(db, supplier(db, "Other Supplier"), 400_000_000, bad)
    db.commit()

    names = [s.name for s in P.standings(db)]
    assert names.index("Runaway") < names.index("Healthy")


def test_finished_jobs_can_be_left_out(db):
    a_job(db, "Old one", status=PROJECT_DONE)
    a_job(db, "Current one")
    db.commit()

    assert len(P.standings(db, include_finished=True)) == 2
    open_only = P.standings(db, include_finished=False)
    assert [s.name for s in open_only] == ["Current one"]


def test_what_is_coded_to_no_job_at_all_is_reported(db):
    """Job costing where most of the money is uncoded looks complete and is not."""
    job = a_job(db)
    bill_customer(db, customer(db), 400_000_000, job)
    bill_customer(db, customer(db, "Walk-in"), 90_000_000)      # no job
    spend_on(db, supplier(db), 50_000_000)                      # no job
    db.commit()

    loose = P.unallocated(db)
    assert loose.revenue == 90_000_000
    assert loose.cost == 50_000_000


def test_only_open_jobs_are_offered_to_code_work_to(db):
    a_job(db, "Open one")
    a_job(db, "Closed one", status=PROJECT_DONE)
    db.commit()
    assert [p.name for p in P.choices(db)] == ["Open one"]


def test_a_job_with_nothing_on_it_reports_nothing_rather_than_failing(db):
    job = a_job(db)
    db.commit()
    standing = P.one(db, job.id)
    assert standing.figures.revenue == 0 and standing.figures.cost == 0
    assert standing.figures.margin == 0.0
    assert P.ledger(db, job.id) == []


def test_asking_for_a_job_that_is_not_there(db):
    assert P.one(db, 99999) is None


# --------------------------------------------------------------------------
# The screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    tmp = tempfile.mkdtemp(prefix="nexora-projweb-")
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


def test_the_project_screens_open(client):
    r = client.get("/projects", follow_redirects=True)
    assert r.status_code == 200
    assert "No projects yet" in r.text


def test_a_project_can_be_created_and_edited(client):
    r = client.post("/projects/save", data={
        "name": "Ikoyi site phase 2", "contract_value": "10,000,000.00",
        "budget_cost": "7,000,000.00", "status": "OPEN",
        "due_on": "2026-12-31",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Ikoyi site phase 2" in r.text

    r = client.post("/projects/save", data={
        "id": "1", "name": "Ikoyi site phase 2 (revised)",
        "contract_value": "12,000,000.00", "budget_cost": "7,000,000.00",
        "status": "OPEN",
    }, follow_redirects=True)
    assert "revised" in r.text


def test_a_project_without_a_name_is_refused(client):
    r = client.post("/projects/save", data={"name": "   "}, follow_redirects=True)
    assert "needs a name" in r.text


def test_the_job_box_appears_on_invoices_and_bills_once_a_job_exists(client):
    # No projects yet, so nobody is asked about one
    assert "line_project" not in client.get("/sales/invoices/new",
                                            follow_redirects=True).text

    client.post("/projects/save", data={"name": "Site A", "status": "OPEN"},
                follow_redirects=True)
    for url in ("/sales/invoices/new", "/purchases/bills/new"):
        text = client.get(url, follow_redirects=True).text
        assert "line_project" in text, url
        assert "Site A" in text, url


def test_a_missing_project_sends_you_back_to_the_list(client):
    r = client.get("/projects/99999", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/projects"


def test_projects_are_linked_from_the_menu(client):
    assert "/projects" in client.get("/", follow_redirects=True).text
