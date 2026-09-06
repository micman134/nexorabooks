"""Files that hold secrets must be closed to everybody else — on Windows too.

The bug these tests exist for: ``os.chmod(path, 0o600)`` is the whole answer on
Linux and macOS and almost nothing on Windows, where it can only toggle the
read-only flag. The customer's mail password was sitting at 0o666 on every
Windows machine that had ever saved email settings, and the test that should
have said so skipped itself on that operating system.

So these are written to make a claim about *this* computer, whichever one it
is, rather than about a POSIX mode.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app import fileguard


@pytest.fixture()
def secret():
    folder = Path(tempfile.mkdtemp(prefix="nexora-guard-"))
    path = folder / "secret.txt"
    path.write_text("the customer's mail password", encoding="utf-8")
    yield path


def test_a_secret_file_is_closed_to_everybody_else(secret):
    assert fileguard.restrict_to_owner(secret)
    assert fileguard.only_owner_can_read(secret)


def test_the_owner_can_still_read_it_afterwards(secret):
    """Locking a file so hard that the program cannot open it is not security."""
    fileguard.restrict_to_owner(secret)
    assert secret.read_text(encoding="utf-8") == "the customer's mail password"


def test_a_file_left_wide_open_is_reported_as_wide_open(secret):
    """The check has to be capable of saying no, or it is saying nothing."""
    if os.name == "nt":
        pytest.skip("a fresh Windows file inherits its folder's permissions, "
                    "which vary by machine; the POSIX case proves the logic")
    os.chmod(secret, 0o644)
    assert not fileguard.only_owner_can_read(secret)


def test_a_file_that_is_not_there_is_not_claimed_to_be_safe():
    missing = Path(tempfile.mkdtemp(prefix="nexora-guard-")) / "nothing"
    assert not fileguard.restrict_to_owner(missing)
    assert not fileguard.only_owner_can_read(missing)


def test_it_never_raises_whatever_it_is_handed():
    """Called while saving the customer's settings. It may fail; it may not
    take the settings down with it."""
    for awkward in ("", "   ", "\0bad", "/nonexistent/deeply/nested/thing"):
        try:
            assert fileguard.restrict_to_owner(awkward) is False
            assert fileguard.only_owner_can_read(awkward) is False
        except Exception as exc:                            # noqa: BLE001
            pytest.fail(f"raised on {awkward!r}: {exc!r}")


# --------------------------------------------------------------------------
# The one that got away
# --------------------------------------------------------------------------
#
# An empty path is Path("."), the current working directory, and it exists.
# Asking only "does it exist?" therefore pointed a permissions change at
# whatever folder the program happened to be standing in — on a customer's
# machine, the folder the software was being built in, which it locked them
# out of. These four are the fence around that.


def test_an_empty_path_is_refused_and_is_not_the_current_folder():
    for nothing in ("", "   ", "\t", Path("")):
        assert fileguard.restrict_to_owner(nothing) is False
        assert fileguard.only_owner_can_read(nothing) is False


def test_a_folder_is_refused_even_though_it_exists(tmp_path):
    """A folder's permissions carry into everything inside it. Nothing here
    has any business changing one."""
    assert tmp_path.is_dir()
    assert fileguard.restrict_to_owner(tmp_path) is False
    assert fileguard.restrict_to_owner(".") is False
    assert fileguard.restrict_to_owner(Path.cwd()) is False


def test_the_current_folder_is_left_exactly_as_it_was(tmp_path, monkeypatch):
    """Belt and braces: try to lock the working directory, then prove that
    the files in it can still be read afterwards."""
    (tmp_path / "a-file-that-must-stay-readable.txt").write_text(
        "still here", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    for attempt in ("", ".", str(tmp_path)):
        assert fileguard.restrict_to_owner(attempt) is False

    assert (tmp_path / "a-file-that-must-stay-readable.txt").read_text(
        encoding="utf-8") == "still here"
    assert list(tmp_path.iterdir()), "the folder must still be listable"


def test_nothing_reaches_icacls_that_should_not(tmp_path, monkeypatch):
    """The Windows branch, exercised on any machine.

    The damage was done by ``icacls`` and only by ``icacls``, so the claim
    worth testing is not "it returns False" but "it never got that far". This
    pretends to be Windows and records every call that would have been made.
    """
    calls = []
    monkeypatch.setattr(fileguard, "_on_windows", lambda: True)
    monkeypatch.setattr(fileguard, "_icacls", lambda arguments: calls.append(arguments))

    for refused in ("", "   ", ".", str(tmp_path), str(tmp_path / "never-written")):
        fileguard.restrict_to_owner(refused)
        fileguard.only_owner_can_read(refused)

    assert calls == [], f"icacls was asked to touch {calls}"


def test_a_path_that_is_not_there_yet_is_refused_rather_than_created(tmp_path):
    missing = tmp_path / "not-written-yet.json"
    assert fileguard.restrict_to_owner(missing) is False
    assert not missing.exists(), "it must not bring the file into being"


def test_the_windows_listing_is_read_the_way_windows_writes_it():
    """The parser, exercised without needing to be on Windows.

    icacls puts the filename on the first line and indents the rest. Getting
    this wrong in the lenient direction would mean reporting a file as private
    when half the machine can read it, so it is worth a test of its own.
    """
    path = Path(r"C:\Users\great\AppData\Roaming\Nexora Books\email.json")
    listing = (
        f"{path} DESKTOP-9F2\\great:(F)\n"
        "                                    NT AUTHORITY\\SYSTEM:(F)\n"
        "                                    BUILTIN\\Administrators:(F)\n"
        "\n"
        "Successfully processed 1 files; Failed processing 0 files\n"
    )
    assert fileguard._people_in(listing, path) == [
        "DESKTOP-9F2\\great", "NT AUTHORITY\\SYSTEM", "BUILTIN\\Administrators"]


def test_one_account_only_is_what_a_tightened_file_looks_like():
    path = Path(r"C:\Users\great\AppData\Roaming\Nexora Books\email.json")
    listing = (f"{path} DESKTOP-9F2\\great:(F)\n\n"
               "Successfully processed 1 files; Failed processing 0 files\n")
    assert fileguard._people_in(listing, path) == ["DESKTOP-9F2\\great"]
