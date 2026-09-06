"""Setting up email, and sending documents with it."""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import (
    Company,
    Contact,
    EmailLog,
    Employee,
    Invoice,
    PayrollRun,
    Payslip,
)
from ..security import P_ADMIN, P_ENTRY, P_VIEW
from ..services import mailer, pdfdocs, reports
from ..services.posting import audit
from ._common import client_ip, db_of, need, parse_date, parse_int, redirect

router = APIRouter(prefix="/settings/email")
send = APIRouter(prefix="/send")


def _log(db, user, *, to, cc, subject, kind, ref_id, ref_number,
         ok=True, error="") -> EmailLog:
    row = EmailLog(to_address=to[:300], cc=cc[:300], subject=subject[:300],
                   kind=kind, ref_id=ref_id, ref_number=ref_number,
                   ok=ok, error=error[:2000],
                   sent_by_id=user.id if user else None)
    db.add(row)
    db.flush()
    return row


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


class _ExampleLine:
    description = "Contract Bidding, Tender & Proposal Administration"


class _ExampleContact:
    name = "Chinedu Okafor"
    email = "chinedu@example.com"


class _ExampleInvoice:
    """A stand-in document, so wording can be seen before a customer sees it.

    Deliberately not a real invoice pulled from the books: a preview must not
    depend on there being one, and must not quietly show a real customer's
    name to whoever is editing the wording.
    """

    number = "INV-0042"
    contact = _ExampleContact()
    lines = [_ExampleLine()]

    def __init__(self, on):
        self.date = on


def _preview(company) -> tuple[str, str]:
    """The wording as it will arrive, or ('', '') when there is none to show."""
    if not (company.invoice_email_subject or company.invoice_email_body):
        return "", ""
    doc = _ExampleInvoice(date.today())
    amount = pdfdocs.money(250_000_00)
    due = pdfdocs.when(date.today() + timedelta(days=14))
    return (mailer.invoice_subject(company, doc, "Invoice"),
            mailer.invoice_body(company, doc, "Invoice", amount, due))


