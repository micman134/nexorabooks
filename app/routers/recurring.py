"""Invoices and bills that repeat."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from sqlalchemy import select

from ..models import (
    RECUR_ACTIVE,
    RECURRENCE_LABELS,
    Account,
    Bill,
    Contact,
    Invoice,
    Item,
    RecurringLine,
    RecurringTemplate,
)
from ..security import P_ENTRY, P_VIEW
from ..services import recurring as R
from ..services.posting import PostingError, audit
from ..services.tax import vat_codes, wht_codes
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

router = APIRouter(prefix="/recurring")

DOC_CHOICES = [
    ("INVOICE", "Invoice a customer"),
    ("BILL", "A bill from a supplier"),
]


def _form_context(db, template):
    return dict(
        rec=template,
        contacts=list(
            db.scalars(select(Contact).where(Contact.is_active.is_(True)).order_by(Contact.name))
        ),
        items=list(db.scalars(select(Item).where(Item.is_active.is_(True)).order_by(Item.name))),
        accounts=list(
            db.scalars(
                select(Account)
                .where(Account.is_active.is_(True),
                       Account.type.in_(("INCOME", "EXPENSE", "ASSET")))
                .order_by(Account.code)
            )
        ),
        vat_codes=vat_codes(db),
        wht_codes=wht_codes(db),
        frequencies=list(RECURRENCE_LABELS.items()),
        doc_choices=DOC_CHOICES,
    )


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    templates = list(
        db.scalars(select(RecurringTemplate).order_by(RecurringTemplate.next_date))
    )
    owed = R.due(db)
    return render(
        request, "recurring/index.html",
        templates=templates, owed=owed,
        owed_count=sum(d.count for d in owed),
        owed_value=sum(d.value for d in owed),
        active=len([t for t in templates if t.status == RECUR_ACTIVE]),
    )


@router.get("/new")
def new(request: Request):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    today = date.today()
    template = RecurringTemplate(
        name="", doc_type=request.query_params.get("kind", "INVOICE").upper(),
        frequency="MONTHLY", anchor_day=today.day,
        start_date=today, next_date=today, payment_terms_days=30,
    )
    template.lines = [RecurringLine(line_no=i, qty=1000) for i in range(1, 4)]
    return render(request, "recurring/form.html", is_new=True, **_form_context(db, template))


@router.post("/run-all")
def run_all(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    result = R.run_all(db, date.today(), user=user)
    db.commit()
    made = len(result["made"])
    if made:
        flash(request, f"{made} document{'s' if made != 1 else ''} generated. "
                       "They are drafts unless the template posts automatically.")
    else:
        flash(request, "Nothing was due.", "warning")
    for template, message in result["failed"]:
        flash(request, message, "danger")
    return redirect("/recurring")


@router.get("/{template_id}")
def detail(request: Request, template_id: int):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")

    # Resolve each generated document so the list can link to it
    history = []
    for link in template.generated:
        model = Invoice if link.doc_type in ("INVOICE", "QUOTE", "CREDIT_NOTE") else Bill
        history.append((link, db.get(model, link.doc_id)))

    return render(
        request, "recurring/detail.html",
        rec=template, history=history,
        upcoming=R.occurrences_between(template, date.min, R.add_months(date.today(), 12)),
        owed=R.occurrences_between(template, date.min, date.today()),
    )


@router.get("/{template_id}/edit")
def edit(request: Request, template_id: int):
    from ..main import render

    need(request, P_ENTRY)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")
    while len(template.lines) < 3:
        template.lines.append(RecurringLine(line_no=len(template.lines) + 1, qty=1000))
    return render(request, "recurring/form.html", is_new=False, **_form_context(db, template))


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()
    tid = parse_id(form.get("id"))
    template = db.get(RecurringTemplate, tid) if tid else None
    is_new = template is None

    name = (form.get("name") or "").strip()
    contact_id = parse_id(form.get("contact_id"))
    if not name or not contact_id:
        flash(request, "A recurring document needs a name and a customer or supplier.", "danger")
        return redirect("/recurring/new")

    start = parse_date(form.get("start_date"))
    if is_new:
        template = RecurringTemplate(name=name, contact_id=contact_id,
                                     start_date=start, next_date=start,
                                     created_by_id=user.id)
        db.add(template)

    template.name = name
    template.doc_type = form.get("doc_type") or "INVOICE"
    template.contact_id = contact_id
    template.frequency = form.get("frequency") or "MONTHLY"
    template.start_date = start
    if is_new or parse_bool(form.get("reset_next")):
        template.next_date = start
    # The anchor is the day of the month the customer expects the bill on.
    template.anchor_day = parse_int(form.get("anchor_day"), 0) or start.day
    template.end_date = parse_date(form.get("end_date"), None) if form.get("end_date") else None
    template.max_occurrences = parse_int(form.get("max_occurrences"), 0) or 0
    template.auto_post = parse_bool(form.get("auto_post"))
    template.payment_terms_days = parse_int(form.get("payment_terms_days"), 30)
    template.reference = (form.get("reference") or "").strip()
    template.memo = form.get("memo") or ""
    template.terms = form.get("terms") or ""
    template.wht_code_id = parse_id(form.get("wht_code_id"))
    if template.status != "FINISHED":
        template.status = RECUR_ACTIVE if parse_bool(form.get("is_active")) else "PAUSED"
    db.flush()

    for old in list(template.lines):
        db.delete(old)
    db.flush()
    template.lines = []

    descs = form.getlist("line_description")
    get = lambda key, i: (form.getlist(key)[i] if i < len(form.getlist(key)) else None)  # noqa: E731
    n = 0
    for i in range(len(descs)):
        item_id = parse_id(get("line_item_id", i))
        desc = (descs[i] or "").strip()
        qty = parse_qty(get("line_qty", i), 0)
        price = parse_money(get("line_price", i))
        if not desc and not item_id and not price:
            continue
        n += 1
        db.add(RecurringLine(
            template_id=template.id, line_no=n, item_id=item_id, description=desc,
            qty=qty or 1000, unit_price=price,
            discount_pct=(get("line_disc", i) or "0").strip() or "0",
            account_id=parse_id(get("line_account", i)),
            tax_code_id=parse_id(get("line_tax", i)),
        ))

    if n == 0:
        db.rollback()
        flash(request, "Add at least one line — otherwise there is nothing to bill.", "danger")
        return redirect("/recurring/new" if is_new else f"/recurring/{tid}/edit")

    db.flush()
    audit(db, user, "CREATE" if is_new else "UPDATE", "RecurringTemplate", template.id,
          detail=template.name, ip=client_ip(request))
    db.commit()
    flash(request, f"'{template.name}' saved. Next due {template.next_date:%d %b %Y}.")
    return redirect(f"/recurring/{template.id}")


@router.post("/{template_id}/generate")
def generate(request: Request, template_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")
    try:
        made = R.catch_up(db, template, date.today(), user=user)
        db.commit()
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
        return redirect(f"/recurring/{template_id}")

    if not made:
        flash(request, f"Nothing is due for '{template.name}' yet — "
                       f"the next one falls on {template.next_date:%d %b %Y}.", "warning")
        return redirect(f"/recurring/{template_id}")
    if len(made) == 1:
        doc = made[0]
        where = "/sales/invoices" if template.is_sales else "/purchases/bills"
        flash(request, f"{doc.number} created.")
        return redirect(f"{where}/{doc.id}")
    flash(request, f"{len(made)} documents created — one for each date that was owed.")
    return redirect(f"/recurring/{template_id}")


@router.post("/{template_id}/skip")
def skip(request: Request, template_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")
    skipped = template.next_date
    R.skip_next(db, template, user=user)
    db.commit()
    flash(request, f"{skipped:%d %b %Y} skipped. Next due "
                   f"{template.next_date:%d %b %Y}.", "warning")
    return redirect(f"/recurring/{template_id}")


@router.post("/{template_id}/pause")
def pause(request: Request, template_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")
    try:
        R.pause(db, template, user=user)
        db.commit()
        flash(request, f"'{template.name}' "
                       f"{'paused' if template.status == 'PAUSED' else 'started again'}.",
              "warning")
    except PostingError as e:
        db.rollback()
        flash(request, str(e), "danger")
    return redirect(f"/recurring/{template_id}")


@router.post("/{template_id}/delete")
def delete(request: Request, template_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    template = db.get(RecurringTemplate, template_id)
    if template is None:
        return redirect("/recurring")
    name = template.name
    audit(db, user, "DELETE", "RecurringTemplate", template.id, detail=name,
          ip=client_ip(request))
    db.delete(template)
    db.commit()
    flash(request, f"'{name}' deleted. The documents it already made are untouched.",
          "warning")
    return redirect("/recurring")
