"""Fill a fresh installation with a few months of realistic trading.

    python seed_demo.py

Useful for trying Nexora Books out before you commit your real figures to it.
It refuses to run on a database that already has transactions.
"""
from __future__ import annotations

import random
import sys
from datetime import date, timedelta

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import (
    DRAFT,
    PAYMENT,
    RECEIPT,
    STOCK_ITEM,
    Account,
    BankAccount,
    Bill,
    BillLine,
    Company,
    Contact,
    Invoice,
    InvoiceLine,
    Item,
    JournalEntry,
    Payment,
)
from app.models import (
    ACTIVE,
    EARNING,
    AssetCategory,
    Employee,
    EmployeeComponent,
    FixedAsset,
    Location,
    RecurringLine,
    RecurringTemplate,
    RequisitionLine,
    StockLevel,
    StockMove,
    User,
)
from app.security import hash_password
from app.seed import bootstrap
from app.services import assets as FA
from app.services import landed as LC
from app.services import requisitions as REQ
from app.services import budgets as BUD
from app.services import reports as R0
from app.services import cash, documents, tax
from app.services import payroll as P
from app.services import payroll_run as PR
from app.services.posting import EntryDraft, next_number, post_entry, sys_account

random.seed(11)

CUSTOMERS = [
    ("Zenith Construction Ltd", "20304050-0001", False),
    ("Lekki Homes Development", "30405060-0001", False),
    ("Ibadan Roads Contractors", "40506070-0001", False),
    ("Chidera Ventures", "", True),
    ("Government of Lagos State", "50607080-0001", False),
    ("Aliyu & Sons Building", "60708090-0001", True),
]

SUPPLIERS = [
    ("Dangote Cement Distribution", "11223344-0001", False),
    ("Ogun Aggregate Supplies", "22334455-0001", False),
    ("Steel Traders Nigeria Ltd", "33445566-0001", False),
    ("Okonkwo Haulage", "", True),
]

ITEMS = [
    ("Dangote Cement 50kg", "bag", 6_500_00, 4_800_00),
    ("Granite chippings 3/4in", "tonne", 32_000_00, 24_500_00),
    ("Sharp sand", "tonne", 18_000_00, 12_000_00),
    ("Reinforcement rod 12mm", "length", 9_800_00, 7_600_00),
    ("Reinforcement rod 16mm", "length", 16_500_00, 13_200_00),
    ("Binding wire", "roll", 12_000_00, 9_000_00),
]

SERVICES = [
    ("Delivery within Lagos", "trip", 45_000_00),
    ("Site consultancy", "day", 150_000_00),
]

# first, last, job title, department, basic, housing, transport, extra allowance
STAFF = [
    ("Adaeze", "Okonkwo", "Financial Controller", "Finance",
     650_000_00, 200_000_00, 100_000_00, ("Leave allowance", 54_000_00)),
    ("Tunde", "Bakare", "Sales Manager", "Sales",
     480_000_00, 150_000_00, 80_000_00, ("Phone and data", 25_000_00)),
    ("Ngozi", "Eze", "Accounts Officer", "Finance",
     220_000_00, 70_000_00, 45_000_00, None),
    ("Ibrahim", "Yusuf", "Warehouse Supervisor", "Yard",
     195_000_00, 60_000_00, 40_000_00, ("Shift allowance", 20_000_00)),
    ("Blessing", "Ohakwe", "Sales Assistant", "Sales",
     140_000_00, 45_000_00, 30_000_00, None),
    ("Emeka", "Nwachukwu", "Driver", "Logistics",
     110_000_00, 30_000_00, 25_000_00, ("Trip allowance", 18_000_00)),
    ("Fatima", "Aliyu", "Office Assistant", "Admin",
     72_000_00, 18_000_00, 15_000_00, None),
]

# Site hands paid a daily rate but settled monthly
SITE_HANDS = [
    ("Musa", "Danjuma", 7_500_00),
    ("Sunday", "Effiong", 7_000_00),
    ("Chinedu", "Obi", 6_500_00),
]


