"""Guards that apply to the whole test run.

Tests are supposed to leave the computer they ran on exactly as they found it.
That is easy to believe and worth checking, because the one time it was not
true the damage was serious: a permissions test handed an empty path to code
that then stripped the access rights off the folder the software was being
built in, and the person who ran the build could no longer open their own
files. Every test after that point failed for reasons that had nothing to do
with accounting.

So the run now checks, at the start and again at the end, that the folder it is
standing in is still there and still usable. A test suite that breaks the
machine has to say so plainly rather than burying it in four hundred errors.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


def _usable(folder: Path) -> tuple[bool, str]:
    """Can this folder still be listed, and its files opened?"""
    try:
        names = sorted(p.name for p in folder.iterdir())
    except OSError as exc:
        return False, f"{folder} can no longer be listed ({exc})"
    for name in ("version.py", "requirements.txt"):
        path = folder / name
        if not path.exists():
            continue
        try:
            path.read_text(encoding="utf-8")
        except OSError as exc:
            return False, f"{path} can no longer be read ({exc})"
    if not names:
        return False, f"{folder} is suddenly empty"
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def the_computer_is_left_as_it_was_found():
    fine, why = _usable(PROJECT)
    if not fine:                                            # pragma: no cover
        pytest.exit(f"the project folder was already unusable before any test "
                    f"ran: {why}", returncode=1)
    here = Path.cwd()

    yield

    fine, why = _usable(PROJECT)
    assert fine, (
        "the test run damaged the folder it was running in — " + why + "\n"
        "Nothing in these tests may change the permissions, contents or "
        "location of the project folder. On Windows this is usually a call "
        "that was meant for one file and reached a directory instead."
    )
    assert Path.cwd() == here, (
        f"the tests finished in {Path.cwd()} instead of {here}; something "
        "changed the working directory and did not put it back")
    assert os.environ.get("NEXORA_DATA") != str(PROJECT), (
        "a test pointed the data folder at the project folder itself")
