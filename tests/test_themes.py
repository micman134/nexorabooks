"""Letting people choose what the software looks like.

The interesting failures here are not cosmetic. A theme that leaves one colour
undefined produces a white-on-white screen somebody cannot read; a theme stored
per company rather than per person means one member of staff choosing dark mode
imposes it on everybody. Both are checked.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-theme-")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod, themes  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company, User  # noqa: E402
from app.seed import bootstrap  # noqa: E402


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-theme-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# The themes themselves
# --------------------------------------------------------------------------


def test_there_is_a_real_choice_not_three_shades_of_one_colour():
    assert len(themes.THEMES) >= 8
    assert len(themes.dark()) >= 2
    accents = {t.tokens["accent"] for t in themes.THEMES}
    assert len(accents) == len(themes.THEMES), "two themes share an accent colour"


@pytest.mark.parametrize("theme", themes.THEMES, ids=lambda t: t.key)
def test_every_theme_defines_every_token(theme):
    """One missing token is white text on a white card for whoever picks it."""
    complete = set(themes.get("ledger").tokens)
    missing = complete - set(theme.tokens)
    assert not missing, f"{theme.key} is missing {sorted(missing)}"


@pytest.mark.parametrize("theme", themes.THEMES, ids=lambda t: t.key)
def test_every_colour_is_a_colour(theme):
    for name, value in theme.tokens.items():
        assert re.fullmatch(r"#[0-9a-fA-F]{3,8}", value), f"{theme.key}.{name} = {value}"


@pytest.mark.parametrize("theme", themes.THEMES, ids=lambda t: t.key)
def test_text_is_not_the_same_colour_as_what_it_sits_on(theme):
    """The cheapest possible readability check, and it catches the worst bug."""
    for ink, ground in (("ink", "card"), ("ink", "bg"), ("side-text", "side-bg"),
                        ("ink-soft", "card")):
        assert theme.tokens[ink].lower() != theme.tokens[ground].lower(), \
            f"{theme.key}: {ink} matches {ground}"


@pytest.mark.parametrize("theme", themes.dark(), ids=lambda t: t.key)
def test_a_dark_theme_is_actually_dark(theme):
    def brightness(value):
        r, g, b = themes.rgb(value)
        return 0.299 * r + 0.587 * g + 0.114 * b

    assert brightness(theme.tokens["bg"]) < 0.35
    assert brightness(theme.tokens["ink"]) > 0.6


@pytest.mark.parametrize("theme", themes.light(), ids=lambda t: t.key)
def test_a_light_theme_is_actually_light(theme):
    def brightness(value):
        r, g, b = themes.rgb(value)
        return 0.299 * r + 0.587 * g + 0.114 * b

    assert brightness(theme.tokens["bg"]) > 0.8
    assert brightness(theme.tokens["ink"]) < 0.4


def test_an_unknown_theme_falls_back_rather_than_breaking():
    assert themes.get("chartreuse").key == themes.DEFAULT
    assert themes.get(None).key == themes.DEFAULT
    assert themes.get("").key == themes.DEFAULT


def test_the_stylesheet_carries_every_theme():
    css = themes.stylesheet()
    for theme in themes.THEMES:
        assert f'[data-theme="{theme.key}"]' in css
        assert theme.tokens["accent"] in css


def test_a_colour_converts_to_what_a_pdf_wants():
    assert themes.rgb("#000000") == (0.0, 0.0, 0.0)
    assert themes.rgb("#ffffff") == (1.0, 1.0, 1.0)
    r, g, b = themes.rgb("#0b6b3a")
    assert 0 < r < 0.1 and 0.3 < g < 0.5


def test_nonsense_instead_of_a_colour_does_not_crash():
    assert themes.rgb("not a colour") == (0.0, 0.0, 0.0)
    assert themes.rgb("") == (0.0, 0.0, 0.0)


# --------------------------------------------------------------------------
# Whose choice wins
# --------------------------------------------------------------------------


def test_with_nobody_choosing_anything_the_default_is_used():
    assert themes.resolve(None, None) == themes.DEFAULT


def test_the_company_sets_what_everybody_starts_with():
    class Co:
        theme = "ocean"

    assert themes.resolve(None, Co()) == "ocean"


def test_a_persons_own_choice_beats_the_companys():
    class Co:
        theme = "ocean"

    class Person:
        theme = "midnight"

    assert themes.resolve(Person(), Co()) == "midnight"


def test_a_person_who_has_chosen_nothing_follows_the_company():
    class Co:
        theme = "bronze"

    class Person:
        theme = ""

    assert themes.resolve(Person(), Co()) == "bronze"


def test_a_theme_that_no_longer_exists_is_ignored_rather_than_obeyed():
    class Person:
        theme = "a-theme-from-an-older-version"

    assert themes.resolve(Person(), None) == themes.DEFAULT


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123",
                               "next": "/"}, follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def test_the_stylesheet_is_served(client):
    r = client.get("/themes.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert '[data-theme="midnight"]' in r.text


def test_it_can_be_fetched_without_signing_in():
    """A stylesheet behind a login makes the sign-in page unstyled."""
    dbmod.reset_all()
    with TestClient(app) as anon:
        assert anon.get("/themes.css").status_code == 200


def test_the_page_says_which_theme_it_is_drawn_in(client):
    assert 'data-theme="ledger"' in client.get("/", follow_redirects=True).text


def test_the_picker_offers_every_theme(client):
    page = client.get("/account", follow_redirects=True).text
    for theme in themes.THEMES:
        assert theme.name in page, theme.key


def test_choosing_one_changes_every_page(client):
    client.post("/account/theme", data={"theme": "midnight"}, follow_redirects=True)
    for url in ("/", "/settings", "/sales/invoices"):
        assert 'data-theme="midnight"' in client.get(url, follow_redirects=True).text


def test_the_choice_is_remembered(client):
    client.post("/account/theme", data={"theme": "plum"}, follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).theme == "plum"


def test_it_can_be_handed_back_to_the_company(client):
    client.post("/account/theme", data={"theme": "plum"}, follow_redirects=True)
    r = client.post("/account/theme", data={"theme": ""}, follow_redirects=True)
    assert "company" in r.text.lower()
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).theme == ""


def test_a_made_up_theme_is_refused_rather_than_stored(client):
    client.post("/account/theme", data={"theme": "../../etc/passwd"},
                follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.scalar(select(User).where(User.username == "admin")).theme == ""
    assert 'data-theme="ledger"' in client.get("/", follow_redirects=True).text


def test_the_admin_can_set_what_the_office_looks_like(client):
    client.post("/settings/company", data={
        "name": "Adeyemi Building Materials Ltd", "theme": "teal",
        "currency_code": "NGN", "fiscal_year_start_month": "1",
    }, follow_redirects=True)
    with dbmod.session_scope_for(registry.default_slug()) as db:
        assert db.get(Company, 1).theme == "teal"
    assert 'data-theme="teal"' in client.get("/", follow_redirects=True).text


def test_one_persons_dark_mode_is_not_forced_on_everybody(client):
    """Two people share the network and the books. Not each other's eyesight."""
    client.post("/settings/company", data={
        "name": "Adeyemi Building Materials Ltd", "theme": "ocean",
        "currency_code": "NGN", "fiscal_year_start_month": "1",
    }, follow_redirects=True)
    client.post("/account/theme", data={"theme": "midnight"}, follow_redirects=True)

    with dbmod.session_scope_for(registry.default_slug()) as db:
        company = db.get(Company, 1)
        other = User(username="chioma", full_name="Chioma", role="ENTRY",
                     password_hash="x")
        db.add(other)
        db.flush()
        assert themes.resolve(other, company) == "ocean"
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert themes.resolve(admin, company) == "midnight"


