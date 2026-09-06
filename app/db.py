"""Database engines and sessions — one engine per company.

Each company has its own SQLite file, so each gets its own engine and session
factory, cached by slug. Two members of staff can work on different companies
at the same time without either seeing the other's books.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from . import config


class Base(DeclarativeBase):
    pass


_engines: dict[str, object] = {}
_sessions: dict[str, object] = {}
_lock = threading.Lock()

# Set by the request middleware; used by the few helpers that have no slug
# to hand. Falls back to the default company outside a request.
_current = threading.local()


def _apply_pending_restore(slug: str) -> None:
    """A restore staged from Settings is completed here, on the next start."""
    import shutil
    from datetime import datetime

    from .companies import company_dir, company_db

    pending = company_dir(slug) / "restore-pending.db"
    if not pending.exists():
        return
    live = company_db(slug)
    if live.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.move(str(live), str(company_dir(slug) / "backups" / f"replaced-{stamp}.db"))
    for suffix in ("-wal", "-shm"):
        stale = live.with_name(live.name + suffix)
        if stale.exists():
            stale.unlink()
    shutil.move(str(pending), str(live))


def engine_for(slug: str):
    from .companies import company_db

    with _lock:
        if slug not in _engines:
            _apply_pending_restore(slug)
            eng = create_engine(
                f"sqlite:///{company_db(slug)}",
                future=True,
                # One server process serves every workstation on the LAN, so
                # the only concurrency SQLite sees is this app's own threads.
                connect_args={"check_same_thread": False, "timeout": 30},
            )

            @event.listens_for(eng, "connect")
            def _pragmas(dbapi_conn, _rec):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")      # concurrent readers
                cur.execute("PRAGMA foreign_keys=ON")       # referential integrity
                cur.execute("PRAGMA synchronous=FULL")      # never lose a posted entry
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

            _engines[slug] = eng
            _sessions[slug] = sessionmaker(bind=eng, expire_on_commit=False, future=True)
        return _engines[slug]


def set_current(slug: str) -> None:
    _current.slug = slug


def current_slug() -> str:
    slug = getattr(_current, "slug", None)
    if slug:
        return slug
    from .companies import default_slug

    return default_slug()


def forget(slug: str) -> None:
    """Drop a company's engine — used after a restore or an archive."""
    with _lock:
        eng = _engines.pop(slug, None)
        _sessions.pop(slug, None)
    if eng is not None:
        eng.dispose()


def reset_all() -> None:
    """Drop every cached engine and forget the current company.

    Engines are cached by slug, and a slug says nothing about which data
    folder it came from — so any test that swaps NEXORA_DATA must call
    this, or it will silently keep talking to the previous folder's database.
    """
    from . import licensing

    with _lock:
        engines = list(_engines.values())
        _engines.clear()
        _sessions.clear()
    for eng in engines:
        eng.dispose()
    _current.slug = None
    # The licence lives in the data folder too, so it is just as stale.
    licensing.forget_cached()


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def SessionLocal(slug: str | None = None):
    slug = slug or current_slug()
    engine_for(slug)
    return _sessions[slug]()


def engine():
    """The engine for the company currently in scope."""
    return engine_for(current_slug())


@contextmanager
def session_scope_for(slug: str):
    init_db(slug)
    s = SessionLocal(slug)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def session_scope():
    with session_scope_for(current_slug()) as s:
        yield s


def get_db():
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def init_db(slug: str | None = None):
    from . import models  # noqa: F401  (registers mappers)

    from . import schema

    slug = slug or current_slug()
    eng = engine_for(slug)
    Base.metadata.create_all(eng)
    # A file made by an earlier version has the tables but not the newer
    # columns. Add them before anything queries them.
    schema.upgrade(eng)
