"""Inventory valuation and stock tracking.

Two things are tracked separately and deliberately.

**Quantity** lives in ``stock_levels``, one row per item / location / batch, and
is summed onto ``item.qty_on_hand``. That is what tells you there are 40 bags in
the Ikeja yard and 12 at Apapa.

**Value** lives in exactly one place per item, never two:

* Weighted average — running totals on the item. Unit cost is derived from
  them, never stored, so rounding cannot drift. Issuing the last of an item
  releases exactly the remaining value, leaving no orphaned kobo behind.
* First in, first out — a cost layer per receipt. Issuing eats layers oldest
  first, so the cost of a sale is what those goods actually cost. The item's
  running totals are kept in step so every report keeps working unchanged.

Batches and serial numbers ride on top of both. An item with none of this
switched on behaves exactly as it did before any of it existed.
"""
from __future__ import annotations

from datetime import date as Date
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AVERAGE_COST,
    FIFO_COST,
    SERIAL_IN_STOCK,
    SERIAL_SCRAPPED,
    SERIAL_SOLD,
    Batch,
    Item,
    Location,
    SerialNumber,
    StockLayer,
    StockLevel,
    StockMove,
)
from .posting import PostingError


class StockError(PostingError):
    """Safe to show the user.

    Deliberately a posting error: a stock problem found while posting — a
    serial number that is not there, a batch with nothing left in it — must
    stop the entry and roll the whole document back, not leave half of it in
    the ledger. Every posting route already handles PostingError that way.
    """


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------


def default_location(db: Session) -> Location | None:
    loc = db.scalar(select(Location).where(Location.is_default.is_(True),
                                           Location.is_active.is_(True)))
    if loc is None:
        loc = db.scalar(
            select(Location).where(Location.is_active.is_(True)).order_by(Location.sort,
                                                                         Location.id)
        )
    return loc


def ensure_default_location(db: Session) -> Location:
    """Every installation has somewhere to put stock, even before anyone says so."""
    loc = default_location(db)
    if loc is None:
        loc = Location(code="MAIN", name="Main store", is_default=True, sort=0)
        db.add(loc)
        db.flush()
    return loc


def _location_id(db: Session, location) -> int:
    if location is None:
        return ensure_default_location(db).id
    return location if isinstance(location, int) else location.id


# --------------------------------------------------------------------------
# Batches
# --------------------------------------------------------------------------


def get_or_create_batch(
    db: Session,
    item: Item,
    batch_no: str,
    *,
    expiry_date: Date | None = None,
    received_on: Date | None = None,
    supplier_id: int | None = None,
    note: str = "",
) -> Batch:
    batch_no = (batch_no or "").strip()
    if not batch_no:
        raise StockError(f"{item.name} is tracked by batch, so a batch number is required.")
    batch = db.scalar(
        select(Batch).where(Batch.item_id == item.id, Batch.batch_no == batch_no)
    )
    if batch is not None:
        if expiry_date and not batch.expiry_date:
            batch.expiry_date = expiry_date
        return batch

    received_on = received_on or Date.today()
    if expiry_date is None and item.shelf_life_days:
        expiry_date = received_on + timedelta(days=item.shelf_life_days)
    batch = Batch(item_id=item.id, batch_no=batch_no, expiry_date=expiry_date,
                  received_on=received_on, supplier_id=supplier_id, note=note[:255])
    db.add(batch)
    db.flush()
    return batch


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------


def _level(db: Session, item_id: int, location_id: int, batch_id: int | None) -> StockLevel:
    level = db.scalar(
        select(StockLevel).where(
            StockLevel.item_id == item_id,
            StockLevel.location_id == location_id,
            StockLevel.batch_id.is_(None) if batch_id is None
            else StockLevel.batch_id == batch_id,
        )
    )
    if level is None:
        level = StockLevel(item_id=item_id, location_id=location_id,
                           batch_id=batch_id, qty=0)
        db.add(level)
        db.flush()
    return level


