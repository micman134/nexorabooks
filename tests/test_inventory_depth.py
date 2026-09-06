"""Warehouses, batches, serial numbers and FIFO.

The rule this whole module is built around: value lives in exactly one place
per item. For an average-cost item that is the running totals; for a FIFO item
it is the cost layers. If the two ever disagree with each other — or with the
quantity held across the warehouses — the inventory account is wrong and the
balance sheet is wrong with it. Most of these tests check exactly that.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-inv-")

from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import (  # noqa: E402
    AVERAGE_COST,
    FIFO_COST,
    SERIAL_IN_STOCK,
    SERIAL_SOLD,
    STOCK_ITEM,
    Batch,
    Item,
    Location,
    SerialNumber,
    StockLevel,
)
from app.money import to_kobo  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import costing as C  # noqa: E402

M = to_kobo
Q = lambda n: int(n * 1000)  # noqa: E731 — quantities are milli-units


@pytest.fixture()
def db():
    tmp = tempfile.mkdtemp(prefix="nexora-inv-")
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


def item(db, *, name="Dangote cement 50kg", code="CEM-50", method=AVERAGE_COST,
         batches=False, serials=False, shelf_life=0, warranty=0) -> Item:
    it = Item(code=code, name=name, item_type=STOCK_ITEM, unit="bag",
              costing_method=method, track_batches=batches, track_serials=serials,
              shelf_life_days=shelf_life, warranty_months=warranty,
              purchase_price=M("7,000"), sale_price=M("8,500"))
    db.add(it)
    db.flush()
    return it


def place(db, code, name) -> Location:
    loc = Location(code=code, name=name)
    db.add(loc)
    db.flush()
    return loc


# --------------------------------------------------------------------------
# Nothing changes for an ordinary item
# --------------------------------------------------------------------------


def test_a_plain_item_behaves_exactly_as_before(db):
    cement = item(db)
    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5))
    _move, cost = C.issue(db, cement, Q(30), date(2026, 1, 10))

    assert cost == M("210,000")
    assert cement.qty_on_hand == Q(70)
    assert cement.stock_value == M("490,000")
    assert C.unit_cost(cement) == M("7,000")


def test_stock_always_lands_somewhere(db):
    """Even with nobody thinking about warehouses, the levels must add up."""
    cement = item(db)
    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5))
    assert C.levels_agree(db, cement)
    levels = C.levels_for(db, cement)
    assert len(levels) == 1
    assert levels[0].qty == Q(100)
    assert levels[0].location.name == "Main store"


# --------------------------------------------------------------------------
# Warehouses
# --------------------------------------------------------------------------


def test_stock_is_counted_per_place(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")

    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5), location=ikeja)
    C.receive(db, cement, Q(40), M("290,000"), date(2026, 1, 6), location=apapa)

    assert C.qty_at(db, cement, ikeja) == Q(100)
    assert C.qty_at(db, cement, apapa) == Q(40)
    assert cement.qty_on_hand == Q(140)
    assert C.levels_agree(db, cement)


def test_issuing_takes_it_from_the_place_you_say(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5), location=ikeja)
    C.receive(db, cement, Q(40), M("280,000"), date(2026, 1, 6), location=apapa)

    C.issue(db, cement, Q(25), date(2026, 1, 10), location=apapa)
    assert C.qty_at(db, cement, ikeja) == Q(100)
    assert C.qty_at(db, cement, apapa) == Q(15)
    assert C.levels_agree(db, cement)


def test_transferring_between_stores_changes_nothing_but_the_place(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5), location=ikeja)

    before_value = cement.stock_value
    C.transfer(db, cement, Q(30), date(2026, 1, 12),
               from_location=ikeja, to_location=apapa, memo="Van 3")

    assert cement.stock_value == before_value       # the books do not move
    assert cement.qty_on_hand == Q(100)
    assert C.qty_at(db, cement, ikeja) == Q(70)
    assert C.qty_at(db, cement, apapa) == Q(30)
    assert C.levels_agree(db, cement)


def test_you_cannot_move_stock_that_is_not_there(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, cement, Q(10), M("70,000"), date(2026, 1, 5), location=ikeja)

    with pytest.raises(C.StockError) as e:
        C.transfer(db, cement, Q(30), date(2026, 1, 12),
                   from_location=ikeja, to_location=apapa)
    assert "Only 10 bag" in str(e.value)


def test_a_transfer_to_the_same_place_is_refused(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    C.receive(db, cement, Q(10), M("70,000"), date(2026, 1, 5), location=ikeja)
    with pytest.raises(C.StockError):
        C.transfer(db, cement, Q(5), date(2026, 1, 12),
                   from_location=ikeja, to_location=ikeja)


# --------------------------------------------------------------------------
# First in, first out
# --------------------------------------------------------------------------


def test_fifo_sells_the_oldest_stock_first(db):
    """Two receipts at different prices. The first sale costs the older price."""
    paint = item(db, name="Emulsion paint 20L", code="PNT-20", method=FIFO_COST)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5))    # ₦10,000 each
    C.receive(db, paint, Q(10), M("120,000"), date(2026, 2, 5))    # ₦12,000 each

    _move, cost = C.issue(db, paint, Q(6), date(2026, 3, 1))
    assert cost == M("60,000")                                     # all from the old lot

    _move, cost = C.issue(db, paint, Q(8), date(2026, 3, 2))
    # Four left at ₦10,000 and four at ₦12,000
    assert cost == M("88,000")
    assert paint.qty_on_hand == Q(6)
    assert paint.stock_value == M("72,000")
    assert C.layers_agree(db, paint)


def test_average_cost_would_have_given_a_different_answer(db):
    """The same trades under weighted average, to show the methods really differ."""
    paint = item(db, name="Emulsion paint 20L", code="PNT-20", method=AVERAGE_COST)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5))
    C.receive(db, paint, Q(10), M("120,000"), date(2026, 2, 5))

    _move, cost = C.issue(db, paint, Q(6), date(2026, 3, 1))
    assert cost == M("66,000")                                     # 6 × ₦11,000


def test_fifo_layers_always_add_up_to_the_item(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    C.receive(db, paint, Q(7), M("70,000"), date(2026, 1, 5))
    C.receive(db, paint, Q(3), M("36,000"), date(2026, 1, 9))
    C.issue(db, paint, Q(4), date(2026, 1, 11))
    C.receive(db, paint, Q(5), M("65,000"), date(2026, 1, 15))
    C.issue(db, paint, Q(9), date(2026, 1, 20))

    assert C.layers_agree(db, paint)
    assert C.levels_agree(db, paint)


def test_clearing_a_fifo_item_leaves_nothing_behind(db):
    """The classic rounding trap: sell the lot and the value must land on nil."""
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    C.receive(db, paint, Q(3), M("100,000"), date(2026, 1, 5))     # does not divide evenly
    _move, cost = C.issue(db, paint, Q(3), date(2026, 1, 20))

    assert cost == M("100,000")
    assert paint.qty_on_hand == 0
    assert paint.stock_value == 0
    assert C.layers_agree(db, paint)


def test_fifo_survives_a_partial_layer_with_awkward_arithmetic(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    C.receive(db, paint, Q(3), M("100,000"), date(2026, 1, 5))
    C.issue(db, paint, Q(1), date(2026, 1, 6))
    C.issue(db, paint, Q(1), date(2026, 1, 7))
    C.issue(db, paint, Q(1), date(2026, 1, 8))
    assert paint.stock_value == 0
    assert paint.qty_on_hand == 0
    assert C.layers_agree(db, paint)


def test_voiding_a_fifo_sale_puts_the_goods_back(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5))
    move, cost = C.issue(db, paint, Q(4), date(2026, 1, 10), doc_type="INVOICE", doc_id=1)
    assert cost == M("40,000")

    C.reverse_move(db, move, date(2026, 1, 12))
    assert paint.qty_on_hand == Q(10)
    assert paint.stock_value == M("100,000")
    assert C.layers_agree(db, paint)


def test_voiding_a_fifo_purchase_takes_the_goods_out(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    move = C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5),
                     doc_type="BILL", doc_id=7)
    C.reverse_move(db, move, date(2026, 1, 6))
    assert paint.qty_on_hand == 0
    assert paint.stock_value == 0
    assert C.layers_agree(db, paint)


def test_a_fifo_transfer_carries_its_costs_with_it(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), location=ikeja)
    C.receive(db, paint, Q(10), M("140,000"), date(2026, 2, 5), location=ikeja)

    C.transfer(db, paint, Q(10), date(2026, 3, 1), from_location=ikeja, to_location=apapa)
    assert C.layers_agree(db, paint)
    assert paint.stock_value == M("240,000")
    moved = [l for l in C.open_layers(db, paint) if l.location_id == apapa.id]
    assert sum(l.qty_left for l in moved) == Q(10)
    assert sum(l.value_left for l in moved) == M("100,000")     # the older, cheaper lot


def test_a_fifo_stock_count_still_balances(db):
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5))
    # Three tins broke
    C.revalue_to(db, paint, Q(7), M("70,000"), date(2026, 1, 20), "Breakages")
    assert paint.qty_on_hand == Q(7)
    assert C.layers_agree(db, paint)


# --------------------------------------------------------------------------
# Batches and expiry
# --------------------------------------------------------------------------


def test_a_batch_tracked_item_needs_a_batch_number(db):
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    with pytest.raises(C.StockError) as e:
        C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5))
    assert "batch number is required" in str(e.value)


def test_receiving_into_batches(db):
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), batch_no="B-001",
              expiry_date=date(2027, 1, 5))
    C.receive(db, paint, Q(6), M("66,000"), date(2026, 2, 5), batch_no="B-002",
              expiry_date=date(2026, 8, 1))

    batches = list(db.scalars(select(Batch).where(Batch.item_id == paint.id)))
    assert {b.batch_no for b in batches} == {"B-001", "B-002"}
    assert paint.qty_on_hand == Q(16)
    assert C.levels_agree(db, paint)


def test_the_shelf_life_sets_the_expiry_date(db):
    cement = item(db, batches=True, shelf_life=90)
    C.receive(db, cement, Q(100), M("700,000"), date(2026, 1, 5), batch_no="LOT-9")
    batch = db.scalar(select(Batch).where(Batch.batch_no == "LOT-9"))
    assert batch.expiry_date == date(2026, 4, 5)


def test_the_batch_expiring_soonest_goes_out_first(db):
    """Not a preference — the alternative is watching good stock expire."""
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), batch_no="LATER",
              expiry_date=date(2027, 6, 1))
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 2, 5), batch_no="SOONER",
              expiry_date=date(2026, 6, 1))

    move, _cost = C.issue(db, paint, Q(4), date(2026, 3, 1))
    assert move.batch.batch_no == "SOONER"


def test_a_named_batch_is_used_when_you_say_so(db):
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), batch_no="A",
              expiry_date=date(2027, 6, 1))
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 2, 5), batch_no="B",
              expiry_date=date(2026, 6, 1))
    wanted = db.scalar(select(Batch).where(Batch.batch_no == "A"))

    move, _cost = C.issue(db, paint, Q(4), date(2026, 3, 1), batch=wanted)
    assert move.batch_id == wanted.id
    assert C.qty_at(db, paint, None, wanted) == Q(6)


def test_what_is_going_off_soon(db):
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    soon = date.today() + timedelta(days=30)
    later = date.today() + timedelta(days=400)
    C.receive(db, paint, Q(10), M("100,000"), date.today(), batch_no="SOON",
              expiry_date=soon)
    C.receive(db, paint, Q(10), M("100,000"), date.today(), batch_no="LATER",
              expiry_date=later)

    rows = C.expiring(db, within_days=90)
    assert len(rows) == 1
    assert rows[0][0].batch_no == "SOON"
    assert rows[0][1] == Q(10)


def test_an_emptied_batch_stops_showing_as_expiring(db):
    paint = item(db, name="Marine paint", code="MAR", batches=True)
    C.receive(db, paint, Q(10), M("100,000"), date.today(), batch_no="SOON",
              expiry_date=date.today() + timedelta(days=10))
    C.issue(db, paint, Q(10), date.today())
    assert C.expiring(db, within_days=90) == []


# --------------------------------------------------------------------------
# Serial numbers
# --------------------------------------------------------------------------


def test_a_serial_item_needs_one_serial_per_unit(db):
    gen = item(db, name="5.5 KVA generator", code="GEN-55", serials=True)
    with pytest.raises(C.StockError) as e:
        C.receive(db, gen, Q(3), M("1,500,000"), date(2026, 1, 5),
                  serials=["SN-1", "SN-2"])
    assert "3 serial numbers are needed" in str(e.value)


def test_receiving_and_selling_by_serial_number(db):
    gen = item(db, name="5.5 KVA generator", code="GEN-55", serials=True, warranty=12)
    C.receive(db, gen, Q(3), M("1,500,000"), date(2026, 1, 5),
              serials=["SN-1", "SN-2", "SN-3"], doc_number="BILL-00001")

    in_stock = C.serials_in_stock(db, gen)
    assert [s.serial for s in in_stock] == ["SN-1", "SN-2", "SN-3"]
    assert in_stock[0].cost == M("500,000")

    C.issue(db, gen, Q(1), date(2026, 3, 10), serials=["SN-2"],
            doc_number="INV-00042", doc_id=42)

    sold = db.scalar(select(SerialNumber).where(SerialNumber.serial == "SN-2"))
    assert sold.status == SERIAL_SOLD
    assert sold.sold_doc == "INV-00042"
    assert sold.warranty_until == date(2027, 3, 10)
    assert sold.in_warranty is True
    assert len(C.serials_in_stock(db, gen)) == 2


def test_the_same_serial_cannot_be_in_stock_twice(db):
    gen = item(db, name="Generator", code="GEN", serials=True)
    C.receive(db, gen, Q(1), M("500,000"), date(2026, 1, 5), serials=["SN-1"])
    with pytest.raises(C.StockError) as e:
        C.receive(db, gen, Q(1), M("500,000"), date(2026, 2, 5), serials=["SN-1"])
    assert "already in stock" in str(e.value)


def test_a_serial_that_was_sold_can_be_received_again(db):
    """A trade-in, or a return that goes back to the supplier and comes back."""
    gen = item(db, name="Generator", code="GEN", serials=True)
    C.receive(db, gen, Q(1), M("500,000"), date(2026, 1, 5), serials=["SN-1"])
    C.issue(db, gen, Q(1), date(2026, 2, 1), serials=["SN-1"])
    C.receive(db, gen, Q(1), M("400,000"), date(2026, 3, 1), serials=["SN-1"])
    assert len(C.serials_in_stock(db, gen)) == 1


def test_selling_a_serial_that_is_not_in_stock_is_refused(db):
    gen = item(db, name="Generator", code="GEN", serials=True)
    C.receive(db, gen, Q(1), M("500,000"), date(2026, 1, 5), serials=["SN-1"])
    with pytest.raises(C.StockError) as e:
        C.issue(db, gen, Q(1), date(2026, 2, 1), serials=["SN-99"])
    assert "not in stock" in str(e.value)


def test_serials_move_with_a_transfer(db):
    gen = item(db, name="Generator", code="GEN", serials=True)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, gen, Q(2), M("1,000,000"), date(2026, 1, 5),
              serials=["SN-1", "SN-2"], location=ikeja)

    C.transfer(db, gen, Q(1), date(2026, 2, 1), from_location=ikeja, to_location=apapa)
    at_apapa = [s for s in C.serials_in_stock(db, gen) if s.location_id == apapa.id]
    assert len(at_apapa) == 1


# --------------------------------------------------------------------------
# Everything at once
# --------------------------------------------------------------------------


def test_batches_locations_and_fifo_together(db):
    """A drum of paint, two depots, three lots, first in first out."""
    paint = item(db, name="Marine paint", code="MAR", method=FIFO_COST, batches=True)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")

    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), location=ikeja,
              batch_no="L1", expiry_date=date(2027, 1, 1))
    C.receive(db, paint, Q(10), M("130,000"), date(2026, 2, 5), location=ikeja,
              batch_no="L2", expiry_date=date(2026, 9, 1))
    C.receive(db, paint, Q(5), M("70,000"), date(2026, 3, 5), location=apapa,
              batch_no="L3", expiry_date=date(2026, 12, 1))

    assert paint.qty_on_hand == Q(25)
    assert paint.stock_value == M("300,000")
    assert C.layers_agree(db, paint)
    assert C.levels_agree(db, paint)

    # Sell 8 from Ikeja: the batch expiring soonest (L2), costed oldest-first (L1)
    move, cost = C.issue(db, paint, Q(8), date(2026, 4, 1), location=ikeja)
    assert move.batch.batch_no == "L2"
    assert cost == M("80,000")               # eight tins from the ₦10,000 layer

    assert C.qty_at(db, paint, ikeja) == Q(12)
    assert C.qty_at(db, paint, apapa) == Q(5)
    assert paint.stock_value == M("220,000")
    assert C.layers_agree(db, paint)
    assert C.levels_agree(db, paint)


def test_the_value_in_each_store_follows_the_goods_under_fifo(db):
    """Two yards, two prices. Each yard is worth what its own goods cost."""
    paint = item(db, name="Paint", code="PNT", method=FIFO_COST)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, paint, Q(10), M("100,000"), date(2026, 1, 5), location=ikeja)
    C.receive(db, paint, Q(10), M("160,000"), date(2026, 2, 5), location=apapa)

    values = C.value_by_level(db, paint)
    levels = {l.location_id: l for l in C.levels_for(db, paint)}
    assert values[levels[ikeja.id].id] == M("100,000")
    assert values[levels[apapa.id].id] == M("160,000")
    assert sum(values.values()) == paint.stock_value


def test_the_value_in_each_store_under_weighted_average(db):
    cement = item(db)
    ikeja = place(db, "IKJ", "Ikeja yard")
    apapa = place(db, "APA", "Apapa depot")
    C.receive(db, cement, Q(60), M("420,000"), date(2026, 1, 5), location=ikeja)
    C.receive(db, cement, Q(40), M("300,000"), date(2026, 2, 5), location=apapa)

    values = C.value_by_level(db, cement)
    assert sum(values.values()) == cement.stock_value
    levels = {l.location_id: l for l in C.levels_for(db, cement)}
    # One cost for everything: 720,000 / 100 = 7,200 a bag
    assert values[levels[ikeja.id].id] == M("432,000")
    assert values[levels[apapa.id].id] == M("288,000")
