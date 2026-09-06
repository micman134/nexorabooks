"""Landed cost — freight, duty and clearing into the value of the goods.

Two things must hold. The charges must land on the stock in full, with nothing
lost to rounding across a dozen lines. And the entry must move cost sideways
only: what the profit and loss is relieved of, the balance sheet takes on, so
the trial balance does not move by a kobo.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-lc-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    BY_QUANTITY,
    BY_VALUE,
    BY_WEIGHT,
    DRAFT,
    FIFO_COST,
    POSTED,
    STOCK_ITEM,
    Bill,
    BillLine,
    Contact,
    Item,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import costing as C  # noqa: E402
from app.services import documents, landed  # noqa: E402
from app.services import reports  # noqa: E402
from app.services.posting import account_by_code, next_number  # noqa: E402

M = to_kobo
Q = lambda n: int(n * 1000)  # noqa: E731


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-lc-")
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


def supplier(db, name="Ogun Cement Depot") -> Contact:
    c = Contact(code=next_number(db, "CONTACT"), name=name, is_vendor=True,
                payment_terms_days=21)
    db.add(c)
    db.flush()
    return c


def item(db, name, code, price, method="AVERAGE") -> Item:
    it = Item(code=code, name=name, item_type=STOCK_ITEM, unit="bag",
              purchase_price=M(price), sale_price=M(price) * 2,
              costing_method=method,
              inventory_account_id=account_by_code(db, "1200").id,
              cogs_account_id=account_by_code(db, "5000").id,
              purchase_account_id=account_by_code(db, "5010").id,
              sales_account_id=account_by_code(db, "4000").id)
    db.add(it)
    db.flush()
    return it


def purchase(db, vendor, rows, on=date(2026, 3, 5)) -> Bill:
    """``rows`` is a list of (item, qty, unit price as a string)."""
    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=on, due_date=on, status=DRAFT)
    db.add(bill)
    db.flush()
    for n, (it, qty, price) in enumerate(rows, start=1):
        # No account: post_bill capitalises stock into inventory by itself
        db.add(BillLine(bill_id=bill.id, line_no=n, item_id=it.id, description=it.name,
                        qty=Q(qty), unit_price=M(price)))
    db.flush()
    db.refresh(bill)
    documents.recalc_bill(db, bill)
    documents.post_bill(db, bill)
    return bill


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


def test_freight_by_value_falls_more_heavily_on_the_dearer_goods(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000"), (sand, 100, "3,000")])

    lc = landed.create(db, date(2026, 3, 10), basis=BY_VALUE)
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Shipping and clearing", M("2,700,000"),
                      account_by_code(db, "5030").id)

    by_item = {l.item_id: l for l in lc.lines}
    # Tiles are ₦2.4m of a ₦2.7m consignment: eight ninths of the freight
    assert by_item[tiles.id].allocated == M("2,400,000")
    assert by_item[sand.id].allocated == M("300,000")
    assert lc.total_allocated == lc.total_charges


def test_freight_by_quantity_falls_evenly_on_every_unit(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000"), (sand, 300, "3,000")])

    lc = landed.create(db, date(2026, 3, 10), basis=BY_QUANTITY)
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Haulage", M("400,000"), account_by_code(db, "5030").id)

    by_item = {l.item_id: l for l in lc.lines}
    assert by_item[tiles.id].allocated == M("100,000")     # a quarter of the units
    assert by_item[sand.id].allocated == M("300,000")


def test_freight_by_weight(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000"), (sand, 10, "3,000")])

    lc = landed.create(db, date(2026, 3, 10), basis=BY_WEIGHT)
    landed.add_bill(db, lc, bill)
    for line in lc.lines:
        line.weight = 200_000 if line.item_id == tiles.id else 600_000
    db.flush()
    landed.add_charge(db, lc, "Haulage", M("800,000"), account_by_code(db, "5030").id)

    by_item = {l.item_id: l for l in lc.lines}
    assert by_item[tiles.id].allocated == M("200,000")
    assert by_item[sand.id].allocated == M("600,000")


def test_nothing_is_lost_across_an_awkward_split(db):
    """₦100 across three lines does not divide. Every kobo must still land."""
    vendor = supplier(db)
    rows = [(item(db, f"Item {i}", f"IT-{i}", "1,000"), 1, "1,000") for i in range(3)]
    bill = purchase(db, vendor, rows)

    lc = landed.create(db, date(2026, 3, 10), basis=BY_VALUE)
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Odd charge", M("100"), account_by_code(db, "5030").id)

    assert lc.total_allocated == M("100")
    assert sum(l.allocated for l in lc.lines) == M("100")


def test_several_charges_add_up(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000")])

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Ocean freight", M("1,200,000"), account_by_code(db, "5030").id)
    landed.add_charge(db, lc, "Import duty", M("480,000"), account_by_code(db, "5040").id)
    landed.add_charge(db, lc, "Clearing agent", M("175,000"), account_by_code(db, "5030").id)

    assert lc.total_charges == M("1,855,000")
    assert lc.lines[0].allocated == M("1,855,000")


# --------------------------------------------------------------------------
# What it does to the books
# --------------------------------------------------------------------------


def test_posting_moves_cost_from_expense_into_stock(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000")])
    assert tiles.stock_value == M("2,400,000")

    # The freight bill was booked to Freight and Clearing In
    from app.services.posting import EntryDraft, post_entry

    freight = EntryDraft(date=date(2026, 3, 8), memo="Clearing agent")
    freight.debit(account_by_code(db, "5030"), M("600,000"), "Apapa clearing")
    freight.credit(account_by_code(db, "1020"), M("600,000"), "Paid")
    post_entry(db, freight)

    before_rows, before_dr, before_cr = reports.trial_balance(db, None, date(2026, 12, 31))

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Apapa clearing", M("600,000"),
                      account_by_code(db, "5030").id)
    entry = landed.post(db, lc)

    # The stock is now worth what it really cost
    assert tiles.stock_value == M("3,000,000")
    assert C.unit_cost(tiles) == M("30,000")

    # And the ledger moved sideways, not up
    debits = {l.account.code: l.debit for l in entry.lines if l.debit}
    credits = {l.account.code: l.credit for l in entry.lines if l.credit}
    assert debits == {"1200": M("600,000")}
    assert credits == {"5030": M("600,000")}

    after_rows, after_dr, after_cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert after_dr == after_cr
    by_code = {r.account.code: r.debit - r.credit for r in after_rows}
    assert by_code["5030"] == 0                      # the freight account is cleared
    assert by_code["1200"] == M("3,000,000")         # and it is in the stock


def test_the_stock_valuation_still_ties_to_the_ledger(db):
    from app.services.posting import account_net

    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000"), (sand, 200, "3,000")])

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("450,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)

    items, total = reports.inventory_valuation(db)
    ledger = account_net(db, account_by_code(db, "1200").id, None, date(2026, 12, 31))
    assert total == ledger


def test_the_cost_of_a_later_sale_carries_the_freight(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000")])

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("600,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)

    _move, cost = C.issue(db, tiles, Q(10), date(2026, 4, 1))
    assert cost == M("300,000")          # ₦30,000 a box, not ₦24,000


def test_landed_cost_on_a_fifo_item_lifts_the_layers(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000", method=FIFO_COST)
    bill = purchase(db, vendor, [(tiles, 100, "24,000")])

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("600,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)

    assert C.layers_agree(db, tiles)
    layers = C.open_layers(db, tiles)
    assert sum(l.value_left for l in layers) == M("3,000,000")
    assert layers[0].unit_cost == M("30,000")


def test_voiding_takes_the_cost_back_off(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000")])

    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("600,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)
    assert tiles.stock_value == M("3,000,000")

    landed.void(db, lc)
    assert lc.status == "VOID"
    assert tiles.stock_value == M("2,400,000")

    _rows, dr, cr = reports.trial_balance(db, None, date(2026, 12, 31))
    assert dr == cr


# --------------------------------------------------------------------------
# What it refuses to do
# --------------------------------------------------------------------------


def test_a_draft_purchase_cannot_carry_freight(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = Bill(number=next_number(db, "BILL"), doc_type="BILL", contact_id=vendor.id,
                date=date(2026, 3, 5), due_date=date(2026, 3, 26), status=DRAFT)
    db.add(bill)
    db.flush()

    lc = landed.create(db, date(2026, 3, 10))
    with pytest.raises(landed.LandedCostError) as e:
        landed.add_bill(db, lc, bill)
    assert "not been posted" in str(e.value)


def test_a_purchase_of_services_has_nowhere_to_put_freight(db):
    vendor = supplier(db)
    service = Item(code="SVC", name="Consultancy", item_type="SERVICE", track_stock=False,
                   purchase_account_id=account_by_code(db, "6500").id)
    db.add(service)
    db.flush()
    bill = purchase(db, vendor, [(service, 1, "500,000")])

    lc = landed.create(db, date(2026, 3, 10))
    with pytest.raises(landed.LandedCostError) as e:
        landed.add_bill(db, lc, bill)
    assert "no stock lines" in str(e.value)


def test_a_charge_needs_an_account_to_come_out_of(db):
    lc = landed.create(db, date(2026, 3, 10))
    with pytest.raises(landed.LandedCostError) as e:
        landed.add_charge(db, lc, "Freight", M("100,000"), None)
    assert "which account" in str(e.value)


def test_it_cannot_be_posted_with_no_charges(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000")])
    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    with pytest.raises(landed.LandedCostError) as e:
        landed.post(db, lc)
    assert "at least one charge" in str(e.value)


def test_it_cannot_be_posted_twice(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000")])
    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("60,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)
    with pytest.raises(landed.LandedCostError):
        landed.post(db, lc)


def test_a_posted_landed_cost_cannot_be_edited(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000")])
    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Freight", M("60,000"), account_by_code(db, "5030").id)
    landed.post(db, lc)
    with pytest.raises(landed.LandedCostError) as e:
        landed.add_charge(db, lc, "More freight", M("10,000"),
                          account_by_code(db, "5030").id)
    assert "cannot be changed" in str(e.value)


def test_the_same_purchase_is_not_added_twice(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000")])
    lc = landed.create(db, date(2026, 3, 10))
    landed.add_bill(db, lc, bill)
    with pytest.raises(landed.LandedCostError):
        landed.add_bill(db, lc, bill)
    assert len(lc.lines) == 1


def test_changing_the_basis_re_spreads_everything(db):
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 100, "24,000"), (sand, 100, "3,000")])

    lc = landed.create(db, date(2026, 3, 10), basis=BY_VALUE)
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Haulage", M("270,000"), account_by_code(db, "5030").id)
    by_value = {l.item_id: l.allocated for l in lc.lines}

    lc.basis = BY_QUANTITY
    landed.recalc(db, lc)
    by_qty = {l.item_id: l.allocated for l in lc.lines}

    assert by_value[tiles.id] == M("240,000")
    assert by_qty[tiles.id] == M("135,000")          # same number of units each
    assert sum(by_qty.values()) == M("270,000")


def test_spreading_by_weight_with_no_weights_entered_still_lands(db):
    """Rather than allocating nothing and silently losing the charge."""
    vendor = supplier(db)
    tiles = item(db, "Porcelain tiles", "TIL", "24,000")
    sand = item(db, "Sharp sand", "SND", "3,000")
    bill = purchase(db, vendor, [(tiles, 10, "24,000"), (sand, 10, "3,000")])

    lc = landed.create(db, date(2026, 3, 10), basis=BY_WEIGHT)
    landed.add_bill(db, lc, bill)
    landed.add_charge(db, lc, "Haulage", M("100,000"), account_by_code(db, "5030").id)

    assert lc.total_allocated == M("100,000")
    assert all(l.allocated == M("50,000") for l in lc.lines)