def _move_level(db: Session, item_id: int, location_id: int, batch_id: int | None,
                delta: int) -> None:
    level = _level(db, item_id, location_id, batch_id)
    level.qty += delta
    db.flush()


def levels_for(db: Session, item: Item) -> list[StockLevel]:
    """Where this item is, and how much is in each place. Empty rows are hidden."""
    return list(
        db.scalars(
            select(StockLevel)
            .where(StockLevel.item_id == item.id, StockLevel.qty != 0)
            .order_by(StockLevel.location_id, StockLevel.batch_id)
        )
    )


def value_by_level(db: Session, item: Item) -> dict[int, int]:
    """What the stock in each place is worth, keyed by StockLevel id.

    For an average-cost item that is quantity times the one cost. For a FIFO
    item it is the layers actually sitting in that store, which is a different
    number — and the right one, because the goods in one yard may have cost
    more than the same goods in another.
    """
    levels = levels_for(db, item)
    if item.costing_method != FIFO_COST:
        cost = unit_cost(item)
        return {l.id: _round(Decimal(l.qty) * Decimal(cost) / 1000) for l in levels}

    by_place: dict[tuple[int | None, int | None], int] = {}
    for layer in open_layers(db, item):
        key = (layer.location_id, layer.batch_id)
        by_place[key] = by_place.get(key, 0) + layer.value_left
    out = {}
    for level in levels:
        key = (level.location_id, level.batch_id)
        if key in by_place:
            out[level.id] = by_place[key]
        else:
            # A layer without a batch backing a level that has one, or the
            # other way round: fall back to the location total.
            same_place = sum(v for (loc, _b), v in by_place.items() if loc == level.location_id)
            out[level.id] = same_place
    return out


def qty_at(db: Session, item: Item, location, batch=None) -> int:
    """How much of an item is in one place — the question before every issue."""
    location_id = _location_id(db, location)
    stmt = select(func.coalesce(func.sum(StockLevel.qty), 0)).where(
        StockLevel.item_id == item.id, StockLevel.location_id == location_id
    )
    if batch is not None:
        batch_id = batch if isinstance(batch, int) else batch.id
        stmt = stmt.where(StockLevel.batch_id == batch_id)
    return int(db.scalar(stmt) or 0)


def by_location(db: Session, location) -> list[StockLevel]:
    location_id = _location_id(db, location)
    return list(
        db.scalars(
            select(StockLevel)
            .where(StockLevel.location_id == location_id, StockLevel.qty != 0)
            .order_by(StockLevel.item_id)
        )
    )


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def unit_cost(item: Item) -> int:
    """Current cost per whole unit, in kobo.

    For a FIFO item this is the average of what is on hand — the right figure
    for valuing the balance sheet, and only a guide to what the next sale will
    cost, which depends on which layers it eats.
    """
    if item.qty_on_hand <= 0:
        return item.purchase_price
    return _round(Decimal(item.stock_value) * 1000 / Decimal(item.qty_on_hand))


def open_layers(db: Session, item: Item) -> list[StockLayer]:
    return list(
        db.scalars(
            select(StockLayer)
            .where(StockLayer.item_id == item.id, StockLayer.qty_left > 0)
            .order_by(StockLayer.date, StockLayer.id)
        )
    )


def _consume_layers(db: Session, item: Item, qty: int) -> tuple[int, list[tuple[StockLayer, int]]]:
    """Eat ``qty`` milli-units from the oldest layers. Returns (cost, what was eaten)."""
    taken: list[tuple[StockLayer, int]] = []
    cost = 0
    remaining = qty
    for layer in open_layers(db, item):
        if remaining <= 0:
            break
        use = min(layer.qty_left, remaining)
        if use <= 0:
            continue
        if use >= layer.qty_left:
            # Clearing a layer: release exactly what is left in it, so no
            # rounding residue is stranded in the inventory account.
            slice_cost = layer.value_left
            layer.qty_left = 0
            layer.value_left = 0
        else:
            slice_cost = _round(Decimal(layer.value_left) * Decimal(use) / Decimal(layer.qty_left))
            layer.qty_left -= use
            layer.value_left -= slice_cost
        cost += slice_cost
        remaining -= use
        taken.append((layer, use))

    if remaining > 0:
        # Selling stock that is not there yet. Value it at the last known cost
        # and let the negative balance show; the count will correct it.
        cost += _round(Decimal(unit_cost(item)) * Decimal(remaining) / 1000)
    db.flush()
    return cost, taken


