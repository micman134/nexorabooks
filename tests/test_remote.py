"""Reaching the books from somewhere that is not the office.

The software was written for one building. These tests cover the three things
that have to be true before a member of staff in another state can sign in
safely: the connection can be encrypted, a password cannot be guessed at leisure,
and the screen that explains all this does not quietly recommend something
dangerous.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-remote-")

from fastapi.testclient import TestClient  # noqa: E402

from app import companies as registry, config, db as dbmod, fileguard, tls  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Company  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import throttle  # noqa: E402


#: Four tests below check the certificate against a second, independent
#: implementation rather than against this one's own opinion of itself — which
#: is the only way to know the file is a real certificate and not merely
#: self-consistent. OpenSSL is that second opinion, and it is not on a plain
#: Windows machine. Skipping is honest there; quietly weakening the check to
#: something that passes everywhere would not be.
needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="openssl is not installed here, so there is nothing to check the "
           "certificate against; run this on a machine that has it")


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-remote-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    throttle.reset()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
        db.get(Company, 1).setup_complete = True
    dbmod.reset_all()
    yield tmp
    throttle.reset()
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def client(home):
    with TestClient(app) as c:
        c.post("/login", data={"username": "admin", "password": "admin123"},
               follow_redirects=True)
        yield c


@pytest.fixture(autouse=True)
def clean_throttle():
    throttle.reset()
    yield
    throttle.reset()


# --------------------------------------------------------------------------
# Guessing passwords
# --------------------------------------------------------------------------


def test_a_wrong_password_is_simply_wrong_the_first_few_times(home):
    with TestClient(app) as c:
        for _ in range(3):
            page = c.post("/login", data={"username": "admin", "password": "no"})
            assert "was not recognised" in page.text
            assert "Try again in" not in page.text


def test_enough_wrong_passwords_and_the_guesser_has_to_wait(home):
    with TestClient(app) as c:
        for _ in range(throttle.PER_USER + 1):
            page = c.post("/login", data={"username": "admin", "password": "no"})
    assert "Too many wrong passwords" in page.text
    assert "minutes" in page.text


def test_the_right_password_is_refused_too_while_the_wait_is_running(home):
    """Otherwise the limit protects nothing — a guesser just keeps going."""
    with TestClient(app) as c:
        for _ in range(throttle.PER_USER + 1):
            c.post("/login", data={"username": "admin", "password": "no"})
        page = c.post("/login", data={"username": "admin", "password": "admin123"},
                      follow_redirects=True)
    assert "Too many wrong passwords" in page.text


def test_a_username_that_does_not_exist_is_counted_as_well(home):
    """Otherwise the difference tells an attacker which names are real."""
    with TestClient(app) as c:
        for _ in range(throttle.PER_USER + 1):
            page = c.post("/login", data={"username": "ghost", "password": "no"})
    assert "Too many wrong passwords" in page.text


def test_one_persons_mistakes_do_not_shut_out_a_colleague(home):
    for _ in range(throttle.PER_USER + 1):
        throttle.failed("ngozi", "10.0.0.9")
    assert throttle.wait_needed("ngozi", "10.0.0.9") > 0
    assert throttle.wait_needed("chinedu", "10.0.0.8") == 0


def test_a_whole_office_behind_one_address_is_given_more_room(home):
    """A shared connection must not lock the building out over two typos."""
    assert throttle.PER_ADDRESS > throttle.PER_USER
    for n in range(throttle.PER_USER + 1):
        throttle.failed(f"person{n}", "197.210.1.1")
    assert throttle.wait_needed("someone-else", "197.210.1.1") == 0


def test_signing_in_correctly_clears_the_count(home):
    throttle.failed("admin", "10.0.0.1")
    throttle.failed("admin", "10.0.0.1")
    throttle.succeeded("admin", "10.0.0.1")
    for _ in range(throttle.PER_USER - 1):
        throttle.failed("admin", "10.0.0.1")
    assert throttle.wait_needed("admin", "10.0.0.1") == 0, "the count restarted"


def test_the_wait_ends_by_itself(home):
    import time as _time

    now = _time.time()
    for _ in range(throttle.PER_USER + 1):
        throttle.failed("admin", "10.0.0.1", now=now)
    assert throttle.wait_needed("admin", "10.0.0.1", now=now) > 0
    assert throttle.wait_needed("admin", "10.0.0.1", now=now + throttle.WAIT + 2) == 0


def test_nobody_is_ever_locked_out_permanently(home):
    """A stranger must not be able to take somebody's account away from them."""
    assert throttle.WAIT <= 30 * 60


# --------------------------------------------------------------------------
# Encryption
# --------------------------------------------------------------------------


