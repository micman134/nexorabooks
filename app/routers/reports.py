"""Financial reports and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..models import Account, AuditLog, JournalEntry
from ..money import fmt_plain
from ..security import P_ADMIN, P_VIEW
from ..services import reports as R
from ._common import db_of, need, parse_date, parse_id, period_from_query, redirect

router = APIRouter(prefix="/reports")


def csv_response(filename: str, header: list[str], rows: list[list]) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    return render(request, "reports/index.html")


# --------------------------------------------------------------------------


@router.get("/trial-balance")
def trial_balance(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    cumulative = request.query_params.get("cumulative", "1") == "1"
    rows, td, tc = R.trial_balance(db, None if cumulative else start, end)

    if request.query_params.get("format") == "csv":
        return csv_response(
            f"trial-balance-{end}.csv",
            ["Code", "Account", "Type", "Debit", "Credit"],
            [[r.account.code, r.account.name, r.account.type,
              fmt_plain(r.net_debit), fmt_plain(r.net_credit)] for r in rows],
        )
    return render(
        request, "reports/trial_balance.html",
        rows=rows, total_debit=td, total_credit=tc, start=start, end=end,
        preset=preset, cumulative=cumulative,
    )


@router.get("/profit-and-loss")
def profit_and_loss(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    days = (end - start).days + 1
    prior_end = start - timedelta(days=1)
    prior_start = prior_end - timedelta(days=days - 1)
    pl = R.profit_and_loss(db, start, end, prior_start, prior_end,
                           compare_label="Previous period")

    if request.query_params.get("format") == "csv":
        rows = []
        for sec in (pl.revenue, pl.cogs, pl.expenses, pl.other_income, pl.tax):
            for acc, v, pv in sec.rows:
                rows.append([sec.title, acc.code, acc.name, fmt_plain(v), fmt_plain(pv)])
        rows.append(["", "", "NET PROFIT", fmt_plain(pl.net_profit), fmt_plain(pl.net_profit_prior)])
        return csv_response(f"profit-and-loss-{start}-to-{end}.csv",
                            ["Section", "Code", "Account", "This period", "Prior period"], rows)
    return render(request, "reports/profit_loss.html", pl=pl, preset=preset)


@router.get("/balance-sheet")
def balance_sheet(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    as_of = parse_date(request.query_params.get("end"), date.today())
    bs = R.balance_sheet(db, as_of)

    if request.query_params.get("format") == "csv":
        rows = []
        for sec in (bs.current_assets, bs.fixed_assets, bs.current_liabilities,
                    bs.long_term_liabilities, bs.equity):
            for acc, v in sec.rows:
                rows.append([sec.title, acc.code, acc.name, fmt_plain(v)])
        rows.append(["Equity", "", "Retained earnings brought forward",
                     fmt_plain(bs.retained_brought_forward)])
        rows.append(["Equity", "", "Profit for the period", fmt_plain(bs.current_earnings)])
        return csv_response(f"balance-sheet-{as_of}.csv",
                            ["Section", "Code", "Account", "Amount"], rows)
    return render(request, "reports/balance_sheet.html", bs=bs, as_of=as_of)


@router.get("/cash-flow")
def cash_flow(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    cf = R.cash_flow(db, start, end)
    return render(request, "reports/cash_flow.html", cf=cf, preset=preset)


@router.get("/general-ledger")
def general_ledger(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    account_id = parse_id(request.query_params.get("account"))
    accounts = list(db.scalars(select(Account).order_by(Account.code)))
    if not account_id and accounts:
        account_id = accounts[0].id
    acc, opening, rows, closing = R.general_ledger(db, account_id, start, end)

    if request.query_params.get("format") == "csv":
        data = [["", "", "Opening balance", "", "", fmt_plain(opening)]]
        for r in rows:
            data.append([r.entry.date.isoformat(), r.entry.number, r.line.memo or r.entry.memo,
                         fmt_plain(r.line.debit), fmt_plain(r.line.credit), fmt_plain(r.running)])
        return csv_response(f"ledger-{acc.code}-{start}-to-{end}.csv",
                            ["Date", "Journal", "Description", "Debit", "Credit", "Balance"], data)
    return render(
        request, "reports/general_ledger.html",
        accounts=accounts, account=acc, opening=opening, rows=rows, closing=closing,
        start=start, end=end, preset=preset,
    )


@router.get("/aging")
def aging(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    receivable = request.query_params.get("kind", "ar") != "ap"
    as_of = parse_date(request.query_params.get("end"), date.today())
    rows, totals, grand = R.aging(db, as_of, receivable)

    if request.query_params.get("format") == "csv":
        data = [[r.contact.name] + [fmt_plain(b) for b in r.buckets] + [fmt_plain(r.total)]
                for r in rows]
        return csv_response(
            f"{'receivables' if receivable else 'payables'}-ageing-{as_of}.csv",
            ["Contact"] + R.AGE_BUCKETS + ["Total"], data,
        )
    return render(
        request, "reports/aging.html",
        rows=rows, totals=totals, grand=grand, buckets=R.AGE_BUCKETS,
        receivable=receivable, as_of=as_of,
    )


@router.get("/vat")
def vat(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    if not request.query_params.get("period") and not request.query_params.get("start"):
        prev = date.today().replace(day=1) - timedelta(days=1)
        start = prev.replace(day=1)
        end = prev
        preset = "last_month"
    ret = R.vat_return(db, start, end)
    return render(request, "reports/vat.html", r=ret, preset=preset)


@router.get("/wht")
def wht(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    payable = request.query_params.get("kind", "payable") == "payable"
    rows, total = R.wht_schedule(db, start, end, payable)

    if request.query_params.get("format") == "csv":
        data = [
            [e.date.isoformat(), e.number, c.name if c else "", c.tin if c else "",
             line.memo, fmt_plain(amount)]
            for e, line, c, amount in rows
        ]
        return csv_response(
            f"wht-{'deducted' if payable else 'suffered'}-{start}-to-{end}.csv",
            ["Date", "Journal", "Party", "Tax ID", "Narration", "Amount"], data,
        )
    return render(
        request, "reports/wht.html",
        rows=rows, total=total, payable=payable, start=start, end=end, preset=preset,
    )


@router.get("/audit-trail")
def audit_trail(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    logs = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.at >= start, AuditLog.at <= end + timedelta(days=1))
            .order_by(AuditLog.at.desc()).limit(1000)
        )
    )
    return render(request, "reports/audit.html", logs=logs, start=start, end=end, preset=preset)