# --------------------------------------------------------------------------
# Serial numbers
# --------------------------------------------------------------------------


def receive_serials(
    db: Session,
    item: Item,
    serials: list[str],
    *,
    unit_value: int,
    on: Date,
    location_id: int | None = None,
    batch_id: int | None = None,
    doc_number: str = "",
    supplier_id: int | None = None,
) -> list[SerialNumber]:
    made = []
    for raw in serials:
        serial = (raw or "").strip()
        if not serial:
            continue
        clash = db.scalar(
            select(SerialNumber).where(SerialNumber.item_id == item.id,
                                       SerialNumber.serial == serial)
        )
        if clash is not None and clash.status == SERIAL_IN_STOCK:
            raise StockError(
                f"Serial {serial} is already in stock for {item.name}. "
                "Two units cannot carry the same serial number."
            )
        row = SerialNumber(
            item_id=item.id, serial=serial, status=SERIAL_IN_STOCK,
            location_id=location_id, batch_id=batch_id, cost=unit_value,
            received_on=on, received_doc=doc_number, supplier_id=supplier_id,
        )
        db.add(row)
        made.append(row)
    db.flush()
    return made


def issue_serials(
    db: Session,
    item: Item,
    serials: list[str],
    *,
    on: Date,
    doc_number: str = "",
    doc_id: int | None = None,
    customer_id: int | None = None,
    scrapped: bool = False,
) -> list[SerialNumber]:
    gone = []
    for raw in serials:
        serial = (raw or "").strip()
        if not serial:
            continue
        row = db.scalar(
            select(SerialNumber).where(
                SerialNumber.item_id == item.id, SerialNumber.serial == serial,
                SerialNumber.status == SERIAL_IN_STOCK,
            )
        )
        if row is None:
            raise StockError(
                f"Serial {serial} is not in stock for {item.name}. "
                "Check the number, or receive it first."
            )
        row.status = SERIAL_SCRAPPED if scrapped else SERIAL_SOLD
        row.sold_on = on
        row.sold_doc = doc_number
        row.sold_doc_id = doc_id
        row.customer_id = customer_id
        if item.warranty_months and not scrapped:
            months = item.warranty_months
            y, m = divmod((on.year * 12 + on.month - 1) + months, 12)
            from calendar import monthrange

            row.warranty_until = Date(y, m + 1, min(on.day, monthrange(y, m + 1)[1]))
        gone.append(row)
    db.flush()
    return gone


def serials_in_stock(db: Session, item: Item) -> list[SerialNumber]:
    return list(
        db.scalars(
            select(SerialNumber)
            .where(SerialNumber.item_id == item.id,
                   SerialNumber.status == SERIAL_IN_STOCK)
            .order_by(SerialNumber.serial)
        )
    )


# --------------------------------------------------------------------------
# Movements
# --------------------------------------------------------------------------


