"""Paperwork attached to records.

Two questions matter here. Can a file that is not what it claims to be get
stored and served back? And can a file attached in one company's books ever be
reached from another's? Everything else is housekeeping.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="nexora-att-")
os.environ["NEXORA_DATA"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Attachment  # noqa: E402
from app.services import attachments as A  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.4\n" + b"trailer\n" * 8
XLSX = b"PK\x03\x04" + b"\x00" * 64
CSV = b"date,amount\n2026-01-31,150000\n"


# --------------------------------------------------------------------------
# What the file actually is
# --------------------------------------------------------------------------


def test_identifies_by_content_not_by_name():
    assert A.identify(PDF, "receipt.pdf") == ("pdf", "application/pdf")
    # The name says PDF; the bytes say PNG. The bytes win.
    ext, mime = A.identify(PNG, "receipt.pdf")
    assert ext == "png" and mime == "image/png"


def test_office_files_are_told_apart_by_extension():
    assert A.identify(XLSX, "bank statement.xlsx")[0] == "xlsx"
    assert A.identify(XLSX, "contract.docx")[0] == "docx"
    # A zip that is not an Office file stays a zip, not whatever it claims
    assert A.identify(XLSX, "payload.exe")[0] == "zip"


def test_csv_and_text_are_accepted_when_they_really_are_text():
    assert A.identify(CSV, "statement.csv") == ("csv", "text/csv")
    assert A.identify(b"just notes", "notes.txt") == ("txt", "text/plain")


def test_a_script_dressed_as_a_pdf_is_refused():
    """The classic upload attack: an executable payload with a safe extension."""
    with pytest.raises(A.AttachmentError):
        A.identify(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32, "invoice.pdf")


def test_an_unknown_binary_is_refused():
    with pytest.raises(A.AttachmentError):
        A.identify(b"\x00\x01\x02\x03\xfe\xff", "photo.jpg")


# --------------------------------------------------------------------------
# Storing
# --------------------------------------------------------------------------


@pytest.fixture()
def books():
    """A company with tables, opened directly rather than through the web."""
    tmp = tempfile.mkdtemp(prefix="nexora-attdb-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    from app.seed import bootstrap

    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
    yield ref.slug
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)
    os.environ["NEXORA_DATA"] = _TMP


def test_stored_name_is_ours_not_theirs(books):
    with dbmod.session_scope_for(books) as db:
        row = A.save(db, books, "BILL", 1, "../../etc/passwd.png", PNG, io.BytesIO(PNG))
        # The name on disk is generated; the original is kept only as a label
        assert row.stored_name.endswith(".png")
        assert "/" not in row.stored_name and ".." not in row.stored_name
        assert row.filename == "passwd.png"
        assert A.path_for(books, row).exists()


def test_a_file_larger_than_ten_megabytes_is_refused(books):
    big = io.BytesIO(PNG + b"\x00" * (A.MAX_BYTES + 1))
    with dbmod.session_scope_for(books) as db:
        with pytest.raises(A.AttachmentError) as e:
            A.save(db, books, "BILL", 1, "scan.png", PNG, big)
        assert "10 MB" in str(e.value)
    # and nothing is left behind on disk
    assert list(A.folder(books).glob("*")) == []


def test_an_empty_file_is_refused(books):
    with dbmod.session_scope_for(books) as db:
        with pytest.raises(A.AttachmentError):
            A.save(db, books, "BILL", 1, "empty.pdf", PDF, io.BytesIO(b""))


def test_path_for_refuses_to_escape_the_folder(books):
    row = Attachment(doc_type="BILL", doc_id=1, filename="x",
                     stored_name="../../company.db", content_type="application/pdf")
    with pytest.raises(A.AttachmentError):
        A.path_for(books, row)


def test_deleting_removes_the_file_from_disk(books):
    with dbmod.session_scope_for(books) as db:
        row = A.save(db, books, "BILL", 7, "note.pdf", PDF, io.BytesIO(PDF))
        path = A.path_for(books, row)
        assert path.exists()
        A.delete(db, books, row)
        assert not path.exists()
        assert A.list_for(db, "BILL", 7) == []


def test_deleting_a_draft_takes_its_files_with_it(books):
    with dbmod.session_scope_for(books) as db:
        A.save(db, books, "BILL", 9, "a.pdf", PDF, io.BytesIO(PDF))
        A.save(db, books, "BILL", 9, "b.png", PNG, io.BytesIO(PNG))
        assert A.delete_all_for(db, books, "BILL", 9) == 2
        assert list(A.folder(books).glob("*")) == []


def test_counts_for_drives_the_paperclip(books):
    with dbmod.session_scope_for(books) as db:
        A.save(db, books, "INVOICE", 1, "a.pdf", PDF, io.BytesIO(PDF))
        A.save(db, books, "INVOICE", 1, "b.pdf", PDF, io.BytesIO(PDF))
        A.save(db, books, "INVOICE", 4, "c.pdf", PDF, io.BytesIO(PDF))
        assert A.counts_for(db, "INVOICE", [1, 2, 4]) == {1: 2, 4: 1}
        assert A.counts_for(db, "INVOICE", []) == {}


def test_size_label_reads_like_a_person_wrote_it(books):
    with dbmod.session_scope_for(books) as db:
        row = A.save(db, books, "BILL", 1, "small.pdf", PDF, io.BytesIO(PDF))
        assert row.size_label.endswith("B")
        row.size = 250 * 1024
        assert row.size_label == "250 KB"
        row.size = 3 * 1024 * 1024
        assert row.size_label == "3.0 MB"


# --------------------------------------------------------------------------
# One company's paperwork never reaches another's
# --------------------------------------------------------------------------


def test_each_company_keeps_its_files_in_its_own_folder(books):
    second = registry.create("Adeyemi Logistics Ltd")
    from app.seed import bootstrap

    dbmod.init_db(second.slug)
    with dbmod.session_scope_for(second.slug) as db:
        bootstrap(db)

    with dbmod.session_scope_for(books) as db:
        A.save(db, books, "BILL", 1, "first.pdf", PDF, io.BytesIO(PDF))
    with dbmod.session_scope_for(second.slug) as db:
        A.save(db, second.slug, "BILL", 1, "second.pdf", PDF, io.BytesIO(PDF))

    assert A.folder(books) != A.folder(second.slug)
    assert len(list(A.folder(books).glob("*"))) == 1
    assert len(list(A.folder(second.slug).glob("*"))) == 1

    # The index is separate too — same doc_type and id, different books
    with dbmod.session_scope_for(books) as db:
        assert [f.filename for f in A.list_for(db, "BILL", 1)] == ["first.pdf"]
    with dbmod.session_scope_for(second.slug) as db:
        assert [f.filename for f in A.list_for(db, "BILL", 1)] == ["second.pdf"]


# --------------------------------------------------------------------------
# Through the interface
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        c.post("/settings/company", data={
            "name": "Adeyemi Trading Ltd", "currency_symbol": "₦", "currency_code": "NGN",
            "fiscal_year_start_month": "1", "is_vat_registered": "1", "vat_rate": "7.5",
            "default_payment_terms_days": "30",
        }, follow_redirects=True)
        yield c
    shutil.rmtree(_TMP, ignore_errors=True)


@pytest.fixture(scope="module")
def customer(client):
    client.post("/contacts/save", data={
        "name": "Dangote Cement Plc", "is_customer": "1",
        "payment_terms_days": "30", "is_active": "1",
    }, follow_redirects=True)
    r = client.get("/contacts?q=Dangote", follow_redirects=True)
    assert "Dangote Cement Plc" in r.text
    import re

    return int(re.search(r'/contacts/(\d+)"', r.text).group(1))


def test_the_panel_appears_on_a_record(client, customer):
    r = client.get(f"/contacts/{customer}", follow_redirects=True)
    assert "Attachments" in r.text
    assert "Nothing attached." in r.text or 'name="files"' in r.text


def test_upload_view_and_remove(client, customer):
    r = client.post(
        "/attachments/upload",
        data={"doc_type": "CONTACT", "doc_id": str(customer), "note": "Signed contract"},
        files={"files": ("contract.pdf", PDF, "application/pdf")},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert "contract.pdf" in r.text
    assert "Signed contract" in r.text

    import re

    att_id = int(re.search(r'/attachments/(\d+)"', r.text).group(1))

    got = client.get(f"/attachments/{att_id}", follow_redirects=True)
    assert got.status_code == 200
    assert got.content == PDF
    assert got.headers["content-type"].startswith("application/pdf")
    assert "inline" in got.headers["content-disposition"]

    gone = client.post(f"/attachments/{att_id}/delete", follow_redirects=True)
    assert "removed" in gone.text
    assert f'/attachments/{att_id}"' not in gone.text
    assert client.get(f"/attachments/{att_id}", follow_redirects=False).status_code == 303


def test_several_files_at_once(client, customer):
    r = client.post(
        "/attachments/upload",
        data={"doc_type": "CONTACT", "doc_id": str(customer), "note": "Bank details"},
        files=[
            ("files", ("cheque.png", PNG, "image/png")),
            ("files", ("mandate.pdf", PDF, "application/pdf")),
        ],
        follow_redirects=True,
    )
    assert "cheque.png" in r.text and "mandate.pdf" in r.text
    assert "2 files attached" in r.text


def test_a_disguised_upload_is_rejected_at_the_door(client, customer):
    r = client.post(
        "/attachments/upload",
        data={"doc_type": "CONTACT", "doc_id": str(customer)},
        files={"files": ("invoice.pdf", b"\x7fELF\x02\x01\x01" + b"\x00" * 40,
                         "application/pdf")},
        follow_redirects=True,
    )
    assert "not accepted" in r.text
    assert "invoice.pdf" not in r.text.split("Attachments")[-1]


def test_a_non_image_downloads_rather_than_displays(client, customer):
    client.post(
        "/attachments/upload",
        data={"doc_type": "CONTACT", "doc_id": str(customer)},
        files={"files": ("ledger.xlsx", XLSX, "application/octet-stream")},
        follow_redirects=True,
    )
    r = client.get(f"/contacts/{customer}", follow_redirects=True)
    import re

    ids = [int(i) for i in re.findall(r'/attachments/(\d+)"', r.text)]
    got = client.get(f"/attachments/{ids[-1]}", follow_redirects=True)
    assert "attachment" in got.headers["content-disposition"]


def test_an_upload_with_no_record_goes_nowhere(client):
    r = client.post("/attachments/upload", data={"doc_type": "", "doc_id": ""},
                    files={"files": ("x.pdf", PDF, "application/pdf")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "not linked to a record" in r.text


def test_a_viewer_cannot_attach_or_remove(client, customer):
    """Read-only staff see the paperwork but cannot change it."""
    client.post("/settings/users/save", data={
        "username": "audu", "full_name": "Audu Bello", "role": "VIEWER",
        "password": "Kaduna2026", "is_active": "1",
    }, follow_redirects=True)

    with TestClient(app) as viewer:
        viewer.post("/login", data={"username": "audu", "password": "Kaduna2026", "next": "/"},
                    follow_redirects=True)
        r = viewer.get(f"/contacts/{customer}", follow_redirects=True)
        assert r.status_code == 200
        assert 'action="/attachments/upload"' not in r.text

        blocked = viewer.post(
            "/attachments/upload",
            data={"doc_type": "CONTACT", "doc_id": str(customer)},
            files={"files": ("sneaky.pdf", PDF, "application/pdf")},
            follow_redirects=True,
        )
        assert "sneaky.pdf" not in blocked.text
