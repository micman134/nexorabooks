"""Backups that happen whether or not anybody remembers to take one.

A backup a business has to remember is a backup a business does not have. So
this runs on its own, inside the server process, and copies every company's
books on a schedule — with two properties that matter more than the schedule.

**Old backups are pruned, but never below the number the customer asked to
keep.** A disk that filled up with backups until the application could no
longer write would be a self-inflicted disaster.

**A second copy can go somewhere else.** A backup sitting on the same disk as
the original protects against a mistake; it does not protect against the disk,
the office, or the laptop being gone. Pointing this at a flash drive, a network
share or a synced folder is the difference between an inconvenience and the end
of a business, and it is one text box.

The settings live in a small JSON file in the data folder rather than in a
company database, because they describe the installation — every company on
this computer is backed up by the same schedule — and because they have to be
readable before any database is opened.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import companies as registry
from .. import config

OFF, DAILY, WEEKLY = "OFF", "DAILY", "WEEKLY"

SCHEDULE_LABELS = {
    OFF: "Never — I will take my own",
    DAILY: "Every day",
    WEEKLY: "Once a week",
}

#: How often the thread wakes to see whether anything is due.
TICK_SECONDS = 15 * 60


@dataclass
class Settings:
    schedule: str = DAILY
    hour: int = 19                    # 24-hour clock, local time
    weekday: int = 4                  # 0 = Monday; only used for WEEKLY
    keep: int = 14                    # how many automatic copies to keep
    copy_to: str = ""                 # a second folder: flash drive, Drive, share
    last_run: str = ""                # ISO date of the last successful round
    last_result: str = ""
    last_error: str = ""

    @property
    def on(self) -> bool:
        return self.schedule in (DAILY, WEEKLY)

    @property
    def when(self) -> str:
        if not self.on:
            return "Not running"
        at = f"{self.hour:02d}:00"
        if self.schedule == DAILY:
            return f"Every day at {at}"
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday")
        return f"Every {days[self.weekday % 7]} at {at}"


def _file() -> Path:
    return config.data_dir() / "backup-schedule.json"


def load() -> Settings:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    known = {f for f in Settings().__dataclass_fields__}
    return Settings(**{k: v for k, v in data.items() if k in known})


def save(settings: Settings) -> None:
    _file().write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# Doing one round
# --------------------------------------------------------------------------


def due(settings: Settings, now: datetime | None = None) -> bool:
    """Is a backup owed right now?

    Deliberately generous: if the computer was switched off at seven o'clock,
    the backup is still owed at nine, and at nine the next morning. A schedule
    that only fires if somebody happened to be running the software at the
    exact minute would quietly never fire at all.
    """
    if not settings.on:
        return False
    now = now or datetime.now()
    try:
        last = date.fromisoformat(settings.last_run) if settings.last_run else None
    except ValueError:
        last = None

    if last is None:
        return True                      # never yet — take one now and prove it works

    days = (now.date() - last).days
    if days <= 0:
        return False                     # already done today
    if settings.schedule == DAILY:
        # Due at the chosen hour, or straight away if a whole day was missed
        # because the computer was switched off.
        return now.hour >= settings.hour or days > 1
    return days >= 7 and (now.weekday() == settings.weekday
                          and now.hour >= settings.hour or days >= 8)


def prune(slug: str, keep: int) -> int:
    """Drop the oldest automatic backups, keeping the newest ``keep``.

    Only files this scheduler made are ever removed. A backup the owner took by
    hand before doing something frightening is theirs, and stays.
    """
    folder = registry.company_dir(slug) / "backups"
    ours = sorted(folder.glob("*-auto.db"), key=lambda p: p.stat().st_mtime,
                  reverse=True)
    removed = 0
    for path in ours[max(1, keep):]:
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def copy_out(path: Path, folder: str) -> str:
    """Put a second copy somewhere else. Returns a message, empty when fine."""
    if not folder.strip():
        return ""
    target = Path(folder.strip()).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / path.name)
    except OSError as exc:
        return f"the copy to {target} did not work ({exc.strerror or exc})"
    return ""


def run_once(settings: Settings | None = None, *, force: bool = False) -> Settings:
    """Back up every company, prune, and copy out. Records what happened."""
    settings = settings or load()
    if not force and not due(settings):
        return settings

    made, problems = [], []
    for ref in registry.all_companies(include_archived=False):
        try:
            path = registry.backup(ref.slug, "auto")
            made.append(ref.name)
            prune(ref.slug, settings.keep)
            trouble = copy_out(path, settings.copy_to)
            if trouble:
                problems.append(f"{ref.name}: {trouble}")
        except Exception as exc:                     # noqa: BLE001 — never crash
            problems.append(f"{ref.name}: {exc}")

    settings.last_run = date.today().isoformat()
    settings.last_error = "; ".join(problems)
    if made and not problems:
        settings.last_result = (
            f"{len(made)} {'company' if len(made) == 1 else 'companies'} backed up"
            + (f", second copy in {settings.copy_to}" if settings.copy_to else "")
        )
    elif made:
        settings.last_result = f"{len(made)} backed up, with problems"
    else:
        settings.last_result = "nothing was backed up"
    save(settings)
    return settings


def next_due(settings: Settings, now: datetime | None = None) -> datetime | None:
    if not settings.on:
        return None
    now = now or datetime.now()
    candidate = now.replace(hour=settings.hour, minute=0, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    if settings.schedule == WEEKLY:
        while candidate.weekday() != settings.weekday:
            candidate += timedelta(days=1)
    return candidate


# --------------------------------------------------------------------------
# The thread that keeps an eye on the clock
# --------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop() -> None:
    while not _stop.wait(TICK_SECONDS):
        try:
            run_once()
        except Exception:                            # noqa: BLE001
            # A failed backup must never take the accounting software down with
            # it. The reason is recorded on the settings for the next screen.
            pass


def start() -> None:
    """Begin watching the clock. Safe to call more than once."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="nexora-backup", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    global _thread
    _thread = None
