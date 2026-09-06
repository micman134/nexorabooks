"""Bring an older company file up to the current schema, in place.

``create_all`` makes tables that do not exist. It does nothing about a table
that exists but has gained a column since it was created — and that is exactly
what happens to a customer who has been keeping books for six months and then
installs an update. Without this module their file opens and then falls over on
the first query that touches the new column.

So: every time a company database is opened, compare what the models declare
against what the file actually has, and add whatever is missing. Only additions
are ever made. Nothing is renamed, retyped or dropped, because a wrong guess
about intent would cost somebody their ledger, and an unused column costs
nothing at all.

Everything here runs on **one** connection. SQLite keeps a per-connection view
of the schema, so reading the columns down one connection and altering the
table down another can leave the two disagreeing — the reader says a column is
missing while the writer insists it is already there. Reading and writing
through the same connection makes that impossible.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .db import Base


def _sql_literal(value) -> str:
    """Render a Python default as a SQLite literal, or '' if it cannot be."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return ""


def _default_for(column) -> str:
    """The literal to backfill existing rows with, or '' for none."""
    default = column.default
    if default is None or default.is_callable or default.is_sequence:
        return ""
    return _sql_literal(getattr(default, "arg", None))


def _column_sql(column, dialect) -> str:
    """``name TYPE [DEFAULT x]`` for an ALTER TABLE ADD COLUMN.

    Deliberately never NOT NULL: SQLite refuses to add a NOT NULL column
    without a default, and the mappers supply a value on every insert anyway,
    so the constraint would only ever have blocked the upgrade itself.
    """
    try:
        type_sql = column.type.compile(dialect)
    except Exception:                       # an exotic type with no DDL form
        type_sql = "TEXT"
    literal = _default_for(column)
    tail = f" DEFAULT {literal}" if literal else ""
    return f'"{column.name}" {type_sql}{tail}'


def _existing(conn, kind: str) -> set[str]:
    rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = :k"), {"k": kind})
    return {r[0] for r in rows}


def upgrade(engine) -> list[str]:
    """Add any columns and indexes the models declare but the file lacks.

    Returns what was changed, for the log. Safe to call on every start: on a
    current file it looks and does nothing.
    """
    from . import models  # noqa: F401  (registers the mappers)

    dialect = engine.dialect
    changes: list[str] = []

    with engine.begin() as conn:
        tables = _existing(conn, "table")
        indexes = _existing(conn, "index")

        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue                    # create_all will make it whole
            have = {r[1] for r in conn.execute(text(f'PRAGMA table_info("{table.name}")'))}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN {_column_sql(column, dialect)}'
                try:
                    conn.execute(text(ddl))
                    changes.append(f"{table.name}.{column.name}")
                except OperationalError:
                    # Another process opened the same file at the same moment
                    # and got there first. Its column is as good as ours.
                    pass

        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            for index in table.indexes:
                if index.name in indexes:
                    continue
                try:
                    index.create(bind=conn)
                    changes.append(f"index {index.name}")
                except OperationalError:
                    pass

    return changes
