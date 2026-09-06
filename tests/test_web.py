"""End-to-end tests through the web interface.

These drive the application the way a user does — filling forms and following
redirects — so a broken template or route fails the build.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

_TMP = tempfile.mkdtemp(prefix="nexora-web-")
os.environ["NEXORA_DATA"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402

TODAY = date.today()
D = TODAY.isoformat()


@pytest.fixture(scope="module")
def client():
    dbmod.reset_all()
    with TestClient(app) as c:
        # Sign in and get the forced password change out of the way
        r = c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
                   follow_redirects=True)
        assert r.status_code == 200
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        # Complete first-run company setup
        c.post("/settings/company", data={
            "name": "Adeyemi Trading Ltd", "tin": "12345678-0001",
            "vat_reg_no": "VAT-99881", "rc_number": "RC123456",
            "address": "14 Awolowo Road, Ikoyi", "city": "Lagos", "state": "Lagos",
            "phone": "+234 801 234 5678", "email": "accounts@adeyemi.ng",
            "currency_symbol": "₦", "currency_code": "NGN",
            "fiscal_year_start_month": "1", "is_vat_registered": "1", "vat_rate": "7.5",
            "annual_turnover_band": "ABOVE_50M", "default_payment_terms_days": "30",
            "invoice_terms": "Payment due within 30 days.",
            "invoice_footer": "Thank you for your business.",
        }, follow_redirects=True)
        yield c
    shutil.rmtree(_TMP, ignore_errors=True)


def ok(client, url):
    r = client.get(url, follow_redirects=True)
    assert r.status_code == 200, f"{url} returned {r.status_code}"
    assert "Internal Server Error" not in r.text, f"{url} raised an error"
    return r


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_signed_out_user_is_sent_to_login():
    dbmod.reset_all()
    with TestClient(app) as anon:
        r = anon.get("/", follow_redirects=False)
        assert r.status_code == 303
        assert "/login" in r.headers["location"]


def test_bad_password_is_rejected(client):
    with TestClient(app) as anon:
        r = anon.post("/login", data={"username": "admin", "password": "wrong", "next": "/"})
        assert "not recognised" in r.text


def test_dashboard_loads(client):
    r = ok(client, "/")
    assert "Adeyemi Trading Ltd" in r.text
    assert "Dashboard" in r.text


# --------------------------------------------------------------------------
# Every page renders
# --------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "/", "/search?q=test",
    "/contacts?kind=customer", "/contacts?kind=vendor", "/contacts/new",
    "/sales/invoices", "/sales/invoices/new", "/sales/quotes", "/sales/quotes/new",
    "/sales/credit-notes", "/receipts", "/receipts/new",
    "/purchases/bills", "/purchases/bills/new", "/purchases/orders",
    "/purchases/debit-notes", "/purchases/expense/new", "/payments", "/payments/new",
    "/inventory", "/inventory/new", "/inventory/valuation",
    "/inventory/locations", "/inventory/transfer", "/inventory/expiring",
    "/inventory/serials", "/landed-costs",
    "/requisitions", "/requisitions?view=mine", "/requisitions?view=all",
    "/requisitions/new", "/requisitions/outstanding",
    "/assets", "/assets/new", "/assets/categories", "/assets/schedule",
    "/assets/depreciation",
    "/recurring", "/recurring/new?kind=INVOICE", "/recurring/new?kind=BILL",
    "/budgets",
    "/banking", "/banking/new", "/banking/transfer",
    "/payroll", "/payroll/employees", "/payroll/employees/new", "/payroll/runs/new",
    "/payroll/remittances", "/payroll/reports/schedules",
    "/payroll/reports/schedules?kind=pension", "/payroll/reports/schedules?kind=summary",
    "/payroll/settings",
    "/journals", "/journals/new", "/journals/opening-balances",
    "/accounts", "/accounts/new",
    "/reports", "/reports/trial-balance", "/reports/profit-and-loss",
    "/reports/balance-sheet", "/reports/cash-flow", "/reports/general-ledger",
    "/reports/aging?kind=ar", "/reports/aging?kind=ap", "/reports/vat",
    "/reports/wht?kind=payable", "/reports/wht?kind=credit", "/reports/audit-trail",
    "/settings", "/settings/company", "/settings/users", "/settings/tax",
    "/settings/periods", "/settings/backup", "/settings/network",
    "/account", "/account/password",
])
def test_page_renders(client, url):
    ok(client, url)


# --------------------------------------------------------------------------
# A full trading cycle, driven through the forms
# --------------------------------------------------------------------------


def test_full_cycle_through_the_interface(client):
    # --- A customer -------------------------------------------------------
    r = client.post("/contacts/save", data={
        "name": "Zenith Construction Ltd", "contact_type": "COMPANY",
        "is_customer": "1", "is_active": "1", "tin": "20304050-0001",
        "phone": "08031112222", "email": "pay@zenithcon.ng",
        "address": "5 Marina, Lagos Island", "payment_terms_days": "30",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Zenith Construction Ltd" in r.text

    # --- A supplier -------------------------------------------------------
    client.post("/contacts/save", data={
        "name": "Ogun Cement Depot", "contact_type": "COMPANY",
        "is_vendor": "1", "is_active": "1", "tin": "60708090-0001",
        "payment_terms_days": "14",
    }, follow_redirects=True)

    from app.models import Account, Contact, Invoice, Item, TaxCode
    from sqlalchemy import select

    db = dbmod.SessionLocal()
    customer = db.scalar(select(Contact).where(Contact.name == "Zenith Construction Ltd"))
    vendor = db.scalar(select(Contact).where(Contact.name == "Ogun Cement Depot"))
    vat_std = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    wht_goods = db.scalar(select(TaxCode).where(TaxCode.code == "WHT-GOODS"))
    sales_acc = db.scalar(select(Account).where(Account.system_key == "SALES"))
    cust_id, vend_id = customer.id, vendor.id
    vat_id, wht_id, sales_id = vat_std.id, wht_goods.id, sales_acc.id
    db.close()

    # --- An item ----------------------------------------------------------
    r = client.post("/inventory/save", data={
        "name": "Dangote Cement 50kg", "item_type": "STOCK", "unit": "bag",
        "sale_price": "6500.00", "purchase_price": "4800.00",
        "track_stock": "1", "is_active": "1", "reorder_level": "50",
        "sales_account_id": str(sales_id),
        "sale_tax_code_id": str(vat_id), "purchase_tax_code_id": str(vat_id),
    }, follow_redirects=True)
    assert "Dangote Cement 50kg" in r.text

    db = dbmod.SessionLocal()
    item = db.scalar(select(Item).where(Item.name == "Dangote Cement 50kg"))
    item_id = item.id
    db.close()

    # --- Buy 500 bags -----------------------------------------------------
    r = client.post("/purchases/bills/save", data={
        "contact_id": str(vend_id), "date": D, "due_date": D,
        "vendor_invoice_no": "OCD-4471", "action": "post",
        "wht_code_id": str(wht_id),
        "line_item_id": str(item_id), "line_description": "Dangote Cement 50kg",
        "line_qty": "500", "line_price": "4800.00", "line_disc": "0",
        "line_account": "", "line_tax": str(vat_id),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "posted" in r.text.lower()

    db = dbmod.SessionLocal()
    item = db.get(Item, item_id)
    assert item.qty_on_hand == 500 * 1000
    assert item.stock_value == 2_400_000_00
    db.close()

    # --- Sell 120 bags ----------------------------------------------------
    r = client.post("/sales/invoices/save", data={
        "contact_id": str(cust_id), "date": D,
        "due_date": (TODAY + timedelta(days=30)).isoformat(),
        "po_number": "PO-8891", "action": "post", "wht_code_id": str(wht_id),
        "line_item_id": str(item_id), "line_description": "Dangote Cement 50kg",
        "line_qty": "120", "line_price": "6500.00", "line_disc": "0",
        "line_account": str(sales_id), "line_tax": str(vat_id),
        "memo": "", "terms": "Payment due within 30 days.",
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    inv = db.scalar(select(Invoice).where(Invoice.contact_id == cust_id))
    assert inv.status == "POSTED"
    assert inv.subtotal == 780_000_00           # 120 × ₦6,500
    assert inv.vat_total == 58_500_00           # 7.5%
    assert inv.total == 838_500_00
    assert inv.wht_total == 15_600_00           # 2% of the net
    assert inv.cogs_total == 576_000_00         # 120 × ₦4,800
    inv_id = inv.id
    item = db.get(Item, item_id)
    assert item.qty_on_hand == 380 * 1000
    db.close()

    # The invoice pages render
    ok(client, f"/sales/invoices/{inv_id}")
    ok(client, f"/sales/invoices/{inv_id}/print")

    # --- Get paid, less the WHT the customer withheld ----------------------
    r = client.post("/receipts/save", data={
        "contact_id": str(cust_id), "date": D, "bank_account_id": "1",
        "method": "Bank transfer", "reference": "FT26061500123",
        "amount": "8229.00" and "822900.00", "wht_amount": "15600.00",
        "discount_amount": "", "bank_charge": "",
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    inv = db.get(Invoice, inv_id)
    assert inv.amount_paid == 838_500_00
    assert inv.balance_due == 0
    assert inv.status == "PAID"
    db.close()

    # --- Pay the supplier, withholding 2% ---------------------------------
    from app.models import Bill

    db = dbmod.SessionLocal()
    bill = db.scalar(select(Bill).where(Bill.contact_id == vend_id))
    bill_id, bill_total, bill_wht = bill.id, bill.total, bill.wht_total
    db.close()

    r = client.post("/payments/save", data={
        "contact_id": str(vend_id), "date": D, "bank_account_id": "1",
        "method": "Bank transfer", "reference": "FT26061500456",
        "amount": f"{(bill_total - bill_wht) / 100:.2f}",
        "wht_amount": f"{bill_wht / 100:.2f}",
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    bill = db.get(Bill, bill_id)
    assert bill.status == "PAID"
    db.close()

    # --- A cash expense with VAT ------------------------------------------
    db = dbmod.SessionLocal()
    diesel = db.scalar(select(Account).where(Account.code == "6120"))
    diesel_id = diesel.id
    db.close()

    r = client.post("/purchases/expense/save", data={
        "date": D, "bank_account_id": "1", "account_id": str(diesel_id),
        "amount": "150000.00", "tax_code_id": str(vat_id),
        "payee": "Total Filling Station", "memo": "Diesel for the generator",
    }, follow_redirects=True)
    assert r.status_code == 200

    # --- A manual journal --------------------------------------------------
    db = dbmod.SessionLocal()
    rent = db.scalar(select(Account).where(Account.code == "6100"))
    bank_acc = db.scalar(select(Account).where(Account.code == "1020"))
    rent_id, bank_id = rent.id, bank_acc.id
    db.close()

    r = client.post("/journals/save", data={
        "date": D, "memo": "June office rent", "reference": "RENT-06",
        "line_account": [str(rent_id), str(bank_id)],
        "line_debit": ["250000.00", ""],
        "line_credit": ["", "250000.00"],
        "line_memo": ["Office rent for June", "Paid by transfer"],
        "line_contact": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "posted" in r.text.lower()

    # --- The books must still balance -------------------------------------
    from app.services import reports as R

    db = dbmod.SessionLocal()
    _rows, td, tc = R.trial_balance(db, None, date(2030, 1, 1))
    assert td == tc, "Trial balance is out after a full cycle through the interface"
    bs = R.balance_sheet(db, date(2030, 1, 1))
    assert bs.difference == 0, "Balance sheet does not balance"
    cf = R.cash_flow(db, date(2020, 1, 1), date(2030, 1, 1))
    assert cf.difference == 0, "Cash flow does not reconcile"
    db.close()

    # --- The reports render with real data --------------------------------
    for url in [
        "/reports/trial-balance", "/reports/profit-and-loss", "/reports/balance-sheet",
        "/reports/cash-flow", "/reports/vat", "/reports/wht?kind=payable",
        "/reports/aging?kind=ar", "/reports/aging?kind=ap", "/reports/general-ledger",
        "/inventory/valuation", f"/contacts/{cust_id}", f"/contacts/{cust_id}/statement",
        f"/inventory/{item_id}", "/banking/1", "/banking/1/reconcile",
    ]:
        ok(client, url)


def test_payroll_through_the_interface(client):
    from sqlalchemy import select

    from app.models import Employee, PayrollRun

    # --- Hire two people --------------------------------------------------
    r = client.post("/payroll/employees/save", data={
        "first_name": "Adaeze", "last_name": "Okonkwo", "status": "ACTIVE",
        "job_title": "Accountant", "department": "Finance",
        "frequency": "MONTHLY", "pay_basis": "FIXED", "default_units": "1",
        "basic": "450000.00", "housing": "150000.00", "transport": "75000.00",
        "pension_enrolled": "1", "nhf_enrolled": "1",
        "tin": "30405060-0001", "pfa_name": "ARM Pensions", "pension_pin": "PEN100200300",
        "bank_name": "GTBank", "bank_account_no": "0123456789",
        "annual_rent_paid": "1800000.00",
        "state_of_residence": "Lagos",
        "comp_name": ["Leave allowance", "", ""],
        "comp_kind": ["EARNING", "EARNING", "EARNING"],
        "comp_amount": ["37500.00", "", ""],
        "comp_rate": ["", "", ""],
        "comp_taxable": ["1"],
        "comp_account": ["", "", ""],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Adaeze Okonkwo" in r.text

    r = client.post("/payroll/employees/save", data={
        "first_name": "Musa", "last_name": "Danjuma", "status": "ACTIVE",
        "job_title": "Yard hand", "frequency": "MONTHLY",
        "pay_basis": "DAILY_RATE", "default_units": "22",
        "basic": "6500.00", "housing": "0", "transport": "0",
        "bank_name": "Access Bank", "bank_account_no": "0987654321",
        "comp_name": [""], "comp_kind": ["EARNING"], "comp_amount": [""],
        "comp_rate": [""], "comp_account": [""],
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    people = list(db.scalars(select(Employee)))
    assert len(people) == 2
    ada = [e for e in people if e.first_name == "Adaeze"][0]
    ada_id = ada.id
    db.close()

    ok(client, f"/payroll/employees/{ada_id}")
    ok(client, f"/payroll/employees/{ada_id}/edit")

    # --- Run the payroll --------------------------------------------------
    start = TODAY.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    r = client.post("/payroll/runs/create", data={
        "frequency": "MONTHLY",
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "pay_date": end.isoformat(),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "drafted for 2 employee" in r.text

    db = dbmod.SessionLocal()
    run = db.scalar(select(PayrollRun))
    run_id = run.id
    assert run.status == "DRAFT"
    assert run.employee_count == 2
    # Adaeze: 450k + 150k + 37.5k leave = ₦675,000 gross
    ada_slip = [s for s in run.payslips if s.employee_name == "Adaeze Okonkwo"][0]
    assert ada_slip.gross == 712_500_00
    assert ada_slip.pension_employee == 54_000_00     # 8% of 675,000 pensionable
    assert ada_slip.nhf == 11_250_00                  # 2.5% of basic
    assert ada_slip.rent_relief == 360_000_00         # 20% of ₦1.8m
    # Musa: 22 days at ₦6,500
    musa_slip = [s for s in run.payslips if s.employee_name == "Musa Danjuma"][0]
    assert musa_slip.gross == 143_000_00
    musa_slip_id = musa_slip.id
    db.close()

    ok(client, f"/payroll/runs/{run_id}")
    ok(client, f"/payroll/runs/{run_id}/payslips")
    ok(client, f"/payroll/runs/{run_id}/bank-schedule")
    ok(client, f"/payroll/payslips/{musa_slip_id}")

    # --- Musa only worked 18 days ------------------------------------------
    db = dbmod.SessionLocal()
    run = db.get(PayrollRun, run_id)
    ids = [str(s.id) for s in run.payslips]
    units = ["1" if s.employee_name.startswith("Adaeze") else "18" for s in run.payslips]
    db.close()

    r = client.post(f"/payroll/runs/{run_id}/units", data={
        "payslip_id": ids, "units": units,
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    run = db.get(PayrollRun, run_id)
    musa = [s for s in run.payslips if s.employee_name == "Musa Danjuma"][0]
    assert musa.gross == 117_000_00      # 18 days at ₦6,500
    db.close()

    # --- Post it -----------------------------------------------------------
    r = client.post(f"/payroll/runs/{run_id}/post", follow_redirects=True)
    assert r.status_code == 200
    assert "posted" in r.text.lower()

    db = dbmod.SessionLocal()
    run = db.get(PayrollRun, run_id)
    assert run.status == "POSTED"
    gross, net, paye = run.gross_total, run.net_total, run.paye_total
    assert gross > 0 and paye > 0
    db.close()

    # --- Pay the staff ------------------------------------------------------
    r = client.post(f"/payroll/runs/{run_id}/pay", data={
        "bank_account_id": "1", "pay_date": end.isoformat(),
    }, follow_redirects=True)
    assert r.status_code == 200

    db = dbmod.SessionLocal()
    run = db.get(PayrollRun, run_id)
    assert run.status == "PAID"
    db.close()

    # --- Remit the PAYE ------------------------------------------------------
    from app.models import Account

    db = dbmod.SessionLocal()
    paye_acc = db.scalar(select(Account).where(Account.system_key == "PAYE_PAYABLE"))
    paye_id = paye_acc.id
    db.close()

    r = client.post("/payroll/remittances/pay", data={
        "account_id": str(paye_id), "bank_account_id": "1",
        "amount": f"{paye / 100:.2f}", "date": TODAY.isoformat(),
        "reference": "LIRS-2026-08", "memo": "PAYE for the month",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "remitted" in r.text.lower()

    # --- And the books still balance ----------------------------------------
    from app.services import reports as R
    from app.services.posting import account_net

    db = dbmod.SessionLocal()
    _rows, td, tc = R.trial_balance(db, None, date(2030, 1, 1))
    assert td == tc, "Trial balance is out after running payroll"
    bs = R.balance_sheet(db, date(2030, 1, 1))
    assert bs.difference == 0, "Balance sheet does not balance after payroll"
    assert account_net(db, paye_id, None, date(2030, 1, 1)) == 0
    db.close()

    for url in ["/payroll", "/payroll/remittances", "/payroll/reports/schedules",
                "/payroll/reports/schedules?kind=pension", "/payroll/employees"]:
        ok(client, url)


def test_payroll_schedule_csv(client):
    r = client.get("/payroll/reports/schedules?kind=paye&format=csv", follow_redirects=True)
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "Pension PIN" in r.text


def test_csv_exports(client):
    for url in [
        "/reports/trial-balance?format=csv",
        "/reports/profit-and-loss?format=csv",
        "/reports/balance-sheet?format=csv",
        "/reports/aging?kind=ar&format=csv",
        "/reports/wht?kind=payable&format=csv",
    ]:
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]
        assert len(r.text.splitlines()) >= 1


# --------------------------------------------------------------------------
# A fixed asset, from purchase to disposal, through the forms
# --------------------------------------------------------------------------


def test_fixed_assets_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import AssetCategory

    db = dbmod.SessionLocal()
    vehicles = db.scalar(select(AssetCategory).where(AssetCategory.name == "Motor Vehicles")).id
    db.close()

    # --- Buy a bus --------------------------------------------------------
    r = client.post("/assets/save", data={
        "name": "Toyota Hiace bus", "category_id": str(vehicles),
        "purchase_date": "2026-01-08", "in_service_date": "2026-01-08",
        "cost": "12,000,000.00", "residual_value": "",
        "method": "STRAIGHT", "useful_life_years": "4", "useful_life_months_extra": "",
        "rate_pct": "25", "registration_no": "LAG-441-XA",
        "location": "Ikeja yard", "custodian": "Sunday the driver",
        "capitalise": "1", "funding_account_id": _account_id(client, "1020"),
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Toyota Hiace bus" in r.text
    assert "posted to the ledger" in r.text
    asset_id = int(re.search(r"/assets/(\d+)/edit", r.text).group(1))

    # The forecast should show ₦250,000 a month
    detail = client.get(f"/assets/{asset_id}", follow_redirects=True)
    assert "250,000.00" in detail.text
    assert "LAG-441-XA" in detail.text

    # --- Run January ------------------------------------------------------
    r = client.post("/assets/depreciation/run", data={"period": "202601"},
                    follow_redirects=True)
    assert "Toyota Hiace bus" in r.text
    run_id = int(re.search(r"/assets/depreciation/(\d+)", r.text).group(1))
    assert "250,000.00" in r.text

    r = client.post(f"/assets/depreciation/{run_id}/post", follow_redirects=True)
    assert "posted" in r.text.lower()

    # The register now shows the written-down value
    reg = client.get("/assets", follow_redirects=True)
    assert "11,750,000.00" in reg.text

    # --- The same month cannot be run twice -------------------------------
    again = client.post("/assets/depreciation/run", data={"period": "202601"},
                        follow_redirects=True)
    assert "already exists" in again.text

    # --- The schedule ties to the register --------------------------------
    sched = client.get("/assets/schedule?start=2026-01-01&end=2026-12-31",
                       follow_redirects=True)
    assert "12,000,000.00" in sched.text
    assert "11,750,000.00" in sched.text

    csv = client.get("/assets/schedule?start=2026-01-01&end=2026-12-31&format=csv",
                     follow_redirects=True)
    assert "text/csv" in csv.headers["content-type"]
    assert "Motor Vehicles" in csv.text

    # --- Sell it ----------------------------------------------------------
    r = client.post(f"/assets/{asset_id}/dispose", data={
        "date": "2026-02-14", "proceeds": "12,500,000.00",
        "bank_id": _default_bank_id(client), "note": "Sold to Musa Motors",
    }, follow_redirects=True)
    assert "off the register" in r.text
    assert "disposed" in r.text.lower()
    assert "Musa Motors" in r.text

    # and the gain lands in the profit and loss
    pl = client.get("/reports/profit-and-loss?start=2026-01-01&end=2026-12-31",
                    follow_redirects=True)
    assert "Gain on Disposal" in pl.text

    # --- The books still balance -----------------------------------------
    tb = client.get("/reports/trial-balance", follow_redirects=True)
    assert "The books balance" in tb.text


def _account_id(client, code: str) -> str:
    from sqlalchemy import select

    from app.models import Account

    db = dbmod.SessionLocal()
    acc = db.scalar(select(Account).where(Account.code == code))
    aid = acc.id
    db.close()
    return str(aid)


def _default_bank_id(client) -> str:
    from sqlalchemy import select

    from app.models import BankAccount

    db = dbmod.SessionLocal()
    bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))
    bid = bank.id
    db.close()
    return str(bid)


def test_an_asset_cannot_have_a_residual_above_its_cost(client):
    from sqlalchemy import select

    from app.models import AssetCategory

    db = dbmod.SessionLocal()
    cat = db.scalar(select(AssetCategory).where(AssetCategory.name == "Plant and Machinery")).id
    db.close()

    r = client.post("/assets/save", data={
        "name": "Impossible mixer", "category_id": str(cat),
        "purchase_date": "2026-03-01", "in_service_date": "2026-03-01",
        "cost": "500,000.00", "residual_value": "900,000.00",
        "method": "STRAIGHT", "useful_life_years": "5",
    }, follow_redirects=True)
    assert "cannot be more than the cost" in r.text


def test_depreciation_categories_can_be_edited(client):
    from sqlalchemy import select

    from app.models import Account, AssetCategory

    db = dbmod.SessionLocal()
    cat = db.scalar(select(AssetCategory).where(AssetCategory.name == "Generators and Power Equipment"))
    cid, asset_a = cat.id, cat.asset_account_id
    accum_a, exp_a = cat.accum_dep_account_id, cat.expense_account_id
    db.close()

    r = client.post("/assets/categories/save", data={
        "id": str(cid), "name": "Generators and Power Equipment",
        "method": "REDUCING", "useful_life_years": "0", "rate_pct": "20",
        "residual_pct": "0", "asset_account_id": str(asset_a),
        "accum_dep_account_id": str(accum_a), "expense_account_id": str(exp_a),
        "is_active": "1",
    }, follow_redirects=True)
    assert "20% reducing balance" in r.text


# --------------------------------------------------------------------------
# A recurring invoice, through the forms
# --------------------------------------------------------------------------


def test_recurring_invoices_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account, Contact, TaxCode

    db = dbmod.SessionLocal()
    customer = db.scalar(select(Contact).where(Contact.name == "Zenith Construction Ltd")).id
    sales = db.scalar(select(Account).where(Account.code == "4010")).id
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD")).id
    db.close()

    # --- Set up monthly rent, back-dated three months ---------------------
    start = (TODAY.replace(day=1) - timedelta(days=70)).replace(day=1)
    r = client.post("/recurring/save", data={
        "name": "Ikoyi office rent", "doc_type": "INVOICE",
        "contact_id": str(customer), "frequency": "MONTHLY",
        "start_date": start.isoformat(), "anchor_day": "1",
        "payment_terms_days": "30", "is_active": "1",
        "memo": "Rent for the Ikoyi office.",
        "line_description": ["Monthly office rent", "", ""],
        "line_item_id": ["", "", ""],
        "line_qty": ["1", "", ""],
        "line_price": ["450,000.00", "", ""],
        "line_disc": ["0", "0", "0"],
        "line_account": [str(sales), "", ""],
        "line_tax": [str(vat), "", ""],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Ikoyi office rent" in r.text
    template_id = int(re.search(r"/recurring/(\d+)/edit", r.text).group(1))

    # It should say three are owed
    detail = client.get(f"/recurring/{template_id}", follow_redirects=True)
    assert "owed" in detail.text
    assert "450,000.00" in detail.text

    # --- Raise them -------------------------------------------------------
    r = client.post(f"/recurring/{template_id}/generate", follow_redirects=True)
    assert r.status_code == 200
    assert "documents created" in r.text or "created" in r.text

    detail = client.get(f"/recurring/{template_id}", follow_redirects=True)
    assert "What it has raised" in detail.text
    numbers = set(re.findall(r"INV-\d+", detail.text))
    assert len(numbers) >= 3

    # Each generated invoice is a draft with the right figures
    listing = client.get("/sales/invoices?status=DRAFT", follow_redirects=True)
    assert "483,750.00" in listing.text        # 450,000 + 7.5% VAT

    # --- Nothing is due a second time -------------------------------------
    r = client.post(f"/recurring/{template_id}/generate", follow_redirects=True)
    assert "Nothing is due" in r.text

    # --- Pause it ---------------------------------------------------------
    r = client.post(f"/recurring/{template_id}/pause", follow_redirects=True)
    assert "paused" in r.text
    index = client.get("/recurring", follow_redirects=True)
    assert "paused" in index.text


def test_a_recurring_template_needs_a_line(client):
    from sqlalchemy import select

    from app.models import Contact

    db = dbmod.SessionLocal()
    customer = db.scalar(select(Contact).where(Contact.is_customer.is_(True))).id
    db.close()

    r = client.post("/recurring/save", data={
        "name": "Empty template", "doc_type": "INVOICE",
        "contact_id": str(customer), "frequency": "MONTHLY",
        "start_date": D, "anchor_day": "1", "is_active": "1",
        "line_description": ["", ""], "line_item_id": ["", ""],
        "line_qty": ["", ""], "line_price": ["", ""],
        "line_disc": ["0", "0"], "line_account": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert "at least one line" in r.text


# --------------------------------------------------------------------------
# A budget, through the forms
# --------------------------------------------------------------------------


def test_budgets_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account

    db = dbmod.SessionLocal()
    sales = db.scalar(select(Account).where(Account.code == "4000")).id
    rent = db.scalar(select(Account).where(Account.code == "6100")).id
    db.close()

    year = TODAY.year
    r = client.post("/budgets/create", data={
        "name": f"{year} plan", "start_date": f"{year}-01-01",
        "end_date": f"{year}-12-31",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert f"{year} plan" in r.text
    budget_id = int(re.search(r"/budgets/(\d+)/(?:edit|save)", r.text).group(1))

    # --- Spread a year's rent evenly --------------------------------------
    r = client.post(f"/budgets/{budget_id}/spread", data={
        "account_id": str(rent), "annual": "12,000,000.00",
    }, follow_redirects=True)
    assert "Spread evenly" in r.text
    assert "1,000,000.00" in r.text          # 12m / 12 months

    # --- Type a revenue figure into one month -----------------------------
    r = client.post(f"/budgets/{budget_id}/save", data={
        "name": f"{year} plan", "is_active": "1", "notes": "First cut.",
        f"cell_{sales}_{year}01": "5,000,000.00",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Budget against actual" in r.text

    # --- The variance report reads the ledger -----------------------------
    r = client.get(f"/budgets/variance?budget={budget_id}"
                   f"&start={year}-01-01&end={year}-12-31", follow_redirects=True)
    assert "12,000,000.00" in r.text         # the rent budget for the year
    assert "favourable" in r.text or "adverse" in r.text

    csv = client.get(f"/budgets/variance?budget={budget_id}"
                     f"&start={year}-01-01&end={year}-12-31&format=csv",
                     follow_redirects=True)
    assert "text/csv" in csv.headers["content-type"]
    assert "Rent and Rates" in csv.text


def test_a_budget_cannot_be_created_twice(client):
    year = TODAY.year
    data = {"name": f"{year} duplicate", "start_date": f"{year}-01-01",
            "end_date": f"{year}-12-31"}
    client.post("/budgets/create", data=data, follow_redirects=True)
    r = client.post("/budgets/create", data=data, follow_redirects=True)
    assert "already starts on" in r.text


# --------------------------------------------------------------------------
# Warehouses, batches, serial numbers and FIFO, through the forms
# --------------------------------------------------------------------------


def test_inventory_depth_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account, Contact, Item, Location, TaxCode

    db = dbmod.SessionLocal()
    sales_acc = db.scalar(select(Account).where(Account.code == "4000")).id
    purch_acc = db.scalar(select(Account).where(Account.code == "5010")).id
    inv_acc = db.scalar(select(Account).where(Account.code == "1200")).id
    cogs_acc = db.scalar(select(Account).where(Account.code == "5000")).id
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD")).id
    supplier = db.scalar(select(Contact).where(Contact.name == "Ogun Cement Depot")).id
    customer = db.scalar(select(Contact).where(Contact.name == "Zenith Construction Ltd")).id
    db.close()

    # --- Two more stores --------------------------------------------------
    for code, name, default in [("IKJ", "Ikeja yard", "1"), ("APA", "Apapa depot", "")]:
        r = client.post("/inventory/locations/save", data={
            "code": code, "name": name, "is_active": "1", "is_default": default,
            "manager": "Sunday", "address": "",
        }, follow_redirects=True)
        assert name in r.text

    db = dbmod.SessionLocal()
    ikeja = db.scalar(select(Location).where(Location.code == "IKJ")).id
    apapa = db.scalar(select(Location).where(Location.code == "APA")).id
    db.close()

    # --- A generator tracked by serial number, costed first in first out ---
    r = client.post("/inventory/save", data={
        "code": "GEN-55", "name": "5.5 KVA generator", "item_type": "STOCK",
        "unit": "each", "sale_price": "750,000.00", "purchase_price": "500,000.00",
        "sales_account_id": str(sales_acc), "purchase_account_id": str(purch_acc),
        "inventory_account_id": str(inv_acc), "cogs_account_id": str(cogs_acc),
        "sale_tax_code_id": str(vat), "track_stock": "1", "is_active": "1",
        "costing_method": "FIFO", "track_serials": "1", "warranty_months": "12",
    }, follow_redirects=True)
    assert "5.5 KVA generator" in r.text
    gen_id = int(re.search(r"/inventory/(\d+)/edit", r.text).group(1))

    # --- Buy three, into Ikeja, with serial numbers ------------------------
    r = client.post("/purchases/bills/save", data={
        "contact_id": str(supplier), "date": D, "due_date": D,
        "location_id": str(ikeja), "action": "post",
        "line_item_id": [str(gen_id)], "line_description": ["5.5 KVA generator"],
        "line_qty": ["3"], "line_price": ["500,000.00"], "line_disc": ["0"],
        "line_account": [str(purch_acc)], "line_tax": [""],
        "line_batch": [""], "line_expiry": [""],
        "line_serials": ["SN-A100\nSN-A101\nSN-A102"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text

    detail = client.get(f"/inventory/{gen_id}", follow_redirects=True)
    assert "SN-A100" in detail.text
    assert "Ikeja yard" in detail.text
    assert "first in, first out" in detail.text

    serials = client.get("/inventory/serials?status=IN_STOCK", follow_redirects=True)
    assert "SN-A101" in serials.text

    # --- Move one to Apapa ------------------------------------------------
    r = client.post("/inventory/transfer", data={
        "item_id": str(gen_id), "qty": "1", "date": D,
        "from_location_id": str(ikeja), "to_location_id": str(apapa),
        "memo": "Van 3",
    }, follow_redirects=True)
    assert "Apapa depot" in r.text
    assert "Nothing was posted to the ledger" in r.text

    # --- Sell one by serial number ----------------------------------------
    r = client.post("/sales/invoices/save", data={
        "contact_id": str(customer), "date": D, "due_date": D,
        "location_id": str(ikeja), "action": "post",
        "line_item_id": [str(gen_id)], "line_description": ["5.5 KVA generator"],
        "line_qty": ["1"], "line_price": ["750,000.00"], "line_disc": ["0"],
        "line_account": [str(sales_acc)], "line_tax": [str(vat)],
        "line_batch": [""], "line_serials": ["SN-A100"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text

    sold = client.get("/inventory/serials?q=SN-A100", follow_redirects=True)
    assert "sold" in sold.text
    assert "in warranty" in sold.text

    # --- Selling a serial that is not in stock is refused ------------------
    r = client.post("/sales/invoices/save", data={
        "contact_id": str(customer), "date": D, "due_date": D,
        "location_id": str(ikeja), "action": "post",
        "line_item_id": [str(gen_id)], "line_description": ["5.5 KVA generator"],
        "line_qty": ["1"], "line_price": ["750,000.00"], "line_disc": ["0"],
        "line_account": [str(sales_acc)], "line_tax": [str(vat)],
        "line_batch": [""], "line_serials": ["SN-NOT-REAL"],
    }, follow_redirects=True)
    assert "not in stock" in r.text

    # --- The books still balance ------------------------------------------
    tb = client.get("/reports/trial-balance", follow_redirects=True)
    assert "The books balance" in tb.text


def test_batches_and_expiry_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account, Contact, TaxCode

    db = dbmod.SessionLocal()
    sales_acc = db.scalar(select(Account).where(Account.code == "4000")).id
    purch_acc = db.scalar(select(Account).where(Account.code == "5010")).id
    inv_acc = db.scalar(select(Account).where(Account.code == "1200")).id
    cogs_acc = db.scalar(select(Account).where(Account.code == "5000")).id
    supplier = db.scalar(select(Contact).where(Contact.is_vendor.is_(True))).id
    db.close()

    r = client.post("/inventory/save", data={
        "code": "PNT-20", "name": "Marine paint 20L", "item_type": "STOCK",
        "unit": "drum", "sale_price": "45,000.00", "purchase_price": "30,000.00",
        "sales_account_id": str(sales_acc), "purchase_account_id": str(purch_acc),
        "inventory_account_id": str(inv_acc), "cogs_account_id": str(cogs_acc),
        "track_stock": "1", "is_active": "1", "track_batches": "1",
        "shelf_life_days": "60",
    }, follow_redirects=True)
    paint_id = int(re.search(r"/inventory/(\d+)/edit", r.text).group(1))

    r = client.post("/purchases/bills/save", data={
        "contact_id": str(supplier), "date": D, "due_date": D, "action": "post",
        "line_item_id": [str(paint_id)], "line_description": ["Marine paint 20L"],
        "line_qty": ["10"], "line_price": ["30,000.00"], "line_disc": ["0"],
        "line_account": [str(purch_acc)], "line_tax": [""],
        "line_batch": ["LOT-2026-04"], "line_expiry": [""], "line_serials": [""],
    }, follow_redirects=True)
    assert "Internal Server Error" not in r.text

    detail = client.get(f"/inventory/{paint_id}", follow_redirects=True)
    assert "LOT-2026-04" in detail.text
    assert "batch tracked" in detail.text

    # The shelf life set an expiry date, so it shows in the expiry report
    expiring = client.get("/inventory/expiring?days=90", follow_redirects=True)
    assert "LOT-2026-04" in expiring.text
    assert "Marine paint 20L" in expiring.text

    csv = client.get("/inventory/expiring?days=90&format=csv", follow_redirects=True)
    assert "text/csv" in csv.headers["content-type"]
    assert "LOT-2026-04" in csv.text


def test_the_costing_method_is_locked_while_stock_is_on_hand(client):
    from sqlalchemy import select

    from app.models import Item

    db = dbmod.SessionLocal()
    item = db.scalar(select(Item).where(Item.code == "PNT-20"))
    item_id, name = item.id, item.name
    accounts = dict(sales=item.sales_account_id, purch=item.purchase_account_id,
                    inv=item.inventory_account_id, cogs=item.cogs_account_id)
    db.close()

    r = client.post("/inventory/save", data={
        "id": str(item_id), "code": "PNT-20", "name": name, "item_type": "STOCK",
        "unit": "drum", "sale_price": "45,000.00", "purchase_price": "30,000.00",
        "sales_account_id": str(accounts["sales"]),
        "purchase_account_id": str(accounts["purch"]),
        "inventory_account_id": str(accounts["inv"]),
        "cogs_account_id": str(accounts["cogs"]),
        "track_stock": "1", "is_active": "1", "track_batches": "1",
        "costing_method": "FIFO",
    }, follow_redirects=True)
    assert "costing method cannot change" in r.text

    db = dbmod.SessionLocal()
    assert db.scalar(select(Item).where(Item.id == item_id)).costing_method == "AVERAGE"
    db.close()


def test_stock_bought_through_the_form_lands_in_the_inventory_account(client):
    """A purchase of stock is an asset, not an expense.

    If it goes to Purchases instead, the stock records and the inventory
    account drift apart for good and the balance sheet understates stock.
    """
    r = client.get("/inventory/valuation", follow_redirects=True)
    assert r.status_code == 200
    assert "does not agree" not in r.text

    from sqlalchemy import select

    from app.models import Account, Item
    from app.services import reports
    from app.services.posting import account_net

    db = dbmod.SessionLocal()
    items, total = reports.inventory_valuation(db)
    inv_acc = db.scalar(select(Account).where(Account.code == "1200"))
    ledger = account_net(db, inv_acc.id, None, TODAY)
    db.close()
    assert total == ledger, (
        f"Stock records say {total} but the inventory account says {ledger}"
    )


# --------------------------------------------------------------------------
# Landed cost, through the forms
# --------------------------------------------------------------------------


def test_landed_cost_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account, Bill, Contact, Item

    client.post("/contacts/save", data={
        "name": "Guangzhou Tile Imports", "is_vendor": "1", "is_active": "1",
        "payment_terms_days": "30",
    }, follow_redirects=True)

    db = dbmod.SessionLocal()
    supplier = db.scalar(
        select(Contact).where(Contact.name == "Guangzhou Tile Imports")).id
    inv_acc = db.scalar(select(Account).where(Account.code == "1200")).id
    cogs_acc = db.scalar(select(Account).where(Account.code == "5000")).id
    sales_acc = db.scalar(select(Account).where(Account.code == "4000")).id
    freight_acc = db.scalar(select(Account).where(Account.code == "5030")).id
    db.close()

    # --- An imported item, bought and posted ------------------------------
    r = client.post("/inventory/save", data={
        "code": "TIL-60", "name": "Porcelain tiles 60x60", "item_type": "STOCK",
        "unit": "carton", "sale_price": "36,000.00", "purchase_price": "24,000.00",
        "sales_account_id": str(sales_acc), "inventory_account_id": str(inv_acc),
        "cogs_account_id": str(cogs_acc), "track_stock": "1", "is_active": "1",
        "costing_method": "AVERAGE",
    }, follow_redirects=True)
    tile_id = int(re.search(r"/inventory/(\d+)/edit", r.text).group(1))

    client.post("/purchases/bills/save", data={
        "contact_id": str(supplier), "date": D, "due_date": D, "action": "post",
        "line_item_id": [str(tile_id)], "line_description": ["Porcelain tiles 60x60"],
        "line_qty": ["100"], "line_price": ["24,000.00"], "line_disc": ["0"],
        "line_account": [""], "line_tax": [""],
        "line_batch": [""], "line_expiry": [""], "line_serials": [""],
    }, follow_redirects=True)

    db = dbmod.SessionLocal()
    bill = db.scalar(select(Bill).where(Bill.doc_type == "BILL")
                     .order_by(Bill.id.desc()))
    bill_id = bill.id
    tiles_before = db.get(Item, tile_id).stock_value
    db.close()
    assert tiles_before == 2_400_000_00    # ₦2.4m, the supplier's price only

    # --- The clearing agent's bill, booked to Freight and Clearing In -----
    client.post("/purchases/expense/save", data={
        "contact_id": str(supplier), "date": D, "bank_account_id": "1",
        "account_id": str(freight_acc), "amount": "600,000.00",
        "description": "Apapa clearing and haulage", "tax_code_id": "",
    }, follow_redirects=True)

    # --- Spread it over the tiles ----------------------------------------
    r = client.post("/landed-costs/create", data={
        "date": D, "basis": "VALUE", "reference": "CONT-4471",
        "note": "40ft container, cleared at Apapa", "bill_id": str(bill_id),
    }, follow_redirects=True)
    assert "CONT-4471" in r.text or "LC-" in r.text
    lc_id = int(re.search(r"/landed-costs/(\d+)/", r.text).group(1))

    r = client.post(f"/landed-costs/{lc_id}/add-charge", data={
        "description": "Apapa clearing and haulage", "amount": "600,000.00",
        "account_id": str(freight_acc), "contact_id": str(supplier),
    }, follow_redirects=True)
    assert "600,000.00" in r.text

    r = client.post(f"/landed-costs/{lc_id}/post", follow_redirects=True)
    assert "part of the stock" in r.text

    # --- The tiles are now worth what they really cost --------------------
    db = dbmod.SessionLocal()
    tiles = db.get(Item, tile_id)
    assert tiles.stock_value == 3_000_000_00      # ₦3.0m
    db.close()

    detail = client.get(f"/inventory/{tile_id}", follow_redirects=True)
    assert "30,000.00" in detail.text              # the new cost per carton

    # --- The books still balance and the stock still ties -----------------
    tb = client.get("/reports/trial-balance", follow_redirects=True)
    assert "The books balance" in tb.text

    from app.services import reports
    from app.services.posting import account_net

    db = dbmod.SessionLocal()
    _items, total = reports.inventory_valuation(db)
    ledger = account_net(db, inv_acc, None, TODAY)
    db.close()
    assert total == ledger

    csv = client.get(f"/landed-costs/{lc_id}?format=csv", follow_redirects=True)
    assert "text/csv" in csv.headers["content-type"]
    assert "Porcelain tiles" in csv.text


def test_a_posted_landed_cost_cannot_be_changed_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import LandedCost

    from app.models import Account

    db = dbmod.SessionLocal()
    lc = db.scalar(select(LandedCost).where(LandedCost.status == "POSTED")
                   .order_by(LandedCost.id.desc()))
    account_id = db.scalar(select(Account).where(Account.code == "5030")).id
    if lc is None:
        db.close()
        pytest.skip("no posted landed cost in this run")
    lc_id = lc.id
    db.close()

    r = client.post(f"/landed-costs/{lc_id}/add-charge", data={
        "description": "Sneaky extra", "amount": "1,000.00",
        "account_id": str(account_id),
    }, follow_redirects=True)
    assert "cannot be changed" in r.text


# --------------------------------------------------------------------------
# VAT withheld at source, through the forms
# --------------------------------------------------------------------------


def test_withholding_vat_through_the_interface(client):
    import re

    from sqlalchemy import select

    from app.models import Account, BankAccount, Contact, Invoice, TaxCode

    db = dbmod.SessionLocal()
    sales_acc = db.scalar(select(Account).where(Account.code == "4000")).id
    vat = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD")).id
    bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True))).id
    db.close()

    # --- A ministry, appointed to withhold VAT ----------------------------
    r = client.post("/contacts/save", data={
        "name": "Lagos State Ministry of Works", "is_customer": "1", "is_active": "1",
        "tin": "01020304-0001", "payment_terms_days": "30", "withholds_vat": "1",
    }, follow_redirects=True)
    assert "Lagos State Ministry of Works" in r.text

    db = dbmod.SessionLocal()
    ministry = db.scalar(
        select(Contact).where(Contact.name == "Lagos State Ministry of Works"))
    ministry_id = ministry.id
    assert ministry.withholds_vat is True
    db.close()

    # --- Invoice them -----------------------------------------------------
    r = client.post("/sales/invoices/save", data={
        "contact_id": str(ministry_id), "date": D, "due_date": D, "action": "post",
        "line_item_id": [""], "line_description": ["Supply of building materials"],
        "line_qty": ["1"], "line_price": ["10,000,000.00"], "line_disc": ["0"],
        "line_account": [str(sales_acc)], "line_tax": [str(vat)],
        "line_batch": [""], "line_serials": [""],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "withholds VAT at source" in r.text
    assert "750,000.00" in r.text              # the VAT they will keep back
    invoice_id = int(re.search(r"/sales/invoices/(\d+)", r.text).group(1))

    # The printed invoice tells the customer what to remit
    printed = client.get(f"/sales/invoices/{invoice_id}/print", follow_redirects=True)
    assert "withheld at source" in printed.text
    assert "10,000,000.00" in printed.text     # net payable

    # --- They pay the net, and send the VAT credit note --------------------
    r = client.post("/receipts/save", data={
        "contact_id": str(ministry_id), "date": D, "bank_account_id": str(bank),
        "method": "Bank transfer", "reference": "MOW/2026/0918",
        "amount": "10,000,000.00", "vat_withheld": "750,000.00",
        "wht_amount": "", "discount_amount": "", "bank_charge": "",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text

    inv = client.get(f"/sales/invoices/{invoice_id}", follow_redirects=True)
    assert "paid" in inv.text.lower()

    # --- And it comes off the VAT return, not out of pocket ---------------
    vat_return = client.get("/reports/vat?period=month", follow_redirects=True)
    assert "withheld at source" in vat_return.text
    assert "750,000.00" in vat_return.text

    tb = client.get("/reports/trial-balance", follow_redirects=True)
    assert "The books balance" in tb.text


# --------------------------------------------------------------------------
# A requisition, from raising it to retiring it, through the forms
# --------------------------------------------------------------------------


def test_requisitions_through_the_interface(client):
    """The whole route: staff, manager, director, finance, retirement."""
    import re

    from sqlalchemy import select

    from app.models import Account, BankAccount, Company, Requisition, User

    # --- Set a limit so the director step is exercised --------------------
    db = dbmod.SessionLocal()
    company = db.get(Company, 1)
    fuel = db.scalar(select(Account).where(Account.code == "6120")).id
    bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True))).id
    db.close()

    r = client.post("/settings/company", data={
        "name": "Adeyemi Trading Ltd", "currency_symbol": "₦", "currency_code": "NGN",
        "fiscal_year_start_month": "1", "is_vat_registered": "1", "vat_rate": "7.5",
        "default_payment_terms_days": "30", "requisition_limit": "500,000.00",
    }, follow_redirects=True)
    assert r.status_code == 200

    # --- Three people: a director, a manager, an accountant, a storekeeper --
    for username, name, role in [
        ("dayo", "Dayo Adeyemi", "admin"),
        ("chioma", "Chioma Eze", "accountant"),
        ("tunde", "Tunde Bello", "accountant"),
        ("musa", "Musa Ibrahim", "clerk"),
    ]:
        client.post("/settings/users/save", data={
            "username": username, "full_name": name, "role": role, "is_active": "1",
        }, follow_redirects=True)

    db = dbmod.SessionLocal()
    ids = {u.username: u.id for u in db.scalars(select(User))}
    db.close()

    def save_user(username, **extra):
        data = {"username": username, "id": str(ids[username]), "is_active": "1"}
        data.update(extra)
        return client.post("/settings/users/save", data=data, follow_redirects=True)

    save_user("dayo", full_name="Dayo Adeyemi", role="admin", job_title="Managing Director",
              approves_large_requisitions="1")
    save_user("chioma", full_name="Chioma Eze", role="accountant",
              job_title="Yard Manager", manager_id=str(ids["dayo"]))
    save_user("tunde", full_name="Tunde Bello", role="accountant",
              job_title="Finance Manager", manager_id=str(ids["dayo"]),
              pays_requisitions="1")
    r = save_user("musa", full_name="Musa Ibrahim", role="clerk",
                  job_title="Storekeeper", department="Yard",
                  manager_id=str(ids["chioma"]),
                  bank_name="GTBank", bank_account_no="0123456789",
                  bank_account_name="Musa Ibrahim")
    assert "bank on file" in r.text

    # Everyone needs a password we know
    db = dbmod.SessionLocal()
    from app.security import hash_password

    for u in db.scalars(select(User)):
        u.password_hash = hash_password("Lagos2026")
        u.must_change_password = False
    db.commit()
    db.close()

    def as_person(username):
        c = TestClient(app)
        c.__enter__()
        c.post("/login", data={"username": username, "password": "Lagos2026", "next": "/"},
               follow_redirects=True)
        return c

    musa = as_person("musa")
    chioma = as_person("chioma")
    tunde = as_person("tunde")
    dayo = as_person("dayo")

    # --- Musa raises one, over the limit ----------------------------------
    r = musa.post("/requisitions/save", data={
        "purpose": "Rewire the yard lighting", "date": D, "department": "Yard",
        "action": "submit",
        "line_description": ["Cable and fittings", "Electrician"],
        "line_account": [str(fuel), str(fuel)],
        "line_vendor": ["", ""],
        "line_qty": ["1", "1"],
        "line_price": ["800,000.00", "150,000.00"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "sent to Chioma Eze" in r.text

    db = dbmod.SessionLocal()
    req_id = db.scalar(
        select(Requisition).order_by(Requisition.id.desc())).id
    db.close()

    # --- Musa cannot approve his own --------------------------------------
    r = musa.post(f"/requisitions/{req_id}/approve", data={"note": "Looks fine to me"},
                  follow_redirects=True)
    assert "your own requisition" in r.text

    # --- Tunde cannot pay it before the approvals -------------------------
    r = tunde.post(f"/requisitions/{req_id}/pay", data={
        "bank_account_id": str(bank), "date": D, "amount": "950,000.00",
    }, follow_redirects=True)
    assert "not ready to be paid" in r.text

    # --- Chioma sends it back, and must say why ---------------------------
    r = chioma.post(f"/requisitions/{req_id}/reject", data={"reason": ""},
                    follow_redirects=True)
    assert "Say why" in r.text

    r = chioma.post(f"/requisitions/{req_id}/reject", data={
        "reason": "Get a second quote for the electrician before I sign this.",
    }, follow_redirects=True)
    assert "sent back to Musa Ibrahim" in r.text

    # Musa sees it, with the reason, and so does his dashboard
    mine = musa.get("/requisitions?view=mine", follow_redirects=True)
    assert "second quote" in mine.text
    home = musa.get("/", follow_redirects=True)
    assert "sent back" in home.text

    # --- He corrects it and sends it again --------------------------------
    r = musa.post("/requisitions/save", data={
        "id": str(req_id), "purpose": "Rewire the yard lighting", "date": D,
        "department": "Yard", "action": "submit",
        "line_description": ["Cable and fittings", "Electrician (best of 3 quotes)"],
        "line_account": [str(fuel), str(fuel)],
        "line_vendor": ["", ""],
        "line_qty": ["1", "1"],
        "line_price": ["800,000.00", "120,000.00"],
    }, follow_redirects=True)
    assert "sent to Chioma Eze" in r.text

    # --- Chioma approves. It is over the limit, so it goes to the MD ------
    r = chioma.post(f"/requisitions/{req_id}/approve", data={
        "note": "Agreed, the wiring is a fire risk.",
    }, follow_redirects=True)
    assert "now with a director" in r.text

    # Finance still cannot touch it
    r = tunde.post(f"/requisitions/{req_id}/pay", data={
        "bank_account_id": str(bank), "date": D, "amount": "920,000.00",
    }, follow_redirects=True)
    assert "not ready to be paid" in r.text

    # --- The MD approves --------------------------------------------------
    r = dayo.post(f"/requisitions/{req_id}/approve", data={"note": "Approved."},
                  follow_redirects=True)
    assert "now with finance" in r.text

    # --- Finance pays it --------------------------------------------------
    r = tunde.post(f"/requisitions/{req_id}/pay", data={
        "bank_account_id": str(bank), "date": D, "amount": "920,000.00",
        "reference": "GTB/44120",
    }, follow_redirects=True)
    assert "sent to Musa Ibrahim" in r.text
    assert "0123456789" in r.text

    # --- Musa retires it, having spent less -------------------------------
    db = dbmod.SessionLocal()
    req = db.get(Requisition, req_id)
    line_ids = [l.id for l in req.lines]
    assert req.status == "PAID"
    assert req.paid_amount == 920_000_00
    db.close()

    r = musa.post(f"/requisitions/{req_id}/retire", data={
        "date": D, "bank_account_id": str(bank),
        "note": "Cable was cheaper at Alaba. Receipts attached.",
        f"spent_{line_ids[0]}": "740,000.00",
        f"spent_{line_ids[1]}": "120,000.00",
    }, follow_redirects=True)
    assert "came back into the bank" in r.text

    db = dbmod.SessionLocal()
    req = db.get(Requisition, req_id)
    assert req.status == "RETIRED"
    assert req.amount_spent == 860_000_00
    assert req.balance_to_return == 60_000_00
    db.close()

    # --- The books balance and the expense is what was actually spent -----
    tb = dayo.get("/reports/trial-balance", follow_redirects=True)
    assert "The books balance" in tb.text

    for c in (musa, chioma, tunde, dayo):
        c.__exit__(None, None, None)


def test_a_requisition_can_carry_the_vendors_invoice(client):
    import re

    from sqlalchemy import select

    from app.models import Account

    db = dbmod.SessionLocal()
    fuel = db.scalar(select(Account).where(Account.code == "6120")).id
    db.close()

    r = client.post("/requisitions/save", data={
        "purpose": "Diesel for the generator", "date": D, "action": "save",
        "line_description": ["Two drums of diesel"], "line_account": [str(fuel)],
        "line_vendor": [""], "line_qty": ["1"], "line_price": ["150,000.00"],
    }, follow_redirects=True)
    assert "Attachments" in r.text

    from app.models import Requisition

    db = dbmod.SessionLocal()
    req_id = db.scalar(select(Requisition).order_by(Requisition.id.desc())).id
    db.close()

    r = client.post("/attachments/upload", data={
        "doc_type": "REQUISITION", "doc_id": str(req_id),
        "note": "Vendor invoice from Ogba filling station",
    }, files={"files": ("invoice.pdf", b"%PDF-1.4\ntrailer\n", "application/pdf")},
        follow_redirects=True)
    assert "invoice.pdf" in r.text
    assert "Vendor invoice" in r.text


def test_somebody_with_no_manager_is_told_plainly(client):
    from sqlalchemy import select

    from app.models import Account, User

    db = dbmod.SessionLocal()
    fuel = db.scalar(select(Account).where(Account.code == "6120")).id
    admin = db.scalar(select(User).where(User.username == "admin"))
    admin.manager_id = None
    db.commit()
    db.close()

    r = client.post("/requisitions/save", data={
        "purpose": "Something", "date": D, "action": "submit",
        "line_description": ["Bits"], "line_account": [str(fuel)],
        "line_vendor": [""], "line_qty": ["1"], "line_price": ["10,000.00"],
    }, follow_redirects=True)
    assert "no manager set" in r.text


def test_unbalanced_journal_is_rejected(client):
    from sqlalchemy import select

    from app.models import Account

    db = dbmod.SessionLocal()
    a = db.scalar(select(Account).where(Account.code == "6100")).id
    b = db.scalar(select(Account).where(Account.code == "1020")).id
    db.close()

    r = client.post("/journals/save", data={
        "date": D, "memo": "Deliberately unbalanced",
        "line_account": [str(a), str(b)],
        "line_debit": ["1000.00", ""], "line_credit": ["", "900.00"],
        "line_memo": ["", ""], "line_contact": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert "does not balance" in r.text


def test_period_lock_is_enforced_in_the_interface(client):
    from sqlalchemy import select

    from app.models import Account

    lock_to = TODAY.isoformat()
    client.post("/settings/periods/lock", data={"lock_date": lock_to}, follow_redirects=True)

    db = dbmod.SessionLocal()
    a = db.scalar(select(Account).where(Account.code == "6100")).id
    b = db.scalar(select(Account).where(Account.code == "1020")).id
    db.close()

    r = client.post("/journals/save", data={
        "date": D, "memo": "Into a locked period",
        "line_account": [str(a), str(b)],
        "line_debit": ["1000.00", ""], "line_credit": ["", "1000.00"],
        "line_memo": ["", ""], "line_contact": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert "locked" in r.text.lower()

    # Remove the lock again so later tests are unaffected
    client.post("/settings/periods/lock", data={"lock_date": ""}, follow_redirects=True)


def test_viewer_cannot_post(client):
    """A read-only user is refused, with a message that explains why."""
    r = client.post("/settings/users/save", data={
        "username": "chidinma", "full_name": "Chidinma O.", "role": "viewer",
        "is_active": "1",
    }, follow_redirects=True)
    assert "Temporary password" in r.text
    temp = r.text.split("Temporary password:")[1].split("—")[0].strip()

    dbmod.reset_all()
    with TestClient(app) as viewer:
        viewer.post("/login", data={"username": "chidinma", "password": temp, "next": "/"},
                    follow_redirects=True)
        viewer.post("/account/password", data={
            "new_password": "Abuja2026", "confirm_password": "Abuja2026",
        }, follow_redirects=True)

        # Reading is fine
        assert viewer.get("/reports/trial-balance", follow_redirects=True).status_code == 200
        # Writing is not
        r = viewer.post("/journals/save", data={
            "date": D, "memo": "Should not be allowed",
            "line_account": ["1"], "line_debit": ["100"], "line_credit": [""],
            "line_memo": [""], "line_contact": [""], "line_tax": [""],
        }, follow_redirects=True)
        assert r.status_code == 403
        assert "does not allow" in r.text


def test_backup_produces_a_usable_file(client):
    r = client.get("/settings/backup/download-live", follow_redirects=True)
    assert r.status_code == 200
    assert len(r.content) > 1000
    assert r.content[:16].startswith(b"SQLite format 3")


# --------------------------------------------------------------------------
# Electronic invoicing
# --------------------------------------------------------------------------


def test_einvoicing_settings_screen_loads(client):
    r = ok(client, "/settings/einvoicing")
    assert "Electronic invoicing" in r.text
    # It must not oversell what it can do.
    assert "rehearsal" in r.text.lower()


def test_the_settings_index_links_to_it(client):
    assert "/settings/einvoicing" in ok(client, "/settings").text


def test_turning_on_rehearsal_and_saving(client):
    r = client.post("/settings/einvoicing", data={
        "mode": "REHEARSAL", "auto_submit": "1",
        "irn_path": "data.irn", "csid_path": "data.csid", "qr_path": "data.qr",
    }, follow_redirects=True)
    assert r.status_code == 200
    from app import db as dbmod
    from app.services import einvoice as ei

    assert ei.load(dbmod.current_slug()).mode == ei.REHEARSAL


def test_live_without_credentials_falls_back_to_rehearsal_rather_than_pretending(client):
    """Claiming to be filing when nothing is configured is the one outcome
    that would actually expose a customer to a penalty."""
    r = client.post("/settings/einvoicing", data={"mode": "LIVE"},
                    follow_redirects=True)
    assert r.status_code == 200
    from app import db as dbmod
    from app.services import einvoice as ei

    settings = ei.load(dbmod.current_slug())
    assert settings.mode == ei.REHEARSAL
    assert "credentials" in r.text or "rehearsal" in r.text.lower()


def test_saving_settings_does_not_wipe_a_stored_secret(client):
    """The form never renders the secret back, so a blank field means
    'unchanged'. Treating it as 'delete' would take a business offline every
    time somebody ticked a checkbox."""
    from app import db as dbmod
    from app.services import einvoice as ei

    slug = dbmod.current_slug()
    ei.save(slug, ei.Settings(mode=ei.REHEARSAL, submit_url="https://example.test/x",
                              client_id="abc", client_secret="keep-me"))
    client.post("/settings/einvoicing", data={
        "mode": "REHEARSAL", "submit_url": "https://example.test/x",
        "client_id": "abc", "client_secret": "",
    }, follow_redirects=True)
    assert ei.load(slug).client_secret == "keep-me"
