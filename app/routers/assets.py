"""The fixed asset register: assets, monthly depreciation and disposals."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import or_, select

from ..models import (
    ASSET_ACTIVE,
    DEPRECIATION_METHODS,
    DRAFT,
    POSTED,
    VOID,
    Account,
    AssetCategory,
    BankAccount,
    Contact,
    DepreciationLine,
    DepreciationRun,
    FixedAsset,
    JournalEntry,
)
from ..money import fmt_plain
from ..security import P_ENTRY, P_JOURNAL, P_VIEW, P_VOID
from ..services import assets as FA
from ..services import attachments as A
from ..services.posting import PostingError, account_by_code, audit, next_number
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_int,
    parse_money,
    period_from_query,
    redirect,
)

router = APIRouter(prefix="/assets")


def _categories(db):
    return list(
        db.scalars(
            select(AssetCategory)
            .where(AssetCategory.is_active.is_(True))
            .order_by(AssetCategory.sort, AssetCategory.name)
        )
    )


def _fixed_asset_accounts(db):
    """Accounts an asset's cost can sit in, and that money can come out of."""
    return list(
        db.scalars(
            select(Account)
            .where(Account.is_active.is_(True))
            .order_by(Account.code)
        )
    )


def _funding_accounts(db):
    """Where the money for an asset came from — a bank, or the supplier."""
    return list(
        db.scalars(
            select(Account)
            .where(
                Account.is_active.is_(True),
                or_(
                    Account.is_bank.is_(True),
                    Account.subtype.in_(("PAYABLE", "LOAN", "CURRENT_LIABILITY",
                                         "OTHER_LIABILITY", "CAPITAL")),
                ),
            )
            .order_by(Account.code)
        )
    )


