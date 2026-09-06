"""Installing a new version must never look like losing your books.

Two things happen to a customer who has been keeping accounts for six months
and then updates: their data folder may have moved, and their company file is
missing every column added since they first ran the application. Both of those
are silent until the moment they are catastrophic, so both are tested here.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-upg-")

from sqlalchemy import select, text  # noqa: E402

from app import companies as registry  # noqa: E402
from app import config, db as dbmod, schema  # noqa: E402
from app.models import Company  # noqa: E402
from app.seed import bootstrap  # noqa: E402


@pytest.fixture()
def books():
    """A company file, made and populated the ordinary way."""
    tmp = tempfile.mkdtemp(prefix="nexora-upg-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
    yield ref.slug
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# A company file made by an earlier version
# --------------------------------------------------------------------------


def test_a_missing_column_is_added_on_open(books):
    """The exact shape of an upgrade: the table is there, a column is not."""
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE company DROP COLUMN currency_decimals'))

    with engine.connect() as conn:
        names = {r[1] for r in conn.execute(text("PRAGMA table_info(company)"))}
    assert "currency_decimals" not in names

    changes = schema.upgrade(engine)
    assert "company.currency_decimals" in changes

    with engine.connect() as conn:
        names = {r[1] for r in conn.execute(text("PRAGMA table_info(company)"))}
    assert "currency_decimals" in names


def test_the_restored_column_carries_its_default_not_a_null(books):
    """An existing row has to come back with a usable value in the new column.

    A null here would mean the customer's currency silently loses its decimal
    places on the first page they open.
    """
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE company DROP COLUMN currency_decimals'))
    schema.upgrade(engine)

    with engine.connect() as conn:
        value = conn.execute(text("SELECT currency_decimals FROM company WHERE id = 1")).scalar()
    assert value == 2


def test_the_books_still_open_and_read_after_the_upgrade(books):
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE company DROP COLUMN tax_label'))
        conn.execute(text('ALTER TABLE company DROP COLUMN date_format'))
    dbmod.init_db(books)                      # what happens on every start

    with dbmod.session_scope_for(books) as db:
        company = db.get(Company, 1)
        assert company.tax_label == "VAT"
        assert company.date_format == "%d %b %Y"


def test_upgrading_twice_changes_nothing_the_second_time(books):
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE company DROP COLUMN tax_authority'))
    assert schema.upgrade(engine) == ["company.tax_authority"]
    assert schema.upgrade(engine) == []


def test_a_current_file_is_left_completely_alone(books):
    assert schema.upgrade(dbmod.engine_for(books)) == []


def test_nothing_that_exists_is_ever_dropped(books):
    """An older file may carry a column the models no longer declare.

    Whatever it is, it stays. A dropped column is somebody's data.
    """
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE company ADD COLUMN old_favourite TEXT'))
        conn.execute(text("UPDATE company SET old_favourite = 'keep me'"))

    schema.upgrade(engine)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT old_favourite FROM company")).scalar() == "keep me"


def test_every_table_is_checked_not_just_the_company(books):
    engine = dbmod.engine_for(books)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE users DROP COLUMN job_title'))
        conn.execute(text('ALTER TABLE accounts DROP COLUMN is_system'))
    changes = set(schema.upgrade(engine))
    assert "users.job_title" in changes
    assert "accounts.is_system" in changes


# --------------------------------------------------------------------------
# The data folder moving when the software was renamed
# --------------------------------------------------------------------------


class FakeHome:
    """Point Path.home() and APPDATA somewhere disposable."""

    def __init__(self, root: Path):
        self.root = root
        self._home = None
        self._env = {}

    def __enter__(self):
        self._home = Path.home
        Path.home = staticmethod(lambda: self.root)      # type: ignore[assignment]
        for key in ("NEXORA_DATA", "APPDATA"):
            self._env[key] = os.environ.pop(key, None)
        os.environ["APPDATA"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        Path.home = self._home                            # type: ignore[assignment]
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


@pytest.fixture()
def home():
    root = Path(tempfile.mkdtemp(prefix="nexora-home-"))
    with FakeHome(root):
        yield root
    shutil.rmtree(root, ignore_errors=True)


def old_folder(root: Path) -> Path:
    return root / ("NaijaBooks" if os.name == "nt" else ".naijabooks")


def test_books_kept_under_the_old_name_are_moved_across(home):
    old = old_folder(home)
    (old / "backups").mkdir(parents=True)
    (old / "company.db").write_text("pretend this is a ledger", encoding="utf-8")
    (old / "backups" / "weekly.db").write_text("pretend this is last week's", encoding="utf-8")

    new = config.data_dir()

    assert (new / "company.db").read_text(encoding="utf-8") == "pretend this is a ledger"
    assert (new / "backups" / "weekly.db").exists()
    assert (new / "moved-from-naijabooks.txt").exists()


def test_a_fresh_installation_with_nothing_to_move_is_simply_empty(home):
    new = config.data_dir()
    assert new.exists()
    assert not (new / "moved-from-naijabooks.txt").exists()
    assert not any(new.glob("*.db"))


def test_live_books_are_never_overwritten_by_an_old_folder(home):
    """The move happens once. It must not run again over real work."""
    old = old_folder(home)
    old.mkdir(parents=True)
    (old / "company.db").write_text("stale", encoding="utf-8")

    new = config.data_dir()
    assert (new / "company.db").read_text(encoding="utf-8") == "stale"

    # Time passes; the customer works; the old folder reappears somehow.
    (new / "company.db").write_text("six months of real accounts", encoding="utf-8")
    old.mkdir(parents=True, exist_ok=True)
    (old / "company.db").write_text("stale again", encoding="utf-8")

    again = config.data_dir()
    assert (again / "company.db").read_text(encoding="utf-8") == "six months of real accounts"


def test_an_explicit_data_folder_is_never_second_guessed(home):
    """NEXORA_DATA means what it says: use this, move nothing."""
    old = old_folder(home)
    old.mkdir(parents=True)
    (old / "company.db").write_text("do not move me", encoding="utf-8")

    chosen = Path(tempfile.mkdtemp(prefix="nexora-explicit-"))
    os.environ["NEXORA_DATA"] = str(chosen)
    try:
        assert config.data_dir() == chosen
        assert not (chosen / "company.db").exists()
        assert (old / "company.db").exists()
    finally:
        os.environ.pop("NEXORA_DATA", None)
        shutil.rmtree(chosen, ignore_errors=True)


def test_the_move_leaves_a_note_saying_where_the_books_came_from(home):
    old = old_folder(home)
    old.mkdir(parents=True)
    (old / "company.db").write_text("ledger", encoding="utf-8")

    note = (config.data_dir() / "moved-from-naijabooks.txt").read_text(encoding="utf-8")
    assert str(old) in note
