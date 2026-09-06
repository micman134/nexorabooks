"""Files kept alongside a record.

Receipts, delivery notes, WHT credit notes, supplier invoices, signed
contracts — the paperwork that proves a transaction. The file goes in the
company's own attachments folder; the database only holds an index entry.

Uploaded files are never trusted: the type is decided by sniffing the first
bytes, the name on disk is one we generate, and the extension comes from the
sniffed type rather than from whatever the browser sent.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import companies as registry
from ..models import Attachment, User

MAX_BYTES = 10 * 1024 * 1024  # 10 MB

# What a small business actually needs to attach, and nothing else.
SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"%PDF-", "pdf", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    # Modern Office files and ODF are all zip containers
    (b"PK\x03\x04", "zip", "application/zip"),
    # Legacy .doc/.xls
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "doc", "application/msword"),
]

ZIP_KINDS = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

DOC_LABELS = {
    "INVOICE": "invoice",
    "BILL": "bill",
    "PAYMENT": "payment",
    "RECEIPT": "receipt",
    "JOURNAL": "journal entry",
    "EMPLOYEE": "employee",
    "ITEM": "item",
    "CONTACT": "contact",
    "ASSET": "asset",
    "REQUISITION": "requisition",
}


class AttachmentError(Exception):
    """Safe to show the user."""


def identify(head: bytes, filename: str) -> tuple[str, str]:
    """Work out the real type from the file's first bytes.

    Returns ``(extension, content type)``. Plain text is the only thing we
    accept on trust, and only when it really is text.
    """
    for sig, ext, mime in SIGNATURES:
        if head.startswith(sig):
            if ext == "zip":
                # Office formats are zips; use the claimed extension to tell
                # them apart, but only from the list we allow.
                claimed = Path(filename).suffix.lower().lstrip(".")
                if claimed in ZIP_KINDS:
                    return claimed, ZIP_KINDS[claimed]
                return "zip", "application/zip"
            return ext, mime

    # CSV and plain text have no signature to check, so they are the one case
    # decided by the extension — and only after the bytes prove they really are
    # text. A compiled program is mostly readable ASCII too, which is why the
    # control characters are checked as well as the decoding.
    claimed = Path(filename).suffix.lower().lstrip(".")
    if claimed in ("csv", "txt") and _looks_like_text(head):
        return ("csv", "text/csv") if claimed == "csv" else ("txt", "text/plain")

    raise AttachmentError(
        "That file type is not accepted. Attach a PDF, a photo (PNG or JPG), "
        "a Word or Excel file, or a CSV."
    )


def _looks_like_text(head: bytes) -> bool:
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ch < " " and ch not in "\t\n\r" for ch in text)


def folder(slug: str) -> Path:
    return registry.company_dir(slug) / "attachments"


def path_for(slug: str, attachment: Attachment) -> Path:
    """Resolve an attachment to a real file, refusing anything outside the folder."""
    base = folder(slug).resolve()
    target = (base / attachment.stored_name).resolve()
    if not str(target).startswith(str(base)):
        raise AttachmentError("That attachment could not be found.")
    return target


def list_for(db: Session, doc_type: str, doc_id: int) -> list[Attachment]:
    return list(
        db.scalars(
            select(Attachment)
            .where(Attachment.doc_type == doc_type, Attachment.doc_id == doc_id)
            .order_by(Attachment.uploaded_at)
        )
    )


def counts_for(db: Session, doc_type: str, doc_ids: list[int]) -> dict[int, int]:
    """How many files each record has — for showing a paperclip in a list."""
    from sqlalchemy import func

    if not doc_ids:
        return {}
    rows = db.execute(
        select(Attachment.doc_id, func.count(Attachment.id))
        .where(Attachment.doc_type == doc_type, Attachment.doc_id.in_(doc_ids))
        .group_by(Attachment.doc_id)
    )
    return {r[0]: int(r[1]) for r in rows}


def save(
    db: Session,
    slug: str,
    doc_type: str,
    doc_id: int,
    filename: str,
    head: bytes,
    stream,
    user: User | None = None,
    note: str = "",
) -> Attachment:
    """Store one uploaded file against a record.

    ``stream`` is anything with ``.read(n)`` returning bytes — the caller has
    already read ``head`` and rewound it.
    """
    ext, mime = identify(head, filename)
    stored = f"{uuid.uuid4().hex}.{ext}"
    target = folder(slug) / stored
    folder(slug).mkdir(parents=True, exist_ok=True)

    size = 0
    with open(target, "wb") as out:
        while chunk := stream.read(64 * 1024):
            size += len(chunk)
            if size > MAX_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise AttachmentError(
                    f"'{filename}' is larger than 10 MB. Photograph receipts at a "
                    "lower resolution, or attach a PDF instead."
                )
            out.write(chunk)

    if size == 0:
        target.unlink(missing_ok=True)
        raise AttachmentError(f"'{filename}' is empty.")

    row = Attachment(
        doc_type=doc_type,
        doc_id=doc_id,
        filename=Path(filename).name[:255],
        stored_name=stored,
        content_type=mime,
        size=size,
        note=note[:255],
        uploaded_by_id=user.id if user else None,
        uploaded_by_name=user.username if user else "",
    )
    db.add(row)
    db.flush()
    return row


async def save_upload(
    db: Session,
    slug: str,
    doc_type: str,
    doc_id: int,
    upload,
    user: User | None = None,
    note: str = "",
) -> Attachment:
    """Save a Starlette UploadFile."""
    head = await upload.read(16)
    await upload.seek(0)

    class _Sync:
        """Adapt the async UploadFile to the sync reader ``save`` expects."""

        def __init__(self, f):
            self.f = f.file

        def read(self, n):
            return self.f.read(n)

    return save(db, slug, doc_type, doc_id, upload.filename, head, _Sync(upload),
                user=user, note=note)


def delete(db: Session, slug: str, attachment: Attachment) -> None:
    try:
        path_for(slug, attachment).unlink(missing_ok=True)
    except AttachmentError:
        pass
    db.delete(attachment)
    db.flush()


def delete_all_for(db: Session, slug: str, doc_type: str, doc_id: int) -> int:
    """Used when a draft is deleted outright."""
    rows = list_for(db, doc_type, doc_id)
    for row in rows:
        delete(db, slug, row)
    return len(rows)