@router.get("")
def form(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    db = db_of(request)
    company = db.get(Company, 1)
    subject, body = _preview(company)
    return render(
        request, "settings/email.html",
        mail=mailer.load(),
        presets=mailer.PRESETS,
        securities=mailer.SECURITY_LABELS,
        placeholders=mailer.PLACEHOLDERS,
        preview_subject=subject,
        preview_body=body,
        recent=list(db.scalars(
            select(EmailLog).order_by(EmailLog.sent_at.desc()).limit(25))),
    )


@router.post("/wording")
async def wording_save(request: Request):
    """What this company says when it emails an invoice."""
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    company = db.get(Company, 1)
    data = await request.form()

    company.invoice_email_subject = (data.get("invoice_email_subject") or "").strip()[:200]
    company.invoice_email_body = (data.get("invoice_email_body") or "").strip()

    unknown = mailer.unknown_placeholders(
        company.invoice_email_subject + "\n" + company.invoice_email_body)
    audit(db, user, "UPDATE", "Company", 1, detail="invoice email wording",
          ip=client_ip(request))
    db.commit()

    if not (company.invoice_email_subject or company.invoice_email_body):
        flash(request, "Cleared. Invoices go out with the plain wording again.")
    elif unknown:
        flash(request, "Saved — but " + ", ".join(unknown) + " "
              + ("is not something" if len(unknown) == 1 else "are not things")
              + " this can fill in, so it will be sent to your customer exactly "
                "as typed. Check the list of what you can use.", "warning")
    else:
        flash(request, "Saved. That is what goes out with your invoices from now on.")
    return redirect("/settings/email")


def _apply(form_data, mail: mailer.Settings) -> mailer.Settings:
    mail.host = (form_data.get("host") or "").strip()
    mail.port = parse_int(form_data.get("port"), 587) or 587
    security = (form_data.get("security") or "").strip().upper()
    if security in mailer.SECURITY_LABELS:
        mail.security = security
    mail.username = (form_data.get("username") or "").strip()
    # An empty password box means "leave the one that is already saved" —
    # otherwise every other edit on this screen would silently wipe it.
    typed = form_data.get("password")
    if typed:
        mail.password = typed
    mail.from_name = (form_data.get("from_name") or "").strip()
    mail.from_email = (form_data.get("from_email") or "").strip()
    mail.reply_to = (form_data.get("reply_to") or "").strip()
    mail.signature = form_data.get("signature") or ""
    return mail


@router.post("")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    mail = _apply(await request.form(), mailer.load())
    mailer.save(mail)
    audit(db, user, "UPDATE", "EmailSettings", 1, detail=mail.host,
          ip=client_ip(request))
    db.commit()
    flash(request, "Email settings saved. Send yourself a test to be sure.")
    return redirect("/settings/email")


@router.post("/test")
async def test(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    data = await request.form()
    mail = _apply(data, mailer.load())
    mailer.save(mail)

    to = (data.get("to") or mail.from_email or "").strip()
    company = db.get(Company, 1)
    try:
        mailer.send(
            to,
            f"Test message from {company.name if company else 'Nexora Books'}",
            "This is a test.\n\nIf you are reading it, your email settings work "
            "and invoices will reach your customers from this address.",
            settings=mail,
        )
    except mailer.MailError as exc:
        _log(db, user, to=to, cc="", subject="Test message", kind="TEST",
             ref_id=None, ref_number="", ok=False, error=str(exc))
        db.commit()
        flash(request, str(exc), "danger")
        return redirect("/settings/email")

    _log(db, user, to=to, cc="", subject="Test message", kind="TEST",
         ref_id=None, ref_number="")
    db.commit()
    flash(request, f"Sent. Check {to} — if it is not there in a minute, look in "
                   "the spam folder.")
    return redirect("/settings/email")


# --------------------------------------------------------------------------
# Sending a document
# --------------------------------------------------------------------------


@send.get("/invoice/{doc_id}")
def invoice_form(request: Request, doc_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    doc = db.get(Invoice, doc_id)
    if doc is None:
        return redirect("/sales/invoices")
    company = db.get(Company, 1)
    label = pdfdocs.LABELS.get(doc.doc_type, "Invoice")
    amount = pdfdocs.money(doc.total if doc.doc_type == "QUOTE" else doc.balance_due)
    due = pdfdocs.when(doc.due_date)
    return render(
        request, "email/send.html",
        mail=mailer.load(),
        title=f"Email {label.lower()} {doc.number}",
        to=doc.contact.email or "",
        subject=mailer.invoice_subject(company, doc, label),
        body=mailer.invoice_body(company, doc, label, amount, due),
        attachment=f"{label} {doc.number}.pdf",
        action=f"/send/invoice/{doc.id}",
        back=f"/sales/{'quotations' if doc.doc_type == 'QUOTE' else 'invoices'}/{doc.id}",
        contact=doc.contact,
        history=list(db.scalars(
            select(EmailLog).where(EmailLog.kind == "INVOICE",
                                   EmailLog.ref_id == doc.id)
            .order_by(EmailLog.sent_at.desc()))),
    )


@send.post("/invoice/{doc_id}")
async def invoice_send(request: Request, doc_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    doc = db.get(Invoice, doc_id)
    if doc is None:
        return redirect("/sales/invoices")
    data = await request.form()
    to = (data.get("to") or "").strip()
    cc = (data.get("cc") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""
    label = pdfdocs.LABELS.get(doc.doc_type, "Invoice")

    pdf = pdfdocs.invoice_pdf(db, doc, slug=request.state.company_slug)
    name = f"{label} {doc.number}.pdf"
    try:
        mailer.send(to, subject, body, [(name, pdf, "application/pdf")], cc=cc)
    except mailer.MailError as exc:
        _log(db, user, to=to, cc=cc, subject=subject, kind="INVOICE",
             ref_id=doc.id, ref_number=doc.number, ok=False, error=str(exc))
        db.commit()
        flash(request, f"Not sent. {exc}", "danger")
        return redirect(f"/send/invoice/{doc.id}")

    _log(db, user, to=to, cc=cc, subject=subject, kind="INVOICE",
         ref_id=doc.id, ref_number=doc.number)
    if doc.contact and not doc.contact.email:
        # Worth keeping: next time this screen will fill itself in.
        doc.contact.email = to
    audit(db, user, "EMAIL", "Invoice", doc.id,
          detail=f"{label} {doc.number} to {to}", ip=client_ip(request))
    db.commit()
    flash(request, f"{label} {doc.number} sent to {to}.")
    return redirect(f"/sales/{'quotations' if doc.doc_type == 'QUOTE' else 'invoices'}"
                    f"/{doc.id}")


@send.get("/statement/{contact_id}")
def statement_form(request: Request, contact_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    contact = db.get(Contact, contact_id)
    if contact is None:
        return redirect("/contacts")
    company = db.get(Company, 1)
    end = parse_date(request.query_params.get("end"), date.today())
    start = parse_date(request.query_params.get("start"), end - timedelta(days=90))
    _c, _opening, _rows, closing = reports.statement(db, contact_id, start, end)
    return render(
        request, "email/send.html",
        mail=mailer.load(),
        title=f"Email statement to {contact.name}",
        to=contact.email or "",
        subject=mailer.statement_subject(company, contact),
        body=mailer.statement_body(company, contact, pdfdocs.money(closing)),
        attachment=f"Statement {contact.name}.pdf",
        action=f"/send/statement/{contact.id}?start={start}&end={end}",
        back=f"/contacts/{contact.id}/statement?start={start}&end={end}",
        contact=contact,
        history=list(db.scalars(
            select(EmailLog).where(EmailLog.kind == "STATEMENT",
                                   EmailLog.ref_id == contact.id)
            .order_by(EmailLog.sent_at.desc()))),
    )


@send.post("/statement/{contact_id}")
async def statement_send(request: Request, contact_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    contact = db.get(Contact, contact_id)
    if contact is None:
        return redirect("/contacts")
    data = await request.form()
    to = (data.get("to") or "").strip()
    cc = (data.get("cc") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""

    end = parse_date(request.query_params.get("end"), date.today())
    start = parse_date(request.query_params.get("start"), end - timedelta(days=90))
    c, opening, rows, closing = reports.statement(db, contact_id, start, end)
    pdf = pdfdocs.statement_pdf(db, c, rows, opening, closing, start, end,
                                slug=request.state.company_slug)
    name = f"Statement {contact.name}.pdf"
    try:
        mailer.send(to, subject, body, [(name, pdf, "application/pdf")], cc=cc)
    except mailer.MailError as exc:
        _log(db, user, to=to, cc=cc, subject=subject, kind="STATEMENT",
             ref_id=contact.id, ref_number=contact.code, ok=False, error=str(exc))
        db.commit()
        flash(request, f"Not sent. {exc}", "danger")
        return redirect(f"/send/statement/{contact.id}")

    _log(db, user, to=to, cc=cc, subject=subject, kind="STATEMENT",
         ref_id=contact.id, ref_number=contact.code)
    if not contact.email:
        contact.email = to
    audit(db, user, "EMAIL", "Contact", contact.id,
          detail=f"Statement to {to}", ip=client_ip(request))
    db.commit()
    flash(request, f"Statement sent to {to}.")
    return redirect(f"/contacts/{contact.id}/statement")


# --------------------------------------------------------------------------
# Payslips
# --------------------------------------------------------------------------
#
# A payslip is not an invoice, and the differences all point the same way: it
# is nobody's business but the employee's. So the sending here is deliberately
# narrower than the sending above.
#
#   * There is no "copy to". A payslip carries somebody's pay, their tax and
#     their loan repayments, and a copy field is one mis-click away from
#     putting all three in front of a colleague.
#   * It only ever goes to the address on that employee's own record. The
#     address is not typed on the sending screen, because a typo there is a
#     payslip delivered to a stranger.
#   * Sending a whole run skips anybody with no address on file and says who
#     was skipped, rather than quietly leaving them out.


def _payslip_pdf(request, db, slip) -> tuple[str, bytes]:
    from ..services import payroll_run as PR

    settings = PR.settings(db)
    data = pdfdocs.payslip_pdf(db, slip, slug=request.state.company_slug,
                               note=settings.payslip_note or "")
    return f"Payslip {slip.staff_no} {mailer.payslip_period(slip.run)}.pdf", data


@send.get("/payslip/{slip_id}")
def payslip_form(request: Request, slip_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    slip = db.get(Payslip, slip_id)
    if slip is None:
        return redirect("/payroll")

    company = db.get(Company, 1)
    employee = db.get(Employee, slip.employee_id) if slip.employee_id else None
    period = mailer.payslip_period(slip.run)
    return render(
        request, "email/send.html",
        mail=mailer.load(),
        title=f"Email payslip to {slip.employee_name}",
        to=(employee.email if employee else "") or "",
        locked_to=True,                       # the address comes from the record
        no_cc=True,                           # a payslip is copied to nobody
        subject=mailer.payslip_subject(company, slip),
        body=mailer.payslip_body(company, slip, period,
                                 pdfdocs.money(slip.net_pay)),
        attachment=f"Payslip {slip.staff_no} {period}.pdf",
        action=f"/send/payslip/{slip.id}",
        back=f"/payroll/payslips/{slip.id}",
        contact=employee,
        missing_address=not (employee and employee.email),
        history=list(db.scalars(
            select(EmailLog).where(EmailLog.kind == "PAYSLIP",
                                   EmailLog.ref_id == slip.id)
            .order_by(EmailLog.sent_at.desc()))),
    )


@send.post("/payslip/{slip_id}")
async def payslip_send(request: Request, slip_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    slip = db.get(Payslip, slip_id)
    if slip is None:
        return redirect("/payroll")

    data = await request.form()
    employee = db.get(Employee, slip.employee_id) if slip.employee_id else None
    to = (employee.email if employee else "") or ""
    if not to:
        flash(request, f"{slip.employee_name} has no email address on their "
                       "employee record. Add one there first — a payslip is "
                       "never sent to an address typed on this screen.",
              "danger")
        return redirect(f"/send/payslip/{slip.id}")

    subject = (data.get("subject") or "").strip()
    body = data.get("body") or ""
    name, pdf = _payslip_pdf(request, db, slip)

    try:
        mailer.send(to, subject, body, [(name, pdf, "application/pdf")])
    except mailer.MailError as exc:
        _log(db, user, to=to, cc="", subject=subject, kind="PAYSLIP",
             ref_id=slip.id, ref_number=slip.staff_no, ok=False, error=str(exc))
        db.commit()
        flash(request, f"Not sent. {exc}", "danger")
        return redirect(f"/send/payslip/{slip.id}")

    _log(db, user, to=to, cc="", subject=subject, kind="PAYSLIP",
         ref_id=slip.id, ref_number=slip.staff_no)
    audit(db, user, "EMAIL", "Payslip", slip.id,
          detail=f"Payslip {slip.staff_no} to {to}", ip=client_ip(request))
    db.commit()
    flash(request, f"Payslip sent to {slip.employee_name} at {to}.")
    return redirect(f"/payroll/payslips/{slip.id}")


@send.get("/payslips/run/{run_id}")
def payslip_run_form(request: Request, run_id: int):
    """Who would get one, and who would not, before anything is sent."""
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    if run is None:
        return redirect("/payroll")

    ready, missing = [], []
    for slip in run.payslips:
        employee = db.get(Employee, slip.employee_id) if slip.employee_id else None
        address = (employee.email if employee else "") or ""
        (ready if address else missing).append((slip, address))

    already = {row.ref_id for row in db.scalars(
        select(EmailLog).where(EmailLog.kind == "PAYSLIP", EmailLog.ok.is_(True)))}
    return render(request, "payroll/email_run.html", run=run,
                  mail=mailer.load(), ready=ready, missing=missing,
                  already=already, period=mailer.payslip_period(run))


@send.post("/payslips/run/{run_id}")
async def payslip_run_send(request: Request, run_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    run = db.get(PayrollRun, run_id)
    if run is None:
        return redirect("/payroll")

    company = db.get(Company, 1)
    period = mailer.payslip_period(run)
    data = await request.form()
    only = {int(v) for v in data.getlist("slip_id") if str(v).isdigit()}

    sent, failed, skipped = 0, [], []
    for slip in run.payslips:
        if only and slip.id not in only:
            continue
        employee = db.get(Employee, slip.employee_id) if slip.employee_id else None
        to = (employee.email if employee else "") or ""
        if not to:
            skipped.append(slip.employee_name)
            continue

        subject = mailer.payslip_subject(company, slip)
        body = mailer.payslip_body(company, slip, period,
                                   pdfdocs.money(slip.net_pay))
        name, pdf = _payslip_pdf(request, db, slip)
        try:
            mailer.send(to, subject, body, [(name, pdf, "application/pdf")])
        except mailer.MailError as exc:
            failed.append(f"{slip.employee_name} ({exc})")
            _log(db, user, to=to, cc="", subject=subject, kind="PAYSLIP",
                 ref_id=slip.id, ref_number=slip.staff_no, ok=False,
                 error=str(exc))
            continue
        _log(db, user, to=to, cc="", subject=subject, kind="PAYSLIP",
             ref_id=slip.id, ref_number=slip.staff_no)
        sent += 1

    audit(db, user, "EMAIL", "PayrollRun", run.id,
          detail=f"{sent} payslip(s) for {run.number}", ip=client_ip(request))
    db.commit()

    parts = [f"{sent} payslip{'' if sent == 1 else 's'} sent."]
    if skipped:
        parts.append("No email address on file for " + ", ".join(skipped) +
                     " — they were not sent one.")
    if failed:
        parts.append("Failed: " + "; ".join(failed))
    flash(request, " ".join(parts),
          "danger" if failed else ("warning" if skipped else "success"))
    return redirect(f"/payroll/runs/{run.id}/payslips")
