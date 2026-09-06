"""Stock items, stock movements and stock adjustments."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import (
    COSTING_METHODS,
    SERIAL_IN_STOCK,
    STOCK_ITEM,
    Account,
    Batch,
    Item,
    Location,
    SerialNumber,
    StockLevel,
    StockMove,
)
from ..money import fmt
from ..security import P_ENTRY, P_JOURNAL, P_VIEW
from sqlalchemy import func

from ..services import costing, reports
from ..services.posting import EntryDraft, PostingError, audit, next_number, post_entry, sys_account
from ..services.tax import vat_codes
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_int,
    parse_money,
    parse_qty,
    redirect,
)

router = APIRouter(prefix="/inventory")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    kind = request.query_params.get("kind", "")
    show_inactive = parse_bool(request.query_params.get("inactive"))

    stmt = select(Item)
    if kind:
        stmt = stmt.where(Item.item_type == kind)
    if not show_inactive:
        stmt = stmt.where(Item.is_active.is_(True))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Item.name.ilike(like), Item.code.ilike(like),
                Item.barcode.ilike(like), Item.category.ilike(like))
        )
    items = list(db.scalars(stmt.order_by(Item.name)))
    stock_value = sum(i.stock_value for i in items if i.item_type == STOCK_ITEM)
    return render(
        request, "inventory/index.html",
        items=items, q=q, kind=kind, show_inactive=show_inactive,
        stock_value=stock_value, low=reports.low_stock(db),
    )


def _accounts(db, *types):
    return list(
        db.scalars(
            select(Account).where(Account.type.in_(types), Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    item = Item(code="", name="", item_type=STOCK_ITEM, unit="each", track_stock=True)
    item.sales_account_id = sys_account(db, "SALES").id
    item.cogs_account_id = sys_account(db, "COGS").id
    item.inventory_account_id = sys_account(db, "INVENTORY").id
    item.purchase_account_id = sys_account(db, "PURCHASES").id
    return render(
        request, "inventory/form.html", item=item, is_new=True,
        income_accounts=_accounts(db, "INCOME"),
        expense_accounts=_accounts(db, "EXPENSE"),
        asset_accounts=_accounts(db, "ASSET"),
        vat_codes=vat_codes(db), costing_methods=COSTING_METHODS,
    )


@router.get("/valuation")
def valuation(request: Request):
    from ..main import render
    from ..services.posting import account_net

    need(request, P_VIEW)
    db = db_of(request)
    items, total = reports.inventory_valuation(db)
    inv_acc = sys_account(db, "INVENTORY")
    ledger = account_net(db, inv_acc.id, None, date.today())
    return render(
        request, "inventory/valuation.html",
        items=items, total=total, ledger=ledger, difference=ledger - total,
    )


def _locations(db):
    return list(
        db.scalars(
            select(Location).where(Location.is_active.is_(True))
            .order_by(Location.sort, Location.name)
        )
    )


# --------------------------------------------------------------------------
# Where stock is kept
# --------------------------------------------------------------------------


@router.get("/locations")
def locations(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    places = list(db.scalars(select(Location).order_by(Location.sort, Location.name)))
    counts, values = {}, {}
    for place in places:
        rows = costing.by_location(db, place)
        counts[place.id] = len(rows)
        values[place.id] = sum(
            round(level.qty * costing.unit_cost(level.item) / 1000) for level in rows
        )
    return render(request, "inventory/locations.html",
                  places=places, counts=counts, values=values)


@router.post("/locations/save")
async def save_location(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    lid = parse_id(form.get("id"))
    place = db.get(Location, lid) if lid else None
    is_new = place is None

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "A place needs a name.", "danger")
        return redirect("/inventory/locations")

    code = (form.get("code") or "").strip().upper() or name[:6].upper().replace(" ", "")
    clash = db.scalar(select(Location).where(Location.code == code))
    if clash is not None and (is_new or clash.id != place.id):
        flash(request, f"The code {code} is already used by {clash.name}.", "danger")
        return redirect("/inventory/locations")

    if is_new:
        place = Location(code=code, name=name)
        db.add(place)
    place.code = code
    place.name = name
    place.address = form.get("address") or ""
    place.manager = (form.get("manager") or "").strip()
    place.phone = (form.get("phone") or "").strip()
    place.is_active = parse_bool(form.get("is_active"))

    if parse_bool(form.get("is_default")):
        for other in db.scalars(select(Location)):
            other.is_default = False
        place.is_default = True
    db.flush()

    audit(db, user, "CREATE" if is_new else "UPDATE", "Location", place.id,
          detail=place.name, ip=client_ip(request))
    db.commit()
    flash(request, f"{place.name} saved.")
    return redirect("/inventory/locations")


@router.get("/transfer")
def transfer_form(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    item_id = parse_id(request.query_params.get("item"))
    return render(
        request, "inventory/transfer.html",
        items=list(db.scalars(
            select(Item).where(Item.item_type == STOCK_ITEM, Item.is_active.is_(True),
                               Item.track_stock.is_(True)).order_by(Item.name)
        )),
        locations=_locations(db), item_id=item_id, today=date.today(),
    )


@router.post("/transfer")
async def do_transfer(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    item = db.get(Item, parse_id(form.get("item_id")))
    src = parse_id(form.get("from_location_id"))
    dst = parse_id(form.get("to_location_id"))
    qty = parse_qty(form.get("qty"))
    on = parse_date(form.get("date"))
    memo = (form.get("memo") or "").strip()

    if item is None or not src or not dst:
        flash(request, "Choose an item and both places.", "danger")
        return redirect("/inventory/transfer")

    try:
        costing.transfer(db, item, qty, on, from_location=src, to_location=dst, memo=memo)
    except costing.StockError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect("/inventory/transfer")

    from_name = db.get(Location, src).name
    to_name = db.get(Location, dst).name
    audit(db, user, "TRANSFER", "Item", item.id,
          detail=f"{qty / 1000:g} {item.unit} of {item.name}: {from_name} to {to_name}",
          ip=client_ip(request))
    db.commit()
    flash(request, f"{qty / 1000:g} {item.unit} of {item.name} moved from "
                   f"{from_name} to {to_name}. Nothing was posted to the ledger — "
                   "the stock is worth the same wherever it sits.")
    return redirect(f"/inventory/{item.id}")


@router.get("/expiring")
def expiring(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    days = parse_int(request.query_params.get("days"), 90) or 90
    rows = costing.expiring(db, within_days=days)

    if request.query_params.get("format") == "csv":
        from .reports import csv_response
        from ..money import fmt_plain

        return csv_response(
            f"expiring-stock-{date.today()}.csv",
            ["Item", "Batch", "Expires", "Days left", "Quantity", "Unit", "Value"],
            [[b.item.name, b.batch_no, b.expiry_date, b.days_to_expiry,
              f"{qty / 1000:g}", b.item.unit,
              fmt_plain(round(qty * costing.unit_cost(b.item) / 1000))]
             for b, qty in rows],
        )

    return render(
        request, "inventory/expiring.html", rows=rows, days=days,
        value=sum(round(qty * costing.unit_cost(b.item) / 1000) for b, qty in rows),
        expired=[(b, q) for b, q in rows if b.is_expired],
    )


@router.get("/serials")
def serials(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    status = request.query_params.get("status", "")

    stmt = select(SerialNumber)
    if status:
        stmt = stmt.where(SerialNumber.status == status)
    if q:
        like = f"%{q}%"
        stmt = stmt.join(Item, SerialNumber.item_id == Item.id).where(
            or_(SerialNumber.serial.ilike(like), Item.name.ilike(like),
                SerialNumber.sold_doc.ilike(like), SerialNumber.received_doc.ilike(like))
        )
    rows = list(db.scalars(stmt.order_by(SerialNumber.id.desc()).limit(400)))
    return render(request, "inventory/serials.html", rows=rows, q=q, status=status,
                  in_stock=int(db.scalar(
                      select(func.count(SerialNumber.id))
                      .where(SerialNumber.status == SERIAL_IN_STOCK)) or 0))


@router.get("/{item_id}")
def detail(request: Request, item_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    item = db.get(Item, item_id)
    if item is None:
        return redirect("/inventory")
    moves = list(
        db.scalars(
            select(StockMove).where(StockMove.item_id == item_id)
            .order_by(StockMove.date.desc(), StockMove.id.desc()).limit(200)
        )
    )
    return render(
        request, "inventory/detail.html", item=item, moves=moves,
        unit_cost=costing.unit_cost(item),
        levels=costing.levels_for(db, item),
        level_values=costing.value_by_level(db, item),
        layers=costing.open_layers(db, item) if item.costing_method == "FIFO" else [],
        serials=costing.serials_in_stock(db, item) if item.track_serials else [],
        locations=_locations(db),
        layers_ok=costing.layers_agree(db, item),
        levels_ok=costing.levels_agree(db, item),
    )


@router.get("/{item_id}/edit")
def edit(request: Request, item_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    item = db.get(Item, item_id)
    if item is None:
        return redirect("/inventory")
    return render(
        request, "inventory/form.html", item=item, is_new=False,
        income_accounts=_accounts(db, "INCOME"),
        expense_accounts=_accounts(db, "EXPENSE"),
        asset_accounts=_accounts(db, "ASSET"),
        vat_codes=vat_codes(db), costing_methods=COSTING_METHODS,
    )


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    item_id = parse_id(form.get("id"))
    item = db.get(Item, item_id) if item_id else None
    is_new = item is None
    if is_new:
        item = Item(code=(form.get("code") or "").strip() or next_number(db, "ITEM"))
        db.add(item)
    elif form.get("code"):
        item.code = form.get("code").strip()

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "An item needs a name.", "danger")
        return redirect("/inventory/new")

    item.name = name
    item.description = form.get("description") or ""
    item.item_type = form.get("item_type") or STOCK_ITEM
    item.unit = (form.get("unit") or "each").strip()
    item.category = (form.get("category") or "").strip()
    item.barcode = (form.get("barcode") or "").strip()
    item.sale_price = parse_money(form.get("sale_price"))
    item.purchase_price = parse_money(form.get("purchase_price"))
    item.sales_account_id = parse_id(form.get("sales_account_id"))
    item.purchase_account_id = parse_id(form.get("purchase_account_id"))
    item.inventory_account_id = parse_id(form.get("inventory_account_id"))
    item.cogs_account_id = parse_id(form.get("cogs_account_id"))
    item.sale_tax_code_id = parse_id(form.get("sale_tax_code_id"))
    item.purchase_tax_code_id = parse_id(form.get("purchase_tax_code_id"))
    item.reorder_level = parse_qty(form.get("reorder_level"))
    item.track_stock = item.item_type == STOCK_ITEM and parse_bool(form.get("track_stock"))
    item.is_active = parse_bool(form.get("is_active"))

    # Depth. Changing how something is costed while stock is on hand would
    # revalue it silently, so that switch is closed until the shelf is empty.
    wanted = form.get("costing_method") or "AVERAGE"
    if wanted != item.costing_method:
        if item.qty_on_hand or item.stock_value:
            flash(request,
                  f"{item.name} still has stock on hand, so the costing method cannot "
                  "change — the value of what is there would move without an entry. "
                  "Sell or write off the stock first.", "warning")
        else:
            item.costing_method = wanted
    item.track_batches = parse_bool(form.get("track_batches"))
    item.track_serials = parse_bool(form.get("track_serials"))
    item.shelf_life_days = parse_int(form.get("shelf_life_days"), 0) or 0
    item.warranty_months = parse_int(form.get("warranty_months"), 0) or 0
    db.flush()

    audit(db, user, "CREATE" if is_new else "UPDATE", "Item", item.id,
          detail=item.name, ip=client_ip(request))
    db.commit()
    flash(request, f"{item.name} saved.")
    return redirect(f"/inventory/{item.id}")


# --------------------------------------------------------------------------
# Stock adjustment
# --------------------------------------------------------------------------


@router.get("/{item_id}/adjust")
def adjust_form(request: Request, item_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    item = db.get(Item, item_id)
    if item is None or item.item_type != STOCK_ITEM:
        return redirect("/inventory")
    return render(
        request, "inventory/adjust.html", item=item,
        unit_cost=costing.unit_cost(item), today=date.today(),
    )


@router.post("/{item_id}/adjust")
async def adjust(request: Request, item_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    item = db.get(Item, item_id)
    on = parse_date(form.get("date"))

    mode = form.get("mode", "count")
    if mode == "count":
        new_qty = parse_qty(form.get("counted_qty"))
        unit = parse_money(form.get("unit_cost")) or costing.unit_cost(item)
        new_value = round(new_qty * unit / 1000)
    else:
        delta = parse_qty(form.get("delta_qty"))
        unit = parse_money(form.get("unit_cost")) or costing.unit_cost(item)
        new_qty = item.qty_on_hand + delta
        new_value = item.stock_value + round(delta * unit / 1000)

    reason = (form.get("reason") or "Stock adjustment").strip()
    _move, diff = costing.revalue_to(db, item, new_qty, new_value, on, reason)

    if diff:
        inv_acc = item.inventory_account_id or sys_account(db, "INVENTORY").id
        gain = sys_account(db, "STOCK_GAIN")
        loss = sys_account(db, "STOCK_LOSS")
        draft = EntryDraft(date=on, memo=f"Stock adjustment — {item.name}: {reason}",
                           source="STOCK", source_id=item.id)
        if diff > 0:
            draft.debit(inv_acc, diff, f"Stock increase — {item.name}", item_id=item.id)
            draft.credit(gain, diff, reason, item_id=item.id)
        else:
            draft.debit(loss, -diff, reason, item_id=item.id)
            draft.credit(inv_acc, -diff, f"Stock decrease — {item.name}", item_id=item.id)
        try:
            post_entry(db, draft, user=user)
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect(f"/inventory/{item_id}/adjust")

    audit(db, user, "ADJUST", "Item", item.id,
          detail=f"{item.name}: {reason}, value change {fmt(diff)}", ip=client_ip(request))
    db.commit()
    flash(request, f"{item.name} adjusted — stock now {item.qty_on_hand / 1000:g} {item.unit}, "
                   f"valued at {fmt(item.stock_value)}.")
    return redirect(f"/inventory/{item_id}")


