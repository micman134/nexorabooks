"""Bringing a bank statement in, and deciding what each line was.

The flow is deliberately three steps rather than one button:

  1. **Choose a file.** It is read but nothing is saved, and the preview shows
     what was understood — the columns, the dates, the totals and whether the
     running balance agrees. A statement read upside down is obvious here and
     nowhere later.

  2. **Bring it in.** The lines are stored and matched against the books.
     Still nothing is posted.

  3. **Go through it.** Each line shows what it probably is and why, with the
     evidence. Confirming a line posts that line, and only that line.

The one shortcut, "confirm the strong matches", applies them one at a time
through exactly the same path, so it cannot do anything a person clicking each
button could not.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from ..models import (
    ACTION_CLEAR,
    ACTION_IGNORE,
    ACTION_PAYMENT,
    ACTION_POST,
    ACTION_RECEIPT,
    CONFIRMED,
    IGNORED,
    SUGGESTED,
    UNMATCHED,
    Account,
    BankAccount,
    BankImport,
    BankImportLine,
    Contact,
)
from ..security import P_JOURNAL, P_VIEW
from ..services import bankimport as BI
from ..services import charts, matching, reports, statements
from ..services.importer import ImportError_
from ..services.posting import PostingError, audit
from ._common import client_ip, db_of, need, parse_bool, parse_id, redirect

router = APIRouter(prefix="/banking")

#: A statement file is small. Anything this large is not one.
MAX_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------
# Step 1 — choose a file and see what was understood
# --------------------------------------------------------------------------


@router.get("/{bank_id}/import")
def upload_form(request: Request, bank_id: int):
    from ..main import render

    need(request, P_JOURNAL)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        return redirect("/banking")
    return render(request, "banking/import.html", bank=bank,
                  recent=_recent(db, bank_id))


def _recent(db, bank_id: int, limit: int = 8):
    from sqlalchemy import select

    return list(db.scalars(
        select(BankImport)
        .where(BankImport.bank_account_id == bank_id)
        .order_by(BankImport.id.desc()).limit(limit)
    ))


@router.post("/{bank_id}/import")
async def upload(request: Request, bank_id: int):
    from ..main import render

    user = need(request, P_JOURNAL)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    if bank is None:
        return redirect("/banking")

    form = await request.form()
    upload_file = form.get("file")
    # Duck typing on purpose. ``fastapi.UploadFile`` is a *subclass* of the
    # Starlette class the form actually hands back, so an isinstance check
    # against it is always False and silently rejects every upload.
    filename = getattr(upload_file, "filename", "")
    if not filename or not hasattr(upload_file, "read"):
        return render(request, "banking/import.html", bank=bank, recent=_recent(db, bank_id),
                      error="Choose the statement file your bank gave you.")

    raw = await upload_file.read()
    if len(raw) > MAX_BYTES:
        return render(request, "banking/import.html", bank=bank, recent=_recent(db, bank_id),
                      error="That file is very large for a bank statement. Export a "
                            "single month and try again.")

    date_order = (form.get("date_order") or "").strip()
    flip = parse_bool(form.get("flip"))
    try:
        reading = statements.read(raw, date_order=date_order, flip=flip)
    except ImportError_ as exc:
        return render(request, "banking/import.html", bank=bank, recent=_recent(db, bank_id),
                      error=str(exc))

    # Held in the session only as a name; the file itself is re-read on confirm
    # so nothing large is kept in a cookie.
    request.session["statement"] = {
        "bank_id": bank_id,
        "filename": filename,
        "date_order": reading.date_order,
        "flip": flip,
    }
    _stash(request, raw)

    seen = BI.existing_fingerprints(db, bank_id)
    already = sum(1 for line in reading.lines if line.fingerprint in seen)
    book_balance = reports._cash_balance(db, {bank.account_id}, reading.last_date or date.today())

    return render(
        request,
        "banking/import_preview.html",
        bank=bank,
        reading=reading,
        filename=filename,
        already=already,
        book_balance=book_balance,
        buckets=charts.bucket_lines(reading.lines, reading.opening_balance),
        chart=charts.cash_chart(charts.bucket_lines(reading.lines, reading.opening_balance)),
    )


#: The uploaded bytes, kept in memory between the preview and the confirm.
#: A file is at most a few hundred kilobytes and the alternative — writing it
#: to disk — leaves a copy of somebody's bank statement lying about.
_PENDING: dict[str, bytes] = {}


def _stash(request: Request, raw: bytes) -> None:
    key = request.session.get("statement_key") or _new_key()
    request.session["statement_key"] = key
    _PENDING.clear()          # only ever one at a time, on one computer
    _PENDING[key] = raw


def _new_key() -> str:
    import secrets

    return secrets.token_hex(8)


def _unstash(request: Request) -> bytes | None:
    key = request.session.get("statement_key")
    return _PENDING.get(key) if key else None


# --------------------------------------------------------------------------
# Step 2 — bring it in and match it
# --------------------------------------------------------------------------


@router.post("/{bank_id}/import/confirm")
async def confirm_import(request: Request, bank_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    bank = db.get(BankAccount, bank_id)
    raw = _unstash(request)
    if bank is None or raw is None:
        flash(request, "That upload has expired. Please choose the file again.", "warning")
        return redirect(f"/banking/{bank_id}/import")

    form = await request.form()
    held = request.session.get("statement", {})
    try:
        reading = statements.read(
            raw,
            date_order=(form.get("date_order") or held.get("date_order") or ""),
            flip=parse_bool(form.get("flip")),
        )
    except ImportError_ as exc:
        flash(request, str(exc), "danger")
        return redirect(f"/banking/{bank_id}/import")

    outcome = BI.create(db, bank_id, reading, held.get("filename", ""), user)
    db.flush()

    if outcome.added == 0:
        # Every line was already here. Keeping an empty batch would leave a
        # row in the list that says "all done" about nothing at all.
        db.delete(outcome.batch)
        db.commit()
        _PENDING.clear()
        request.session.pop("statement", None)
        request.session.pop("statement_key", None)
        flash(request, f"All {outcome.duplicates} lines in that file had already been "
                       "imported, so nothing was added.", "warning")
        return redirect(f"/banking/{bank_id}/import")

    BI.run_matching(db, outcome.batch)
    audit(db, user, "STATEMENT_IMPORT", "BankImport", outcome.batch.id,
          detail=f"{outcome.added} lines, {outcome.duplicates} already seen",
          ip=client_ip(request))
    db.commit()

    _PENDING.clear()
    request.session.pop("statement", None)
    request.session.pop("statement_key", None)

    if outcome.duplicates:
        flash(request, f"{outcome.added} lines brought in. {outcome.duplicates} were "
                       "already there and were left alone.")
    else:
        flash(request, f"{outcome.added} lines brought in.")
    return redirect(f"/banking/import/{outcome.batch.id}")


# --------------------------------------------------------------------------
# Step 3 — go through it
# --------------------------------------------------------------------------


@router.get("/import/{batch_id}")
def review(request: Request, batch_id: int):
    from ..main import render
    from sqlalchemy import select

    need(request, P_VIEW)
    db = db_of(request)
    batch = db.get(BankImport, batch_id)
    if batch is None:
        return redirect("/banking")

    show = (request.query_params.get("show") or "todo").lower()
    lines = list(batch.lines)
    if show == "todo":
        visible = [line for line in lines if line.status not in (CONFIRMED, IGNORED)]
    elif show == "done":
        visible = [line for line in lines if line.status in (CONFIRMED, IGNORED)]
    else:
        visible = lines

    bank = batch.bank_account
    book_balance = reports._cash_balance(
        db, {bank.account_id}, batch.last_date or date.today()
    )
    buckets = charts.bucket_lines(lines, batch.opening_balance)

    strong = sum(1 for line in lines
                 if line.status == SUGGESTED and line.score >= matching.STRONG)
    weak = sum(1 for line in lines
               if line.status == SUGGESTED and line.score < matching.STRONG)
    todo = sum(1 for line in lines if line.status == UNMATCHED)
    done = sum(1 for line in lines if line.status in (CONFIRMED, IGNORED))

    return render(
        request,
        "banking/import_review.html",
        batch=batch,
        bank=bank,
        lines=visible,
        show=show,
        chart=charts.cash_chart(buckets),
        buckets=buckets,
        book_balance=book_balance,
        counts={"strong": strong, "weak": weak, "todo": todo, "done": done,
                "all": len(lines)},
        split=charts.split_bar([
            charts.Slice_("Recognised", strong, "var(--good)"),
            charts.Slice_("Worth a look", weak, "var(--warn)"),
            charts.Slice_("Needs you", todo, "var(--danger)"),
            charts.Slice_("Dealt with", done, "var(--accent)"),
        ]),
        breakdown=_breakdown(db, lines),
        accounts=list(db.scalars(
            select(Account).where(Account.is_active.is_(True)).order_by(Account.code)
        )),
        contacts=list(db.scalars(select(Contact).order_by(Contact.name))),
        strong_mark=matching.STRONG,
    )


def _breakdown(db, lines) -> dict:
    """Where the money went and where it came from, biggest first.

    Grouped by the payee as the bank wrote it, because before anything is
    categorised that is all there is to group by — and it is the grouping that
    shows the subscription nobody cancelled.
    """
    out: dict[str, int] = {}
    incoming: dict[str, int] = {}
    for line in lines:
        key = matching.normalise_payee(line.description) or "(no description)"
        key = key[:48]
        if line.amount < 0:
            out[key] = out.get(key, 0) + -line.amount
        else:
            incoming[key] = incoming.get(key, 0) + line.amount

    def top(source: dict) -> list[tuple[str, int, float]]:
        rows = sorted(source.items(), key=lambda pair: -pair[1])[:8]
        biggest = rows[0][1] if rows else 1
        return [(name, value, value * 100 / biggest) for name, value in rows]

    return {"out": top(out), "in": top(incoming)}


@router.post("/import/{batch_id}/line/{line_id}")
async def decide(request: Request, batch_id: int, line_id: int):
    """Carry out one line, exactly as the person said."""
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    line = db.get(BankImportLine, line_id)
    if line is None or line.batch_id != batch_id:
        return redirect(f"/banking/import/{batch_id}")

    form = await request.form()
    action = (form.get("action") or "").strip().upper()
    show = form.get("show") or "todo"

    documents = [
        int(value) for value in form.getlist("document_id")
        if str(value).strip().isdigit()
    ]
    try:
        BI.apply(
            db, line, action,
            account_id=parse_id(form.get("account_id")),
            contact_id=parse_id(form.get("contact_id")),
            document_ids=documents or None,
            journal_line_id=parse_id(form.get("journal_line_id")),
            user=user,
        )
        audit(db, user, "STATEMENT_LINE", "BankImportLine", line.id,
              detail=f"{action} {line.description[:80]}", ip=client_ip(request))
        db.commit()
    except (BI.ImportProblem, PostingError) as exc:
        db.rollback()
        flash(request, str(exc), "danger")
    return redirect(f"/banking/import/{batch_id}?show={show}")


@router.post("/import/{batch_id}/confirm-strong")
def confirm_strong(request: Request, batch_id: int):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    batch = db.get(BankImport, batch_id)
    if batch is None:
        return redirect("/banking")

    result = BI.confirm_strong(db, batch, user)
    audit(db, user, "STATEMENT_BULK", "BankImport", batch.id,
          detail=f"{result['done']} confirmed", ip=client_ip(request))
    db.commit()

    if result["done"]:
        flash(request, f"{result['done']} lines confirmed and posted.")
    else:
        flash(request, "Nothing was confident enough to confirm on its own. "
                       "Go through them below.", "warning")
    for problem in result["problems"][:4]:
        flash(request, problem, "warning")
    return redirect(f"/banking/import/{batch_id}")


@router.post("/import/{batch_id}/rematch")
def rematch(request: Request, batch_id: int):
    """Look again — useful after entering the invoices the statement refers to."""
    from ..main import flash

    need(request, P_JOURNAL)
    db = db_of(request)
    batch = db.get(BankImport, batch_id)
    if batch is None:
        return redirect("/banking")
    for line in batch.lines:
        if line.status == SUGGESTED:
            line.status = UNMATCHED
    BI.run_matching(db, batch)
    db.commit()
    flash(request, "Looked again at everything still outstanding.")
    return redirect(f"/banking/import/{batch_id}")


@router.get("/import/{batch_id}/line/{line_id}/choices")
def choices(request: Request, batch_id: int, line_id: int):
    """The documents and existing entries this line could be."""
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    line = db.get(BankImportLine, line_id)
    if line is None or line.batch_id != batch_id:
        return redirect(f"/banking/import/{batch_id}")
    from sqlalchemy import select

    return render(
        request,
        "banking/import_line.html",
        line=line,
        batch=db.get(BankImport, batch_id),
        accounts=list(db.scalars(
            select(Account).where(Account.is_active.is_(True)).order_by(Account.code)
        )),
        contacts=list(db.scalars(select(Contact).order_by(Contact.name))),
        **BI.choices_for(db, line),
    )


@router.get("/imports")
def all_imports(request: Request):
    from ..main import render
    from sqlalchemy import select

    need(request, P_VIEW)
    db = db_of(request)
    return render(
        request, "banking/imports.html",
        batches=list(db.scalars(select(BankImport).order_by(BankImport.id.desc()).limit(60))),
    )
