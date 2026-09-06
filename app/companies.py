"""The company register.

Each company keeps its own SQLite file, its own logo and its own attachments,
in its own folder:

    <data dir>/companies/<slug>/company.db
                              /logo.png
                              /attachments/
                              /backups/

Nothing is shared between companies. That is deliberate: it means one
company's books can be backed up, handed to its accountant, or restored,
without touching any other, and a mistake in one can never reach another.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import clock
from . import config

REGISTRY = "companies.json"
DEFAULT_SLUG = "main"


class CompanyError(Exception):
    """Raised for anything the user needs to be told about. Safe to display."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def companies_root() -> Path:
    p = config.data_dir() / "companies"
    p.mkdir(parents=True, exist_ok=True)
    return p


def company_dir(slug: str) -> Path:
    p = companies_root() / slug
    p.mkdir(parents=True, exist_ok=True)
    (p / "attachments").mkdir(exist_ok=True)
    (p / "backups").mkdir(exist_ok=True)
    return p


def company_db(slug: str) -> Path:
    return company_dir(slug) / "company.db"


def logo_path(slug: str) -> Path | None:
    """The logo file for a company, if one has been uploaded."""
    folder = company_dir(slug)
    for ext in ("png", "jpg", "jpeg", "gif", "webp"):
        candidate = folder / f"logo.{ext}"
        if candidate.exists():
            return candidate
    return None


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return (slug or "company")[:40]


# --------------------------------------------------------------------------
# The register itself
# --------------------------------------------------------------------------


@dataclass
class CompanyRef:
    slug: str
    name: str
    created_at: str = ""
    last_opened: str = ""
    is_archived: bool = False

    @property
    def db_file(self) -> Path:
        return company_db(self.slug)

    @property
    def exists(self) -> bool:
        return self.db_file.exists()

    @property
    def size_bytes(self) -> int:
        return self.db_file.stat().st_size if self.exists else 0


def _registry_file() -> Path:
    return config.data_dir() / REGISTRY


def _read() -> list[dict]:
    f = _registry_file()
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8") or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # A corrupt register must never lose the books themselves — rebuild
        # it from whatever company folders are actually on disk.
        return []


def _write(rows: list[dict]) -> None:
    _registry_file().write_text(json.dumps(rows, indent=2), encoding="utf-8")


def all_companies(include_archived: bool = False) -> list[CompanyRef]:
    rows = _read()
    known = {r.get("slug") for r in rows}

    # Adopt any company folder that is on disk but missing from the register
    for folder in sorted(companies_root().iterdir()):
        if folder.is_dir() and (folder / "company.db").exists() and folder.name not in known:
            rows.append({
                "slug": folder.name,
                "name": folder.name.replace("-", " ").title(),
                "created_at": clock.now().isoformat(timespec="seconds"),
                "last_opened": "",
                "is_archived": False,
            })
    if rows != _read():
        _write(rows)

    out = [CompanyRef(**{k: r.get(k, "") for k in
                         ("slug", "name", "created_at", "last_opened")},
                      is_archived=bool(r.get("is_archived")))
           for r in rows]
    if not include_archived:
        out = [c for c in out if not c.is_archived]
    return sorted(out, key=lambda c: (c.is_archived, c.name.lower()))


def get(slug: str) -> CompanyRef | None:
    for c in all_companies(include_archived=True):
        if c.slug == slug:
            return c
    return None


def touch(slug: str) -> None:
    """Record that a company was opened, so it can be reopened next time."""
    rows = _read()
    for r in rows:
        if r.get("slug") == slug:
            r["last_opened"] = clock.now().isoformat(timespec="seconds")
    _write(rows)


def rename(slug: str, new_name: str) -> None:
    new_name = (new_name or "").strip()
    if not new_name:
        raise CompanyError("A company needs a name.")
    rows = _read()
    for r in rows:
        if r.get("slug") == slug:
            r["name"] = new_name
    _write(rows)


def set_archived(slug: str, archived: bool) -> None:
    live = [c for c in all_companies() if c.slug != slug]
    if archived and not live:
        raise CompanyError(
            "This is your only active company — archiving it would leave you "
            "with nothing to open."
        )
    rows = _read()
    for r in rows:
        if r.get("slug") == slug:
            r["is_archived"] = archived
    _write(rows)


def register(slug: str, name: str) -> CompanyRef:
    rows = _read()
    if any(r.get("slug") == slug for r in rows):
        raise CompanyError(f"A company folder named '{slug}' already exists.")
    rows.append({
        "slug": slug,
        "name": name,
        "created_at": clock.now().isoformat(timespec="seconds"),
        "last_opened": "",
        "is_archived": False,
    })
    _write(rows)
    return CompanyRef(slug=slug, name=name)