def receive(
    db: Session,
    item: Item,
    qty: int,
    total_cost: int,
    on: Date,
    doc_type: str = "",
    doc_id: int | None = None,
    doc_number: str = "",
    memo: str = "",
    location=None,
    batch_no: str = "",
    expiry_date: Date | None = None,
    serials: list[str] | None = None,
    supplier_id: int | None = None,
) -> StockMove:
    """Bring ``qty`` milli-units into stock at ``total_cost`` kobo."""
    if qty <= 0:
        raise StockError("A stock receipt must be for a positive quantity.")
    if not item.track_stock:
        raise StockError(f"{item.name} is not a stock-tracked item.")

    location_id = _location_id(db, location)
    batch = None
    if item.track_batches:
        batch = get_or_create_batch(db, item, batch_no, expiry_date=expiry_date,
                                    received_on=on, supplier_id=supplier_id)
    batch_id = batch.id if batch else None

    if item.track_serials:
        serials = [s for s in (serials or []) if (s or "").strip()]
        expected = qty // 1000
        if len(serials) != expected:
            raise StockError(
                f"{item.name} is tracked by serial number: {expected} serial "
                f"{'numbers are' if expected != 1 else 'number is'} needed for this "
                f"receipt, but {len(serials)} were given."
            )
        receive_serials(db, item, serials, unit_value=_round(Decimal(total_cost) / expected)
                        if expected else 0, on=on, location_id=location_id,
                        batch_id=batch_id, doc_number=doc_number, supplier_id=supplier_id)

    item.qty_on_hand += qty
    item.stock_value += total_cost
    _move_level(db, item.id, location_id, batch_id, qty)

    if item.costing_method == FIFO_COST:
        db.add(StockLayer(
            item_id=item.id, location_id=location_id, batch_id=batch_id, date=on,
            qty_in=qty, qty_left=qty,
            unit_cost=_round(Decimal(total_cost) * 1000 / Decimal(qty)),
            value_left=total_cost,
            doc_type=doc_type, doc_id=doc_id, doc_number=doc_number,
        ))

    move = StockMove(
        item_id=item.id,
        location_id=location_id,
        batch_id=batch_id,
        date=on,
        doc_type=doc_type,
        doc_id=doc_id,
        doc_number=doc_number,
        qty=qty,
        unit_cost=_round(Decimal(total_cost) * 1000 / Decimal(qty)) if qty else 0,
        value=total_cost,
        balance_qty=item.qty_on_hand,
        balance_value=item.stock_value,
        memo=memo,
    )
    db.add(move)
    db.flush()
    return move


def issue(
    db: Session,
    item: Item,
    qty: int,
    on: Date,
    doc_type: str = "",
    doc_id: int | None = None,
    doc_number: str = "",
    memo: str = "",
    allow_negative: bool = True,
    location=None,
    batch=None,
    serials: list[str] | None = None,
    customer_id: int | None = None,
) -> tuple[StockMove, int]:
    """Take ``qty`` milli-units out of stock. Returns ``(move, cost_of_sale)``."""
    if qty <= 0:
        raise StockError("A stock issue must be for a positive quantity.")
    if not item.track_stock:
        raise StockError(f"{item.name} is not a stock-tracked item.")
    if qty > item.qty_on_hand and not allow_negative:
        raise StockError(
            f"Only {item.qty_on_hand / 1000:g} {item.unit} of {item.name} in stock — "
            f"cannot issue {qty / 1000:g}."
        )

    location_id = _location_id(db, location)
    batch_id = None
    if item.track_batches:
        batch_id = _pick_batch(db, item, location_id, qty, batch, allow_negative)

    if item.track_serials:
        serials = [s for s in (serials or []) if (s or "").strip()]
        expected = qty // 1000
        if len(serials) != expected:
            raise StockError(
                f"{item.name} is tracked by serial number: {expected} serial "
                f"{'numbers are' if expected != 1 else 'number is'} needed to issue "
                f"this quantity, but {len(serials)} were given."
            )
        issue_serials(db, item, serials, on=on, doc_number=doc_number, doc_id=doc_id,
                      customer_id=customer_id)

    if item.costing_method == FIFO_COST:
        cost, _taken = _consume_layers(db, item, qty)
    elif item.qty_on_hand <= 0:
        # Selling from negative stock: value at the last known cost.
        cost = _round(Decimal(unit_cost(item)) * Decimal(qty) / 1000)
    elif qty >= item.qty_on_hand:
        # Clearing the item out: release exactly what is left, no residue.
        cost = item.stock_value
        if qty > item.qty_on_hand:
            extra = qty - item.qty_on_hand
            cost += _round(Decimal(unit_cost(item)) * Decimal(extra) / 1000)
    else:
        cost = _round(Decimal(item.stock_value) * Decimal(qty) / Decimal(item.qty_on_hand))

    item.qty_on_hand -= qty
    item.stock_value -= cost
    _move_level(db, item.id, location_id, batch_id, -qty)

    move = StockMove(
        item_id=item.id,
        location_id=location_id,
        batch_id=batch_id,
        date=on,
        doc_type=doc_type,
        doc_id=doc_id,
        doc_number=doc_number,
        qty=-qty,
        unit_cost=_round(Decimal(cost) * 1000 / Decimal(qty)) if qty else 0,
        value=-cost,
        balance_qty=item.qty_on_hand,
        balance_value=item.stock_value,
        memo=memo,
    )
    db.add(move)
    db.flush()
    return move, cost


