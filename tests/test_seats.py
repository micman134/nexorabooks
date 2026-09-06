"""Buying a licence for a number of people, and what that number then means.

Two things are being protected here and they pull against each other.

A licence has to mean something, or there is no business. If five users are
paid for, a sixth cannot simply be added.

And nobody may ever be shut out of their own bookkeeping over money. A licence
that expires, or one entered with fewer users than the company already has, must
never sign anybody out, switch anybody off, or hide anything. It stops *new*
accounts and it stops *new* ledger entries. That is the whole of its power.

The tests below hold both of those at once, which is the only way either is
worth having.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-seats-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod, licensing, store  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company, User  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.services import seats  # noqa: E402

PRIVATE_KEY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "seller", "private-key.json")
needs_key = pytest.mark.skipif(
    not os.path.exists(PRIVATE_KEY),
    reason="no seller private key here — licences cannot be signed in this copy")


# --------------------------------------------------------------------------
# What a price is
# --------------------------------------------------------------------------


def test_one_user_costs_the_small_band_rate():
    q = store.quote(1)
    assert q.users == 1
    assert q.per_user == store.BANDS[0].per_user
    assert q.total == store.BANDS[0].per_user


def test_the_rate_drops_as_the_company_grows():
    rates = [store.quote(n).per_user for n in (2, 6, 15)]
    assert rates[0] > rates[1] > rates[2], rates


def test_the_band_rate_applies_to_everybody_not_just_the_ones_above_the_line():
    """Simple enough to say on the phone, and cheaper for the customer."""
    q = store.quote(6)
    assert q.total == q.per_user * 6


def test_the_price_never_falls_as_the_company_grows():
    """Nine users must never cost more than ten. See Quote.bumped."""
    totals = [store.quote(n).total for n in range(1, store.TALK_TO_US_ABOVE + 1)]
    assert totals == sorted(totals), totals


def test_nobody_is_ever_charged_more_than_a_bigger_licence_would_cost():
    for n in range(1, store.TALK_TO_US_ABOVE + 1):
        mine = store.quote(n)
        for bigger in range(n, store.TALK_TO_US_ABOVE + 1):
            assert mine.total <= store.quote(bigger).total
        assert mine.users >= n, "a quote must cover at least what was asked for"


def test_at_an_awkward_seam_they_are_given_the_larger_licence(monkeypatch):
    """Charging more for less is the kind of thing a customer never forgives."""
    bumped = [n for n in range(1, store.TALK_TO_US_ABOVE + 1) if store.quote(n).bumped]
    for n in bumped:
        q = store.quote(n)
        assert q.users > n
        assert q.total < store.BANDS[0].per_user * 10 ** 6   # sane
        assert q.total <= _straight_total(n)


def _straight_total(users: int) -> int:
    band = store.band_for(users)
    return band.per_user * users if band else 0


def test_asking_for_none_or_nonsense_still_quotes_the_minimum():
    for asked in (0, -4, None):
        assert store.quote(asked).users == store.MINIMUM_USERS


def test_a_very_large_company_is_sent_to_a_human():
    q = store.quote(store.TALK_TO_US_ABOVE + 1)
    assert q.too_big and not q.is_quotable


def test_money_is_written_the_way_a_person_writes_it():
    assert store.money(45_000_00) == f"{store.SYMBOL}45,000.00"
    assert store.money(0) == f"{store.SYMBOL}0.00"


# --------------------------------------------------------------------------
# Counting people
# --------------------------------------------------------------------------


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-seats-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    licensing.forget_cached()
    licensing.remove()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
        db.get(Company, 1).setup_complete = True
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    licensing.forget_cached()
    shutil.rmtree(tmp, ignore_errors=True)


def add_user(slug: str, username: str, active: bool = True) -> None:
    dbmod.init_db(slug)
    with dbmod.session_scope_for(slug) as db:
        db.add(User(username=username, full_name=username.title(), role="clerk",
                    is_active=active, password_hash=hash_password("Lagos2026")))
        db.commit()


def test_a_fresh_installation_has_one_person_on_it(home):
    assert seats.usage().used == 1                    # admin


def test_switched_off_accounts_do_not_use_a_place(home):
    """Somebody who has left keeps their name on the audit trail, free."""
    slug = registry.default_slug()
    add_user(slug, "ngozi")
    add_user(slug, "chinedu", active=False)
    assert seats.usage().used == 2                    # admin + ngozi
    assert "chinedu" not in seats.usage().names


def test_one_person_keeping_two_companies_books_is_one_person(home):
    """Charging a bookkeeper twice for being trusted twice is indefensible."""
    first = registry.default_slug()
    second = registry.create("Second Company Ltd")
    dbmod.init_db(second.slug)
    with dbmod.session_scope_for(second.slug) as db:
        bootstrap(db)
        db.commit()
    add_user(first, "ngozi")
    add_user(second.slug, "ngozi")

    counted = seats.usage()
    assert counted.names.count("ngozi") == 1
    assert counted.used == 2                          # admin + ngozi, not three


def test_a_trial_has_no_limit(home):
    """A trial run by one lonely person answers a question nobody asked."""
    assert seats.usage().unlimited
    assert seats.room_for("anybody")


# --------------------------------------------------------------------------
# What the limit does — and does not do
# --------------------------------------------------------------------------


def a_licence(users: int, days: int | None = 365, machine: str | None = None) -> str:
    import json

    from app.rsa_lite import sign

    key = json.loads(open(PRIVATE_KEY).read())
    payload = {
        "name": "Procert Academy Limited",
        "machine": machine or licensing.machine_code(),
        "issued": date.today().isoformat(),
        "expires": (date.today() + timedelta(days=days)).isoformat() if days else None,
        "companies": 0,
        "users": users,
        "edition": "Standard",
        "note": "test",
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return licensing.build(payload, sign(message, int(key["n"]), int(key["d"])))


@needs_key
def test_a_licence_carries_the_number_of_users_it_was_bought_for(home):
    licensing.install(a_licence(5))
    licensing.forget_cached()
    assert licensing.status().licence.users == 5
    assert seats.usage().allowed == 5


@needs_key
def test_an_older_licence_with_no_users_in_it_is_not_suddenly_limited(home):
    """Somebody who paid before seats existed must not be limited to nothing."""
    import json

    from app.rsa_lite import sign

    key = json.loads(open(PRIVATE_KEY).read())
    payload = {                                        # note: no "users" key
        "name": "Early Customer Ltd", "machine": licensing.machine_code(),
        "issued": date.today().isoformat(),
        "expires": (date.today() + timedelta(days=365)).isoformat(),
        "companies": 0, "edition": "Standard", "note": "",
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    licensing.install(licensing.build(payload, sign(message, int(key["n"]), int(key["d"]))))
    licensing.forget_cached()

    assert licensing.status().licence.unlimited_users
    assert seats.usage().unlimited
    assert seats.room_for("anybody at all")


@needs_key
def test_the_last_place_can_be_filled_and_the_next_one_cannot(home):
    licensing.install(a_licence(2))
    licensing.forget_cached()
    assert seats.room_for("ngozi")                     # admin + one more
    add_user(registry.default_slug(), "ngozi")
    assert not seats.room_for("chinedu")
    assert "not added" in seats.refusal("chinedu")
    assert "Settings" in seats.refusal("chinedu")      # and where to fix it


@needs_key
def test_somebody_who_already_has_an_account_is_not_refused_a_second_one(home):
    """They are already occupying their place; a second company is not a second person."""
    licensing.install(a_licence(2))
    licensing.forget_cached()
    add_user(registry.default_slug(), "ngozi")
    assert not seats.room_for("chinedu")
    assert seats.room_for("ngozi")


@needs_key
def test_a_smaller_licence_never_switches_anybody_off(home):
    """The whole point. Entering a licence must not lock a bookkeeper out."""
    slug = registry.default_slug()
    for name in ("ngozi", "chinedu", "bola"):
        add_user(slug, name)
    licensing.install(a_licence(2))                    # four people, two users
    licensing.forget_cached()

    counted = seats.usage()
    assert counted.used == 4 and counted.allowed == 2 and counted.over == 2

    with dbmod.session_scope_for(slug) as db:
        still_on = db.scalars(select(User).where(User.is_active.is_(True))).all()
    assert len(still_on) == 4, "nobody may be switched off by a licence"


@needs_key
def test_being_over_the_limit_does_not_stop_the_books_working(home):
    """Over on users is a conversation about money, not a reason to stop work."""
    slug = registry.default_slug()
    for name in ("ngozi", "chinedu", "bola"):
        add_user(slug, name)
    licensing.install(a_licence(1))
    licensing.forget_cached()
    assert licensing.status().can_post is True


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        yield c


def test_the_licence_screen_shows_a_price_and_how_to_pay(client):
    page = client.get("/settings/licence", follow_redirects=True).text
    assert "Buy a licence" in page or "Renew your licence" in page
    assert store.money(store.BANDS[0].per_user) in page
    assert licensing.machine_code() in page
    assert "How to pay" in page


def test_the_screen_quotes_the_number_of_users_asked_for(client):
    page = client.get("/settings/licence?users=6", follow_redirects=True).text
    expected = store.quote(6)
    assert store.money(expected.total) in page
    assert "6 users" in page


def test_a_very_large_request_offers_a_conversation_rather_than_a_number(client):
    page = client.get(f"/settings/licence?users={store.TALK_TO_US_ABOVE + 5}",
                      follow_redirects=True).text
    assert "worth a" in page and "conversation" in page


def test_the_seller_is_warned_that_the_prices_are_not_theirs_yet(client):
    """This box exists so the mistake is found by the seller, not the customer."""
    page = client.get("/settings/licence", follow_redirects=True).text
    if store.PRICES_ARE_EXAMPLES:
        assert "these are not your prices yet" in page.lower()
        assert "store.py" in page


@needs_key
def test_adding_one_person_too_many_is_refused_with_a_way_forward(client):
    licensing.install(a_licence(2))
    licensing.forget_cached()
    client.post("/settings/users/save", data={
        "username": "ngozi", "full_name": "Ngozi Eze", "role": "clerk",
        "is_active": "on"}, follow_redirects=True)

    page = client.post("/settings/users/save", data={
        "username": "chinedu", "full_name": "Chinedu Okafor", "role": "clerk",
        "is_active": "on"}, follow_redirects=True)

    assert "was not added" in page.text
    assert "Settings" in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        assert db.scalar(select(User).where(User.username == "chinedu")) is None


@needs_key
def test_editing_somebody_who_is_already_signed_in_is_never_blocked(client):
    """Being at the limit must not stop a name or a role being corrected."""
    licensing.install(a_licence(2))
    licensing.forget_cached()
    client.post("/settings/users/save", data={
        "username": "ngozi", "full_name": "Ngozi Eze", "role": "clerk",
        "is_active": "on"}, follow_redirects=True)

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        uid = db.scalar(select(User).where(User.username == "ngozi")).id

    page = client.post("/settings/users/save", data={
        "id": str(uid), "username": "ngozi", "full_name": "Ngozi Adeyemi",
        "role": "accountant", "is_active": "on"}, follow_redirects=True)
    assert "was not added" not in page.text
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        changed = db.scalar(select(User).where(User.username == "ngozi"))
        assert changed.full_name == "Ngozi Adeyemi" and changed.role == "accountant"


@needs_key
def test_switching_somebody_off_frees_their_place(client):
    licensing.install(a_licence(2))
    licensing.forget_cached()
    client.post("/settings/users/save", data={
        "username": "ngozi", "role": "clerk", "is_active": "on"},
        follow_redirects=True)
    assert not seats.room_for("chinedu")

    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        uid = db.scalar(select(User).where(User.username == "ngozi")).id
    client.post("/settings/users/save", data={
        "id": str(uid), "username": "ngozi", "role": "clerk"},   # is_active absent
        follow_redirects=True)

    assert seats.room_for("chinedu")


@needs_key
def test_the_screen_says_plainly_when_there_are_more_people_than_users(client):
    for name in ("ngozi", "chinedu", "bola"):
        add_user(dbmod.current_slug(), name)
    licensing.install(a_licence(2))
    licensing.forget_cached()

    page = client.get("/settings/licence", follow_redirects=True).text
    assert "Nobody has been signed out" in page
    assert "4 of 2 users" in page


@needs_key
def test_a_licence_about_to_run_out_says_so_on_every_screen(client):
    licensing.install(a_licence(5, days=10))
    licensing.forget_cached()
    page = client.get("/", follow_redirects=True).text
    assert "Your licence ends in" in page
    assert "Renew it now" in page


@needs_key
def test_a_licence_with_a_year_to_run_does_not_nag(client):
    licensing.install(a_licence(5, days=300))
    licensing.forget_cached()
    assert "Your licence ends in" not in client.get("/", follow_redirects=True).text


@needs_key
def test_an_expired_licence_points_at_the_renewal_screen(client):
    licensing.install(a_licence(5, days=-2))
    licensing.forget_cached()
    page = client.get("/", follow_redirects=True).text
    assert "Renew it" in page
    assert "/settings/licence" in page


@needs_key
def test_the_renewal_screen_starts_from_what_they_already_bought(client):
    """Nobody renewing should have to work out their own user count again."""
    licensing.install(a_licence(7))
    licensing.forget_cached()
    page = client.get("/settings/licence", follow_redirects=True).text
    assert "Renew your licence" in page
    assert store.money(store.quote(7).total) in page