def unique_slug(name: str) -> str:
    base = slugify(name)
    taken = {c.slug for c in all_companies(include_archived=True)}
    if base not in taken and not (companies_root() / base).exists():
        return base
    n = 2
    while f"{base}-{n}" in taken or (companies_root() / f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


# --------------------------------------------------------------------------
# Creating and copying
# --------------------------------------------------------------------------


def create(name: str, copy_setup_from: str | None = None) -> CompanyRef:
    """Start a new set of books.

    ``copy_setup_from`` copies the chart of accounts, tax codes and payroll
    rates from an existing company — useful for a second business run the same
    way — but never copies a single transaction, customer or employee.
    """
    name = (name or "").strip()
    if not name:
        raise CompanyError("Give the company a name.")

    slug = unique_slug(name)
    company_dir(slug)
    ref = register(slug, name)

    from .db import session_scope_for
    from .seed import bootstrap

    with session_scope_for(slug) as db:
        company = bootstrap(db)
        company.name = name

    if copy_setup_from:
        _copy_setup(copy_setup_from, slug)
    return ref


def _copy_setup(from_slug: str, to_slug: str) -> None:
    """Copy the chart of accounts, tax codes and payroll rates across."""
    from sqlalchemy import select

    from .db import session_scope_for
    from .models import Account, PayrollSetting, TaxCode

    with session_scope_for(from_slug) as src:
        accounts = [
            {
                "code": a.code, "name": a.name, "type": a.type, "subtype": a.subtype,
                "description": a.description, "is_active": a.is_active,
                "is_system": a.is_system, "system_key": a.system_key,
                "is_bank": a.is_bank, "cashflow_class": a.cashflow_class,
            }
            for a in src.scalars(select(Account).order_by(Account.code))
        ]
        taxes = [
            {
                "code": t.code, "name": t.name, "kind": t.kind, "rate": t.rate,
                "rate_no_tin": t.rate_no_tin, "is_active": t.is_active,
                "is_exempt": t.is_exempt, "is_zero_rated": t.is_zero_rated,
                "note": t.note, "sort": t.sort,
            }
            for t in src.scalars(select(TaxCode))
        ]
        ps = src.get(PayrollSetting, 1)
        payroll = {
            c.name: getattr(ps, c.name)
            for c in PayrollSetting.__table__.columns if c.name != "id"
        } if ps else None

    with session_scope_for(to_slug) as dst:
        # The new company was seeded with the standard chart a moment ago, so
        # anything it already has must be *updated* to match the source — not
        # skipped, or a renamed account or an adjusted tax rate would be lost.
        existing = {a.code: a for a in dst.scalars(select(Account))}
        for row in accounts:
            target = existing.get(row["code"])
            if target is None:
                dst.add(Account(**row))
            else:
                for field, value in row.items():
                    setattr(target, field, value)

        have = {t.code: t for t in dst.scalars(select(TaxCode))}
        for row in taxes:
            target = have.get(row["code"])
            if target is None:
                dst.add(TaxCode(**row))
            else:
                for field, value in row.items():
                    setattr(target, field, value)
        if payroll:
            target = dst.get(PayrollSetting, 1)
            if target is None:
                target = PayrollSetting(id=1)
                dst.add(target)
            for k, v in payroll.items():
                setattr(target, k, v)


# --------------------------------------------------------------------------
# Upgrading a single-company installation
# --------------------------------------------------------------------------


def migrate_legacy() -> str | None:
    """Move a pre-multi-company database into the new layout.

    Version 1 kept one file at ``<data dir>/company.db``. If that file is
    still there, it becomes the first company and everything carries on
    exactly as before — same books, same numbers.
    """
    legacy = config.data_dir() / "company.db"
    if not legacy.exists():
        return None
    if company_db(DEFAULT_SLUG).exists():
        return None

    name = DEFAULT_SLUG.title()
    try:
        con = sqlite3.connect(str(legacy))
        row = con.execute("SELECT name FROM company WHERE id = 1").fetchone()
        con.close()
        if row and row[0]:
            name = row[0]
    except sqlite3.DatabaseError:
        pass

    target_dir = company_dir(DEFAULT_SLUG)
    shutil.move(str(legacy), str(company_db(DEFAULT_SLUG)))
    for suffix in ("-wal", "-shm"):
        stale = legacy.with_name(legacy.name + suffix)
        if stale.exists():
            stale.unlink()

    # Bring the old logo, attachments and backups along
    old_attachments = config.data_dir() / "attachments"
    if old_attachments.exists():
        for f in old_attachments.iterdir():
            if f.is_file():
                shutil.move(str(f), str(target_dir / "attachments" / f.name))
    old_backups = config.data_dir() / "backups"
    if old_backups.exists():
        for f in old_backups.glob("*.db"):
            shutil.move(str(f), str(target_dir / "backups" / f.name))

    if get(DEFAULT_SLUG) is None:
        register(DEFAULT_SLUG, name)
    else:
        rename(DEFAULT_SLUG, name)
    return DEFAULT_SLUG


def ensure_at_least_one() -> CompanyRef:
    """Guarantee there is always a set of books to open."""
    migrate_legacy()
    live = all_companies()
    if live:
        return live[0]
    archived = all_companies(include_archived=True)
    if archived:
        set_archived(archived[0].slug, False)
        return archived[0]
    return create("My Company Ltd")


def default_slug() -> str:
    """The company to open when nobody has chosen one."""
    ensure_at_least_one()
    live = all_companies()
    with_history = [c for c in live if c.last_opened]
    if with_history:
        return max(with_history, key=lambda c: c.last_opened).slug
    return live[0].slug


# --------------------------------------------------------------------------
# Backups, per company
# --------------------------------------------------------------------------


def backup(slug: str, label: str = "") -> Path:
    """Take a consistent snapshot using SQLite's own backup API."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"backup-{stamp}{('-' + label) if label else ''}.db"
    dest = company_dir(slug) / "backups" / name
    src = sqlite3.connect(str(company_db(slug)))
    dst = sqlite3.connect(str(dest))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    return dest


def list_backups(slug: str) -> list[tuple[str, int, datetime]]:
    folder = company_dir(slug) / "backups"
    files = sorted(folder.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [(p.name, p.stat().st_size, datetime.fromtimestamp(p.stat().st_mtime))
            for p in files]


def looks_like_our_database(path: Path) -> bool:
    try:
        con = sqlite3.connect(str(path))
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
    except sqlite3.DatabaseError:
        return False
    return {"accounts", "journal_entries", "journal_lines", "company", "users"}.issubset(tables)
