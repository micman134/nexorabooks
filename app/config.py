"""Application configuration and data-directory resolution."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "Nexora Books"
APP_VERSION = "2.13.0"

# Nigerian tax defaults (verified against FIRS / Nigeria Tax Act 2025, effective Jan 2026)
DEFAULT_VAT_RATE = "7.5"          # percent
VAT_FILING_DAY = 21               # VAT return due 21st of following month
WHT_REMITTANCE_DAY = 21           # WHT remittance due 21st of following month
WHT_SMALL_TXN_EXEMPTION = 2_000_000_00   # NGN 2,000,000 in kobo
NO_TIN_MULTIPLIER = 2             # double the rate ...
NO_TIN_CAP = "20"                 # ... capped at 20%


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def base_dir() -> Path:
    """Directory holding the bundled templates and static files.

    NexoraBooks.spec copies them in under ``app/``, so inside a PyInstaller
    build they live at ``_MEIPASS/app`` — not at the top of the bundle.
    """
    if is_frozen():
        return Path(sys._MEIPASS) / "app"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def program_dir() -> Path:
    """The folder this copy of the program was started from.

    Not the same thing as ``base_dir``: inside a packaged build the templates
    are unpacked into a temporary folder, while this is the folder the person
    double-clicked. It matters because somebody who has downloaded a new
    version and unzipped it somewhere else needs to be able to tell which of
    the two copies is the one actually running.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: Where the books lived before the software was renamed. An existing
#: installation is moved across once, on first start, so nobody opens the new
#: version and finds their company gone.
LEGACY_DIR_NAMES = ("NaijaBooks", ".naijabooks")


def _legacy_dir() -> Path | None:
    """The old data folder, if one is still sitting there."""
    if os.name == "nt":
        old = Path(os.environ.get("APPDATA", Path.home())) / "NaijaBooks"
    else:
        old = Path.home() / ".naijabooks"
    return old if old.is_dir() else None


def _migrate_legacy(new: Path) -> None:
    """Move a pre-rename data folder into place, once, and never destructively.

    Only runs when the new folder holds no company database of its own, so a
    second run can never overwrite live books. If anything at all goes wrong
    the old folder is left exactly where it was — the worst case is that the
    customer starts empty and can restore from a backup, not that data is lost.
    """
    old = _legacy_dir()
    if old is None or old.resolve() == new.resolve():
        return
    if any(new.glob("*.db")) or (new / "companies.json").exists():
        return                      # the new folder is already in use
    try:
        for item in old.iterdir():
            target = new / item.name
            if target.exists():
                continue
            shutil.move(str(item), str(target))
        (new / "moved-from-naijabooks.txt").write_text(
            f"Your books were moved here from:\n    {old}\n"
            "That folder can be deleted once you are satisfied everything is present.\n",
            encoding="utf-8",
        )
    except OSError:
        pass                        # leave the old folder untouched


def data_dir() -> Path:
    """Writable directory for the company database, backups and attachments.

    Windows:  C:\\Users\\<you>\\AppData\\Roaming\\Nexora Books
    Other OS: ~/.nexorabooks
    Overridable with the NEXORA_DATA environment variable.
    """
    override = os.environ.get("NEXORA_DATA")
    if override:
        p = Path(override)
    elif os.name == "nt":
        p = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    else:
        p = Path.home() / ".nexorabooks"
    fresh = not p.exists()
    p.mkdir(parents=True, exist_ok=True)
    if fresh and not override:
        _migrate_legacy(p)
    (p / "backups").mkdir(exist_ok=True)
    (p / "attachments").mkdir(exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "company.db"


def secret_key() -> str:
    """Stable per-installation secret for signing session cookies."""
    f = data_dir() / "secret.key"
    if not f.exists():
        f.write_text(os.urandom(32).hex(), encoding="utf-8")
        # Whoever can read this can forge a signed-in session for anybody.
        from . import fileguard

        fileguard.restrict_to_owner(f)
    return f.read_text(encoding="utf-8").strip()


def tls_settings() -> dict:
    """Where the certificate is, if this installation is serving over HTTPS.

    Kept in the data folder rather than passed on the command line, because the
    person who turns encryption on is doing it from a settings screen and the
    launcher has to find out about it on the next start without anybody editing
    a shortcut.
    """
    import json

    try:
        return json.loads((data_dir() / "tls.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_tls_settings(settings: dict) -> None:
    import json

    (data_dir() / "tls.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")


def serving_over_tls() -> bool:
    settings = tls_settings()
    if not settings.get("on"):
        return False
    from pathlib import Path as _Path

    return (_Path(settings.get("cert", "")).exists()
            and _Path(settings.get("key", "")).exists())


SERVER_HOST = os.environ.get("NEXORA_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("NEXORA_PORT", "8756"))
