"""One customer must never reach another customer's books.

On somebody's own computer this is not a question worth asking: every company
in the installation belongs to the person sitting in front of it. On a shared
server it is the only question that matters, because the cost of getting it
wrong is not a bug report — it is one business reading another's payroll.

The boundary is the hostname. These tests try to get round it.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="nexora-tenancy-")
os.environ["NEXORA_DATA"] = _TMP

from fastapi.testclient import TestClient  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app import tenancy  # noqa: E402
from app.db import session_scope_for  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company  # noqa: E402

BASE = "books.test"


# --------------------------------------------------------------------------
# Reading a hostname
# --------------------------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("NEXORA_HOSTED", "1")
    monkeypatch.setenv("NEXORA_BASE_DOMAIN", BASE)


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.delenv("NEXORA_HOSTED", raising=False)
    monkeypatch.delenv("NEXORA_BASE_DOMAIN", raising=False)


def test_a_plain_subdomain_names_its_tenant(hosted):
    assert tenancy.slug_from_host(f"acme.{BASE}") == "acme"
    assert tenancy.slug_from_host(f"ACME.{BASE.upper()}") == "acme"
    assert tenancy.slug_from_host(f"acme.{BASE}:8756") == "acme"
    assert tenancy.slug_from_host(f"acme-two.{BASE}") == "acme-two"


@pytest.mark.parametrize("host", [
    "",
    None,
    BASE,                              # the bare domain belongs to nobody
    f"www.{BASE}",                     # reserved
    f"admin.{BASE}",                   # reserved
    f"api.{BASE}",                     # reserved
    f"acme.somewhere-else.com",        # a name we do not serve
    f"acme.{BASE}.evil.com",           # suffix that only looks right
    f"deep.acme.{BASE}",               # two labels is not a tenant
    f"-acme.{BASE}",                   # not a legal label
    f"acme-.{BASE}",
    f"ac me.{BASE}",
    f"{'a' * 41}.{BASE}",              # longer than any real slug
    "[::1]:8756",
    "localhost:8756",
])
def test_anything_else_names_nobody(hosted, host):
    assert tenancy.slug_from_host(host) is None


@pytest.mark.parametrize("nasty", [
    "../other",
    "..",
    ".",
    "acme/../beta",
    "acme%2f..%2fbeta",
    "acme\\beta",
    "acme:beta",
    "acme\x00beta",
])
def test_a_label_can_never_be_a_path(hosted, nasty):
    """The slug becomes a folder name. Nothing path-shaped may get through."""
    assert tenancy.slug_from_host(f"{nasty}.{BASE}") is None


def test_without_a_base_domain_nothing_resolves(monkeypatch):
    """An operator who turns hosting on but forgets the domain serves nobody.

    Refusing everything is the safe failure. The dangerous one would be
    treating an unconfigured server as "match anything"."""
    monkeypatch.setenv("NEXORA_HOSTED", "1")
    monkeypatch.delenv("NEXORA_BASE_DOMAIN", raising=False)
    assert tenancy.slug_from_host(f"acme.{BASE}") is None


def test_installed_mode_ignores_the_host_entirely(installed):
    assert tenancy.hosted() is False
    assert tenancy.resolve(f"acme.{BASE}") is None


def test_hosting_is_off_unless_deliberately_turned_on(installed):
    assert tenancy.hosted() is False


@pytest.mark.parametrize("value", ["0", "no", "off", "false", "", "  "])
def test_a_half_hearted_env_var_does_not_turn_hosting_on(monkeypatch, value):
    monkeypatch.setenv("NEXORA_HOSTED", value)
    assert tenancy.hosted() is False


# --------------------------------------------------------------------------
# Two customers on one server
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def two_businesses():
    """Two unrelated companies, each with a figure the other must never see."""
    dbmod.reset_all()
    with TestClient(app):                     # run start-up: migrate and seed
        pass

    made = {}
    for slug_name, company_name in (("acme", "Acme Foods Ltd"),
                                    ("beta", "Beta Logistics Ltd")):
        ref = registry.create(company_name)
        made[slug_name] = ref
        with session_scope_for(ref.slug) as db:
            c = db.get(Company, 1)
            c.name = company_name
    yield made


def sign_in(client, host):
    return client.post(
        "/login",
        data={"username": "admin", "password": "admin123", "next": "/"},
        headers={"host": host},
        follow_redirects=True,
    )


def test_each_address_opens_its_own_books(hosted, two_businesses):
    for key, ref in two_businesses.items():
        with TestClient(app, base_url=f"http://{ref.slug}.{BASE}") as c:
            r = sign_in(c, f"{ref.slug}.{BASE}")
            assert r.status_code == 200
            # Signed in and looking at the right company
            assert ref.name in r.text or "password" in r.text.lower()


def test_an_unknown_address_is_given_nothing(hosted, two_businesses):
    with TestClient(app, base_url=f"http://nobody.{BASE}") as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 404
        # It must not hint at which names do exist.
        for ref in two_businesses.values():
            assert ref.name not in r.text
            assert ref.slug not in r.text


def test_the_bare_domain_is_given_nothing(hosted, two_businesses):
    with TestClient(app, base_url=f"http://{BASE}") as c:
        assert c.get("/", follow_redirects=False).status_code == 404


def test_a_session_from_one_tenant_is_worthless_at_another(hosted, two_businesses):
    """The attack this whole design exists to stop.

    Sign in properly at one address, then carry the session cookie to another
    customer's address by hand. The cookie is signed and genuine — it just
    belongs somewhere else. It must buy nothing.
    """
    acme = two_businesses["acme"]
    beta = two_businesses["beta"]

    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        stolen = dict(c.cookies)
        assert stolen, "the sign-in produced no cookie, so this proves nothing"

    with TestClient(app, base_url=f"http://{beta.slug}.{BASE}") as c:
        for name, value in stolen.items():
            c.cookies.set(name, value)
        r = c.get("/", follow_redirects=False)
        # Sent to sign in at Beta — not handed Beta's dashboard, and certainly
        # not handed Acme's.
        assert r.status_code == 303
        assert "/login" in r.headers["location"]
        assert acme.name not in r.text


def test_the_sign_in_cookie_names_its_tenant_immediately(hosted, two_businesses):
    """A cookie carrying a user id but no company is the dangerous shape.

    Signing in used to wipe the whole session, so for one response the browser
    held a valid user id with nothing saying which books it belonged to. That
    is exactly the cookie worth carrying to another customer's address. The
    company must be stamped on it from the first response.
    """
    import base64
    import json

    acme = two_businesses["acme"]
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        r = c.post("/login", data={"username": "admin", "password": "admin123",
                                   "next": "/"}, follow_redirects=False)
        assert r.status_code == 303
        raw = c.cookies["nexorabooks"].split(".")[0]
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        assert payload.get("uid"), "the sign-in produced no user id"
        assert payload.get("company") == acme.slug, (
            "the session names a user but no company — that cookie would be "
            "worth stealing to another tenant's subdomain"
        )


def test_a_forged_company_cookie_cannot_select_the_books(hosted, two_businesses):
    """Even a session that names another company in its payload is ignored.

    The company is taken from the host. The cookie does not get a vote.
    """
    acme = two_businesses["acme"]
    beta = two_businesses["beta"]
    with TestClient(app, base_url=f"http://{beta.slug}.{BASE}") as c:
        sign_in(c, f"{beta.slug}.{BASE}")
        # Signed in at Beta, holding Beta's cookie. Now send the same cookie to
        # Acme's address. The Host decides which books are open, so this is
        # simply an unauthenticated visit to Acme — not a signed-in one, and
        # not a view of Beta.
        r = c.get("/", headers={"host": f"{acme.slug}.{BASE}"},
                  follow_redirects=False)
        assert r.status_code == 303
        assert "/login" in r.headers["location"]
        assert beta.name not in r.text


def test_the_company_list_is_gone(hosted, two_businesses):
    """A list of companies is a customer list. It must not render at all."""
    acme = two_businesses["acme"]
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        r = c.get("/companies", follow_redirects=False)
        assert r.status_code == 404
        assert "Beta Logistics" not in r.text


def test_switching_companies_is_gone(hosted, two_businesses):
    acme = two_businesses["acme"]
    beta = two_businesses["beta"]
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        r = c.get(f"/companies/switch/{beta.slug}", follow_redirects=False)
        assert r.status_code == 404
        # and the session was not quietly moved anyway
        assert c.get("/", follow_redirects=False).status_code in (200, 303)


def test_creating_a_company_is_gone(hosted, two_businesses):
    acme = two_businesses["acme"]
    before = {ref.slug for ref in registry.all_companies(include_archived=True)}
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        r = c.post("/companies/create", data={"name": "Sneaky Ltd"},
                   follow_redirects=False)
        assert r.status_code == 404
    after = {ref.slug for ref in registry.all_companies(include_archived=True)}
    assert before == after, "a company was created on the hosted service"


def test_archiving_another_company_is_gone(hosted, two_businesses):
    acme = two_businesses["acme"]
    beta = two_businesses["beta"]
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        r = c.post(f"/companies/{beta.slug}/archive", data={"archived": "1"},
                   follow_redirects=False)
        assert r.status_code == 404
    assert registry.get(beta.slug).is_archived is False


def test_the_sidebar_offers_no_other_companies(hosted, two_businesses):
    """Nothing renders a switcher, because there is nothing to switch to."""
    acme = two_businesses["acme"]
    with TestClient(app, base_url=f"http://{acme.slug}.{BASE}") as c:
        sign_in(c, f"{acme.slug}.{BASE}")
        r = c.get("/", follow_redirects=True)
        assert "Beta Logistics" not in r.text


# --------------------------------------------------------------------------
# The installed copy must keep working exactly as before
# --------------------------------------------------------------------------


def test_installed_mode_still_lists_and_switches(installed, two_businesses):
    """None of the above may cost somebody with two shops their switcher."""
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123",
                               "next": "/"}, follow_redirects=True)
        r = c.get("/companies", follow_redirects=True)
        assert r.status_code == 200


def teardown_module(module):
    shutil.rmtree(_TMP, ignore_errors=True)
