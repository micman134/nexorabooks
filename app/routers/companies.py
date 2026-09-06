"""Switching between companies, and the company logo."""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, Response

from .. import companies as registry
from .. import db as dbmod
from .. import tenancy
from ..security import P_ADMIN, P_VIEW
from ..services.posting import audit
from ._common import client_ip, db_of, need, parse_bool, redirect

router = APIRouter()

# Only real image formats, matched on their magic bytes rather than trusting
# whatever extension the file happens to carry.
IMAGE_SIGNATURES = {
    b"\x89PNG\r\n\x1a\n": ("png", "image/png"),
    b"\xff\xd8\xff": ("jpg", "image/jpeg"),
    b"GIF87a": ("gif", "image/gif"),
    b"GIF89a": ("gif", "image/gif"),
}
MAX_LOGO_BYTES = 2 * 1024 * 1024


def sniff_image(head: bytes) -> tuple[str, str] | None:
    for sig, kind in IMAGE_SIGNATURES.items():
        if head.startswith(sig):
            return kind
    # WebP is RIFF....WEBP
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ("webp", "image/webp")
    return None


# --------------------------------------------------------------------------
# The company list
# --------------------------------------------------------------------------


@router.get("/companies")
def index(request: Request):
    from ..main import render

    tenancy.guard_installed_only("The company list")
    need(request, P_VIEW)
    show_archived = parse_bool(request.query_params.get("archived"))
    refs = registry.all_companies(include_archived=show_archived)

    # A quick headline figure for each, so the list is useful at a glance
    summaries = {}
    for ref in refs:
        try:
            from datetime import date

            from .. import db as _db
            from ..models import Company as C
            from ..services import reports as R

            with _db.session_scope_for(ref.slug) as s:
                c = s.get(C, 1)
                bs = R.balance_sheet(s, date.today())
                summaries[ref.slug] = {
                    "name": c.name if c else ref.name,
                    "assets": bs.total_assets,
                    "balances": bs.difference == 0,
                    "vat": c.is_vat_registered if c else False,
                }
        except Exception:
            summaries[ref.slug] = None

    return render(request, "companies/index.html", refs=refs, summaries=summaries,
                  show_archived=show_archived)


@router.get("/companies/switch/{slug}")
def switch(request: Request, slug: str):
    from ..main import flash

    tenancy.guard_installed_only("Switching companies")
    need(request, P_VIEW)
    ref = registry.get(slug)
    if ref is None or not ref.exists:
        flash(request, "That company could not be found.", "danger")
        return redirect("/companies")
    if ref.is_archived:
        flash(request, f"{ref.name} is archived. Restore it before opening it.", "warning")
        return redirect("/companies")

    request.session["company"] = slug
    # A user account belongs to one company's books, so the sign-in does not
    # carry across. Whoever switches signs in to the company they opened.
    request.session.pop("uid", None)
    registry.touch(slug)
    flash(request, f"Opened {ref.name}. Please sign in to these books.")
    return redirect("/login")


@router.post("/companies/create")
async def create(request: Request):
    from ..main import flash

    tenancy.guard_installed_only("Creating a company")
    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    name = (form.get("name") or "").strip()
    copy_from = form.get("copy_setup_from") or None

    try:
        ref = registry.create(name, copy_setup_from=copy_from)
    except registry.CompanyError as e:
        flash(request, str(e), "danger")
        return redirect("/companies")

    audit(db, user, "CREATE", "CompanyFile", detail=f"{ref.name} ({ref.slug})",
          ip=client_ip(request))
    db.commit()
    flash(request, f"{ref.name} created. Its books are completely separate from your "
                   "other companies. Sign in with 'admin' and the password 'admin123', "
                   "then change it.", "info")
    return redirect(f"/companies/switch/{ref.slug}")


