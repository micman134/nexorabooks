"""Multiple companies, and the logo.

The point of these tests is one question: can anything from one company's books
ever reach another's? Everything else here is secondary.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import date

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-co-")

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.models import Account, Company, Contact, TaxCode, User  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services.posting import EntryDraft, next_number, post_entry, sys_account  # noqa: E402


@pytest.fixture()
def data_dir():
    tmp = tempfile.mkdtemp(prefix="nexora-co-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def seeded(slug: str):
    """Open a company and make sure its tables and chart of accounts exist."""
    dbmod.init_db(slug)
    with dbmod.session_scope_for(slug) as db:
        bootstrap(db)


# --------------------------------------------------------------------------
# Creating companies
# --------------------------------------------------------------------------


def test_first_run_creates_one_company(data_dir):
    ref = registry.ensure_at_least_one()
    assert ref.exists
    assert len(registry.all_companies()) == 1


def test_creating_a_second_company(data_dir):
    registry.ensure_at_least_one()
    ref = registry.create("Adeyemi Logistics Ltd")

    assert ref.slug == "adeyemi-logistics-ltd"
    assert ref.exists
    assert len(registry.all_companies()) == 2
    with dbmod.session_scope_for(ref.slug) as db:
        assert db.get(Company, 1).name == "Adeyemi Logistics Ltd"


def test_two_companies_with_the_same_name_get_different_folders(data_dir):
    registry.ensure_at_least_one()
    a = registry.create("Sunrise Ltd")
    b = registry.create("Sunrise Ltd")
    assert a.slug != b.slug
    assert a.db_file != b.db_file


def test_a_company_needs_a_name(data_dir):
    registry.ensure_at_least_one()
    with pytest.raises(registry.CompanyError):
        registry.create("   ")


# --------------------------------------------------------------------------
# The thing that must never happen
# --------------------------------------------------------------------------


def test_books_are_completely_separate(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    seeded(first.slug)
    seeded(second.slug)

    # Put a customer and a posted entry in the first company only
    with dbmod.session_scope_for(first.slug) as db:
        db.add(Contact(code=next_number(db, "CONTACT"), name="Only In Company One",
                       is_customer=True))
        draft = EntryDraft(date=date(2026, 6, 1), memo="Company one sale")
        draft.debit(sys_account(db, "CASH"), 500_000_00)
        draft.credit(sys_account(db, "SALES"), 500_000_00)
        post_entry(db, draft)

    from app.services.posting import account_net

    with dbmod.session_scope_for(first.slug) as db:
        assert db.query(Contact).count() == 1
        assert account_net(db, sys_account(db, "CASH").id, None, date(2030, 1, 1)) == 500_000_00

    # None of it exists in the second company
    with dbmod.session_scope_for(second.slug) as db:
        assert db.query(Contact).count() == 0
        assert account_net(db, sys_account(db, "CASH").id, None, date(2030, 1, 1)) == 0


def test_each_company_numbers_its_own_documents_from_one(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    seeded(first.slug)
    seeded(second.slug)

    with dbmod.session_scope_for(first.slug) as db:
        numbers = [next_number(db, "INVOICE") for _ in range(3)]
    assert numbers == ["INV-00001", "INV-00002", "INV-00003"]

    with dbmod.session_scope_for(second.slug) as db:
        assert next_number(db, "INVOICE") == "INV-00001"


def test_each_company_has_its_own_users(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    seeded(first.slug)
    seeded(second.slug)

    from app.security import hash_password

    with dbmod.session_scope_for(first.slug) as db:
        db.add(User(username="chidinma", password_hash=hash_password("x"), role="admin"))

    with dbmod.session_scope_for(second.slug) as db:
        assert db.query(User).filter_by(username="chidinma").first() is None


def test_the_files_really_are_separate_on_disk(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    assert first.db_file != second.db_file
    assert first.db_file.parent != second.db_file.parent
    seeded(first.slug)
    seeded(second.slug)
    assert first.db_file.exists() and second.db_file.exists()


# --------------------------------------------------------------------------
# Copying the setup
# --------------------------------------------------------------------------


def test_copy_setup_brings_accounts_but_no_data(data_dir):
    first = registry.ensure_at_least_one()
    seeded(first.slug)

    with dbmod.session_scope_for(first.slug) as db:
        db.add(Account(code="6555", name="Site security — Ikeja yard",
                       type="EXPENSE", subtype="OPERATING_EXPENSE"))
        db.add(Contact(code="C9999", name="Should Not Be Copied", is_customer=True))

    second = registry.create("Second Company Ltd", copy_setup_from=first.slug)

    with dbmod.session_scope_for(second.slug) as db:
        assert db.query(Account).filter_by(code="6555").one().name == "Site security — Ikeja yard"
        assert db.query(Contact).count() == 0
        assert db.query(TaxCode).filter_by(code="VAT-STD").first() is not None


def test_copy_setup_carries_renamed_accounts(data_dir):
    """A renamed standard account must come across renamed, not reset."""
    first = registry.ensure_at_least_one()
    seeded(first.slug)
    with dbmod.session_scope_for(first.slug) as db:
        db.query(Account).filter_by(code="6100").one().name = "Yard and office rent"

    second = registry.create("Second Company Ltd", copy_setup_from=first.slug)
    with dbmod.session_scope_for(second.slug) as db:
        assert db.query(Account).filter_by(code="6100").one().name == "Yard and office rent"
        # and the system wiring still works in the new company
        assert sys_account(db, "SALES") is not None
        assert sys_account(db, "PAYE_PAYABLE") is not None


def test_copy_setup_carries_changed_tax_rates(data_dir):
    first = registry.ensure_at_least_one()
    seeded(first.slug)
    with dbmod.session_scope_for(first.slug) as db:
        db.query(TaxCode).filter_by(code="VAT-STD").one().rate = "9"

    second = registry.create("Second Company Ltd", copy_setup_from=first.slug)
    with dbmod.session_scope_for(second.slug) as db:
        assert db.query(TaxCode).filter_by(code="VAT-STD").one().rate == "9"


# --------------------------------------------------------------------------
# Renaming and archiving
# --------------------------------------------------------------------------


def test_rename(data_dir):
    ref = registry.ensure_at_least_one()
    registry.rename(ref.slug, "Renamed Ltd")
    assert registry.get(ref.slug).name == "Renamed Ltd"


def test_archiving_hides_a_company_but_keeps_the_books(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    seeded(second.slug)

    registry.set_archived(second.slug, True)
    assert [c.slug for c in registry.all_companies()] == [first.slug]
    assert second.db_file.exists()

    registry.set_archived(second.slug, False)
    assert len(registry.all_companies()) == 2


def test_you_cannot_archive_your_only_company(data_dir):
    ref = registry.ensure_at_least_one()
    with pytest.raises(registry.CompanyError, match="only active company"):
        registry.set_archived(ref.slug, True)


# --------------------------------------------------------------------------
# Upgrading a version 1 installation
# --------------------------------------------------------------------------


def test_a_version_one_database_is_adopted_on_upgrade(data_dir):
    """The single company.db of version 1 becomes the first company."""
    from pathlib import Path

    from app import config

    # Build a v1-style database at the old location
    legacy = Path(data_dir) / "company.db"
    dbmod.reset_all()
    con = sqlite3.connect(str(legacy))
    con.close()

    # Seed it through a temporary engine pointed at that file
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base

    eng = create_engine(f"sqlite:///{legacy}")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    bootstrap(s)
    s.get(Company, 1).name = "Adeyemi Building Materials Ltd"
    s.add(Contact(code="C1001", name="Legacy Customer", is_customer=True))
    s.commit()
    s.close()
    eng.dispose()

    slug = registry.migrate_legacy()
    assert slug == "main"
    assert not legacy.exists()
    assert registry.company_db("main").exists()
    assert registry.get("main").name == "Adeyemi Building Materials Ltd"

    # And the books came through untouched
    with dbmod.session_scope_for("main") as db:
        assert db.query(Contact).filter_by(name="Legacy Customer").one() is not None


def test_migrating_twice_is_harmless(data_dir):
    registry.ensure_at_least_one()
    assert registry.migrate_legacy() is None


# --------------------------------------------------------------------------
# Backups are per company
# --------------------------------------------------------------------------


def test_backups_belong_to_one_company(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")
    seeded(first.slug)
    seeded(second.slug)

    registry.backup(first.slug)
    assert len(registry.list_backups(first.slug)) == 1
    assert len(registry.list_backups(second.slug)) == 0


def test_a_backup_is_a_real_readable_database(data_dir):
    ref = registry.ensure_at_least_one()
    seeded(ref.slug)
    path = registry.backup(ref.slug)
    assert registry.looks_like_our_database(path)


def test_a_random_file_is_rejected_as_a_backup(data_dir):
    from pathlib import Path

    junk = Path(data_dir) / "not-a-backup.db"
    junk.write_bytes(b"this is definitely not a database")
    assert registry.looks_like_our_database(junk) is False


# --------------------------------------------------------------------------
# The logo
# --------------------------------------------------------------------------

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c636000000200010005fe02fe0000000049454e44ae426082"
)


def test_image_sniffing_accepts_real_images():
    from app.routers.companies import sniff_image

    assert sniff_image(PNG_1PX) == ("png", "image/png")
    assert sniff_image(b"\xff\xd8\xff\xe0abcd") == ("jpg", "image/jpeg")
    assert sniff_image(b"GIF89a....") == ("gif", "image/gif")
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("webp", "image/webp")


def test_image_sniffing_rejects_anything_else():
    from app.routers.companies import sniff_image

    assert sniff_image(b"<?php system($_GET[0]); ?>") is None
    assert sniff_image(b"%PDF-1.7") is None
    assert sniff_image(b"") is None


def test_each_company_has_its_own_logo(data_dir):
    first = registry.ensure_at_least_one()
    second = registry.create("Second Company Ltd")

    (registry.company_dir(first.slug) / "logo.png").write_bytes(PNG_1PX)

    assert registry.logo_path(first.slug) is not None
    assert registry.logo_path(second.slug) is None
