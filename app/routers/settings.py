"""Company settings, users, tax codes, backups and year-end close."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from .. import clock
from .. import companies as registry
from .. import config, countries, currency, licensing, prefs, security, store, themes
from .. import network as network_mod
from ..models import (
    EXPENSE,
    INCOME,
    ROLES,
    VAT,
    WHT,
    Account,
    Company,
    FiscalYear,
    JournalEntry,
    TaxCode,
    User,
)
from ..money import fmt
from ..security import P_ADMIN, P_VIEW
from ..services import autobackup, reports as R, seats, support
from ..services.posting import EntryDraft, PostingError, audit, post_entry, sys_account
from ._common import (
    client_ip,
    db_of,
    need,
    parse_bool,
    parse_date,
    parse_id,
    parse_int,
    parse_money,
    redirect,
    user_of,
)

router = APIRouter(prefix="/settings")


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    return render(
        request, "settings/index.html",
        data_dir=str(registry.company_dir(request.state.company_slug)),
        db_size=registry.company_db(request.state.company_slug).stat().st_size
        if registry.company_db(request.state.company_slug).exists() else 0,
        user_count=db.scalar(select(Account.id).limit(1)) is not None,
    )


# --------------------------------------------------------------------------
# Company
# --------------------------------------------------------------------------


def books_started(db) -> bool:
    """True once anything at all has been posted to the ledger.

    The currency can be changed freely before that and never after it. Every
    amount in the books is an integer count of minor units, so switching from
    naira to yen would not convert ₦1,250.00 into yen — it would silently
    reinterpret 125000 as ¥125,000. There is no safe version of that, so the
    screen stops offering it rather than warning about it.
    """
    return db.scalar(select(JournalEntry.id).limit(1)) is not None


def _country_table() -> list[dict]:
    """The country presets, in the shape the settings page's script wants."""
    rows = []
    for c in countries.choices():
        spec = currency.preset(c.currency) or currency.DEFAULT
        rows.append({
            "code": c.code, "name": c.name,
            "currency_code": spec.code, "currency_symbol": spec.symbol,
            "currency_decimals": spec.decimals,
            "currency_symbol_after": spec.symbol_after,
            "currency_thousands": spec.thousands, "currency_point": spec.point,
            "tax_label": c.tax_label, "tax_rate": c.tax_rate,
            "tax_id_label": c.tax_id_label, "reg_no_label": c.reg_no_label,
            "tax_authority": c.tax_authority, "date_format": c.date_format,
        })
    return rows


