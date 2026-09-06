"""Uploading, viewing and removing the paperwork attached to a record."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..models import Attachment
from ..security import P_ENTRY, P_VIEW
from ..services import attachments as A
from ..services.posting import audit
from ._common import client_ip, db_of, need, parse_id, redirect

router = APIRouter(prefix="/attachments")

# Where to send the user back to after uploading or deleting
RETURN_TO = {
    "INVOICE": "/sales/invoices/{id}",
    "QUOTE": "/sales/quotes/{id}",
    "CREDIT_NOTE": "/sales/credit-notes/{id}",
    "BILL": "/purchases/bills/{id}",
    "PO": "/purchases/orders/{id}",
    "DEBIT_NOTE": "/purchases/debit-notes/{id}",
    "RECEIPT": "/receipts/{id}",
    "PAYMENT": "/payments/{id}",
    "JOURNAL": "/journals/{id}",
    "EMPLOYEE": "/payroll/employees/{id}",
    "ITEM": "/inventory/{id}",
    "CONTACT": "/contacts/{id}",
    "ASSET": "/assets/{id}",
    "REQUISITION": "/requisitions/{id}",
}


def _back(doc_type: str, doc_id: int) -> str:
    return RETURN_TO.get(doc_type, "/").format(id=doc_id)


@router.post("/upload")
async def upload(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    slug = request.state.company_slug
    form = await request.form()

    doc_type = (form.get("doc_type") or "").strip().upper()
    doc_id = parse_id(form.get("doc_id"))
    note = (form.get("note") or "").strip()
    if not doc_type or not doc_id:
        flash(request, "That upload was not linked to a record.", "danger")
        return redirect("/")

    files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
    if not files:
        flash(request, "Choose a file to attach.", "danger")
        return redirect(_back(doc_type, doc_id))

    saved, failed = 0, []
    for upload_file in files:
        try:
            row = await A.save_upload(db, slug, doc_type, doc_id, upload_file,
                                      user=user, note=note)
            audit(db, user, "ATTACH", doc_type, doc_id,
                  detail=f"{row.filename} ({row.size_label})", ip=client_ip(request))
            saved += 1
        except A.AttachmentError as e:
            failed.append(str(e))

    db.commit()
    if saved:
        flash(request, f"{saved} file{'s' if saved != 1 else ''} attached.")
    for message in failed:
        flash(request, message, "danger")
    return redirect(_back(doc_type, doc_id))


@router.get("/{attachment_id}")
def view(request: Request, attachment_id: int):
    """Open an attachment. Images and PDFs display; everything else downloads."""
    need(request, P_VIEW)
    db = db_of(request)
    row = db.get(Attachment, attachment_id)
    if row is None:
        return redirect("/")
    try:
        path = A.path_for(request.state.company_slug, row)
    except A.AttachmentError:
        return redirect(_back(row.doc_type, row.doc_id))
    if not path.exists():
        return redirect(_back(row.doc_type, row.doc_id))

    inline = row.is_image or row.content_type == "application/pdf"
    disposition = "inline" if inline else "attachment"
    return FileResponse(
        str(path),
        media_type=row.content_type,
        headers={"Content-Disposition": f'{disposition}; filename="{row.filename}"'},
    )


@router.post("/{attachment_id}/delete")
def remove(request: Request, attachment_id: int):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    row = db.get(Attachment, attachment_id)
    if row is None:
        return redirect("/")
    doc_type, doc_id, name = row.doc_type, row.doc_id, row.filename
    A.delete(db, request.state.company_slug, row)
    audit(db, user, "DETACH", doc_type, doc_id, detail=name, ip=client_ip(request))
    db.commit()
    flash(request, f"'{name}' removed.", "warning")
    return redirect(_back(doc_type, doc_id))
