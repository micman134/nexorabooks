"""Selling it: a trial, a key that works on one computer, and a kind refusal.

Two things are being held to account here. That a licence cannot be forged by
the person holding the copy — otherwise there is no product to sell. And that a
customer whose licence has lapsed can still open, read, print, export and back
up every figure they ever entered — otherwise there is no product worth buying.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-lic-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import config, db as dbmod, licensing  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Account  # noqa: E402
from app.money import to_minor as M  # noqa: E402
from app.rsa_lite import generate, sign, verify  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services.posting import (  # noqa: E402
    EntryDraft,
    UnlicensedError,
    account_by_code,
    post_entry,
)

SELLER_KEY = Path(__file__).resolve().parent.parent / "seller" / "private-key.json"

# Issuing a licence needs the seller's private key, and that key is deliberately
# not in the folder customers receive — it is the one file that must never be
# shipped. So on a customer's machine these tests skip rather than fail, and on
# the seller's machine, where the key is present, they all run.
needs_key = pytest.mark.skipif(
    not SELLER_KEY.exists(),
    reason="no seller/private-key.json here — run make_licence_keys.py to issue licences",
)


@pytest.fixture()
def home():
    """A data folder of its own, so the trial and the licence start clean."""
    tmp = tempfile.mkdtemp(prefix="nexora-lic-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    yield Path(tmp)
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def issue(machine: str, *, name="Adeyemi Trading Ltd", expires: date | None = None,
          companies: int = 0, note: str = "") -> str:
    """Sign a licence the way issue_licence.py does."""
    key = json.loads(SELLER_KEY.read_text(encoding="utf-8"))
    payload = {
        "name": name, "machine": machine,
        "issued": date.today().isoformat(),
        "expires": expires.isoformat() if expires else None,
        "companies": companies, "edition": "Standard", "note": note,
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return licensing.build(payload, sign(message, int(key["n"]), int(key["d"])))


# --------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------


@needs_key
def test_the_shipped_public_key_matches_the_sellers_private_key():
    key = json.loads(SELLER_KEY.read_text(encoding="utf-8"))
    assert int(key["n"]) == licensing.PUBLIC_KEY_N
    assert key["e"] == licensing.PUBLIC_KEY_E


def test_a_signature_verifies_only_over_what_was_signed():
    n, e, d = generate(1024)
    signature = sign(b"one machine", n, d)
    assert verify(b"one machine", signature, n, e)
    assert not verify(b"another machine", signature, n, e)


@needs_key
def test_a_signature_from_a_different_key_is_refused():
    """The whole product rests on this: only the seller can issue licences."""
    other_n, _, other_d = generate(1024)
    payload = {"name": "Pirate Ltd", "machine": licensing.machine_code(),
               "issued": date.today().isoformat(), "expires": None,
               "companies": 0, "edition": "Standard", "note": ""}
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    forged = licensing.build(payload, sign(message, other_n, other_d))
    assert licensing.read(forged) is None


@needs_key
def test_changing_one_character_of_the_payload_breaks_it(home):
    good = issue(licensing.machine_code())
    assert licensing.read(good) is not None

    body, _, signature = "".join(good.split()).partition(".")
    tampered = ("A" if body[0] != "A" else "B") + body[1:] + "." + signature
    assert licensing.read(tampered) is None


def test_rubbish_is_refused_rather_than_crashing():
    for text in ("", "   ", "hello", "a.b", "....", "x" * 500, "eyJhIjoxfQ.zzz"):
        assert licensing.read(text) is None


@needs_key
def test_a_licence_survives_being_copied_out_of_an_email(home):
    """People paste with stray line breaks and spaces. That must not matter."""
    good = issue(licensing.machine_code())
    mangled = "  " + good.replace("\n", " \n\t ") + "\n\n"
    assert licensing.read(mangled) is not None


# --------------------------------------------------------------------------
# The machine
# --------------------------------------------------------------------------


def test_the_machine_code_is_stable_and_says_nothing_about_anybody():
    code = licensing.machine_code()
    assert code == licensing.machine_code()
    assert len(code) == 24 and code.count("-") == 4
    assert all(c in "0123456789ABCDEF-" for c in code)


@needs_key
def test_a_licence_for_another_computer_is_not_installed(home):
    other = issue("AAAA-BBBB-CCCC-DDDD-EEEE")
    assert licensing.read(other) is not None       # it is a real licence
    assert licensing.install(other) is None        # just not for this machine
    assert licensing.installed_text() == ""


@needs_key
def test_a_licence_carried_to_another_computer_says_so(home):
    (home / "licence.key").write_text(issue("AAAA-BBBB-CCCC-DDDD-EEEE"), encoding="utf-8")
    licensing.forget_cached()
    state = licensing.status()
    assert state.kind == licensing.WRONG_MACHINE
    assert not state.can_post
    assert "different computer" in state.headline


# --------------------------------------------------------------------------
# Trial, licence, expiry
# --------------------------------------------------------------------------


def test_a_fresh_installation_is_on_trial_and_fully_working(home):
    state = licensing.status()
    assert state.kind == licensing.TRIAL
    assert state.can_post
    assert state.days_left == licensing.TRIAL_DAYS


def test_the_trial_runs_out(home):
    licensing.trial_started()
    long_ago = date.today() - timedelta(days=licensing.TRIAL_DAYS + 1)
    (home / "started.txt").write_text(long_ago.isoformat(), encoding="utf-8")
    licensing.forget_cached()

    state = licensing.status()
    assert state.kind == licensing.TRIAL_OVER
    assert not state.can_post


def test_the_last_day_of_the_trial_still_works(home):
    (home / "started.txt").write_text(
        (date.today() - timedelta(days=licensing.TRIAL_DAYS)).isoformat())
    licensing.forget_cached()
    state = licensing.status()
    assert state.kind == licensing.TRIAL
    assert state.days_left == 0
    assert state.can_post


@needs_key
def test_a_licence_replaces_the_trial(home):
    (home / "started.txt").write_text(
        (date.today() - timedelta(days=999)).isoformat())
    licensing.forget_cached()
    assert not licensing.status().can_post

    licence = licensing.install(issue(licensing.machine_code(), name="Kwame Motors"))
    assert licence is not None
    state = licensing.status()
    assert state.kind == licensing.LICENSED
    assert state.can_post
    assert "Kwame Motors" in state.headline


@needs_key
def test_a_perpetual_licence_never_runs_out(home):
    licensing.install(issue(licensing.machine_code()))
    assert licensing.status().licence.perpetual


@needs_key
def test_a_yearly_licence_expires(home):
    licensing.install(issue(licensing.machine_code(),
                            expires=date.today() + timedelta(days=365)))
    assert licensing.status().can_post

    licensing.remove()
    (home / "licence.key").write_text(
        issue(licensing.machine_code(), expires=date.today() - timedelta(days=1)))
    licensing.forget_cached()

    state = licensing.status()
    assert state.kind == licensing.EXPIRED
    assert not state.can_post


@needs_key
def test_a_licence_expiring_today_is_still_good_today(home):
    (home / "licence.key").write_text(
        issue(licensing.machine_code(), expires=date.today()))
    licensing.forget_cached()
    assert licensing.status().can_post


@needs_key
def test_removing_a_licence_falls_back_to_the_trial(home):
    licensing.install(issue(licensing.machine_code()))
    assert licensing.status().is_licensed
    licensing.remove()
    assert licensing.status().kind == licensing.TRIAL


# --------------------------------------------------------------------------
# What a lapsed licence actually stops
# --------------------------------------------------------------------------


@pytest.fixture()
def books(home):
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
    with dbmod.session_scope_for(ref.slug) as db:
        yield db


def an_entry(db) -> EntryDraft:
    draft = EntryDraft(date=date.today(), memo="Office rent")
    draft.debit(account_by_code(db, "6100"), M("250,000"))
    draft.credit(account_by_code(db, "1020"), M("250,000"))
    return draft


def expire_the_trial(home):
    (home / "started.txt").write_text(
        (date.today() - timedelta(days=999)).isoformat())
    licensing.forget_cached()


def test_the_ledger_can_be_written_while_the_trial_lasts(books):
    entry = post_entry(books, an_entry(books))
    assert entry.total_debit == M("250,000")


def test_the_ledger_cannot_be_written_once_it_has_lapsed(books, home):
    expire_the_trial(home)
    with pytest.raises(UnlicensedError) as caught:
        post_entry(books, an_entry(books))
    assert "trial has finished" in str(caught.value)
    assert "Settings" in str(caught.value)


def test_the_refusal_says_the_books_are_untouched(books, home):
    expire_the_trial(home)
    with pytest.raises(UnlicensedError) as caught:
        post_entry(books, an_entry(books))
    message = str(caught.value)
    assert "backup" in message
    assert "export" in message


def test_everything_already_posted_is_still_readable(books, home):
    post_entry(books, an_entry(books))
    expire_the_trial(home)

    from app.services import reports as R

    rows, debits, credits = R.trial_balance(books, None, date(2099, 1, 1))
    assert debits == credits == M("250,000")
    assert R.balance_sheet(books, date(2099, 1, 1)).difference == 0
    assert books.scalar(select(Account.id).limit(1)) is not None


@needs_key
def test_a_licence_lets_the_work_carry_on_exactly_where_it_stopped(books, home):
    expire_the_trial(home)
    with pytest.raises(UnlicensedError):
        post_entry(books, an_entry(books))

    licensing.install(issue(licensing.machine_code()))
    entry = post_entry(books, an_entry(books))
    assert entry.total_debit == M("250,000")


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123", "next": "/"},
               follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def test_the_licence_screen_shows_this_computers_code(client):
    page = client.get("/settings/licence", follow_redirects=True).text
    assert licensing.machine_code() in page
    assert "Trial" in page


@needs_key
def test_a_licence_can_be_pasted_in(client):
    r = client.post("/settings/licence",
                    data={"licence": issue(licensing.machine_code(), name="Kwame Motors")},
                    follow_redirects=True)
    assert r.status_code == 200
    assert "Licensed to Kwame Motors" in r.text
    assert licensing.status().is_licensed


def test_pasting_nonsense_says_so_without_breaking_anything(client):
    r = client.post("/settings/licence", data={"licence": "not a licence"},
                    follow_redirects=True)
    assert "not a licence this software recognises" in r.text
    assert licensing.status().kind == licensing.TRIAL


@needs_key
def test_pasting_somebody_elses_licence_names_both_computers(client):
    r = client.post("/settings/licence",
                    data={"licence": issue("AAAA-BBBB-CCCC-DDDD-EEEE")},
                    follow_redirects=True)
    assert "AAAA-BBBB-CCCC-DDDD-EEEE" in r.text
    assert licensing.machine_code() in r.text
    assert not licensing.status().is_licensed


def test_the_lapsed_banner_appears_on_every_page(client, home):
    expire_the_trial(home)
    for url in ("/", "/reports", "/settings"):
        page = client.get(url, follow_redirects=True).text
        assert "trial has finished" in page, url


def test_the_reports_still_open_when_it_has_lapsed(client, home):
    expire_the_trial(home)
    for url in ("/reports/trial-balance", "/reports/balance-sheet",
                "/settings/backup", "/accounts"):
        r = client.get(url, follow_redirects=True)
        assert r.status_code == 200, url
        assert "Internal Server Error" not in r.text


def test_trying_to_post_when_it_has_lapsed_is_refused_kindly(client, home):
    expire_the_trial(home)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        rent = account_by_code(db, "6100").id
        bank = account_by_code(db, "1020").id

    r = client.post("/journals/save", data={
        "date": date.today().isoformat(), "memo": "Rent", "reference": "R1",
        "line_account": [str(rent), str(bank)],
        "line_debit": ["250,000", ""],
        "line_credit": ["", "250,000"],
        "line_memo": ["", ""], "line_contact": ["", ""], "line_tax": ["", ""],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text
    assert "licence" in r.text.lower()


def test_a_backup_can_still_be_taken_when_it_has_lapsed(client, home):
    """The one thing a lapsed customer must always be able to do."""
    expire_the_trial(home)
    r = client.post("/settings/backup", follow_redirects=True)
    assert r.status_code == 200
    assert "Internal Server Error" not in r.text