@router.get("/company")
def company_form(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    from ..models import BankAccount

    banks = list(db.scalars(select(BankAccount).where(BankAccount.is_active.is_(True))
                            .order_by(BankAccount.sort, BankAccount.id)))
    return render(request, "settings/company.html",
                  banks=banks,
                  payable=[b for b in banks if b.show_on_invoices and b.can_be_shown],
                  first_run=request.query_params.get("first_run") == "1",
                  countries=countries.choices(),
                  country_table=_country_table(),
                  currencies=currency.choices(),
                  date_formats=prefs.DATE_FORMATS,
                  currency_locked=books_started(db))


@router.post("/company")
async def company_save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    company = db.get(Company, 1)

    company.name = (form.get("name") or "").strip() or company.name
    company.legal_name = (form.get("legal_name") or "").strip()
    company.rc_number = (form.get("rc_number") or "").strip()
    company.tin = (form.get("tin") or "").strip()
    company.vat_reg_no = (form.get("vat_reg_no") or "").strip()
    company.address = form.get("address") or ""
    company.city = (form.get("city") or "").strip()
    company.state = (form.get("state") or "").strip()
    company.phone = (form.get("phone") or "").strip()
    company.email = (form.get("email") or "").strip()
    company.website = (form.get("website") or "").strip()

    # --- Country, currency and wording -----------------------------------
    country = countries.get(form.get("country_code") or company.country_code)
    company.country_code = country.code
    company.country_name = country.name
    company.tax_label = (form.get("tax_label") or country.tax_label).strip()
    company.tax_id_label = (form.get("tax_id_label") or country.tax_id_label).strip()
    company.reg_no_label = (form.get("reg_no_label") or country.reg_no_label).strip()
    company.tax_authority = (form.get("tax_authority") or country.tax_authority).strip()
    company.date_format = (form.get("date_format") or country.date_format).strip()
    chosen_theme = (form.get("theme") or "").strip().lower()
    if chosen_theme in themes.BY_KEY:
        company.theme = chosen_theme

    if not books_started(db):
        spec = (currency.preset(form.get("currency_code") or "")
                or currency.preset(company.currency_code)
                or currency.DEFAULT)
        company.currency_code = spec.code
        company.currency_symbol = (form.get("currency_symbol") or spec.symbol).strip() \
            or spec.symbol
        company.currency_decimals = spec.decimals
        company.currency_symbol_after = spec.symbol_after
        company.currency_thousands = spec.thousands
        company.currency_point = spec.point
        # Amounts further down this form must be read in the currency just
        # chosen, not the one that was in force when the page was drawn.
        currency.set_active(currency.from_company(company))

    company.fiscal_year_start_month = parse_int(form.get("fiscal_year_start_month"), 1)
    company.is_vat_registered = parse_bool(form.get("is_vat_registered"))
    company.vat_rate = (form.get("vat_rate") or company.vat_rate or "0").strip()
    company.annual_turnover_band = form.get("annual_turnover_band") or "ABOVE_50M"
    company.invoice_terms = form.get("invoice_terms") or ""
    company.invoice_footer = form.get("invoice_footer") or ""
    company.payment_instructions = (form.get("payment_instructions") or "").strip()
    company.default_payment_terms_days = parse_int(form.get("default_payment_terms_days"), 30)
    company.requisition_limit = parse_money(form.get("requisition_limit"))
    was_first_run = not company.setup_complete
    company.setup_complete = True

    # Keep the standard VAT code in step with the company's rate
    std = db.scalar(select(TaxCode).where(TaxCode.code == "VAT-STD"))
    if std and std.rate != company.vat_rate:
        std.rate = company.vat_rate
        std.name = f"{company.tax_label} — standard rate ({company.vat_rate}%)"

    audit(db, user, "UPDATE", "Company", 1, detail=company.name, ip=client_ip(request))
    db.commit()
    flash(request, "Company details saved." if not was_first_run
          else f"Welcome to {company.name}. Your books are ready.")
    return redirect("/" if was_first_run else "/settings/company")


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


@router.get("/diagnostics")
def diagnostics(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    return render(request, "settings/diagnostics.html",
                  report=support.report(), errors=support.recent(10))


@router.get("/diagnostics/download")
def diagnostics_download(request: Request):
    from fastapi.responses import PlainTextResponse

    need(request, P_ADMIN)
    return PlainTextResponse(
        support.report(include_errors=10),
        headers={"Content-Disposition":
                 f'attachment; filename="{support.report_filename()}"'},
    )


@router.post("/diagnostics/clear")
def diagnostics_clear(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    support.clear()
    audit(db, user, "DELETE", "ErrorLog", 1, detail="Error log cleared",
          ip=client_ip(request))
    db.commit()
    flash(request, "Error log cleared.")
    return redirect("/settings/diagnostics")


# --------------------------------------------------------------------------
# Licence
# --------------------------------------------------------------------------


def _wanted_users(request, state, usage) -> int:
    """How many users to quote for, before the customer has chosen.

    Their own answer wins. Failing that, what they already have — a company
    with six people should not have to work out that they need six. Failing
    that, one.
    """
    asked = parse_int(request.query_params.get("users"), 0)
    if asked > 0:
        return min(asked, 999)
    if state.licence is not None and getattr(state.licence, "users", 0):
        return int(state.licence.users)
    return max(store.MINIMUM_USERS, usage.used)


@router.get("/licence")
def licence_form(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    state = licensing.status()
    usage = seats.usage(state)
    wanted = _wanted_users(request, state, usage)
    return render(request, "settings/licence.html",
                  state=state,
                  machine_code=licensing.machine_code(),
                  trial_days=licensing.TRIAL_DAYS,
                  trial_ends=licensing.trial_ends(),
                  installed=licensing.installed_text(),
                  seats=usage,
                  store=store,
                  wanted_users=wanted,
                  quote=store.quote(wanted),
                  prices=store.price_table(),
                  ways=store.ways_to_pay(store.quote(wanted).total,
                                         licensing.machine_code(), wanted),
                  renewing=state.kind in ("EXPIRED", "LICENSED"))


@router.post("/licence")
async def licence_save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    text = form.get("licence") or ""

    licence = licensing.read(text)
    if licence is None:
        flash(request, "That is not a licence this software recognises. Check that the "
                       "whole thing was copied, including the last line.", "danger")
        return redirect("/settings/licence")
    if licence.machine != licensing.machine_code():
        flash(request, f"That licence is for computer {licence.machine}, and this one is "
                       f"{licensing.machine_code()}. Ask for a licence for this computer.",
              "danger")
        return redirect("/settings/licence")

    licensing.install(text)
    audit(db, user, "UPDATE", "Licence", 1, detail=f"Licensed to {licence.name}",
          ip=client_ip(request))
    db.commit()
    flash(request, f"Licensed to {licence.name}. Thank you.")
    return redirect("/settings/licence")


@router.post("/licence/remove")
def licence_remove(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    licensing.remove()
    audit(db, user, "DELETE", "Licence", 1, detail="Licence removed",
          ip=client_ip(request))
    db.commit()
    flash(request, "Licence removed from this computer.", "warning")
    return redirect("/settings/licence")


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


@router.get("/users")
def users(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    return render(
        request, "settings/users.html",
        users=list(db.scalars(select(User).order_by(User.username))),
        roles=ROLES,
        limit=db.get(Company, 1).requisition_limit if db.get(Company, 1) else 0,
    )


@router.post("/users/save")
async def user_save(request: Request):
    from ..main import flash

    admin = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    uid = parse_id(form.get("id"))
    user = db.get(User, uid) if uid else None
    is_new = user is None

    username = (form.get("username") or "").strip().lower()
    if not username:
        flash(request, "A username is required.", "danger")
        return redirect("/settings/users")

    clash = db.scalar(select(User).where(User.username == username))
    if clash and (is_new or clash.id != user.id):
        flash(request, f"The username '{username}' is already taken.", "danger")
        return redirect("/settings/users")

    # Seats. Checked before anything is written, and only when this account is
    # about to become one that can sign in — editing somebody who is already
    # active must never be blocked by a limit they are already inside.
    wants_active = parse_bool(form.get("is_active"))
    becoming_active = wants_active and (is_new or not user.is_active)
    if becoming_active:
        refused = seats.refusal(username)
        if refused:
            flash(request, refused, "danger")
            db.rollback()
            return redirect("/settings/users")

    temp = ""
    if is_new:
        temp = security.new_temp_password()
        user = User(username=username, password_hash=security.hash_password(temp),
                    must_change_password=True)
        db.add(user)
    else:
        user.username = username

    user.full_name = (form.get("full_name") or "").strip()
    user.email = (form.get("email") or "").strip()
    user.role = form.get("role") or "clerk"
    user.is_active = parse_bool(form.get("is_active"))

    # --- Requisitions -----------------------------------------------------
    user.job_title = (form.get("job_title") or "").strip()
    user.department = (form.get("department") or "").strip()
    manager_id = parse_id(form.get("manager_id"))
    if manager_id and not is_new and manager_id == user.id:
        flash(request, "Somebody cannot be their own manager — their requisitions "
                       "would have nobody to approve them.", "danger")
        db.rollback()
        return redirect("/settings/users")
    user.manager_id = manager_id
    user.approves_large_requisitions = parse_bool(form.get("approves_large_requisitions"))
    user.pays_requisitions = parse_bool(form.get("pays_requisitions"))
    user.bank_name = (form.get("bank_name") or "").strip()
    user.bank_account_no = (form.get("bank_account_no") or "").strip()
    user.bank_account_name = (form.get("bank_account_name") or "").strip()

    # --- Super administrator ---------------------------------------------
    # Only another super administrator may grant it, it goes only to an
    # administrator, and the last one cannot take it off themselves — an
    # installation with nobody able to grant it is stuck for good.
    if security.can(admin, security.P_DELETE):
        wanted_super = parse_bool(form.get("is_super_admin"))
        if wanted_super and user.role != "admin":
            flash(request, "Only an administrator can be a super administrator. "
                           "Change their role first.", "danger")
            db.rollback()
            return redirect("/settings/users")
        if not wanted_super and user.is_super_admin:
            others = db.scalars(
                select(User).where(User.is_super_admin.is_(True),
                                   User.is_active.is_(True))).all()
            if not [u for u in others if u.id != user.id]:
                flash(request, "This is the only super administrator. Give somebody "
                               "else the power first, or nobody will ever be able to "
                               "grant it again.", "danger")
                db.rollback()
                return redirect("/settings/users")
        user.is_super_admin = wanted_super

    # Never let the last active administrator lock everyone out
    if not is_new:
        admins = [
            u for u in db.scalars(select(User).where(User.role == "admin", User.is_active.is_(True)))
        ]
        if user.role != "admin" or not user.is_active:
            remaining = [u for u in admins if u.id != user.id]
            if not remaining:
                flash(request, "This is the only active administrator — "
                               "promote someone else first.", "danger")
                db.rollback()
                return redirect("/settings/users")

    db.flush()
    audit(db, admin, "CREATE" if is_new else "UPDATE", "User", user.id,
          detail=f"{user.username} ({user.role})", ip=client_ip(request))
    if temp:
        # A new person has to find out how to sign in. Emailing them a link on
        # which they choose their own password is better than emailing them a
        # password, so that is tried first; the temporary password is what is
        # left when there is no address to send to or no mail set up.
        invited, message = _send_invitation(request, db, admin, user)
        db.commit()
        if invited:
            flash(request, f"User '{username}' created. {message}")
        else:
            flash(request, f"User '{username}' created. Temporary password: "
                           f"{temp} — they will be asked to change it at first "
                           f"sign-in. {message}", "info")
    else:
        db.commit()
        flash(request, f"User '{username}' saved.")
    return redirect("/settings/users")


@router.post("/users/{user_id}/reset-password")
def user_reset(request: Request, user_id: int):
    from ..main import flash

    admin = need(request, P_ADMIN)
    db = db_of(request)
    user = db.get(User, user_id)
    if user is None:
        return redirect("/settings/users")
    temp = security.new_temp_password()
    user.password_hash = security.hash_password(temp)
    user.must_change_password = True
    audit(db, admin, "RESET_PASSWORD", "User", user.id, detail=user.username, ip=client_ip(request))
    db.commit()
    flash(request, f"Password for '{user.username}' reset to: {temp}", "info")
    return redirect("/settings/users")


@router.post("/users/{user_id}/clear-two-factor")
def user_clear_two_factor(request: Request, user_id: int):
    """The way back in for somebody who lost the phone and the codes.

    Deliberately available to an administrator: without it, a lost phone means
    a person is permanently locked out of their own company's books, which is
    not a trade any small business would accept. It is recorded in the audit
    trail, because clearing somebody's second factor is exactly the move an
    attacker who reached an admin account would make.
    """
    from ..main import flash
    from ..services import twofactor as TF

    admin = need(request, P_ADMIN)
    db = db_of(request)
    user = db.get(User, user_id)
    if user is None:
        return redirect("/settings/users")

    TF.turn_off(user)
    audit(db, admin, "TWOFACTOR_CLEARED", "User", user.id, detail=user.username,
          ip=client_ip(request))
    db.commit()
    flash(request, f"Two-factor sign-in cleared for '{user.username}'. They sign in "
                   "with their password alone until they set it up again.", "warning")
    return redirect("/settings/users")


@router.post("/users/require-two-factor")
async def require_two_factor(request: Request):
    """Insist on a second factor for everybody in this company."""
    from ..main import flash

    admin = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    company = db.get(Company, 1)
    if company is None:
        return redirect("/settings/users")

    wanted = parse_bool(form.get("require_two_factor"))
    company.require_two_factor = wanted
    audit(db, admin, "TWOFACTOR_POLICY", "Company", 1,
          detail="required" if wanted else "optional", ip=client_ip(request))
    db.commit()
    if wanted:
        flash(request, "Everybody must now set up two-factor sign-in. Nobody is locked "
                       "out — they are shown the setup screen the next time they work.",
              "success")
    else:
        flash(request, "Two-factor sign-in is optional again. Anyone who has it stays "
                       "protected by it.")
    return redirect("/settings/users")


# --------------------------------------------------------------------------
# Tax codes
# --------------------------------------------------------------------------


@router.get("/tax")
def tax_codes(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    return render(
        request, "settings/tax.html",
        vat=list(db.scalars(select(TaxCode).where(TaxCode.kind == VAT).order_by(TaxCode.sort))),
        wht=list(db.scalars(select(TaxCode).where(TaxCode.kind == WHT).order_by(TaxCode.sort))),
        cfg=config,
    )


@router.post("/tax/save")
async def tax_save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    ids = form.getlist("code_id")
    rates = form.getlist("rate")
    rates_no_tin = form.getlist("rate_no_tin")
    actives = {parse_id(v) for v in form.getlist("active")}
    for i, raw in enumerate(ids):
        cid = parse_id(raw)
        code = db.get(TaxCode, cid) if cid else None
        if code is None:
            continue
        if i < len(rates) and rates[i] != "":
            code.rate = rates[i].strip()
        if i < len(rates_no_tin) and rates_no_tin[i] != "":
            code.rate_no_tin = rates_no_tin[i].strip()
        code.is_active = cid in actives
    audit(db, user, "UPDATE", "TaxCode", detail="Tax rates updated", ip=client_ip(request))
    db.commit()
    flash(request, "Tax codes updated.")
    return redirect("/settings/tax")


# --------------------------------------------------------------------------
# Period lock and year end
# --------------------------------------------------------------------------


@router.get("/periods")
def periods(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    years = list(db.scalars(select(FiscalYear).order_by(FiscalYear.start_date.desc())))
    company = db.get(Company, 1)
    fy_start, fy_end = R.fiscal_year_bounds(db, date.today())
    pl = R.profit_and_loss(db, fy_start, fy_end)
    return render(
        request, "settings/periods.html",
        years=years, company=company, fy_start=fy_start, fy_end=fy_end,
        profit=pl.net_profit, today=date.today(),
    )


@router.post("/periods/lock")
async def lock(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    company = db.get(Company, 1)
    raw = form.get("lock_date") or ""
    company.lock_date = parse_date(raw) if raw.strip() else None
    audit(db, user, "LOCK", "Company", 1,
          detail=f"lock date {company.lock_date}", ip=client_ip(request))
    db.commit()
    flash(request, f"Books locked up to {company.lock_date:%d %b %Y}." if company.lock_date
          else "The period lock has been removed.")
    return redirect("/settings/periods")


@router.post("/periods/close-year")
async def close_year(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    end = parse_date(form.get("year_end"))
    start, _ = R.fiscal_year_bounds(db, end)

    fy = db.scalar(select(FiscalYear).where(FiscalYear.start_date == start))
    if fy and fy.is_closed:
        flash(request, f"{fy.name} is already closed.", "warning")
        return redirect("/settings/periods")

    bals = R.balances(db, start, end)
    retained = sys_account(db, "RETAINED_EARNINGS")
    draft = EntryDraft(
        date=end,
        memo=f"Year-end close — profit and loss for the year to {end:%d %b %Y}",
        reference="YEAR-END",
        source="CLOSING",
    )
    net = 0
    for acc in db.scalars(select(Account).where(Account.type.in_([INCOME, EXPENSE]))):
        d, c = bals.get(acc.id, (0, 0))
        if d == c:
            continue
        # Post the opposite of the balance to bring the account to nil
        draft.signed(acc, c - d, f"Close {acc.code} — {acc.name}")
        net += (c - d)
    if not draft.lines:
        flash(request, "There is nothing to close for this year.", "warning")
        return redirect("/settings/periods")

    draft.signed(retained, -net, "Profit for the year transferred to retained earnings")

    try:
        entry = post_entry(db, draft, user=user, allow_locked=True)
        if fy is None:
            from ..seed import ensure_fiscal_year

            fy = ensure_fiscal_year(db, end, db.get(Company, 1).fiscal_year_start_month)
        fy.is_closed = True
        fy.closed_at = clock.now()
        fy.closing_entry_id = entry.id
        company = db.get(Company, 1)
        if parse_bool(form.get("also_lock")):
            company.lock_date = end
        audit(db, user, "CLOSE_YEAR", "FiscalYear", fy.id,
              detail=f"{fy.name} closed, profit {fmt(-net)}", ip=client_ip(request))
        db.commit()
        flash(request, f"{fy.name} closed. {fmt(abs(net))} "
                       f"{'profit' if net < 0 else 'loss'} transferred to retained earnings "
                       f"as journal {entry.number}.")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect("/settings/periods")


# --------------------------------------------------------------------------
# Backup and restore
# --------------------------------------------------------------------------


@router.get("/backup")
def backup_page(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    slug = request.state.company_slug
    auto = autobackup.load()
    return render(
        request, "settings/backup.html",
        backups=registry.list_backups(slug),
        data_dir=str(registry.company_dir(slug)),
        auto=auto,
        schedules=autobackup.SCHEDULE_LABELS,
        next_due=autobackup.next_due(auto),
    )


def _save_schedule(form) -> autobackup.Settings:
    auto = autobackup.load()
    schedule = (form.get("schedule") or "").strip().upper()
    if schedule in autobackup.SCHEDULE_LABELS:
        auto.schedule = schedule
    auto.hour = min(23, max(0, parse_int(form.get("hour"), auto.hour) or 0))
    auto.weekday = min(6, max(0, parse_int(form.get("weekday"), auto.weekday) or 0))
    auto.keep = min(365, max(1, parse_int(form.get("keep"), auto.keep) or 1))
    auto.copy_to = (form.get("copy_to") or "").strip()
    autobackup.save(auto)
    return auto


@router.post("/backup/schedule")
async def backup_schedule(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    auto = _save_schedule(await request.form())
    autobackup.start()
    audit(db, user, "UPDATE", "BackupSchedule", 1, detail=auto.when,
          ip=client_ip(request))
    db.commit()
    flash(request, f"Automatic backups: {auto.when.lower()}."
          if auto.on else "Automatic backups switched off.")
    return redirect("/settings/backup")


@router.post("/backup/schedule/test")
async def backup_schedule_test(request: Request):
    """Save the schedule and immediately prove it works.

    Especially the second copy: a folder that is mistyped, or a flash drive that
    is not plugged in, should be found out now rather than the night everything
    depends on it.
    """
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    _save_schedule(await request.form())
    auto = autobackup.run_once(force=True)
    audit(db, user, "BACKUP", "Company", 1, detail="Scheduled backup, run by hand",
          ip=client_ip(request))
    db.commit()
    if auto.last_error:
        flash(request, f"It ran, but: {auto.last_error}", "danger")
    else:
        flash(request, f"It works — {auto.last_result}.")
    return redirect("/settings/backup")


@router.post("/backup")
def backup_now(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    name = registry.backup(request.state.company_slug).name
    audit(db, user, "BACKUP", "Company", 1, detail=name, ip=client_ip(request))
    db.commit()
    flash(request, f"Backup created: {name}")
    return redirect("/settings/backup")


@router.get("/backup/download/{name}")
def backup_download(request: Request, name: str):
    need(request, P_ADMIN)
    folder = (registry.company_dir(request.state.company_slug) / "backups").resolve()
    path = (folder / name).resolve()
    # Never serve anything outside this company's own backup folder
    if not str(path).startswith(str(folder)) or not path.exists():
        return redirect("/settings/backup")
    return FileResponse(str(path), filename=name, media_type="application/octet-stream")


@router.get("/backup/download-live")
def backup_live(request: Request):
    need(request, P_ADMIN)
    slug = request.state.company_slug
    path = registry.backup(slug, "download")
    return FileResponse(str(path), filename=path.name,
                        media_type="application/octet-stream")


@router.post("/restore")
async def restore(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    upload: UploadFile | None = form.get("file")  # type: ignore[assignment]
    if upload is None or not upload.filename:
        flash(request, "Choose a backup file to restore.", "danger")
        return redirect("/settings/backup")

    slug = request.state.company_slug
    tmp = registry.company_dir(slug) / "backups" / f"restore-{datetime.now():%Y%m%d-%H%M%S}.upload"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(upload.file, f)

    # Make sure the file really is one of our databases before swapping it in
    if not registry.looks_like_our_database(tmp):
        tmp.unlink(missing_ok=True)
        flash(request, "That file is not a Nexora Books backup.", "danger")
        return redirect("/settings/backup")

    audit(db, user, "RESTORE", "Company", 1, detail=upload.filename, ip=client_ip(request))
    db.commit()

    registry.backup(slug, "before-restore")
    pending = registry.company_dir(slug) / "restore-pending.db"
    shutil.move(str(tmp), str(pending))
    flash(request,
          "The backup has been checked and staged. Close Nexora Books and start it again — "
          "the restore completes on the next start.", "warning")
    return redirect("/settings/backup")


# --------------------------------------------------------------------------
# About / network access
# --------------------------------------------------------------------------


@router.get("/network")
def network(request: Request):
    from ..main import render
    import socket

    need(request, P_ADMIN)
    db = db_of(request)
    company = db.get(Company, 1)
    addresses = network_mod.lan_addresses()
    port = network_mod.port_of(str(request.base_url))
    base, how = network_mod.reachable_base(str(request.base_url),
                                           getattr(company, "staff_url", "") or "")
    from .. import tls

    settings = config.tls_settings()
    certificate = tls.existing() if settings.get("made_here") else None
    return render(request, "settings/network.html",
                  host=socket.gethostname(), addresses=addresses, port=port,
                  staff_url=getattr(company, "staff_url", "") or "",
                  outgoing=base, outgoing_how=how,
                  encrypted=config.serving_over_tls(),
                  encrypted_now=str(request.base_url).startswith("https"),
                  tls_settings=settings,
                  certificate=certificate)


@router.post("/network/encrypt")
async def network_encrypt(request: Request):
    """Turn encryption on, making a certificate if there is not one already."""
    from ..main import flash
    from .. import tls

    need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    wanted = parse_bool(form.get("on"))

    if not wanted:
        config.save_tls_settings({"on": False})
        audit(db, user_of(request), "TLS_OFF", "Company", 1, ip=client_ip(request))
        db.commit()
        flash(request, "Encryption switched off. Restart Nexora Books for it to take "
                       "effect — until then it is still running as it was.", "warning")
        return redirect("/settings/network")

    supplied_cert = (form.get("cert_path") or "").strip()
    supplied_key = (form.get("key_path") or "").strip()
    if supplied_cert and supplied_key:
        from pathlib import Path

        if not (Path(supplied_cert).exists() and Path(supplied_key).exists()):
            flash(request, "One of those files is not there. Check both paths and "
                           "try again — nothing has been changed.", "danger")
            return redirect("/settings/network")
        config.save_tls_settings({"on": True, "cert": supplied_cert,
                                  "key": supplied_key, "made_here": False})
        message = ("Encryption is set up with the certificate you supplied. Restart "
                   "Nexora Books and the address becomes https://")
    else:
        company = db.get(Company, 1)
        names = ["localhost", "127.0.0.1", socket_name()]
        names += network_mod.lan_addresses()
        stated = network_mod.host_of(getattr(company, "staff_url", "") or "")
        if stated:
            names.append(stated)
        made = tls.make(names, (company.name if company else "") or config.APP_NAME)
        config.save_tls_settings({"on": True, "cert": str(made.cert_path),
                                  "key": str(made.key_path), "made_here": True})
        message = ("Encryption is on, using a certificate this computer made for "
                   "itself. Restart Nexora Books, then see the note below about the "
                   "warning your staff will see the first time.")

    audit(db, user_of(request), "TLS_ON", "Company", 1, ip=client_ip(request))
    db.commit()
    flash(request, message)
    return redirect("/settings/network")


def socket_name() -> str:
    import socket as _socket

    try:
        return _socket.gethostname()
    except OSError:                                 # pragma: no cover
        return "localhost"


@router.post("/network")
async def network_save(request: Request):
    """Write down the address staff use, when it is not the detected one.

    This is what every emailed link is built from, so it is worth a screen of
    its own rather than being guessed at each time.
    """
    from ..main import flash

    need(request, P_ADMIN)
    db = db_of(request)
    company = db.get(Company, 1)
    form = await request.form()
    typed = (form.get("staff_url") or "").strip()

    if not typed:
        company.staff_url = ""
        db.commit()
        flash(request, "Cleared. Links sent by email will use this computer's "
                       "address on the office network.")
        return redirect("/settings/network")

    tidied = network_mod.tidy(typed, network_mod.port_of(str(request.base_url)))
    if not tidied:
        flash(request, f"'{typed}' is not an address anybody could open. It "
                       "should look like 192.168.1.20:8756 — the one shown "
                       "above.", "danger")
        return redirect("/settings/network")
    if network_mod.is_loopback(network_mod.host_of(tidied)):
        flash(request, f"'{typed}' means 'this computer' wherever it is read, "
                       "so on a member of staff's machine it points at their "
                       "own computer and reaches nothing. Use the address "
                       "shown above instead.", "danger")
        return redirect("/settings/network")

    company.staff_url = tidied
    audit(db, user_of(request), "NETWORK_ADDRESS", "Company", 1, detail=tidied,
          ip=client_ip(request))
    db.commit()
    flash(request, f"Emailed links will point at {tidied}.")
    return redirect("/settings/network")


# --------------------------------------------------------------------------
# Inviting somebody, rather than emailing them a password
# --------------------------------------------------------------------------


def _send_invitation(request: Request, db, admin, user) -> tuple[bool, str]:
    """Send this person their invitation. Returns (sent, what to tell the admin)."""
    from .. import network
    from ..services import invites, mailer

    if not user.email:
        return False, (f"{user.username} has no email address, so there is "
                       "nobody to send an invitation to. Add one, or hand over "
                       "the temporary password in person.")
    settings = mailer.load()
    if not settings.ready:
        return False, ("Email is not set up yet, so no invitation could be "
                       "sent — Settings › Email takes a minute. The temporary "
                       "password above still works.")

    company = db.get(Company, 1)

    # Build the link from an address the *recipient* can reach. The address in
    # this administrator's browser is usually 127.0.0.1, which on the member of
    # staff's computer means their own computer and reaches nothing at all.
    base, how = network.reachable_base(str(request.base_url),
                                       getattr(company, "staff_url", "") or "")
    if not base:
        return False, (
            "No invitation was sent, because there is no address that would "
            "work on anybody else's computer. The address in your browser, "
            f"{network.host_of(str(request.base_url))}, means 'this computer' "
            "wherever it is read, so a link built from it would send "
            f"{user.username} to their own machine. Go to Settings › Access "
            "from other computers and write down the address your staff use, "
            "then invite them again.")

    token = invites.create(db, user)
    url = invites.link(base, token)
    try:
        mailer.send(user.email, invites.subject(company),
                    invites.body(company, user, url, admin))
    except mailer.MailError as exc:
        invites.revoke(db, user)
        return False, f"The invitation could not be sent. {exc}"

    audit(db, admin, "INVITE_SENT", "User", user.id,
          detail=f"{user.username} at {user.email} via {base}", ip=client_ip(request))
    return True, (f"An invitation has been emailed to {user.email}. It lets "
                  f"{user.username} choose their own password, works once, and "
                  f"stops working in {invites.VALID_DAYS} days. No password was "
                  "sent — there is nothing in that message worth stealing. "
                  f"The link points at {base}, which is where their computer "
                  "will look for these books; if that is not the address your "
                  "staff use, set it under Settings › Access from other "
                  "computers and invite them again.")


@router.post("/users/{user_id}/invite")
def user_invite(request: Request, user_id: int):
    from ..main import flash

    admin = need(request, P_ADMIN)
    db = db_of(request)
    user = db.get(User, user_id)
    if user is None:
        return redirect("/settings/users")
    if not user.is_active:
        flash(request, f"{user.username}'s account is switched off. Turn it "
                       "back on before inviting them.", "danger")
        return redirect("/settings/users")

    sent, message = _send_invitation(request, db, admin, user)
    db.commit()
    flash(request, message, "success" if sent else "warning")
    return redirect("/settings/users")


@router.post("/users/{user_id}/cancel-invite")
def user_cancel_invite(request: Request, user_id: int):
    from ..main import flash

    admin = need(request, P_ADMIN)
    db = db_of(request)
    user = db.get(User, user_id)
    if user is None:
        return redirect("/settings/users")

    from ..services import invites

    invites.revoke(db, user)
    audit(db, admin, "INVITE_CANCELLED", "User", user.id, detail=user.username,
          ip=client_ip(request))
    db.commit()
    flash(request, f"The invitation for {user.username} has been cancelled. "
                   "The link in their email no longer works.", "warning")
    return redirect("/settings/users")


# --------------------------------------------------------------------------
# Electronic invoicing
# --------------------------------------------------------------------------


@router.get("/einvoicing")
def einvoicing(request: Request):
    from ..main import render
    from ..services import einvoice as ei

    need(request, P_ADMIN)
    db = db_of(request)
    slug = registry.default_slug() if not hasattr(request.state, "company_slug") \
        else request.state.company_slug
    return render(
        request, "settings/einvoicing.html",
        settings=ei.load(slug),
        report=ei.readiness(db),
        modes=[(k, v) for k, v in ei.MODE_LABELS.items()],
        OFF=ei.OFF, REHEARSAL=ei.REHEARSAL, LIVE=ei.LIVE,
        outbox=ei.outbox(db, limit=20),
    )


@router.post("/einvoicing")
async def einvoicing_save(request: Request):
    from ..main import flash
    from ..services import einvoice as ei

    user = need(request, P_ADMIN)
    db = db_of(request)
    slug = request.state.company_slug
    form = await request.form()

    current = ei.load(slug)
    mode = (form.get("mode") or ei.OFF).strip().upper()
    if mode not in (ei.OFF, ei.REHEARSAL, ei.LIVE):
        mode = ei.OFF

    settings = ei.Settings(
        mode=mode,
        auto_submit=parse_bool(form.get("auto_submit")),
        block_uncleared=parse_bool(form.get("block_uncleared")),
        provider_name=(form.get("provider_name") or "").strip(),
        submit_url=(form.get("submit_url") or "").strip(),
        token_url=(form.get("token_url") or "").strip(),
        client_id=(form.get("client_id") or "").strip(),
        # An unchanged secret arrives as blank, because the form never renders
        # it back. Blank must mean "leave it alone", not "delete it" — or every
        # save of an unrelated checkbox would quietly take a business offline.
        client_secret=((form.get("client_secret") or "").strip()
                       or current.client_secret),
        scope=(form.get("scope") or "").strip(),
        business_id=(form.get("business_id") or "").strip(),
        irn_path=(form.get("irn_path") or "data.irn").strip(),
        csid_path=(form.get("csid_path") or "data.csid").strip(),
        qr_path=(form.get("qr_path") or "data.qr").strip(),
        customization_id=((form.get("customization_id") or "").strip()
                          or current.customization_id),
        extra_headers=current.extra_headers,
    )

    if settings.mode == ei.LIVE and not settings.ready_to_go_live:
        flash(request,
              "Live filing needs an address and credentials from the Revenue "
              "Service or your provider. Saved as a rehearsal instead, so you "
              "can keep checking your invoices in the meantime.", "warning")
        settings.mode = ei.REHEARSAL

    ei.save(slug, settings)
    audit(db, user, "UPDATE", "EInvoiceSettings",
          detail=f"E-invoicing set to {settings.mode}", ip=client_ip(request))
    db.commit()
    flash(request, "E-invoicing settings saved.")
    return redirect("/settings/einvoicing")


@router.post("/einvoicing/send-queue")
def einvoicing_send_queue(request: Request):
    from ..main import flash
    from ..services import einvoice as ei

    need(request, P_ADMIN)
    db = db_of(request)
    cleared, waiting = ei.send_outbox(db)
    db.commit()
    if cleared and not waiting:
        flash(request, f"{cleared} cleared.")
    elif cleared:
        flash(request, f"{cleared} cleared, {waiting} still waiting.", "warning")
    elif waiting:
        flash(request, f"Nothing got through. {waiting} still waiting — they "
                       "will keep trying on their own.", "warning")
    else:
        flash(request, "Nothing was waiting to go.", "info")
    return redirect("/settings/einvoicing")