@router.post("/companies/{slug}/rename")
async def rename(request: Request, slug: str):
    from ..main import flash

    tenancy.guard_installed_only("Renaming a company from here")
    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    new_name = (form.get("name") or "").strip()
    try:
        registry.rename(slug, new_name)
    except registry.CompanyError as e:
        flash(request, str(e), "danger")
        return redirect("/companies")

    # Keep the name inside the books in step with the name in the list
    from ..models import Company

    with dbmod.session_scope_for(slug) as s:
        c = s.get(Company, 1)
        if c:
            c.name = new_name

    audit(db, user, "RENAME", "CompanyFile", detail=f"{slug} to {new_name}",
          ip=client_ip(request))
    db.commit()
    flash(request, f"Renamed to {new_name}.")
    return redirect("/companies")


@router.post("/companies/{slug}/archive")
async def archive(request: Request, slug: str):
    from ..main import flash

    tenancy.guard_installed_only("Archiving a company")
    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    archived = parse_bool(form.get("archived"))

    try:
        registry.set_archived(slug, archived)
    except registry.CompanyError as e:
        flash(request, str(e), "danger")
        return redirect("/companies")

    dbmod.forget(slug)
    audit(db, user, "ARCHIVE" if archived else "RESTORE", "CompanyFile",
          detail=slug, ip=client_ip(request))
    db.commit()
    flash(request, "Company archived. Its books are untouched and it can be "
                   "restored at any time." if archived else "Company restored.",
          "warning" if archived else "success")

    if archived and request.session.get("company") == slug:
        request.session["company"] = registry.default_slug()
        request.session.pop("uid", None)
        return redirect("/login")
    return redirect("/companies?archived=1")


# --------------------------------------------------------------------------
# The logo
# --------------------------------------------------------------------------


@router.get("/company-logo")
def logo(request: Request):
    """Serve the current company's logo. Used by every printed document."""
    slug = getattr(request.state, "company_slug", None) or registry.default_slug()
    path = registry.logo_path(slug)
    if path is None:
        return Response(status_code=404)
    return FileResponse(str(path), headers={"Cache-Control": "no-cache"})


@router.post("/settings/logo")
async def upload_logo(request: Request):
    from ..main import flash

    user = need(request, P_ADMIN)
    db = db_of(request)
    form = await request.form()
    upload: UploadFile | None = form.get("logo")  # type: ignore[assignment]
    slug = request.state.company_slug

    if upload is None or not upload.filename:
        flash(request, "Choose an image file to upload.", "danger")
        return redirect("/settings/company")

    head = await upload.read(32)
    kind = sniff_image(head)
    if kind is None:
        flash(request, "That file is not a PNG, JPG, GIF or WebP image.", "danger")
        return redirect("/settings/company")
    ext, _mime = kind

    await upload.seek(0)
    folder = registry.company_dir(slug)
    # Only one logo per company — clear any earlier format first
    for old in folder.glob("logo.*"):
        old.unlink()

    target = folder / f"logo.{ext}"
    size = 0
    with open(target, "wb") as out:
        while chunk := await upload.read(64 * 1024):
            size += len(chunk)
            if size > MAX_LOGO_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                flash(request, "That image is larger than 2 MB. Please use a smaller one — "
                               "about 600 pixels wide is plenty for an invoice.", "danger")
                return redirect("/settings/company")
            out.write(chunk)

    from ..models import Company

    company = db.get(Company, 1)
    if company:
        company.logo_file = target.name
    audit(db, user, "UPDATE", "Company", 1, detail="Logo uploaded", ip=client_ip(request))
    db.commit()
    flash(request, "Logo saved. It now appears on your invoices, quotations, receipts, "
                   "statements and payslips.")
    return redirect("/settings/company")


@router.post("/settings/logo/remove")
def remove_logo(request: Request):
    from ..main import flash
    from ..models import Company

    user = need(request, P_ADMIN)
    db = db_of(request)
    slug = request.state.company_slug
    for old in registry.company_dir(slug).glob("logo.*"):
        old.unlink()
    company = db.get(Company, 1)
    if company:
        company.logo_file = ""
    audit(db, user, "UPDATE", "Company", 1, detail="Logo removed", ip=client_ip(request))
    db.commit()
    flash(request, "Logo removed.", "warning")
    return redirect("/settings/company")