def main() -> None:
    init_db()
    with session_scope() as db:
        bootstrap(db)

        if db.scalar(select(JournalEntry).limit(1)):
            print("This company already has journal entries — the demo data was not loaded.")
            print("Start with an empty company, or clear the data folder first.")
            sys.exit(1)

        company = db.get(Company, 1)
        company.name = "Adeyemi Building Materials Ltd"
        company.legal_name = "Adeyemi Building Materials Limited"
        company.rc_number = "RC 1284477"
        company.tin = "21458796-0001"
        company.vat_reg_no = "VAT-21458796"
        company.address = "14 Awolowo Road, Ikoyi"
        company.city = "Lagos"
        company.state = "Lagos"
        company.phone = "+234 803 555 0142"
        company.email = "accounts@adeyemibuilding.ng"
        company.invoice_footer = (
            "Payment to: Adeyemi Building Materials Ltd · GTBank 0123456789 · "
            "Thank you for your business."
        )
        company.setup_complete = True
        db.flush()

        # Keep the name in the company switcher in step with the books
        from app import companies as registry
        from app import db as _dbmod

        registry.rename(_dbmod.current_slug(), company.name)

        vat = tax.get_code(db, "VAT-STD")
        wht_goods = tax.get_code(db, "WHT-GOODS")
        wht_serv = tax.get_code(db, "WHT-SERV")
        bank = db.scalar(select(BankAccount).where(BankAccount.is_default.is_(True)))
        cash_acc = db.scalar(select(BankAccount).where(BankAccount.account_type == "CASH"))

        sales = sys_account(db, "SALES")
        services_acc = sys_account(db, "SALES_SERVICES")

        start = date.today().replace(day=1) - timedelta(days=150)
        start = start.replace(day=1)

        # ---- People -------------------------------------------------------
        customers = []
        for name, tin, small in CUSTOMERS:
            c = Contact(code=next_number(db, "CONTACT"), name=name, is_customer=True,
                        tin=tin, is_small_company=small, payment_terms_days=30,
                        default_wht_code_id=wht_goods.id,
                        phone=f"080{random.randint(10000000, 99999999)}",
                        city="Lagos", state="Lagos")
            db.add(c)
            customers.append(c)

        suppliers = []
        for name, tin, small in SUPPLIERS:
            c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                        tin=tin, is_small_company=small, payment_terms_days=21,
                        default_wht_code_id=wht_goods.id, city="Lagos", state="Lagos")
            db.add(c)
            suppliers.append(c)
        db.flush()

        # ---- Things we sell ------------------------------------------------
        items = []
        for name, unit, sale, cost in ITEMS:
            it = Item(code=next_number(db, "ITEM"), name=name, item_type=STOCK_ITEM,
                      unit=unit, sale_price=sale, purchase_price=cost, track_stock=True,
                      reorder_level=20 * 1000, category="Materials",
                      sales_account_id=sales.id,
                      cogs_account_id=sys_account(db, "COGS").id,
                      inventory_account_id=sys_account(db, "INVENTORY").id,
                      purchase_account_id=sys_account(db, "PURCHASES").id,
                      sale_tax_code_id=vat.id, purchase_tax_code_id=vat.id)
            db.add(it)
            items.append(it)
        for name, unit, price in SERVICES:
            it = Item(code=next_number(db, "ITEM"), name=name, item_type="SERVICE",
                      unit=unit, sale_price=price, track_stock=False, category="Services",
                      sales_account_id=services_acc.id, sale_tax_code_id=vat.id)
            db.add(it)
            items.append(it)
        db.flush()

        # Government and one contractor famously take their time paying
        slow_payers = {c.id for c in customers if c.name in
                       ("Government of Lagos State", "Ibadan Roads Contractors")}

        # ---- Staff ----------------------------------------------------------
        for first, last, title, dept, basic, housing, transport, extra in STAFF:
            emp = Employee(
                staff_no=next_number(db, "EMPLOYEE"),
                first_name=first, last_name=last, status=ACTIVE,
                job_title=title, department=dept,
                hire_date=start - timedelta(days=random.randint(120, 900)),
                frequency=P.MONTHLY, pay_basis=P.FIXED,
                basic=basic, housing=housing, transport=transport,
                pension_enrolled=True, nhf_enrolled=True,
                tin=f"{random.randint(10000000, 99999999)}-0001",
                pfa_name=random.choice(["Stanbic IBTC Pensions", "ARM Pensions",
                                        "Leadway Pensure", "Premium Pension"]),
                pension_pin=f"PEN{random.randint(100000000, 999999999)}",
                bank_name=random.choice(["GTBank", "Access Bank", "Zenith Bank", "UBA"]),
                bank_account_no=str(random.randint(1000000000, 9999999999)),
                state_of_residence="Lagos",
                annual_rent_paid=random.choice([0, 1_200_000_00, 1_800_000_00, 2_400_000_00]),
                email=f"{first.lower()}.{last.lower()}@adeyemibuilding.ng",
                phone=f"080{random.randint(10000000, 99999999)}",
            )
            db.add(emp)
            db.flush()
            if extra:
                db.add(EmployeeComponent(employee_id=emp.id, name=extra[0], kind=EARNING,
                                         amount=extra[1], taxable=True, sort=1))
        # Site hands on a daily rate, still paid at month end
        for first, last, rate in SITE_HANDS:
            emp = Employee(
                staff_no=next_number(db, "EMPLOYEE"),
                first_name=first, last_name=last, status=ACTIVE,
                job_title="Site hand", department="Yard",
                hire_date=start - timedelta(days=random.randint(60, 400)),
                frequency=P.MONTHLY, pay_basis=P.DAILY_RATE, default_units="22",
                basic=rate, housing=0, transport=0,
                pension_enrolled=False, nhf_enrolled=False,
                bank_name=random.choice(["Access Bank", "Moniepoint", "Opay"]),
                bank_account_no=str(random.randint(1000000000, 9999999999)),
                state_of_residence="Lagos",
                phone=f"080{random.randint(10000000, 99999999)}",
            )
            db.add(emp)
        db.flush()

        # ---- Capital and opening cash ---------------------------------------
        d = EntryDraft(date=start, memo="Share capital introduced", source="OPENING",
                       reference="OPENING")
        d.debit(bank.account_id, 25_000_000_00, "Opening bank balance")
        d.debit(cash_acc.account_id, 500_000_00, "Opening cash float")
        d.credit(db.scalar(select(Account).where(Account.code == "3000")), 25_500_000_00,
                 "Issued share capital")
        post_entry(db, d)

        # ---- Fixed assets ----------------------------------------------------
        # Real entries on the register, so the asset schedule and the balance
        # sheet are made from the same figures.
        kit = [
            ("Mack tipper truck", "Motor Vehicles", 8_500_000_00, 3, "LAG-772-XR", "Yard"),
            ("100 KVA Mikano generator", "Generators and Power Equipment",
             4_200_000_00, 6, "MK-100-2291", "Yard"),
            ("Office furniture and shelving", "Furniture and Fittings",
             1_350_000_00, 8, "", "Head office"),
            ("Four laptops and a printer", "Computer and Office Equipment",
             1_180_000_00, 10, "", "Head office"),
        ]
        for name, cat_name, cost, offset, reg, where in kit:
            cat = db.scalar(select(AssetCategory).where(AssetCategory.name == cat_name))
            asset = FixedAsset(
                number=next_number(db, "ASSET"), name=name, category_id=cat.id,
                purchase_date=start + timedelta(days=offset),
                in_service_date=start + timedelta(days=offset),
                cost=cost, registration_no=reg, location=where,
            )
            FA.apply_category_defaults(asset, cat)
            db.add(asset)
            db.flush()
            FA.capitalise(db, asset, paid_from_account=bank.account_id)

        # ---- Five months of trading -----------------------------------------
        day = start + timedelta(days=5)
        invoices, bills = [], []
        month_marker = day.month

        while day < date.today():
            # Restock on most working days — a builders' merchant turns stock over fast
            if day.weekday() < 5 and random.random() < 0.7:
                supplier = random.choice(suppliers[:3])
                bill = Bill(number=next_number(db, "BILL"), doc_type="BILL",
                            contact_id=supplier.id, date=day,
                            due_date=day + timedelta(days=21), status=DRAFT,
                            vendor_invoice_no=f"{supplier.name[:3].upper()}-{random.randint(1000, 9999)}",
                            wht_code_id=wht_goods.id)
                db.add(bill)
                db.flush()
                # Reorder whatever is running lowest, the way a merchant would
                low_first = sorted(items[:6], key=lambda i: i.qty_on_hand)
                for n, it in enumerate(low_first[: random.randint(3, 5)]):
                    shortfall = max(0, 70 * 1000 - it.qty_on_hand)
                    qty = max(shortfall, random.randint(20, 45) * 1000)
                    db.add(BillLine(bill_id=bill.id, line_no=n + 1, item_id=it.id,
                                    description=it.name, qty=qty,
                                    unit_price=it.purchase_price, tax_code_id=vat.id))
                db.flush()
                db.refresh(bill)
                documents.post_bill(db, bill)
                bills.append(bill)

            # Sales most working days
            if day.weekday() < 6 and random.random() < 0.75:
                for _ in range(random.randint(1, 3)):
                    customer = random.choice(customers)
                    inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                                  contact_id=customer.id, date=day,
                                  due_date=day + timedelta(days=customer.payment_terms_days),
                                  status=DRAFT, wht_code_id=wht_goods.id,
                                  po_number=f"PO-{random.randint(10000, 99999)}",
                                  terms=company.invoice_terms)
                    db.add(inv)
                    db.flush()
                    # Mostly materials, with the odd delivery or consultancy line
                    picks = [(it, random.randint(4, 60))
                             for it in random.sample(items[:6], k=random.randint(1, 3))]
                    if random.random() < 0.25:
                        picks.append((random.choice(items[6:]), random.randint(1, 3)))
                    for n, (it, units) in enumerate(picks):
                        db.add(InvoiceLine(
                            invoice_id=inv.id, line_no=n + 1, item_id=it.id,
                            description=it.name,
                            qty=units * 1000,
                            unit_price=it.sale_price,
                            discount_pct="5" if random.random() < 0.15 else "0",
                            account_id=it.sales_account_id, tax_code_id=vat.id))
                    db.flush()
                    db.refresh(inv)
                    try:
                        documents.post_invoice(db, inv)
                        invoices.append(inv)
                    except Exception:
                        db.rollback()

            # Collect from customers — several a day, oldest first.
            # Two accounts are deliberately slow payers so the ageing report
            # has something to show in every bucket.
            for _ in range(random.randint(1, 3)):
                open_invs = [
                    i for i in invoices
                    if i.balance_due > 0
                    and (day - i.date).days > (110 if i.contact_id in slow_payers
                                               else random.randint(3, 25))
                ]
                if open_invs:
                    inv = open_invs[0]
                    full = random.random() < 0.85
                    settle = inv.balance_due if full else inv.balance_due // 2
                    wht_part = inv.wht_total if full and inv.wht_total <= settle else 0
                    r = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                                contact_id=inv.contact_id, date=day,
                                bank_account_id=bank.id, method="Bank transfer",
                                reference=f"FT{day:%y%m%d}{random.randint(1000, 9999)}",
                                amount=settle - wht_part, wht_amount=wht_part)
                    db.add(r)
                    db.flush()
                    cash.auto_allocate(db, r)
                    cash.post_payment(db, r)

            # Pay suppliers
            if random.random() < 0.6:
                open_bills = [b for b in bills if b.balance_due > 0
                              and (day - b.date).days > 15]
                if open_bills:
                    bill = open_bills[0]
                    wht_part = bill.wht_total
                    p = Payment(number=next_number(db, "PAYMENT"), kind=PAYMENT,
                                contact_id=bill.contact_id, date=day,
                                bank_account_id=bank.id, method="Bank transfer",
                                reference=f"FT{day:%y%m%d}{random.randint(1000, 9999)}",
                                amount=bill.balance_due - wht_part, wht_amount=wht_part)
                    db.add(p)
                    db.flush()
                    cash.auto_allocate(db, p)
                    cash.post_payment(db, p)

            # Monthly overheads
            if day.month != month_marker:
                month_marker = day.month
                # Run the payroll for the month that just closed
                pm_end = day - timedelta(days=1)
                pm_start = pm_end.replace(day=1)
                try:
                    run = PR.build_run(db, P.MONTHLY, pm_start, pm_end, pm_end)
                    # The site hands worked a varying number of days
                    for slip in list(run.payslips):
                        emp = db.get(Employee, slip.employee_id)
                        if emp.pay_basis == P.DAILY_RATE:
                            PR.rebuild_payslip(db, slip, str(random.randint(17, 24)))
                    db.refresh(run)
                    PR.post_run(db, run)
                    PR.pay_run(db, run, bank, pm_end)
                    # Remit PAYE and pension a few days into the new month
                    for item in PR.outstanding_remittances(db):
                        if item["code"] in ("PAYE", "PENSION", "NHF"):
                            PR.post_remittance(
                                db, item["account"], bank, item["balance"],
                                min(day + timedelta(days=8), date.today()),
                                memo=f"{item['code']} for {pm_end:%B %Y}",
                            )
                except Exception:
                    pass

                overheads = [
                    ("RENT", 850_000_00, "Yard and office rent"),
                    ("6120", 420_000_00, "Diesel for generator and trucks"),
                    ("6110", 180_000_00, "Electricity — Eko Disco"),
                    ("6130", 150_000_00, "Security services"),
                    ("6200", 65_000_00, "Telephone and internet"),
                    ("BANK_CHARGES", 24_500_00, "Bank charges and commission"),
                ]
                for key, amount, memo in overheads:
                    account = (
                        db.scalar(select(Account).where(Account.code == key))
                        if key[0].isdigit() else sys_account(db, key)
                    )
                    d = EntryDraft(date=day, memo=memo)
                    d.debit(account, amount, memo)
                    d.credit(bank.account_id, amount, memo)
                    post_entry(db, d)

                # Depreciation for the month that has just closed
                try:
                    run = FA.open_run(db, FA.period_of(pm_end))
                    if run.lines:
                        FA.post_run(db, run)
                    else:
                        db.delete(run)
                        db.flush()
                except Exception:
                    pass

            day += timedelta(days=1)

        # ---- Two things that come round every month --------------------------
        rent_account = db.scalar(select(Account).where(Account.code == "4010"))
        power_account = db.scalar(select(Account).where(Account.code == "6110"))
        anchor = date.today().replace(day=1) - timedelta(days=75)
        anchor = anchor.replace(day=1)
        for name, kind, contact, account, amount, code in [
            ("Yard sub-let — Apapa", "INVOICE", customers[0], rent_account, 650_000_00, vat),
            ("Ikeja Electric — monthly bill", "BILL", suppliers[0], power_account, 180_000_00, None),
        ]:
            rt = RecurringTemplate(
                name=name, doc_type=kind, contact_id=contact.id,
                frequency="MONTHLY", anchor_day=1,
                start_date=anchor, next_date=anchor,
                payment_terms_days=30,
                memo="Raised on the first of every month.",
            )
            db.add(rt)
            db.flush()
            db.add(RecurringLine(
                template_id=rt.id, line_no=1, description=name,
                qty=1000, unit_price=amount, account_id=account.id,
                tax_code_id=code.id if code else None,
            ))
            db.flush()

        # ---- Two yards, and stock that needs tracking properly ---------------
        ikeja = Location(code="IKJ", name="Ikeja yard", is_default=True, sort=0,
                         manager="Sunday Okoro", address="Km 4 Acme Road, Ogba")
        apapa = Location(code="APA", name="Apapa depot", sort=1,
                         manager="Chidi Nwosu", address="Wharf Road, Apapa")
        db.add_all([ikeja, apapa])
        db.flush()
        # Everything bought so far went to the main store; move it to Ikeja so
        # the demo does not show two stores with one of them mysteriously empty.
        main = db.scalar(select(Location).where(Location.code == "MAIN"))
        if main is not None:
            for level in db.scalars(select(StockLevel).where(StockLevel.location_id == main.id)):
                level.location_id = ikeja.id
            for mv in db.scalars(select(StockMove).where(StockMove.location_id == main.id)):
                mv.location_id = ikeja.id
            main.is_active = False
            main.is_default = False
        db.flush()

        gen = Item(code=next_number(db, "ITEM"), name="5.5 KVA petrol generator",
                   item_type=STOCK_ITEM, unit="each", category="Power",
                   purchase_price=500_000_00, sale_price=735_000_00,
                   sales_account_id=sales.id, purchase_account_id=sys_account(db, "PURCHASES").id,
                   inventory_account_id=sys_account(db, "INVENTORY").id, cogs_account_id=sys_account(db, "COGS").id,
                   sale_tax_code_id=vat.id, purchase_tax_code_id=vat.id,
                   costing_method="FIFO", track_serials=True, warranty_months=12,
                   reorder_level=2000)
        paint = Item(code=next_number(db, "ITEM"), name="Marine paint 20L",
                     item_type=STOCK_ITEM, unit="drum", category="Finishes",
                     purchase_price=30_000_00, sale_price=44_500_00,
                     sales_account_id=sales.id, purchase_account_id=sys_account(db, "PURCHASES").id,
                     inventory_account_id=sys_account(db, "INVENTORY").id, cogs_account_id=sys_account(db, "COGS").id,
                     sale_tax_code_id=vat.id, purchase_tax_code_id=vat.id,
                     track_batches=True, shelf_life_days=270, reorder_level=6000)
        db.add_all([gen, paint])
        db.flush()

        # Three generators in, at two different prices, then one sold
        for when, price, sns, where in [
            (date.today() - timedelta(days=95), 480_000_00, ["ADK-55-1180", "ADK-55-1181"], ikeja),
            (date.today() - timedelta(days=30), 512_000_00, ["ADK-55-2044"], apapa),
        ]:
            bill = Bill(number=next_number(db, "BILL"), doc_type="BILL",
                        contact_id=suppliers[1].id, date=when,
                        due_date=when + timedelta(days=21), status=DRAFT,
                        location_id=where.id, vendor_invoice_no=f"MK/{when:%m%d}")
            db.add(bill)
            db.flush()
            db.add(BillLine(bill_id=bill.id, line_no=1, item_id=gen.id,
                            description=gen.name, qty=len(sns) * 1000,
                            unit_price=price, tax_code_id=vat.id, serials="\n".join(sns)))
            db.flush()
            db.refresh(bill)
            documents.recalc_bill(db, bill)
            documents.post_bill(db, bill)

        sale_day = date.today() - timedelta(days=12)
        inv = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=customers[1].id, date=sale_day,
                      due_date=sale_day + timedelta(days=30), status=DRAFT,
                      location_id=ikeja.id, terms=company.invoice_terms,
                      memo="Generator supplied with a 12-month warranty.")
        db.add(inv)
        db.flush()
        db.add(InvoiceLine(invoice_id=inv.id, line_no=1, item_id=gen.id,
                           description=gen.name, qty=1000, unit_price=gen.sale_price,
                           account_id=sales.id, tax_code_id=vat.id,
                           serials="ADK-55-1180"))
        db.flush()
        db.refresh(inv)
        documents.recalc_invoice(db, inv)
        documents.post_invoice(db, inv)

        # Paint in two lots, one of them close to its date
        paint_bills = []
        for when, lot, drums, price, where in [
            (date.today() - timedelta(days=200), "MP-2025-11", 12, 28_500_00, ikeja),
            (date.today() - timedelta(days=40), "MP-2026-06", 10, 31_000_00, apapa),
        ]:
            bill = Bill(number=next_number(db, "BILL"), doc_type="BILL",
                        contact_id=suppliers[2].id, date=when,
                        due_date=when + timedelta(days=21), status=DRAFT,
                        location_id=where.id, vendor_invoice_no=f"PT/{when:%m%d}")
            db.add(bill)
            db.flush()
            db.add(BillLine(bill_id=bill.id, line_no=1, item_id=paint.id,
                            description=paint.name, qty=drums * 1000,
                            unit_price=price, tax_code_id=vat.id, batch_no=lot))
            db.flush()
            db.refresh(bill)
            documents.recalc_bill(db, bill)
            documents.post_bill(db, bill)
            paint_bills.append(bill)

        # ---- Freight on the paint, spread into the cost of the goods ----------
        freight_day = date.today() - timedelta(days=38)
        d = EntryDraft(date=freight_day, memo="Clearing and haulage — paint consignment")
        d.debit(db.scalar(select(Account).where(Account.code == "5030")), 96_000_00,
                "Apapa clearing and haulage")
        d.credit(bank.account_id, 96_000_00, "Paid to clearing agent")
        post_entry(db, d)

        lc = LC.create(db, freight_day, basis="VALUE")
        lc.reference = "CONT-MP-2026"
        lc.note = "Marine paint consignment, cleared at Apapa."
        for pb in paint_bills:
            LC.add_bill(db, lc, pb)
        LC.add_charge(db, lc, "Apapa clearing and haulage", 96_000_00,
                      db.scalar(select(Account).where(Account.code == "5030")).id,
                      contact_id=suppliers[2].id)
        LC.post(db, lc)

        # ---- A ministry that withholds VAT at source --------------------------
        ministry = Contact(code=next_number(db, "CONTACT"),
                           name="Lagos State Ministry of Works and Infrastructure",
                           is_customer=True, tin="01020304-0001",
                           withholds_vat=True, payment_terms_days=45,
                           default_wht_code_id=wht_goods.id,
                           city="Lagos", state="Lagos",
                           address="Block 12, Secretariat, Alausa, Ikeja")
        db.add(ministry)
        db.flush()

        # Stock in for the order first — a government contract is bought for
        stock_day = date.today() - timedelta(days=33)
        restock = Bill(number=next_number(db, "BILL"), doc_type="BILL",
                       contact_id=suppliers[0].id, date=stock_day,
                       due_date=stock_day + timedelta(days=21), status=DRAFT,
                       location_id=ikeja.id, vendor_invoice_no="OCD/LSMWI/01",
                       memo="Stocked in for the Alausa road works order.")
        db.add(restock)
        db.flush()
        for n, (it, qty) in enumerate(zip(items[:3], (900, 700, 400)), start=1):
            db.add(BillLine(bill_id=restock.id, line_no=n, item_id=it.id,
                            description=it.name, qty=qty * 1000,
                            unit_price=it.purchase_price, tax_code_id=vat.id))
        db.flush()
        db.refresh(restock)
        documents.recalc_bill(db, restock)
        documents.post_bill(db, restock)

        mow_day = date.today() - timedelta(days=26)
        mow = Invoice(number=next_number(db, "INVOICE"), doc_type="INVOICE",
                      contact_id=ministry.id, date=mow_day,
                      due_date=mow_day + timedelta(days=45), status=DRAFT,
                      po_number="LSMWI/PO/2026/0918", terms=company.invoice_terms,
                      wht_code_id=wht_goods.id,
                      memo="Supply of building materials — Alausa road works.")
        db.add(mow)
        db.flush()
        for n, (it, qty) in enumerate(zip(items[:3], (400, 250, 120)), start=1):
            db.add(InvoiceLine(invoice_id=mow.id, line_no=n, item_id=it.id,
                               description=it.name, qty=qty * 1000,
                               unit_price=it.sale_price, account_id=it.sales_account_id,
                               tax_code_id=vat.id))
        db.flush()
        db.refresh(mow)
        documents.recalc_invoice(db, mow)
        documents.post_invoice(db, mow)

        # They pay net of both the WHT and the VAT they keep back
        wht_kept = tax.wht_on(mow.subtotal, mow.wht_code, ministry)[0]
        pay = Payment(number=next_number(db, "RECEIPT"), kind=RECEIPT,
                      contact_id=ministry.id, date=date.today() - timedelta(days=5),
                      bank_account_id=bank.id, method="Bank transfer",
                      reference="LSMWI/PV/44120",
                      amount=mow.total - mow.vat_total - wht_kept,
                      vat_withheld=mow.vat_total, wht_amount=wht_kept,
                      memo="Paid net of VAT withheld at source and 2% WHT.")
        db.add(pay)
        db.flush()
        cash.auto_allocate(db, pay)
        cash.post_payment(db, pay)

        # ---- Staff who can raise and approve requisitions ---------------------
        company.requisition_limit = 500_000_00
        admin_user = db.scalar(select(User).where(User.username == "admin"))
        admin_user.full_name = "Adeyemi Bankole"
        admin_user.job_title = "Managing Director"
        admin_user.approves_large_requisitions = True

        staff_users = {}
        for username, name, role, job, dept, boss, finance, bank_no in [
            ("chioma", "Chioma Eze", "accountant", "Yard Manager", "Yard",
             "admin", False, "0221004417"),
            ("tunde", "Tunde Bello", "accountant", "Finance Manager", "Finance",
             "admin", True, "0110882234"),
            ("musa", "Musa Ibrahim", "clerk", "Storekeeper", "Yard",
             "chioma", False, "0123456789"),
            ("ada", "Ada Nwosu", "clerk", "Sales Officer", "Sales",
             "chioma", False, "3099116540"),
        ]:
            u = User(
                username=username, full_name=name, role=role, job_title=job,
                department=dept, password_hash=hash_password("Lagos2026"),
                approves_large_requisitions=False, pays_requisitions=finance,
                bank_name="GTBank", bank_account_no=bank_no, bank_account_name=name,
            )
            db.add(u)
            db.flush()
            staff_users[username] = u
        for username, boss in [("chioma", "admin"), ("tunde", "admin"),
                               ("musa", "chioma"), ("ada", "chioma")]:
            staff_users[username].manager_id = (
                admin_user.id if boss == "admin" else staff_users[boss].id
            )
        db.flush()

        chioma, tunde, musa, ada = (staff_users["chioma"], staff_users["tunde"],
                                    staff_users["musa"], staff_users["ada"])

        def make_req(who, when, purpose, dept, rows):
            req = REQ.create(db, who, when)
            req.purpose = purpose
            req.department = dept
            for n, (desc, code, amount) in enumerate(rows, start=1):
                db.add(RequisitionLine(
                    requisition_id=req.id, line_no=n, description=desc,
                    account_id=db.scalar(select(Account).where(Account.code == code)).id,
                    qty=1000, unit_price=amount,
                ))
            db.flush()
            db.refresh(req)
            REQ.recalc(db, req)
            return req

        # One waiting for the yard manager
        r1 = make_req(musa, date.today() - timedelta(days=1),
                      "Diesel for the yard generator, this week", "Yard",
                      [("Two 200-litre drums of diesel", "6120", 168_000_00)])
        REQ.submit(db, r1, musa)

        # One sent back, so the demo shows a rejection with its reason
        r2 = make_req(ada, date.today() - timedelta(days=4),
                      "Branded diaries for customers", "Sales",
                      [("200 branded diaries", "6400", 340_000_00)])
        REQ.submit(db, r2, ada)
        REQ.reject(db, r2, chioma,
                   "Too late in the year for diaries. Come back with a quote for "
                   "calendars instead, and get two prices.")

        # One over the limit, waiting for the MD
        r3 = make_req(chioma, date.today() - timedelta(days=2),
                      "Rewire the Ikeja yard lighting", "Yard",
                      [("Armoured cable and fittings", "6140", 760_000_00),
                       ("Electrician — best of three quotes", "6140", 190_000_00)])
        REQ.submit(db, r3, chioma)
        REQ.approve(db, r3, admin_user, "Chioma raised it, so I am signing as manager too.")

        # One paid and not yet retired
        r4 = make_req(musa, date.today() - timedelta(days=21),
                      "Repairs to the Hiace bus — clutch", "Yard",
                      [("Clutch plate and labour", "6310", 245_000_00)])
        REQ.submit(db, r4, musa)
        REQ.approve(db, r4, chioma, "The bus has been off the road three days.")
        REQ.pay(db, r4, tunde, bank_account_id=bank.id,
                on=date.today() - timedelta(days=19), reference="GTB/771204")

        # And one done properly, retired with a balance returned
        r5 = make_req(ada, date.today() - timedelta(days=40),
                      "Site visit to Abeokuta — two nights", "Sales",
                      [("Transport and fuel", "6300", 90_000_00),
                       ("Hotel, two nights", "6410", 110_000_00)])
        REQ.submit(db, r5, ada)
        REQ.approve(db, r5, chioma, "Approved.")
        REQ.pay(db, r5, tunde, bank_account_id=bank.id,
                on=date.today() - timedelta(days=38), reference="GTB/770118")
        REQ.retire(db, r5, ada,
                   spent={r5.lines[0].id: 84_500_00, r5.lines[1].id: 96_000_00},
                   on=date.today() - timedelta(days=33),
                   note="Hotel was cheaper midweek. Receipts attached.")

        # ---- A budget for the year, so the variance report has something to say
        fy_start, fy_end = R0.fiscal_year_bounds(db, date.today())
        budget = BUD.create(db, f"{fy_start.year} plan", fy_start, fy_end)
        elapsed = max(1, (date.today().year * 12 + date.today().month)
                      - (fy_start.year * 12 + fy_start.month) + 1)
        actual = BUD.actuals_for(db, fy_start, date.today())
        periods = BUD.periods_in(fy_start, fy_end)
        for account_id, amount in actual.items():
            # Annualise what has happened so far, then nudge it so the report
            # shows real variances rather than a row of zeroes.
            annual = int(amount * 12 / elapsed)
            jitter = 0.9 + (account_id % 5) * 0.05
            for period, planned in BUD.spread(int(annual * jitter), periods).items():
                BUD.set_line(db, budget, account_id, period, planned)
        db.flush()

        # ---- A couple of quotations out for signature -------------------------
        for customer in customers[:2]:
            q = Invoice(number=next_number(db, "QUOTE"), doc_type="QUOTE",
                        contact_id=customer.id, date=date.today() - timedelta(days=4),
                        due_date=date.today() + timedelta(days=26), status=DRAFT,
                        memo="Prices held for 30 days.", terms=company.invoice_terms)
            db.add(q)
            db.flush()
            for n, it in enumerate(random.sample(items[:6], k=3)):
                db.add(InvoiceLine(invoice_id=q.id, line_no=n + 1, item_id=it.id,
                                   description=it.name, qty=random.randint(10, 60) * 1000,
                                   unit_price=it.sale_price, account_id=it.sales_account_id,
                                   tax_code_id=vat.id))
            db.flush()
            db.refresh(q)
            documents.recalc_invoice(db, q)

        db.flush()

    # ---- Prove the demo books are sound --------------------------------------
    from app.services import reports as R

    with session_scope() as db:
        rows, td, tc = R.trial_balance(db, None, date.today())
        bs = R.balance_sheet(db, date.today())
        pl = R.profit_and_loss(db, *R.fiscal_year_bounds(db, date.today()))
        from app.money import fmt

        print()
        print("Demo company loaded: Adeyemi Building Materials Ltd")
        print("-" * 56)
        print(f"  Trial balance      {fmt(td)} debits / {fmt(tc)} credits"
              f"  {'BALANCED' if td == tc else 'OUT OF BALANCE'}")
        print(f"  Total assets       {fmt(bs.total_assets)}")
        print(f"  Revenue this year  {fmt(pl.revenue.total)}")
        print(f"  Net profit         {fmt(pl.net_profit)}")
        print(f"  Balance sheet      {'balances' if bs.difference == 0 else 'DOES NOT BALANCE'}")
        print("-" * 56)
        print("  Sign in as 'admin' with the password 'admin123'.")
        print("  Or try the requisition route as one of these, password 'Lagos2026':")
        print("    musa    — storekeeper, raises requisitions")
        print("    chioma  — yard manager, approves them")
        print("    tunde   — finance manager, releases the money")
        print()


if __name__ == "__main__":
    main()
