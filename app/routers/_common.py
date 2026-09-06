"""Shared plumbing for the route modules."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from ..models import User
from ..money import to_kobo
from ..security import can


def db_of(request: Request) -> Session:
    return request.state.db


def user_of(request: Request) -> User | None:
    return getattr(request.state, "user", None)


def need(request: Request, permission: str) -> User:
    user = user_of(request)
    if user is None:
        raise HTTPException(401, "Sign in required")
    if not can(user, permission):
        from ..security import ROLE_LABELS

        raise HTTPException(
            403,
            f"Your role ({ROLE_LABELS.get(user.role, user.role)}) does not allow this. "
            "Ask an administrator if you need access.",
        )
    return user


def start_fresh_session(request: Request) -> None:
    """Throw away everything in the session except which books are open.

    Signing in, signing out and abandoning a half-finished second factor all
    need the old session gone — that is what stops one person's sign-in being
    inherited by the next. But the *company* is not part of anybody's identity:
    it is which set of books this browser tab is looking at. Losing it sends
    somebody who signed in to their second business back to their first, and on
    the hosted service it briefly leaves a session carrying a user id and no
    company at all — which is precisely the shape a cookie would need to be
    worth carrying to another customer's address.
    """
    company = request.session.get("company")
    request.session.clear()
    if company:
        request.session["company"] = company


def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else ""
    )


# --------------------------------------------------------------------------
# Form value parsing — always forgiving, never crashes on user input
# --------------------------------------------------------------------------


def parse_date(value: str | None, default: date | None = None) -> date:
    if not value:
        return default or date.today()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return default or date.today()


def parse_money(value: str | None) -> int:
    try:
        return to_kobo(value or 0)
    except Exception:
        return 0


def parse_qty(value: str | None, default: int = 0) -> int:
    """Quantities are stored as milli-units."""
    try:
        from decimal import Decimal

        return int(
            (Decimal(str(value or 0).replace(",", "").strip()) * 1000).to_integral_value()
        )
    except Exception:
        return default


def parse_int(value, default: int | None = None) -> int | None:
    try:
        v = int(str(value).strip())
        return v
    except (TypeError, ValueError):
        return default


def parse_id(value) -> int | None:
    v = parse_int(value, None)
    return v if v and v > 0 else None


def parse_bool(value) -> bool:
    return str(value).lower() in ("1", "true", "on", "yes")


def month_bounds(on: date) -> tuple[date, date]:
    start = on.replace(day=1)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return start, end


def period_from_query(request: Request, db: Session) -> tuple[date, date, str]:
    """Resolve ``?start=&end=&period=`` into a date range."""
    from ..services.reports import fiscal_year_bounds

    q = request.query_params
    preset = q.get("period", "")
    today = date.today()

    if q.get("start") or q.get("end"):
        start = parse_date(q.get("start"), today.replace(day=1))
        end = parse_date(q.get("end"), today)
        return start, end, "custom"

    if preset == "month":
        return (*month_bounds(today), "month")
    if preset == "last_month":
        prev = today.replace(day=1) - timedelta(days=1)
        return (*month_bounds(prev), "last_month")
    if preset == "quarter":
        qn = (today.month - 1) // 3
        start = date(today.year, qn * 3 + 1, 1)
        end = (date(today.year + (qn == 3), (qn * 3 + 4 - 1) % 12 + 1, 1)) - timedelta(days=1)
        return start, end, "quarter"
    if preset == "last_year":
        fs, _ = fiscal_year_bounds(db, today)
        prev_end = fs - timedelta(days=1)
        ps, pe = fiscal_year_bounds(db, prev_end)
        return ps, pe, "last_year"
    fs, fe = fiscal_year_bounds(db, today)
    return fs, min(fe, today), "year"


def redirect(url: str, status: int = 303):
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url, status_code=status)