def test_the_sign_in_page_is_styled_too(client):
    dbmod.reset_all()
    with TestClient(app) as anon:
        page = anon.get("/login").text
        assert "/themes.css" in page
        assert "data-theme=" in page


def test_documents_follow_the_company_not_the_person(home):
    """An invoice is the business writing to its customer. It should not change
    colour depending on who pressed the button."""
    class Co:
        theme = "burgundy"

    assert themes.accent_rgb(Co()) == themes.rgb(
        themes.get("burgundy").tokens["accent-dark"])
    assert themes.accent_rgb(None) == themes.rgb(
        themes.get(themes.DEFAULT).tokens["accent-dark"])


# --------------------------------------------------------------------------
# "Good" is a meaning, not a brand colour
# --------------------------------------------------------------------------


def test_every_theme_says_what_good_looks_like():
    """A figure that helped profit must read as good in all eleven themes.

    Borrowing --accent would show it in red under Burgundy and in blue under
    Ocean, which is exactly backwards, so each theme carries its own.
    """
    for theme in themes.THEMES:
        assert theme.tokens.get("good"), f"{theme.key} has no 'good' colour"


def test_good_is_never_the_same_as_danger():
    for theme in themes.THEMES:
        assert theme.tokens["good"] != theme.tokens["danger"], theme.key


def test_dark_themes_use_a_light_green_and_light_ones_a_dark_green():
    """Contrast against the page, not a fixed hue."""
    for theme in themes.THEMES:
        r, g, b = themes.rgb(theme.tokens["good"])
        brightness = (r + g + b) / 3
        if theme.mode == themes.DARK:
            assert brightness > 0.4, f"{theme.key}'s good colour is too dark to read"
        else:
            assert brightness < 0.5, f"{theme.key}'s good colour is too pale to read"


def test_the_stylesheet_has_a_fallback_good_colour():
    """If /themes.css never loads, the page must still not be unstyled."""
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.css").read_text(encoding="utf-8")
    root = css.split("}", 1)[0]
    assert "--good:" in root
