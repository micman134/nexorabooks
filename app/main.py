"""Application entry point: middleware, template environment and routing."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import companies as company_registry
from . import config, currency, db as dbmod, licensing, prefs, tenancy, themes
from .db import SessionLocal, init_db
from .models import Company, User
from .money import fmt, fmt_plain, to_major
from .security import ROLE_LABELS, can


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Prepare every company's books on start-up.

    A version 1 installation kept a single database file; it is moved into the
    per-company layout here, so an upgrade opens on exactly the same books.
    """
    from .db import session_scope_for
    from .seed import bootstrap

    from .services import autobackup

    company_registry.migrate_legacy()
    company_registry.ensure_at_least_one()
    for ref in company_registry.all_companies(include_archived=True):
        init_db(ref.slug)
        with session_scope_for(ref.slug) as db:
            bootstrap(db)
    # Backups keep themselves from here on, as long as this process is running.
    autobackup.start()
    yield
    autobackup.stop()


app = FastAPI(
    title=config.APP_NAME,
    version=config.APP_VERSION,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

BASE = config.base_dir()
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# --------------------------------------------------------------------------
# Template helpers
# --------------------------------------------------------------------------


def qty_fmt(milli: int | None) -> str:
    if not milli:
        return "0"
    whole = milli / 1000
    return f"{whole:,.3f}".rstrip("0").rstrip(".") if milli % 1000 else f"{milli // 1000:,}"


def date_fmt(d, style: str | None = None) -> str:
    """Dates as the open company writes them, unless a style is forced.

    Goes through ``prefs.strftime`` rather than ``strftime`` directly, so a
    format holding ``%-d`` renders the same on Windows as it does here instead
    of raising. See the note in ``app/prefs.py``.
    """
    if not d:
        return ""
    return prefs.strftime(d, style or prefs.date_format())


def status_class(status: str) -> str:
    return {
        "DRAFT": "badge-draft",
        "POSTED": "badge-open",
        "PART_PAID": "badge-part",
        "PAID": "badge-paid",
        "VOID": "badge-void",
        "CONVERTED": "badge-paid",
        "SENT": "badge-open",
        "ACCEPTED": "badge-paid",
        "DECLINED": "badge-void",
    }.get(status or "", "badge-draft")


def _finalize(value):
    """Render a missing value as nothing at all.

    Without this, a brand-new record whose columns have not been written yet
    puts the literal text "None" into every form field, because a column
    default only applies when the row is inserted.
    """
    return "" if value is None else value


templates.env.finalize = _finalize
templates.env.filters["money"] = fmt
templates.env.filters["money_plain"] = fmt_plain
# Editable money cells: grouped digits, no symbol — parse_money reads them back
# whichever way this company's currency groups and points its figures.
templates.env.filters["amount"] = lambda k: fmt(k, symbol="")
templates.env.filters["major"] = to_major
templates.env.filters["naira"] = to_major        # the name most templates use
templates.env.filters["qty"] = qty_fmt
templates.env.filters["dt"] = date_fmt
templates.env.filters["status_class"] = status_class
templates.env.globals.update(
    APP_NAME=config.APP_NAME,
    APP_VERSION=config.APP_VERSION,
    ROLE_LABELS=ROLE_LABELS,
    can=can,
    today=date.today,
    timedelta=timedelta,
    cur=currency.active,              # {{ cur().code }} on screens that need it
    themes=themes,
)


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("request", request)
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("company", getattr(request.state, "company", None))
    ctx.setdefault("company_slug", getattr(request.state, "company_slug", ""))
    ctx.setdefault("company_ref", getattr(request.state, "company_ref", None))
    ctx.setdefault("companies", getattr(request.state, "companies", []))
    ctx.setdefault("licence", licensing.status())
    ctx.setdefault("theme", themes.resolve(ctx.get("user"), ctx.get("company")))
    ctx.setdefault("flashes", pop_flashes(request))
    return templates.TemplateResponse(request=request, name=template, context=ctx)


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session.setdefault("_flash", []).append({"message": message, "level": level})


def pop_flashes(request: Request) -> list[dict]:
    return request.session.pop("_flash", [])


# --------------------------------------------------------------------------
# Middleware
# --------------------------------------------------------------------------

PUBLIC_PATHS = {"/login", "/static", "/themes.css", "/favicon.ico",
                "/health", "/setup",
                # Somebody accepting an invitation has no password yet.
                "/invite"}

#: Where somebody who still owes the company a second factor may still go.
#: Setting it up, changing a password they were told to change, and leaving.
TWOFACTOR_EXEMPT = ("/account/two-factor", "/account/password", "/logout", "/account/theme")


#: What a visitor sees at a hosted address that names no customer. Deliberately
#: says nothing about which names do exist — a page that distinguishes "no such
#: business" from "that business, please sign in" is a customer list anybody
#: can read one guess at a time.
NO_TENANT_PAGE = (
    "<!doctype html><meta charset='utf-8'>"
    "<title>Nothing here</title>"
    "<style>body{font:16px/1.6 system-ui,sans-serif;margin:12vh auto;max-width:32rem;"
    "padding:0 1.5rem;color:#1a1a1a;background:#fafaf9}"
    "@media(prefers-color-scheme:dark){body{color:#e8e8e6;background:#16171a}}</style>"
    "<h1>Nothing here</h1>"
    "<p>There are no books at this address. If you were given a web address for "
    "your business, check it against the message you were sent.</p>"
)


@app.middleware("http")
async def load_user(request: Request, call_next):
    # Which company's books is this request working on?
    #
    # Installed: the choice is held in the signed session cookie, so somebody
    # with two businesses can have both open in two tabs.
    #
    # Hosted: the choice comes from the hostname and nowhere else. A cookie is
    # something the visitor holds, and anything the visitor holds must not be
    # allowed to select a database. See app/tenancy.py.
    if tenancy.hosted():
        slug = tenancy.resolve(request.headers.get("host"))
        ref = company_registry.get(slug) if slug else None
        if ref is None or ref.is_archived or not ref.exists:
            return HTMLResponse(NO_TENANT_PAGE, status_code=404)
        # Belt and braces. The books were chosen from the host above, so the
        # cookie has already had no say. But if a session that was minted for
        # one customer ever arrives at another's address — a proxy
        # misconfiguration, a Domain attribute somebody adds later — throw it
        # away rather than carry a stranger's user id into these books.
        #
        # A session with no company named in it is the ordinary case: signing
        # in clears the session, so the very next request arrives without one.
        # That is not a mismatch and must not cost the person their sign-in.
        carried = request.session.get("company")
        if carried is not None and carried != slug:
            request.session.clear()
        request.session["company"] = slug
    else:
        slug = request.session.get("company")
        ref = company_registry.get(slug) if slug else None
        if ref is None or ref.is_archived or not ref.exists:
            slug = company_registry.default_slug()
            request.session["company"] = slug
            ref = company_registry.get(slug)
    dbmod.set_current(slug)

    db = SessionLocal(slug)
    try:
        request.state.db = db
        request.state.company_slug = slug
        request.state.company_ref = ref
        # The company switcher in the sidebar is built from this. On the hosted
        # service it is empty, so there is no list of other businesses to render
        # even if a template forgets to check.
        request.state.companies = [] if tenancy.hosted() else company_registry.all_companies()
        request.state.company = db.get(Company, 1)
        # Every figure rendered on this request — and every figure typed into
        # it — is read and written in this company's own currency.
        currency.set_active(currency.from_company(request.state.company))
        prefs.set_date_format(getattr(request.state.company, "date_format", None))

        user = None
        uid = request.session.get("uid")
        if uid:
            user = db.get(User, uid)
            if user and not user.is_active:
                user = None
                request.session.clear()
                request.session["company"] = slug
        request.state.user = user

        path = request.url.path
        is_public = any(path == p or path.startswith(p + "/") for p in PUBLIC_PATHS)
        if user is None and not is_public:
            if request.headers.get("hx-request"):
                return HTMLResponse("Your session has expired. Please sign in again.", status_code=401)
            return RedirectResponse(f"/login?next={path}", status_code=303)

        # A company can insist on two-factor sign-in. Somebody who has not set
        # it up yet is sent to the setup screen rather than turned away — an
        # administrator switching this on must not strand their own staff
        # outside their own books.
        if user is not None and not is_public:
            from .services import twofactor

            if twofactor.must_set_up(user, request.state.company) and not any(
                path.startswith(allowed) for allowed in TWOFACTOR_EXEMPT
            ):
                return RedirectResponse("/account/two-factor?required=1", status_code=303)

        response = await call_next(request)
        return response
    finally:
        db.close()


# Added last so it wraps the loader above: Starlette runs the most recently
# added middleware first, and request.session must exist before load_user runs.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.secret_key(),
    session_cookie="nexorabooks",
    max_age=60 * 60 * 12,
    same_site="lax",
    # Once the connection is encrypted the cookie must never be sent over an
    # unencrypted one, or a single mistyped address hands somebody's session to
    # whoever is listening. Off while serving plain HTTP, because a cookie the
    # browser refuses to send is a sign-in screen nobody can get past.
    https_only=config.serving_over_tls(),
)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "user": getattr(request.state, "user", None),
            "company": getattr(request.state, "company", None),
            "theme": themes.resolve(getattr(request.state, "user", None),
                                    getattr(request.state, "company", None)),
            "code": exc.status_code,
            "detail": exc.detail,
            "flashes": [],
        },
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Anything nobody expected: write it down, and say so calmly.

    A stack trace on the screen tells a bookkeeper nothing and frightens them.
    A reference they can read out, and a file they can send, turns the same
    failure into something that can actually be fixed.
    """
    from .services import support

    user = getattr(request.state, "user", None)
    reference = support.record(
        exc,
        where=f"{request.method} {request.url.path}",
        who=getattr(user, "username", ""),
        company=getattr(getattr(request.state, "company_ref", None), "name", ""),
    )
    return templates.TemplateResponse(
        request=request,
        name="error.html",
        context={
            "user": user,
            "company": getattr(request.state, "company", None),
            "theme": themes.resolve(user, getattr(request.state, "company", None)),
            "licence": licensing.status(),
            "code": 500,
            "detail": "Something went wrong that this was not expecting. "
                      "Nothing you were working on has been saved, so nothing "
                      "in your books has changed.",
            "reference": reference,
            "flashes": [],
        },
        status_code=500,
    )


@app.get("/themes.css")
def theme_styles():
    """Every colour theme, generated from app/themes.py.

    Kept out of app.css so that adding a theme is a Python change and nothing
    has to be hand-edited in a stylesheet.
    """
    from fastapi.responses import Response

    return Response(themes.stylesheet(), media_type="text/css",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/health")
def health(request: Request):
    """Who is answering on this port.

    The launcher asks this before doing anything else. If a copy is already
    running it used to assume that copy was itself and simply open a browser
    at it — so somebody who had downloaded a new version, unzipped it and
    started it was quietly shown the *old* one still holding the port, with
    nothing anywhere to say so. Now the launcher can tell the two apart.

    The folder and the process id are only told to somebody asking from this
    same computer. Over the network the answer stays as it was: the name and
    the version, and nothing about where anything lives on disk.
    """
    from . import network

    body = {"status": "ok", "app": config.APP_NAME, "version": config.APP_VERSION}
    caller = getattr(request.client, "host", "") if request.client else ""
    if network.is_loopback(caller):
        body["pid"] = os.getpid()
        body["program_dir"] = str(config.program_dir())
        body["data_dir"] = str(config.data_dir())
    return body


# --------------------------------------------------------------------------
# Routers
# --------------------------------------------------------------------------

from .routers import (  # noqa: E402
    archive,
    assets,
    attachments,
    auth,
    banking,
    bankimport,
    budgets,
    cash,
    companies,
    contacts,
    dashboard,
    email,
    groupacc,
    importing,
    insights,
    inventory,
    journals,
    landed,
    payroll,
    pos,
    projects,
    purchases,
    recurring,
    requisitions,
    reports,
    sales,
    settings,
)

for module in (
    auth, dashboard, companies, contacts, sales, purchases, inventory,
    bankimport, banking, payroll, journals, assets, recurring, budgets, landed, requisitions, projects,
    reports, insights, cash, pos, groupacc, settings,
    attachments, importing, email, archive,
):
    app.include_router(module.router)

# Cash routers live at the top level so their URLs read /receipts and /payments
app.include_router(sales.receipts)
app.include_router(purchases.payments)
app.include_router(journals.coa)
app.include_router(email.send)