def _pick_batch(db, item, location_id, qty, batch, allow_negative) -> int | None:
    """Which lot goes out.

    Told which one, use it. Otherwise take the one that expires soonest — with
    perishable stock that is not a preference, it is the only sensible default,
    because the alternative is watching good stock expire on the shelf behind
    newer stock.
    """
    if batch is not None:
        return batch if isinstance(batch, int) else batch.id

    rows = list(
        db.execute(
            select(StockLevel, Batch)
            .join(Batch, StockLevel.batch_id == Batch.id)
            .where(StockLevel.item_id == item.id,
                   StockLevel.location_id == location_id,
                   StockLevel.qty > 0)
        )
    )
    if not rows:
        return None
    rows.sort(key=lambda r: (r[1].expiry_date or Date.max, r[1].received_on, r[1].id))
    for level, lot in rows:
        if level.qty >= qty:
            return lot.id
    if not allow_negative:
        raise StockError(
            f"No single batch of {item.name} has {qty / 1000:g} {item.unit} in stock. "
            "Issue from more than one batch, or count the stock first."
        )
    return rows[0][1].id


def add_cost(
    db: Session,
    item: Item,
    amount: int,
    on: Date,
    *,
    doc_type: str = "LANDED",
    doc_id: int | None = None,
    doc_number: str = "",
    memo: str = "",
    layer_qty: int = 0,
) -> StockMove:
    """Add cost to stock already received, without changing the quantity.

    This is how freight, duty and clearing get into the value of the goods.
    Under weighted average it lifts the running value; under FIFO it lifts the
    layers the goods are actually sitting in, so a later sale carries its own
    share of the freight rather than the whole consignment's.
    """
    if amount == 0:
        return None
    item.stock_value += amount

    if item.costing_method == FIFO_COST:
        layers = open_layers(db, item)
        if layers:
            # Weight by what is left in each layer, so goods already sold do
            # not soak up cost that the remaining stock should carry.
            from ..money import allocate

            shares = allocate(amount, [max(1, l.qty_left) for l in layers])
            for layer, share in zip(layers, shares):
                layer.value_left += share
                if layer.qty_left:
                    layer.unit_cost = _round(
                        Decimal(layer.value_left) * 1000 / Decimal(layer.qty_left)
                    )
        db.flush()

    move = StockMove(
        item_id=item.id,
        date=on,
        doc_type=doc_type,
        doc_id=doc_id,
        doc_number=doc_number,
        qty=0,
        unit_cost=unit_cost(item),
        value=amount,
        balance_qty=item.qty_on_hand,
        balance_value=item.stock_value,
        memo=memo or "Landed cost added to stock",
    )
    db.add(move)
    db.flush()
    return move


