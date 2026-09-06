"""Running Nexora Books on a web server instead of somebody's desk.

The entry point cPanel uses lives in ``passenger_wsgi.py``. Most of it is a
three-line adapter between what Passenger speaks (WSGI) and what this
application speaks (ASGI). The part worth testing is the refusal.

Shared hosting invites one specific catastrophe. The folder people know about
is ``public_html``, so that is where they put things — and this application
keeps a company's entire ledger in a single file. Put that file under
``public_html`` and every invoice, every salary and every customer's details
can be downloaded by anybody who guesses the filename. There is no password
in front of it; it is a file on a web server.

So the software refuses to start. These tests make sure it keeps refusing.
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent


def entry_point():
    """Load passenger_wsgi.py, pointed at somewhere harmless."""
    os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-hosting-")
    spec = importlib.util.spec_from_file_location(
        "passenger_wsgi_under_test", SOURCE / "passenger_wsgi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def passenger():
    return entry_point()


@pytest.mark.parametrize("folder", [
    "public_html", "public", "www", "htdocs", "web",
])
def test_it_refuses_to_put_the_books_somewhere_downloadable(passenger, folder):
    exposed = Path(tempfile.mkdtemp()) / folder / "nexorabooks-data"
    exposed.mkdir(parents=True)
    with pytest.raises(RuntimeError) as refused:
        passenger.refuse_if_the_books_would_be_downloadable(exposed)
    message = str(refused.value)
    assert folder in message
    assert "NEXORA_DATA" in message, "it must say how to fix it, not just refuse"


def test_a_folder_outside_the_web_root_is_allowed(passenger):
    fine = Path(tempfile.mkdtemp()) / "nexorabooks-data"
    fine.mkdir(parents=True)
    passenger.refuse_if_the_books_would_be_downloadable(fine)      # no raise


def test_a_folder_merely_containing_the_word_public_is_allowed(passenger):
    """'publications' is not 'public'. Refusing it would be a false alarm the
    customer cannot understand or work around."""
    fine = Path(tempfile.mkdtemp()) / "publications" / "books-data"
    fine.mkdir(parents=True)
    passenger.refuse_if_the_books_would_be_downloadable(fine)      # no raise


def test_passenger_hands_cpanel_something_it_can_call(passenger):
    """Passenger looks for a callable named 'application' and nothing else."""
    assert callable(passenger.application)


def test_the_adapter_is_on_the_hosting_requirements_list():
    """Without a2wsgi the entry point cannot import, and the failure on a
    shared host is a blank 500 page with the reason buried in a log."""
    listed = (SOURCE / "deploy" / "requirements-cpanel.txt").read_text(
        encoding="utf-8")
    assert "a2wsgi" in listed
    for needed in ("fastapi", "sqlalchemy", "jinja2", "itsdangerous",
                   "python-multipart"):
        assert needed in listed, f"{needed} missing from the hosting list"


def test_the_signing_key_is_never_sent_to_a_web_server():
    """Anybody holding it can issue licences in your name."""
    for guide in ("deploy/CPANEL.txt", "deploy/README.txt"):
        text = (SOURCE / guide).read_text(encoding="utf-8")
        assert "seller/" in text, f"{guide} does not warn about the signing key"
