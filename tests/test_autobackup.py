"""Backups that happen on their own.

The point of this feature is the day somebody's laptop dies. So the tests are
about the awkward parts: a computer that was switched off when the backup was
due, a disk that must not fill up, and a second copy that has to actually
arrive somewhere else.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-bak-")

from fastapi.testclient import TestClient  # noqa: E402

from app import companies as registry  # noqa: E402
from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import autobackup as A  # noqa: E402


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-bak-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as session:
        bootstrap(session)
    yield Path(tmp)
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def at(day: str, hour: int, weekday_check: bool = False) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour)


# --------------------------------------------------------------------------
# When one is owed
# --------------------------------------------------------------------------


def test_a_schedule_that_is_off_is_never_due():
    assert A.due(A.Settings(schedule=A.OFF), datetime(2026, 6, 1, 23)) is False


def test_the_first_ever_backup_is_taken_straight_away():
    """Proving it works on day one beats waiting until seven in the evening."""
    assert A.due(A.Settings(schedule=A.DAILY, hour=19), at("2026-06-01", 9)) is True


def test_a_daily_backup_waits_for_its_hour():
    s = A.Settings(schedule=A.DAILY, hour=19, last_run="2026-06-01")
    assert A.due(s, at("2026-06-02", 9)) is False
    assert A.due(s, at("2026-06-02", 19)) is True


def test_one_a_day_and_no_more():
    s = A.Settings(schedule=A.DAILY, hour=19, last_run="2026-06-02")
    assert A.due(s, at("2026-06-02", 23)) is False


def test_a_day_the_computer_was_off_is_caught_up_at_any_hour():
    """The machine was closed at seven. The backup is still owed in the morning."""
    s = A.Settings(schedule=A.DAILY, hour=19, last_run="2026-06-01")
    assert A.due(s, at("2026-06-04", 8)) is True


def test_a_weekly_backup_waits_a_week():
    s = A.Settings(schedule=A.WEEKLY, hour=19, weekday=4, last_run="2026-06-05")
    assert A.due(s, at("2026-06-09", 20)) is False           # only four days
    assert A.due(s, at("2026-06-12", 20)) is True            # the next Friday


def test_a_missed_week_is_caught_up_whatever_day_it_is():
    s = A.Settings(schedule=A.WEEKLY, hour=19, weekday=4, last_run="2026-06-05")
    assert A.due(s, at("2026-06-16", 9)) is True             # a Tuesday, 11 days on


def test_a_corrupted_last_run_does_not_stop_backups_forever():
    s = A.Settings(schedule=A.DAILY, last_run="not a date")
    assert A.due(s, at("2026-06-02", 9)) is True


# --------------------------------------------------------------------------
# Running one
# --------------------------------------------------------------------------


def backups(slug: str) -> list:
    return sorted((registry.company_dir(slug) / "backups").glob("*.db"))


def test_a_round_backs_up_every_company(home):
    registry.create("Second Company Ltd")
    A.run_once(A.Settings(schedule=A.DAILY), force=True)
    for ref in registry.all_companies():
        assert any(p.name.endswith("-auto.db") for p in backups(ref.slug)), ref.name


def test_a_backup_is_a_database_that_actually_opens(home):
    A.run_once(A.Settings(schedule=A.DAILY), force=True)
    slug = registry.default_slug()
    made = [p for p in backups(slug) if p.name.endswith("-auto.db")][0]
    assert registry.looks_like_our_database(made)


def test_what_happened_is_written_down(home):
    settings = A.run_once(A.Settings(schedule=A.DAILY), force=True)
    assert settings.last_run == date.today().isoformat()
    assert "backed up" in settings.last_result
    assert settings.last_error == ""


def test_the_schedule_survives_a_restart(home):
    A.save(A.Settings(schedule=A.WEEKLY, hour=6, keep=3, copy_to="/tmp/x"))
    again = A.load()
    assert (again.schedule, again.hour, again.keep) == (A.WEEKLY, 6, 3)


def test_a_settings_file_written_by_a_newer_version_still_loads(home):
    (home / "backup-schedule.json").write_text(
        '{"schedule": "WEEKLY", "something_new": 42}')
    assert A.load().schedule == A.WEEKLY


# --------------------------------------------------------------------------
# Not filling the disk
# --------------------------------------------------------------------------


def test_old_automatic_backups_are_pruned(home):
    slug = registry.default_slug()
    folder = registry.company_dir(slug) / "backups"
    for i in range(10):
        (folder / f"backup-2026010{i}-000000-auto.db").write_text("x", encoding="utf-8")
    A.prune(slug, keep=3)
    assert len([p for p in folder.glob("*-auto.db")]) == 3


def test_a_backup_taken_by_hand_is_never_deleted(home):
    """Somebody took that one before doing something frightening. It is theirs."""
    slug = registry.default_slug()
    folder = registry.company_dir(slug) / "backups"
    mine = folder / "backup-20260101-120000.db"
    mine.write_text("precious", encoding="utf-8")
    for i in range(6):
        (folder / f"backup-2026020{i}-000000-auto.db").write_text("x", encoding="utf-8")

    A.prune(slug, keep=1)
    assert mine.exists()


def test_pruning_never_takes_the_last_one(home):
    slug = registry.default_slug()
    folder = registry.company_dir(slug) / "backups"
    (folder / "backup-20260101-000000-auto.db").write_text("x", encoding="utf-8")
    A.prune(slug, keep=0)
    assert len(list(folder.glob("*-auto.db"))) == 1


# --------------------------------------------------------------------------
# The copy that actually saves a business
# --------------------------------------------------------------------------


def test_a_second_copy_arrives_where_it_was_asked_to(home):
    elsewhere = Path(tempfile.mkdtemp(prefix="nexora-flash-"))
    try:
        A.run_once(A.Settings(schedule=A.DAILY, copy_to=str(elsewhere)), force=True)
        assert list(elsewhere.glob("*.db")), "nothing reached the second folder"
    finally:
        shutil.rmtree(elsewhere, ignore_errors=True)


def a_place_nothing_can_be_written(where) -> str:
    r"""A path that cannot be created, on any operating system.

    "/proc/nowhere" used to stand in for an unplugged flash drive. That is a
    path Linux refuses and Windows is perfectly happy to create as C:\proc\
    nowhere — so on Windows the copy succeeded, no error was recorded, and
    three tests failed for a reason that had nothing to do with backups.

    A file with a folder path underneath it is refused everywhere: you cannot
    put a directory inside a file. Which is the point — this has to fail the
    same way on every machine a customer might own.
    """
    blocked = Path(where) / "not-a-folder"
    blocked.write_text("This is a file. Nothing can be written underneath it.", encoding="utf-8")
    return str(blocked / "backups")


def test_a_folder_that_does_not_exist_yet_is_made(home):
    elsewhere = Path(tempfile.mkdtemp(prefix="nexora-flash-")) / "Nexora backups"
    try:
        A.run_once(A.Settings(schedule=A.DAILY, copy_to=str(elsewhere)), force=True)
        assert list(elsewhere.glob("*.db"))
    finally:
        shutil.rmtree(elsewhere.parent, ignore_errors=True)


def test_a_flash_drive_that_is_not_plugged_in_is_reported_not_ignored(home):
    """The night it matters is the wrong time to find out the path was wrong."""
    settings = A.run_once(
        A.Settings(schedule=A.DAILY,
                   copy_to=a_place_nothing_can_be_written(home)), force=True)
    assert settings.last_error
    assert "did not work" in settings.last_error


def test_a_failed_second_copy_does_not_stop_the_local_backup(home):
    A.run_once(A.Settings(schedule=A.DAILY,
                          copy_to=a_place_nothing_can_be_written(home)), force=True)
    slug = registry.default_slug()
    assert any(p.name.endswith("-auto.db") for p in backups(slug))


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


def test_the_backup_screen_shows_the_schedule(client):
    page = client.get("/settings/backup", follow_redirects=True).text
    assert "Automatic backups" in page
    assert "Also put a copy in" in page


def test_the_schedule_can_be_changed(client):
    r = client.post("/settings/backup/schedule", data={
        "schedule": "WEEKLY", "hour": "6", "weekday": "0", "keep": "5",
        "copy_to": "",
    }, follow_redirects=True)
    assert r.status_code == 200
    settings = A.load()
    assert settings.schedule == A.WEEKLY and settings.hour == 6 and settings.keep == 5


def test_it_can_be_switched_off(client):
    client.post("/settings/backup/schedule", data={"schedule": "OFF", "keep": "5"},
                follow_redirects=True)
    assert A.load().on is False


def test_run_one_now_proves_the_second_folder_works(client):
    elsewhere = Path(tempfile.mkdtemp(prefix="nexora-flash-"))
    try:
        r = client.post("/settings/backup/schedule/test", data={
            "schedule": "DAILY", "hour": "19", "weekday": "4", "keep": "5",
            "copy_to": str(elsewhere),
        }, follow_redirects=True)
        assert "It works" in r.text
        assert list(elsewhere.glob("*.db"))
    finally:
        shutil.rmtree(elsewhere, ignore_errors=True)


def test_run_one_now_says_so_when_the_second_folder_is_wrong(client, home):
    r = client.post("/settings/backup/schedule/test", data={
        "schedule": "DAILY", "hour": "19", "weekday": "4", "keep": "5",
        "copy_to": a_place_nothing_can_be_written(home),
    }, follow_redirects=True)
    assert "It ran, but" in r.text


def test_a_silly_keep_number_is_brought_back_to_something_sensible(client):
    client.post("/settings/backup/schedule", data={
        "schedule": "DAILY", "hour": "19", "weekday": "4", "keep": "0",
        "copy_to": "",
    }, follow_redirects=True)
    assert A.load().keep == 1