# --------------------------------------------------------------------------
# The register
# --------------------------------------------------------------------------


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    q = (request.query_params.get("q") or "").strip()
    category_id = parse_id(request.query_params.get("category"))
    status = request.query_params.get("status", ASSET_ACTIVE)

    stmt = select(FixedAsset)
    if status:
        stmt = stmt.where(FixedAsset.status == status)
    if category_id:
        stmt = stmt.where(FixedAsset.category_id == category_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(FixedAsset.name.ilike(like), FixedAsset.number.ilike(like),
                FixedAsset.serial_no.ilike(like), FixedAsset.registration_no.ilike(like),
                FixedAsset.location.ilike(like), FixedAsset.custodian.ilike(like))
        )
    items = list(db.scalars(stmt.order_by(FixedAsset.number)))

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        return csv_response(
            f"asset-register-{date.today()}.csv",
            ["Number", "Asset", "Category", "In service", "Method", "Cost",
             "Depreciation to date", "Net book value", "Status", "Location"],
            [[a.number, a.name, a.category.name if a.category else "",
              a.in_service_date, a.method_label, fmt_plain(a.cost),
              fmt_plain(a.accumulated_depreciation), fmt_plain(a.net_book_value),
              a.status, a.location] for a in items],
        )

    return render(
        request, "assets/index.html",
        items=items, categories=_categories(db), q=q, status=status,
        category_id=category_id, totals=FA.register_totals(db),
        shown_cost=sum(a.cost for a in items),
        shown_nbv=sum(a.net_book_value for a in items),
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    cats = _categories(db)
    # Land sorts first on a schedule but is a poor default on a form — it does
    # not depreciate, so the page would open with every rate field hidden.
    # Prefer whatever this business actually buys most of.
    from sqlalchemy import func

    popular = db.execute(
        select(FixedAsset.category_id, func.count(FixedAsset.id).label("n"))
        .group_by(FixedAsset.category_id)
        .order_by(func.count(FixedAsset.id).desc())
    ).first()
    by_id = {c.id: c for c in cats}
    default = by_id.get(popular[0]) if popular else None
    if default is None or default.method == "NONE":
        default = next((c for c in cats if c.method != "NONE"), cats[0] if cats else None)
    asset = FixedAsset(
        number="(assigned on save)",
        name="",
        purchase_date=date.today(),
        in_service_date=date.today(),
        category_id=default.id if default else None,
    )
    if default:
        FA.apply_category_defaults(asset, default)
    return render(
        request, "assets/form.html", asset=asset, is_new=True,
        categories=cats, methods=DEPRECIATION_METHODS,
        suppliers=list(db.scalars(
            select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
            .order_by(Contact.name)
        )),
        funding=_funding_accounts(db),
    )


# --------------------------------------------------------------------------
# Depreciation runs — declared before /{asset_id} so they are not swallowed
# --------------------------------------------------------------------------


@router.get("/depreciation")
def runs(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    all_runs = list(
        db.scalars(select(DepreciationRun).order_by(DepreciationRun.period.desc()))
    )
    posted = [r for r in all_runs if r.status == POSTED]
    last = max((r.period for r in posted), default=0)
    suggested = FA.next_period(last) if last else FA.period_of(date.today())

    # How much is waiting to be charged for the suggested month
    waiting = 0
    for asset in db.scalars(select(FixedAsset).where(FixedAsset.status == ASSET_ACTIVE)):
        charge = FA.charge_for(asset, suggested)
        if charge:
            waiting += charge.amount

    return render(
        request, "assets/runs.html",
        runs=all_runs, suggested=suggested,
        suggested_label=_period_label(suggested), waiting=waiting,
        charged_this_year=sum(r.total for r in posted
                              if r.period // 100 == date.today().year),
    )


def _period_label(period: int) -> str:
    from ..models import MONTH_NAMES

    y, m = divmod(period, 100)
    return f"{MONTH_NAMES[m - 1]} {y}"


@router.post("/depreciation/run")
async def open_run(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    period = parse_int(form.get("period"), 0) or FA.period_of(date.today())
    try:
        run = FA.open_run(db, period, user=user)
    except PostingError as e:
        flash(request, str(e), "danger")
        db.rollback()
        return redirect("/assets/depreciation")
    db.commit()
    if not run.lines:
        flash(request, f"Nothing to depreciate for {run.period_label}.", "warning")
    return redirect(f"/assets/depreciation/{run.id}")


@router.get("/depreciation/{run_id}")
def run_detail(request: Request, run_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    run = db.get(DepreciationRun, run_id)
    if run is None:
        return redirect("/assets/depreciation")
    entry = db.get(JournalEntry, run.journal_entry_id) if run.journal_entry_id else None

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        return csv_response(
            f"depreciation-{run.period}.csv",
            ["Asset", "Name", "Category", "Months", "NBV before", "Charge", "NBV after"],
            [[l.asset.number, l.asset.name,
              l.asset.category.name if l.asset.category else "",
              l.months_charged, fmt_plain(l.nbv_before), fmt_plain(l.amount),
              fmt_plain(l.nbv_after)] for l in run.lines],
        )

    return render(request, "assets/run_detail.html", run=run, entry=entry)


@router.post("/depreciation/{run_id}/post")
def post_run(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    run = db.get(DepreciationRun, run_id)
    if run is None:
        return redirect("/assets/depreciation")
    try:
        FA.post_run(db, run, user=user)
        db.commit()
        flash(request, f"Depreciation for {run.period_label} posted.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/assets/depreciation/{run_id}")


@router.post("/depreciation/{run_id}/void")
def void_run(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    run = db.get(DepreciationRun, run_id)
    if run is None:
        return redirect("/assets/depreciation")
    try:
        FA.void_run(db, run, user=user)
        db.commit()
        flash(request, f"{run.number} reversed. The month can be run again.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/assets/depreciation/{run_id}")


@router.post("/depreciation/{run_id}/delete")
def delete_run(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    run = db.get(DepreciationRun, run_id)
    if run is None:
        return redirect("/assets/depreciation")
    if run.status != DRAFT:
        flash(request, "Only a draft run can be deleted. A posted run is voided.", "danger")
        return redirect(f"/assets/depreciation/{run_id}")
    number = run.number
    audit(db, user, "DELETE", "DepreciationRun", run.id, detail=number, ip=client_ip(request))
    db.delete(run)
    db.commit()
    flash(request, f"Draft {number} deleted.", "warning")
    return redirect("/assets/depreciation")


# --------------------------------------------------------------------------
# Categories
# --------------------------------------------------------------------------


@router.get("/categories")
def categories(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    counts = {}
    for cat_id, count in db.execute(
        select(FixedAsset.category_id, __import__("sqlalchemy").func.count(FixedAsset.id))
        .group_by(FixedAsset.category_id)
    ):
        counts[cat_id] = count
    return render(
        request, "assets/categories.html",
        categories=list(db.scalars(select(AssetCategory).order_by(AssetCategory.sort,
                                                                 AssetCategory.name))),
        accounts=_fixed_asset_accounts(db), methods=DEPRECIATION_METHODS, counts=counts,
    )


@router.post("/categories/save")
async def save_category(request: Request):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    form = await request.form()
    cid = parse_id(form.get("id"))
    cat = db.get(AssetCategory, cid) if cid else None
    is_new = cat is None

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "A category needs a name.", "danger")
        return redirect("/assets/categories")

    if is_new:
        cat = AssetCategory(name=name)
        db.add(cat)
    cat.name = name
    cat.method = form.get("method") or "STRAIGHT"
    years = parse_int(form.get("useful_life_years"), 0) or 0
    months = parse_int(form.get("useful_life_months_extra"), 0) or 0
    cat.useful_life_months = years * 12 + months
    cat.rate_pct = (form.get("rate_pct") or "0").strip()
    cat.residual_pct = (form.get("residual_pct") or "0").strip()
    cat.asset_account_id = parse_id(form.get("asset_account_id"))
    cat.accum_dep_account_id = parse_id(form.get("accum_dep_account_id"))
    cat.expense_account_id = parse_id(form.get("expense_account_id"))
    cat.is_active = parse_bool(form.get("is_active"))

    db.flush()
    audit(db, user, "CREATE" if is_new else "UPDATE", "AssetCategory", cat.id,
          detail=cat.name, ip=client_ip(request))
    db.commit()
    flash(request, f"{cat.name} saved.")
    return redirect("/assets/categories")


# --------------------------------------------------------------------------
# The schedule
# --------------------------------------------------------------------------


@router.get("/schedule")
def asset_schedule(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    rows, total = FA.schedule(db, start, end)

    if request.query_params.get("format") == "csv":
        from .reports import csv_response

        return csv_response(
            f"asset-schedule-{start}-to-{end}.csv",
            ["Category", "Cost b/f", "Additions", "Disposals", "Cost c/f",
             "Depreciation b/f", "Charge", "On disposals", "Depreciation c/f", "Net book value"],
            [[r.category, fmt_plain(r.cost_open), fmt_plain(r.additions),
              fmt_plain(r.disposals_cost), fmt_plain(r.cost_close),
              fmt_plain(r.dep_open), fmt_plain(r.charge), fmt_plain(r.disposals_dep),
              fmt_plain(r.dep_close), fmt_plain(r.nbv_close)]
             for r in rows + [total]],
        )

    return render(request, "assets/schedule.html",
                  rows=rows, total=total, start=start, end=end, preset=preset)


# --------------------------------------------------------------------------
# Saving an asset
# --------------------------------------------------------------------------


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    aid = parse_id(form.get("id"))
    asset = db.get(FixedAsset, aid) if aid else None
    is_new = asset is None

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "An asset needs a name.", "danger")
        return redirect("/assets/new")

    if is_new:
        asset = FixedAsset(number=next_number(db, "ASSET"), name=name,
                           purchase_date=date.today(), in_service_date=date.today(),
                           created_by_id=user.id)
        db.add(asset)

    asset.name = name
    asset.description = form.get("description") or ""
    asset.category_id = parse_id(form.get("category_id"))
    asset.purchase_date = parse_date(form.get("purchase_date"))
    asset.in_service_date = parse_date(form.get("in_service_date"), asset.purchase_date)
    asset.cost = parse_money(form.get("cost"))
    asset.residual_value = parse_money(form.get("residual_value"))
    asset.method = form.get("method") or "STRAIGHT"
    years = parse_int(form.get("useful_life_years"), 0) or 0
    months = parse_int(form.get("useful_life_months_extra"), 0) or 0
    asset.useful_life_months = years * 12 + months
    asset.rate_pct = (form.get("rate_pct") or "0").strip()
    asset.serial_no = (form.get("serial_no") or "").strip()
    asset.registration_no = (form.get("registration_no") or "").strip()
    asset.location = (form.get("location") or "").strip()
    asset.custodian = (form.get("custodian") or "").strip()
    asset.supplier_id = parse_id(form.get("supplier_id"))
    asset.notes = form.get("notes") or ""

    if asset.residual_value > asset.cost:
        flash(request, "The residual value cannot be more than the cost.", "danger")
        db.rollback()
        return redirect("/assets/new" if is_new else f"/assets/{aid}/edit")

    db.flush()

    # Post the purchase, if the user asked for it and it has not been posted
    funding_id = parse_id(form.get("funding_account_id"))
    message = f"{asset.number} {asset.name} saved."
    if is_new and parse_bool(form.get("capitalise")) and funding_id:
        try:
            FA.capitalise(db, asset, paid_from_account=funding_id, user=user)
            message += " The purchase has been posted to the ledger."
        except PostingError as e:
            db.rollback()
            flash(request, str(e), "danger")
            return redirect("/assets/new")

    audit(db, user, "CREATE" if is_new else "UPDATE", "FixedAsset", asset.id,
          detail=f"{asset.number} {asset.name}", ip=client_ip(request))
    db.commit()
    flash(request, message)
    return redirect(f"/assets/{asset.id}")


@router.get("/{asset_id}")
def detail(request: Request, asset_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        return redirect("/assets")

    history = list(
        db.scalars(
            select(DepreciationLine)
            .join(DepreciationRun, DepreciationLine.run_id == DepreciationRun.id)
            .where(DepreciationLine.asset_id == asset.id,
                   DepreciationRun.status == POSTED)
            .order_by(DepreciationRun.period.desc())
        )
    )
    return render(
        request, "assets/detail.html",
        asset=asset, history=history, forecast=FA.forecast(asset, 12),
        acquisition=db.get(JournalEntry, asset.acquisition_entry_id)
        if asset.acquisition_entry_id else None,
        disposal=db.get(JournalEntry, asset.disposal_entry_id)
        if asset.disposal_entry_id else None,
        files=A.list_for(db, "ASSET", asset.id),
        banks=list(db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                              .order_by(BankAccount.sort))),
        funding=_funding_accounts(db),
        period_label=_period_label,
    )


@router.get("/{asset_id}/edit")
def edit(request: Request, asset_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        return redirect("/assets")
    return render(
        request, "assets/form.html", asset=asset, is_new=False,
        categories=_categories(db), methods=DEPRECIATION_METHODS,
        suppliers=list(db.scalars(
            select(Contact).where(Contact.is_vendor.is_(True), Contact.is_active.is_(True))
            .order_by(Contact.name)
        )),
        funding=_funding_accounts(db),
    )


@router.post("/{asset_id}/capitalise")
async def capitalise(request: Request, asset_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        return redirect("/assets")
    form = await request.form()
    try:
        FA.capitalise(db, asset, paid_from_account=parse_id(form.get("funding_account_id")),
                      user=user)
        db.commit()
        flash(request, f"{asset.number} posted to the ledger.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/assets/{asset_id}")


@router.post("/{asset_id}/dispose")
async def dispose(request: Request, asset_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        return redirect("/assets")
    form = await request.form()
    written_off = parse_bool(form.get("written_off"))
    proceeds = 0 if written_off else parse_money(form.get("proceeds"))
    bank_id = parse_id(form.get("bank_id"))
    account_id = None
    if proceeds and bank_id:
        bank = db.get(BankAccount, bank_id)
        account_id = bank.account_id if bank else None
    try:
        FA.dispose(
            db, asset,
            on=parse_date(form.get("date")),
            proceeds=proceeds,
            proceeds_account=account_id,
            note=(form.get("note") or "").strip(),
            written_off=written_off,
            user=user,
        )
        db.commit()
        flash(request, f"{asset.number} {asset.name} taken off the register.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/assets/{asset_id}")


@router.post("/{asset_id}/undo-disposal")
def undo_disposal(request: Request, asset_id: int):
    from ..main import flash

    user = need(request, P_VOID)
    db = db_of(request)
    asset = db.get(FixedAsset, asset_id)
    if asset is None:
        return redirect("/assets")
    try:
        FA.undo_disposal(db, asset, user=user)
        db.commit()
        flash(request, f"{asset.number} is back on the register.", "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/assets/{asset_id}")