@needs_openssl
def test_a_certificate_this_computer_makes_is_a_real_certificate(home):
    made = tls.make(["localhost", "127.0.0.1", "books.local"], "Procert Academy Limited")
    assert made.exists

    read = subprocess.run(["openssl", "x509", "-in", str(made.cert_path),
                           "-noout", "-text"], capture_output=True, text=True)
    assert read.returncode == 0, read.stderr
    assert "sha256WithRSAEncryption" in read.stdout
    assert "DNS:books.local" in read.stdout
    assert "IP Address:127.0.0.1" in read.stdout
    assert "Procert Academy Limited" in read.stdout


@needs_openssl
def test_it_signs_itself_correctly(home):
    """A certificate whose own signature does not check out is worthless."""
    made = tls.make(["localhost"], "Test")
    checked = subprocess.run(
        ["openssl", "verify", "-CAfile", str(made.cert_path), str(made.cert_path)],
        capture_output=True, text=True)
    assert "OK" in checked.stdout, checked.stdout + checked.stderr


@needs_openssl
def test_the_private_key_matches_and_is_well_formed(home):
    made = tls.make(["localhost"], "Test")
    checked = subprocess.run(["openssl", "rsa", "-in", str(made.key_path),
                              "-check", "-noout"], capture_output=True, text=True)
    assert "RSA key ok" in checked.stdout + checked.stderr

    cert_key = subprocess.run(["openssl", "x509", "-in", str(made.cert_path),
                               "-noout", "-modulus"], capture_output=True, text=True)
    priv_key = subprocess.run(["openssl", "rsa", "-in", str(made.key_path),
                               "-noout", "-modulus"], capture_output=True, text=True)
    assert cert_key.stdout.strip() == priv_key.stdout.strip(), \
        "the certificate and the key must be a pair"


def test_the_key_is_not_readable_by_everybody_on_the_machine(home):
    """Asked on every operating system, including the one customers use.

    This test used to skip itself on Windows on the grounds that "file modes
    work differently" there. They do — and the consequence was that the key
    was left readable by every account on the machine while the test suite
    reported green. Windows has real permissions; they are just not modes.
    """
    made = tls.make(["localhost"], "Test")
    assert fileguard.only_owner_can_read(made.key_path)


@needs_openssl
def test_a_fingerprint_is_shown_the_way_a_browser_shows_one(home):
    made = tls.make(["localhost"], "Test")
    assert len(made.fingerprint.split(":")) == 32
    real = subprocess.run(["openssl", "x509", "-in", str(made.cert_path),
                           "-noout", "-fingerprint", "-sha256"],
                          capture_output=True, text=True)
    assert made.fingerprint in real.stdout.upper().replace(" ", "")


def test_encryption_is_off_until_somebody_turns_it_on(home):
    assert not config.serving_over_tls()


def test_turning_it_on_from_the_screen_makes_a_certificate(client):
    page = client.post("/settings/network/encrypt", data={"on": "1"},
                       follow_redirects=True)
    assert "Encryption is on" in page.text
    assert config.serving_over_tls()
    settings = config.tls_settings()
    assert settings["made_here"] is True
    assert os.path.exists(settings["cert"]) and os.path.exists(settings["key"])


def test_a_certificate_that_is_not_there_changes_nothing(client):
    page = client.post("/settings/network/encrypt", data={
        "on": "1", "cert_path": "/nowhere/server.crt",
        "key_path": "/nowhere/server.key"}, follow_redirects=True)
    assert "not there" in page.text
    assert not config.serving_over_tls()


def test_it_can_be_turned_off_again(client):
    client.post("/settings/network/encrypt", data={"on": "1"}, follow_redirects=True)
    assert config.serving_over_tls()
    client.post("/settings/network/encrypt", data={"on": "0"}, follow_redirects=True)
    assert not config.serving_over_tls()


def test_the_screen_says_plainly_what_is_at_risk_while_it_is_off(client):
    page = client.get("/settings/network", follow_redirects=True).text
    assert "travels" in page and "unencrypted" in page
    assert "before you set up remote access" in page


def test_the_fingerprint_is_offered_so_the_warning_can_be_checked(client):
    client.post("/settings/network/encrypt", data={"on": "1"}, follow_redirects=True)
    page = client.get("/settings/network", follow_redirects=True).text
    assert "fingerprint" in page.lower()
    assert "Advanced" in page, "people need to be told what to click"


# --------------------------------------------------------------------------
# What the screen recommends
# --------------------------------------------------------------------------


def test_opening_the_router_to_the_internet_is_warned_against_not_explained(client):
    page = client.get("/settings/network", follow_redirects=True).text
    assert "Do not open your router to the internet" in page
    assert "port forward" not in page.lower(), "do not teach the dangerous thing"


def test_the_private_network_route_is_spelled_out(client):
    page = client.get("/settings/network", follow_redirects=True).text
    assert "Tailscale" in page
    assert "never leave this computer" in page
    for step in ("Install it on this computer", "Install it on each staff computer"):
        assert step in page


def test_staff_are_told_to_have_their_own_sign_in(client):
    page = client.get("/settings/network", follow_redirects=True).text
    assert "own account" in page
    assert "two-factor" in page