def transfer(
    db: Session,
    item: Item,
    qty: int,
    on: Date,
    *,
    from_location,
    to_location,
    batch=None,
    memo: str = "",
    user=None,
) -> tuple[StockMove, StockMove]:
    """Move stock between two places. The value never changes, only where it is."""
    src = _location_id(db, from_location)
    dst = _location_id(db, to_location)
    if src == dst:
        raise StockError("Choose two different places.")
    if qty <= 0:
        raise StockError("A transfer must be for a positive quantity.")

    batch_id = None
    if item.track_batches:
        batch_id = _pick_batch(db, item, src, qty, batch, allow_negative=False)

    available = qty_at(db, item, src, batch_id)
    if qty > available:
        src_name = db.get(Location, src)
        raise StockError(
            f"Only {available / 1000:g} {item.unit} of {item.name} at "
            f"{src_name.name if src_name else 'that place'} — cannot move {qty / 1000:g}."
        )

    cost = _round(Decimal(unit_cost(item)) * Decimal(qty) / 1000)
    _move_level(db, item.id, src, batch_id, -qty)
    _move_level(db, item.id, dst, batch_id, qty)

    # FIFO layers follow the goods, so the cost of a sale from the receiving
    # store is still the cost of the goods that were actually moved.
    if item.costing_method == FIFO_COST:
        remaining = qty
        for layer in open_layers(db, item):
            if remaining <= 0:
                break
            if layer.location_id != src:
                continue
            use = min(layer.qty_left, remaining)
            if use <= 0:
                continue
            if use >= layer.qty_left:
                moved_value = layer.value_left
                layer.qty_left = 0
                layer.value_left = 0
            else:
                moved_value = _round(
                    Decimal(layer.value_left) * Decimal(use) / Decimal(layer.qty_left)
                )
                layer.qty_left -= use
                layer.value_left -= moved_value
            db.add(StockLayer(
                item_id=item.id, location_id=dst, batch_id=layer.batch_id,
                date=layer.date, qty_in=use, qty_left=use,
                unit_cost=layer.unit_cost, value_left=moved_value,
                doc_type="TRANSFER", doc_number=memo[:30],
            ))
            remaining -= use
        db.flush()

    note = memo or "Stock transfer"
    out = StockMove(
        item_id=item.id, location_id=src, batch_id=batch_id, date=on,
        doc_type="TRANSFER", qty=-qty, unit_cost=unit_cost(item), value=0,
        balance_qty=item.qty_on_hand, balance_value=item.stock_value,
        memo=f"{note} — out",
    )
    into = StockMove(
        item_id=item.id, location_id=dst, batch_id=batch_id, date=on,
        doc_type="TRANSFER", qty=qty, unit_cost=unit_cost(item), value=0,
        balance_qty=item.qty_on_hand, balance_value=item.stock_value,
        memo=f"{note} — in",
    )
    db.add_all([out, into])

    if item.track_serials:
        for row in db.scalars(
            select(SerialNumber).where(
                SerialNumber.item_id == item.id,
                SerialNumber.status == SERIAL_IN_STOCK,
                SerialNumber.location_id == src,
            ).limit(max(0, qty // 1000))
        ):
            row.location_id = dst

    db.flush()
    return out, into


def reverse_move(db: Session, move: StockMove, on: Date, memo: str = "") -> StockMove:
    """Undo a stock movement at its original cost (used when voiding a document)."""
    item = db.get(Item, move.item_id)
    item.qty_on_hand -= move.qty
    item.stock_value -= move.value
    if move.location_id:
        _move_level(db, item.id, move.location_id, move.batch_id, -move.qty)

    if item.costing_method == FIFO_COST:
        if move.qty > 0:
            # Undoing a receipt: take the layer it created back out.
            layer = db.scalar(
                select(StockLayer)
                .where(StockLayer.item_id == item.id,
                       StockLayer.doc_type == move.doc_type,
                       StockLayer.doc_id == move.doc_id,
                       StockLayer.qty_in == move.qty)
                .order_by(StockLayer.id.desc())
            )
            if layer is not None:
                layer.qty_left = max(0, layer.qty_left - move.qty)
                layer.value_left = max(0, layer.value_left - move.value)
        else:
            # Undoing an issue: put the goods back as a layer at their own cost,
            # dated when they were originally received so the queue order holds.
            db.add(StockLayer(
                item_id=item.id, location_id=move.location_id, batch_id=move.batch_id,
                date=move.date, qty_in=-move.qty, qty_left=-move.qty,
                unit_cost=move.unit_cost, value_left=-move.value,
                doc_type="REVERSAL", doc_id=move.doc_id, doc_number=move.doc_number,
            ))

    rev = StockMove(
        item_id=item.id,
        location_id=move.location_id,
        batch_id=move.batch_id,
        date=on,
        doc_type=move.doc_type,
        doc_id=move.doc_id,
        doc_number=move.doc_number,
        qty=-move.qty,
        unit_cost=move.unit_cost,
        value=-move.value,
        balance_qty=item.qty_on_hand,
        balance_value=item.stock_value,
        memo=memo or f"Reversal — {move.memo}",
    )
    db.add(rev)
    db.flush()
    return rev


def revalue_to(
    db: Session, item: Item, new_qty: int, new_value: int, on: Date, memo: str = "",
    location=None, batch=None,
) -> tuple[StockMove, int]:
    """Set stock to a counted quantity/value. Returns ``(move, value_difference)``.

    A positive difference is a stock gain, a negative one a write-down.
    """
    dq = new_qty - item.qty_on_hand
    dv = new_value - item.stock_value
    item.qty_on_hand = new_qty
    item.stock_value = new_value

    location_id = _location_id(db, location)
    batch_id = None
    if item.track_batches and batch is not None:
        batch_id = batch if isinstance(batch, int) else batch.id
    if dq:
        _move_level(db, item.id, location_id, batch_id, dq)

    if item.costing_method == FIFO_COST:
        if dq > 0:
            db.add(StockLayer(
                item_id=item.id, location_id=location_id, batch_id=batch_id, date=on,
                qty_in=dq, qty_left=dq,
                unit_cost=_round(Decimal(dv) * 1000 / Decimal(dq)) if dq and dv else 0,
                value_left=max(0, dv), doc_type="ADJUSTMENT",
            ))
        elif dq < 0:
            _consume_layers(db, item, -dq)
        # A count that changes value but not quantity is a write-down of what
        # is on hand; spread it across the open layers so the layers still
        # add up to the item's value.
        if dq == 0 and dv:
            _spread_over_layers(db, item, dv)

    move = StockMove(
        item_id=item.id,
        location_id=location_id,
        batch_id=batch_id,
        date=on,
        doc_type="ADJUSTMENT",
        qty=dq,
        unit_cost=unit_cost(item),
        value=dv,
        balance_qty=new_qty,
        balance_value=new_value,
        memo=memo or "Stock count adjustment",
    )
    db.add(move)
    db.flush()
    return move, dv


def _spread_over_layers(db: Session, item: Item, difference: int) -> None:
    layers = open_layers(db, item)
    total = sum(l.value_left for l in layers)
    if not layers or total <= 0:
        return
    left = difference
    for i, layer in enumerate(layers):
        share = (left if i == len(layers) - 1
                 else _round(Decimal(difference) * Decimal(layer.value_left) / Decimal(total)))
        layer.value_left = max(0, layer.value_left + share)
        left -= share
    db.flush()


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def layers_agree(db: Session, item: Item) -> bool:
    """FIFO layers must add up to the item's own totals. If they don't, say so."""
    if item.costing_method != FIFO_COST:
        return True
    layers = open_layers(db, item)
    return (sum(l.qty_left for l in layers) == item.qty_on_hand
            and sum(l.value_left for l in layers) == item.stock_value)


def levels_agree(db: Session, item: Item) -> bool:
    """The places stock is kept must add up to the total on hand."""
    total = db.scalar(
        select(func.coalesce(func.sum(StockLevel.qty), 0))
        .where(StockLevel.item_id == item.id)
    )
    return int(total or 0) == item.qty_on_hand


def expiring(db: Session, within_days: int = 90) -> list[tuple[Batch, int]]:
    """Batches close to their expiry date, with what is left of them."""
    cutoff = Date.today() + timedelta(days=within_days)
    out = []
    for batch in db.scalars(
        select(Batch).where(Batch.expiry_date.is_not(None), Batch.expiry_date <= cutoff)
        .order_by(Batch.expiry_date)
    ):
        qty = int(db.scalar(
            select(func.coalesce(func.sum(StockLevel.qty), 0))
            .where(StockLevel.batch_id == batch.id)
        ) or 0)
        if qty > 0:
            out.append((batch, qty))
    return out
