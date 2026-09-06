"""Sign in, sign out and password management."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request

from .. import clock
from .. import security, totp
from ..models import ROLE_ADMIN, User
from ..services import invites, throttle, twofactor
from ..services.posting import audit
from ._common import client_ip, db_of, redirect, start_fresh_session, user_of

router = APIRouter()


@router.get("/login")
def login_form(request: Request):
    from ..main import render

    if user_of(request):
        return redirect("/")
    return render(request, "login.html", next=request.query_params.get("next", "/"))


@router.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    from ..main import flash, render

    db = db_of(request)
    where = client_ip(request)

    # Somebody guessing has to wait. See app/services/throttle.py — this is
    # checked before the password is even looked at, so a run of guesses costs
    # the guesser time whether or not the username is real.
    waiting = throttle.wait_needed(username, where)
    if waiting:
        audit(db, None, "LOGIN_THROTTLED", "User", detail=f"username={username}", ip=where)
        db.commit()
        return render(
            request,
            "login.html",
            error=("Too many wrong passwords. Try again in "
                   + throttle.in_words(waiting) + "."),
            username=username,
            next=next,
        )

    user = security.authenticate(db, username, password)
    if user is None:
        throttle.failed(username, where)
        audit(db, None, "LOGIN_FAILED", "User", detail=f"username={username}", ip=where)
        db.commit()
        return render(
            request,
            "login.html",
            error="That username and password combination was not recognised.",
            username=username,
            next=next,
        )
    throttle.succeeded(username, where)

    if twofactor.is_on(user):
        # The password was right, but they are not signed in yet. Hold the
        # user id somewhere that is NOT "uid", so that a half-finished sign-in
        # can never be mistaken for a real one anywhere else in the software.
        start_fresh_session(request)
        request.session["pending_uid"] = user.id
        request.session["pending_next"] = next if next.startswith("/") else "/"
        return render(request, "twofactor_login.html", username=user.username)

    return _finish_login(request, db, user, next)


def _finish_login(request: Request, db, user, next: str):
    """Everything that happens once both steps are past."""
    from ..main import flash

    start_fresh_session(request)
    request.session["uid"] = user.id
    user.last_login = clock.now()
    audit(db, user, "LOGIN", "User", user.id, ip=client_ip(request))
    db.commit()

    if user.must_change_password:
        flash(request, "Please choose a new password before you continue.", "warning")
        return redirect("/account/password")
    return redirect(next if next.startswith("/") else "/")


@router.post("/login/code")
def login_code(request: Request, code: str = Form(""), next: str = Form("/")):
    """The second step. Reached only after the password was accepted."""
    from ..main import flash, render

    db = db_of(request)
    pending = request.session.get("pending_uid")
    if not pending:
        flash(request, "Please sign in again.", "warning")
        return redirect("/login")

    user = db.get(User, int(pending))
    if user is None or not user.is_active:
        start_fresh_session(request)
        return redirect("/login")

    result = twofactor.check(db, user, code)
    if not result.ok:
        audit(db, None, "TWOFACTOR_FAILED", "User", user.id,
              detail=result.message, ip=client_ip(request))
        db.commit()
        if result.locked:
            start_fresh_session(request)
            return render(request, "login.html", error=result.message, next=next)
        return render(request, "twofactor_login.html", username=user.username,
                      error=result.message)

    target = request.session.get("pending_next", next)
    if result.used_recovery:
        audit(db, user, "TWOFACTOR_RECOVERY", "User", user.id, ip=client_ip(request))
        flash(request, result.message, "warning" if result.codes_left <= 2 else "success")
    response = _finish_login(request, db, user, target)
    return response


@router.get("/login/code")
def login_code_form(request: Request):
    """Somebody reloading the page mid-sign-in should see the code box again."""
    from ..main import render

    pending = request.session.get("pending_uid")
    if not pending:
        return redirect("/login")
    db = db_of(request)
    user = db.get(User, int(pending))
    if user is None:
        start_fresh_session(request)
        return redirect("/login")
    return render(request, "twofactor_login.html", username=user.username)


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    db = db_of(request)
    user = user_of(request)
    if user:
        audit(db, user, "LOGOUT", "User", user.id, ip=client_ip(request))
        db.commit()
    start_fresh_session(request)
    return redirect("/login")


@router.get("/account/password")
def password_form(request: Request):
    from ..main import render

    return render(request, "password.html")


@router.post("/account/password")
def password_change(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    from ..main import flash, render

    db = db_of(request)
    user = user_of(request)

    if not user.must_change_password and not security.verify_password(
        current_password, user.password_hash
    ):
        return render(request, "password.html", error="Your current password is not correct.")
    if new_password != confirm_password:
        return render(request, "password.html", error="The two new passwords do not match.")
    problems = security.password_problems(new_password)
    if problems:
        return render(
            request,
            "password.html",
            error="Your new password needs " + ", ".join(problems) + ".",
        )

    user.password_hash = security.hash_password(new_password)
    user.must_change_password = False
    audit(db, user, "PASSWORD_CHANGE", "User", user.id, ip=client_ip(request))
    db.commit()
    flash(request, "Your password has been changed.")
    return redirect("/")


@router.get("/account")
def account(request: Request):
    from ..main import render

    user = user_of(request)
    offset = int(getattr(user, "totp_offset", 0) or 0) if user else 0
    return render(request, "account.html",
                  clock_out=totp.minutes_out(offset) if offset else "")


@router.post("/account/theme")
async def account_theme(request: Request):
    """One person's choice of colours. Never anybody else's."""
    from ..main import flash

    from .. import themes

    user = user_of(request)
    if user is None:
        return redirect("/login")
    db = db_of(request)
    form = await request.form()
    chosen = (form.get("theme") or "").strip().lower()
    user.theme = chosen if chosen in themes.BY_KEY else ""
    db.commit()
    flash(request, f"Colours set to {themes.get(user.theme or None).name}."
          if user.theme else "Following the company's colours again.")
    return redirect("/account")


# --------------------------------------------------------------------------
# Setting up two-factor sign-in
# --------------------------------------------------------------------------


@router.get("/account/two-factor")
def twofactor_start(request: Request):
    """Show the QR code and wait for the first code off the phone."""
    from ..main import render

    user = user_of(request)
    if user is None:
        return redirect("/login")
    db = db_of(request)
    company = request.state.company

    if twofactor.is_on(user):
        return render(request, "twofactor_setup.html", done=True)

    # The same key as last time while setup is in progress — see
    # ``twofactor.setup_secret`` for why that matters more than rotating it.
    secret = twofactor.setup_secret(user, restart=bool(request.query_params.get("again")))
    db.commit()
    return render(
        request,
        "twofactor_setup.html",
        secret=secret,
        typed_key=totp.grouped(secret),
        qr=twofactor.qr_svg(user, company, secret),
    )


@router.post("/account/two-factor")
def twofactor_confirm(request: Request, code: str = Form("")):
    from ..main import flash, render

    user = user_of(request)
    if user is None:
        return redirect("/login")
    db = db_of(request)
    company = request.state.company

    ok, codes = twofactor.confirm_setup(user, code)
    if not ok:
        reason = twofactor.why_it_failed(user, code)
        db.rollback()
        return render(
            request,
            "twofactor_setup.html",
            secret=user.totp_secret,
            typed_key=totp.grouped(user.totp_secret),
            qr=twofactor.qr_svg(user, company),
            error=reason or (
                "That code was not right. The likeliest reason is that your app is "
                "holding an older key — delete the Nexora Books entry in your "
                "authenticator app, scan the code on this page again, and type the "
                "six digits it shows straight away."
            ),
        )

    note = twofactor.clock_note(user)
    audit(db, user, "TWOFACTOR_ON", "User", user.id, ip=client_ip(request))
    db.commit()
    flash(request, "Two-factor sign-in is on for your account.")
    return render(request, "twofactor_codes.html", codes=codes, first_time=True,
                  clock_note=note)


@router.post("/account/two-factor/off")
def twofactor_off(request: Request, password: str = Form("")):
    """Turning it off needs the password, so a borrowed screen cannot do it."""
    from ..main import flash

    user = user_of(request)
    if user is None:
        return redirect("/login")
    db = db_of(request)

    if not security.verify_password(password, user.password_hash):
        flash(request, "Your password is not correct, so nothing was changed.", "danger")
        return redirect("/account")
    if twofactor.required_by_company(request.state.company):
        flash(request, "Your company requires two-factor sign-in, so it cannot be "
                       "turned off. Ask an administrator.", "danger")
        return redirect("/account")

    twofactor.turn_off(user)
    audit(db, user, "TWOFACTOR_OFF", "User", user.id, ip=client_ip(request))
    db.commit()
    flash(request, "Two-factor sign-in is off. Your password is now the only thing "
                   "protecting your books.", "warning")
    return redirect("/account")


@router.post("/account/two-factor/recovery")
def twofactor_new_codes(request: Request, password: str = Form("")):
    from ..main import flash, render

    user = user_of(request)
    if user is None:
        return redirect("/login")
    db = db_of(request)

    if not security.verify_password(password, user.password_hash):
        flash(request, "Your password is not correct, so nothing was changed.", "danger")
        return redirect("/account")
    if not twofactor.is_on(user):
        return redirect("/account")

    codes = twofactor.new_recovery_codes(user)
    audit(db, user, "TWOFACTOR_RECOVERY_NEW", "User", user.id, ip=client_ip(request))
    db.commit()
    flash(request, "New recovery codes. The old ones no longer work.", "warning")
    return render(request, "twofactor_codes.html", codes=codes, first_time=False)


# --------------------------------------------------------------------------
# Setting a first password from an invitation
# --------------------------------------------------------------------------
#
# These two routes are reachable without signing in, which is the whole point:
# the person has no password yet. They are safe to leave open because the token
# is the only thing that identifies anybody, it is checked against a stored
# hash, it works once, and it expires. A wrong token is told nothing beyond
# "this link is no good" — never whether an account exists behind it.


@router.get("/invite/{token}")
def invite_form(request: Request, token: str):
    from ..main import render

    db = db_of(request)
    user = invites.find(db, token)
    if user is None:
        return render(request, "invite.html", dead=True)
    return render(request, "invite.html", token=token, invited=user)


@router.post("/invite/{token}")
def invite_accept(
    request: Request,
    token: str,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    from ..main import flash, render

    db = db_of(request)
    user = invites.find(db, token)
    if user is None:
        return render(request, "invite.html", dead=True)

    def again(error: str):
        return render(request, "invite.html", token=token, invited=user,
                      error=error)

    if new_password != confirm_password:
        return again("The two passwords do not match.")
    problems = security.password_problems(new_password)
    if problems:
        return again("Your password needs " + ", ".join(problems) + ".")

    invites.accept(db, user, new_password)
    audit(db, user, "INVITE_ACCEPTED", "User", user.id,
          detail=user.username, ip=client_ip(request))
    db.commit()

    flash(request, f"Welcome, {user.display_name or user.username}. "
                   "Your password is set.")
    return _finish_login(request, db, user, "/")
