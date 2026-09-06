"""Print the version this copy of the source will build as.

Exists so that build_windows.bat has one quote-free thing to ask. Reading
app/config.py as text rather than importing it keeps this working before the
build environment has been created and before anything is installed.

    python version.py        ->  2.8.4
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def version() -> str:
    text = (Path(__file__).resolve().parent / "app" / "config.py").read_text(encoding="utf-8")
    found = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    if not found:
        raise SystemExit("APP_VERSION is not in app/config.py — nothing can be built.")
    return found.group(1)


if __name__ == "__main__":
    sys.stdout.write(version() + "\n")
