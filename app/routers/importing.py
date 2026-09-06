"""Bringing a business in from a spreadsheet.

The upload is written to a scratch file and read back on the confirm step, so
what gets applied is exactly the bytes that were shown — not a re-parse of a
form, and not something held in a session cookie.
"""
from __future__ import annotations

import secrets
import time
from datetime import date

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import PlainTextResponse

from .. import config
from ..security import P_ADMIN, P_JOURNAL
from ..services import importer
from ..services.importer import ImportError_
from ..services.posting import PostingError, audit
from ._common import client_ip, db_of, need, parse_date, redirect

router = APIRouter(prefix="/import")

#: Uploads are held here between the preview and the confirm.
MAX_UPLOAD = 8 * 1024 * 1024
KEEP_SECONDS = 24 * 60 * 60


def _scratch():
    folder = config.data_dir() / "imports"
    folder.mkdir(exist_ok=True)
    return folder


def _sweep() -> None:
    """Yesterday's abandoned uploads are nobody's business but the bin's."""
    cutoff = time.time() - KEEP_SECONDS
    for path in _scratch().glob("*.csv"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_JOURNAL)
    return render(request, "import/index.html", sheets=importer.sheets())


@router.get("/{key}/template")
def template(request: Request, key: str):
    need(request, P_JOURNAL)
    sheet = importer.sheet(key)
    if sheet is None:
        return PlainTextResponse("No such template.", status_code=404)
    return PlainTextResponse(
        sheet.template(),
        headers={"Content-Disposition": f'attachment; filename="{key}-template.csv"'},
        media_type="text/csv",
    )


@router.get("/{key}")
def upload_form(request: Request, key: str):
    from ..main import render

    need(request, P_JOURNAL)
    sheet = importer.sheet(key)
    if sheet is None:
        return redirect("/import")
    return render(request, "import/upload.html", sheet=sheet, today=date.today())


@router.post("/{key}")
async def upload(request: Request, key: str, file: UploadFile | None = None):
    from ..main import flash, render

    need(request, P_JOURNAL)
    db = db_of(request)
    sheet = importer.sheet(key)
    if sheet is None:
        return redirect("/import")

    form = await request.form()
    upload_file = form.get("file")
    raw = await upload_file.read() if hasattr(upload_file, "read") else b""
    if not raw:
        flash(request, "Choose a file first.", "warning")
        return redirect(f"/import/{key}")
    if len(raw) > MAX_UPLOAD:
        flash(request, "That file is larger than 8 MB. Split it into a few "
                       "smaller files and bring them in one after another.", "danger")
        return redirect(f"/import/{key}")

    try:
        preview = importer.read(db, key, raw)
    except ImportError_ as exc:
        flash(request, str(exc), "danger")
        return redirect(f"/import/{key}")

    _sweep()
    token = secrets.token_hex(16)
    (_scratch() / f"{token}.csv").write_bytes(raw)

    return render(
        request, "import/preview.html",
        sheet=sheet, preview=preview, token=token,
        filename=getattr(upload_file, "filename", "your file"),
        on=parse_date(form.get("date")) if form.get("date") else date.today(),
    )


@router.post("/{key}/apply")
async def apply(request: Request, key: str):
    from ..main import flash

    user = need(request, P_JOURNAL)
    db = db_of(request)
    sheet = importer.sheet(key)
    if sheet is None:
        return redirect("/import")

    form = await request.form()
    token = (form.get("token") or "").strip()
    path = _scratch() / f"{token}.csv"
    # The token comes back through a form, so it decides a filename — anything
    # that is not plain hex is refused rather than joined onto a path.
    if not token or len(token) != 32 or not all(c in "0123456789abcdef" for c in token) \
            or not path.exists():
        flash(request, "That upload has expired. Choose the file again.", "warning")
        return redirect(f"/import/{key}")

    try:
        preview = importer.read(db, key, path.read_bytes())
        result = importer.apply(db, preview, user=user,
                                on=parse_date(form.get("date")))
    except (ImportError_, PostingError) as exc:
        db.rollback()
        flash(request, f"Nothing was brought in. {exc}", "danger")
        return redirect(f"/import/{key}")

    audit(db, user, "IMPORT", sheet.title, 0,
          detail=result.summary(sheet), ip=client_ip(request))
    db.commit()
    try:
        path.unlink()
    except OSError:
        pass

    flash(request, result.summary(sheet),
          "success" if result.touched else "warning")
    for message in result.messages:
        flash(request, message, "warning")
    return redirect("/import")
