"""Which address goes into a link somebody else is going to click.

This file exists because of a bug that wasted several people's morning. An
administrator sat at the computer running Nexora Books, opened it at
``http://127.0.0.1:8756``, and invited three members of staff. The invitations
arrived. Everyone clicked. Everyone got "Hmmm… can't reach this page —
127.0.0.1 refused to connect", because 127.0.0.1 means *the computer you are
sitting at*, and the computers they were sitting at were not running anything.

The links were valid. The tokens were valid. The address was the problem, and
nothing in the software had noticed that the address in one person's browser
is not an address to hand to anybody else.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

os.environ.setdefault("NEXORA_DATA", tempfile.mkdtemp(prefix="nexora-net-"))

from app import companies as registry  # noqa: E402
from app import config, db as dbmod, network  # noqa: E402
from app.models import Company  # noqa: E402
from app.seed import bootstrap  # noqa: E402


# --------------------------------------------------------------------------
# Telling "here" from "somewhere anybody can reach"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.0.1.1", "localhost", "LOCALHOST", "::1", "0.0.0.0", "", None,
])
def test_addresses_that_only_ever_mean_here(address):
    assert network.is_loopback(address)


@pytest.mark.parametrize("address", [
    "192.168.1.20", "10.0.0.5", "books.local", "accounts.example.com", "172.16.4.4",
])
def test_addresses_that_mean_the_same_thing_on_every_computer(address):
    assert not network.is_loopback(address)


def test_the_host_is_pulled_out_of_a_url():
    assert network.host_of("http://192.168.1.20:8756/settings") == "192.168.1.20"
    assert network.host_of("http://127.0.0.1:8756/") == "127.0.0.1"
    assert network.host_of("nonsense") == ""


# --------------------------------------------------------------------------
# Taking what somebody typed and making it work
# --------------------------------------------------------------------------


@pytest.mark.parametrize("typed,expected", [
    ("192.168.1.20", "http://192.168.1.20:8756"),
    ("192.168.1.20:8756", "http://192.168.1.20:8756"),
    ("192.168.1.20:8756/", "http://192.168.1.20:8756"),
    ("  http://192.168.1.20:8756  ", "http://192.168.1.20:8756"),
    ("books.local", "http://books.local:8756"),
    ("https://accounts.example.com", "https://accounts.example.com"),
    ("http://books.local:80", "http://books.local"),
])
def test_however_an_address_is_typed_it_comes_out_usable(typed, expected):
    """People write all of these and mean the same thing by every one."""
    assert network.tidy(typed, 8756) == expected


def test_something_that_is_not_an_address_comes_back_empty():
    for rubbish in ("", "   ", "http://", "://x"):
        assert network.tidy(rubbish, 8756) == ""


def test_the_port_comes_from_what_is_actually_being_served():
    assert network.port_of("http://192.168.1.20:9000/") == 9000
    assert network.port_of("https://accounts.example.com/") == 443
    assert network.port_of("http://books.local/") == 80
    assert network.port_of(None) == config.SERVER_PORT


# --------------------------------------------------------------------------
# Choosing the address to put in a link
# --------------------------------------------------------------------------


def test_what_an_administrator_wrote_down_wins(monkeypatch):
    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    url, how = network.reachable_base("http://127.0.0.1:8756/", "books.local:8756")
    assert (url, how) == ("http://books.local:8756", "stated")


def test_a_real_address_in_the_browser_is_trusted(monkeypatch):
    """They are looking at it from another computer, so it demonstrably works."""
    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    url, how = network.reachable_base("http://192.168.1.20:8756/settings/users", "")
    assert (url, how) == ("http://192.168.1.20:8756/settings/users", "browser")


def test_otherwise_this_computer_is_found_on_the_network(monkeypatch):
    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20", "10.0.0.5"])
    url, how = network.reachable_base("http://127.0.0.1:8756/", "")
    assert (url, how) == ("http://192.168.1.20:8756", "detected")


def test_the_port_being_served_on_is_carried_over(monkeypatch):
    monkeypatch.setattr(network, "lan_addresses", lambda: ["192.168.1.20"])
    url, _ = network.reachable_base("http://127.0.0.1:9999/", "")
    assert url == "http://192.168.1.20:9999"


def test_a_loopback_address_is_never_handed_out(monkeypatch):
    """Not even when somebody has typed it in themselves."""
    monkeypatch.setattr(network, "lan_addresses", lambda: [])
    for stated in ("127.0.0.1:8756", "localhost:8756", "http://127.0.0.1"):
        assert network.reachable_base("http://127.0.0.1:8756/", stated) == ("", "")


def test_when_there_is_no_usable_address_it_says_so_rather_than_guessing(monkeypatch):
    """An empty answer is the point: the caller must refuse, not send rubbish."""
    monkeypatch.setattr(network, "lan_addresses", lambda: [])
    assert network.reachable_base("http://127.0.0.1:8756/", "") == ("", "")


def test_this_computer_can_find_itself():
    """Not a mock: on a real machine there is normally an address to find."""
    for address in network.lan_addresses():
        assert not network.is_loopback(address)


# --------------------------------------------------------------------------
# The screen where it is set
# --------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    tmp = tempfile.mkdtemp(prefix="nexora-net-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
        session.get(Company, 1).setup_complete = True
    dbmod.reset_all()
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        yield c
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def _company_url() -> str:
    with dbmod.session_scope_for(dbmod.current_slug()) as db:
        return db.get(Company, 1).staff_url


def test_the_address_can_be_written_down_and_is_tidied_up(client):
    page = client.post("/settings/network", data={"staff_url": "192.168.1.20:8756/"},
                       follow_redirects=True)
    assert "http://192.168.1.20:8756" in page.text
    assert _company_url() == "http://192.168.1.20:8756"


def test_the_screen_refuses_an_address_that_means_this_computer(client):
    page = client.post("/settings/network", data={"staff_url": "localhost:8756"},
                       follow_redirects=True)
    assert "their own computer" in page.text
    assert _company_url() == ""


def test_the_screen_refuses_something_that_is_not_an_address(client):
    page = client.post("/settings/network", data={"staff_url": "!!!"},
                       follow_redirects=True)
    assert "not an address" in page.text
    assert _company_url() == ""


def test_clearing_it_goes_back_to_working_it_out(client):
    client.post("/settings/network", data={"staff_url": "192.168.1.20"},
                follow_redirects=True)
    client.post("/settings/network", data={"staff_url": "  "}, follow_redirects=True)
    assert _company_url() == ""


def test_the_screen_shows_which_address_links_will_use(client):
    client.post("/settings/network", data={"staff_url": "books.local:8756"},
                follow_redirects=True)
    page = client.get("/settings/network", follow_redirects=True)
    assert "http://books.local:8756" in page.text
    assert "because you set it below" in page.text
