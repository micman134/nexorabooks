"""What happens when something goes wrong, and what the customer can send you.

A crash is not the failure that matters — the failure that matters is a crash
nobody can find out anything about. These tests are about the trail: that it is
written, that it survives, that it does not grow without limit, and that the
person on the other end of the phone can get it to you in one click.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-sup-")

from fastapi import APIRouter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import companies as registry  # noqa: E402
from app import config  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import support  # noqa: E402


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-sup-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    yield Path(tmp)
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def a_failure(message: str = "something specific went wrong") -> Exception:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


# --------------------------------------------------------------------------
# Writing it down
# --------------------------------------------------------------------------


def test_an_error_is_written_with_everything_needed_to_place_it(home):
    support.record(a_failure(), where="POST /sales/invoices/4/post",
                   who="chioma", company="Adeyemi Building Materials")
    written = support.error_log().read_text(encoding="utf-8")
    assert "POST /sales/invoices/4/post" in written
    assert "chioma" in written
    assert "Adeyemi Building Materials" in written
    assert "ValueError: something specific went wrong" in written


def test_the_traceback_is_kept_not_just_the_message(home):
    """Without the traceback there is nothing to fix."""
    support.record(a_failure())
    assert "Traceback (most recent call last)" in support.error_log().read_text(encoding="utf-8")


def test_a_reference_is_handed_back_to_show_the_person(home):
    reference = support.record(a_failure())
    assert reference.isdigit() and len(reference) == 8
    assert reference in support.error_log().read_text(encoding="utf-8")


def test_errors_pile_up_rather_than_replacing_each_other(home):
    support.record(a_failure("first"))
    support.record(a_failure("second"))
    assert len(support.recent()) == 2


def test_the_most_recent_is_first(home):
    support.record(a_failure("older"))
    support.record(a_failure("newer"))
    assert "newer" in support.recent()[0]


def test_the_log_cannot_grow_without_limit(home):
    """A log file that fills the disk would be a fault of its own."""
    support.error_log().write_text("x" * (support.MAX_LOG + 10), encoding="utf-8")
    support.record(a_failure("after the roll"))
    assert (support.log_dir() / "errors-previous.log").exists()
    assert support.error_log().stat().st_size < support.MAX_LOG


def test_what_rolled_over_is_still_readable(home):
    support.record(a_failure("from before the roll"))
    with support.error_log().open("a") as handle:
        handle.write("x" * support.MAX_LOG)
    support.record(a_failure("after the roll"))
    both = "\n".join(support.recent())
    assert "from before the roll" in both
    assert "after the roll" in both


def test_the_log_can_be_cleared(home):
    support.record(a_failure())
    support.clear()
    assert support.recent() == []


def test_recording_never_raises_even_when_it_cannot_write(home, monkeypatch):
    """A logger that throws while logging turns one problem into two."""
    blocked = Path(home) / "not-a-folder"
    blocked.write_text("A file. A log cannot be written inside a file.", encoding="utf-8")
    monkeypatch.setattr(support, "log_dir", lambda: blocked / "logs")
    assert support.record(a_failure())          # a reference, and no exception


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_the_report_says_what_version_and_machine_this_is(home):
    text = support.report()
    assert config.APP_VERSION in text
    assert "data folder" in text
    assert "machine code" in text


def test_it_says_whether_backups_are_running(home):
    from app.services import autobackup

    autobackup.save(autobackup.Settings(schedule=autobackup.WEEKLY, hour=6,
                                        weekday=0, copy_to="/tmp/flash"))
    text = support.report()
    assert "Every Monday at 06:00" in text
    assert "/tmp/flash" in text


def test_it_warns_when_everything_is_on_one_disk(home):
    from app.services import autobackup

    autobackup.save(autobackup.Settings(copy_to=""))
    assert "everything is on this disk only" in support.report()


def test_it_says_whether_email_works(home):
    from app.services import mailer

    mailer.save(mailer.Settings(host="smtp.example.com", from_email="a@b.com"))
    assert "smtp.example.com" in support.report()


def test_it_lists_the_companies_and_how_big_they_are(home):
    registry.create("Second Company Ltd")
    text = support.report()
    assert "My Company Ltd" in text
    assert "Second Company Ltd" in text
    assert "journal entries" in text


def test_the_recent_errors_are_in_it(home):
    support.record(a_failure("this exact thing"))
    assert "this exact thing" in support.report()


def test_it_says_plainly_when_nothing_has_gone_wrong(home):
    assert "none recorded" in support.report()


def test_it_tells_the_reader_what_is_and_is_not_in_it(home):
    text = support.report()
    assert "describes the installation, not the books" in text
    assert "read it before you send it" in text


def test_a_company_file_that_will_not_open_is_reported_not_hidden(home):
    """This is exactly the situation somebody rings about."""
    dbmod.reset_all()                       # as if the software were not running
    path = registry.company_db(registry.default_slug())
    for suffix in ("-wal", "-shm"):
        companion = path.with_name(path.name + suffix)
        if companion.exists():
            companion.unlink()
    path.write_bytes(b"not a database")
    dbmod.reset_all()

    text = support.report()
    assert "could not be opened" in text


# --------------------------------------------------------------------------
# Through the screens
# --------------------------------------------------------------------------


@pytest.fixture()
def client(home):
    with TestClient(app, raise_server_exceptions=False) as c:
        c.post("/login", data={"username": "admin", "password": "admin123",
                               "next": "/"}, follow_redirects=True)
        c.post("/account/password", data={
            "new_password": "Lagos2026", "confirm_password": "Lagos2026",
        }, follow_redirects=True)
        yield c


def test_the_diagnostics_screen_shows_the_report(client):
    page = client.get("/settings/diagnostics", follow_redirects=True).text
    assert "diagnostic report" in page
    assert "Download the report" in page


def test_the_report_downloads_as_a_file(client):
    r = client.get("/settings/diagnostics/download", follow_redirects=True)
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "nexora-diagnostics" in r.headers.get("content-disposition", "")
    assert "THIS INSTALLATION" in r.text


def test_the_log_can_be_cleared_from_the_screen(client):
    support.record(a_failure("to be cleared"))
    r = client.post("/settings/diagnostics/clear", follow_redirects=True)
    assert "Error log cleared" in r.text
    assert support.recent() == []


# --------------------------------------------------------------------------
# A page that actually breaks
# --------------------------------------------------------------------------

breaker = APIRouter()


@breaker.get("/deliberately-broken")
def deliberately_broken():
    raise RuntimeError("a fault nobody expected")


app.include_router(breaker)


def test_an_unexpected_failure_shows_a_calm_page_not_a_stack_trace(client):
    r = client.get("/deliberately-broken", follow_redirects=True)
    assert r.status_code == 500
    assert "RuntimeError" not in r.text
    assert "Traceback" not in r.text
    assert "nothing in your books has changed" in r.text.lower()


def test_it_gives_the_person_a_reference_and_writes_it_down(client):
    import re

    r = client.get("/deliberately-broken", follow_redirects=True)
    reference = re.search(r"reference <strong>(\d+)</strong>", r.text)
    assert reference, "no reference was shown"
    assert reference.group(1) in support.error_log().read_text(encoding="utf-8")


def test_the_failure_reaches_the_report_with_the_page_that_caused_it(client):
    client.get("/deliberately-broken", follow_redirects=True)
    text = client.get("/settings/diagnostics/download", follow_redirects=True).text
    assert "/deliberately-broken" in text
    assert "a fault nobody expected" in text


def test_who_was_signed_in_is_recorded(client):
    client.get("/deliberately-broken", follow_redirects=True)
    assert "admin" in support.error_log().read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Which alphabets this computer can print
# --------------------------------------------------------------------------


def test_the_report_says_which_scripts_this_computer_can_print():
    """"My invoices print as boxes" should be one line of a report, not a
    conversation."""
    facts = "\n".join(support.font_facts())
    assert "fonts folder:" in facts
    for name, _ in support.SCRIPTS:
        assert name in facts


def test_a_script_with_no_font_is_said_so_plainly(monkeypatch):
    from app import fonts as fontfinder

    monkeypatch.setattr(fontfinder, "find", lambda text, bold=False: None)
    facts = "\n".join(support.font_facts())
    assert facts.count("no font here can print this") == len(support.SCRIPTS)


def test_a_font_search_that_goes_wrong_does_not_break_the_report(monkeypatch):
    from app import fonts as fontfinder

    def explode(text, bold=False):
        raise OSError("the font folder is on a disconnected drive")

    monkeypatch.setattr(fontfinder, "find", explode)
    assert "no font here can print this" in "\n".join(support.font_facts())
    assert support.report()                    # and the whole report still comes out
