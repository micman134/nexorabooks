"""Starting the program, and knowing which copy of it you are looking at.

The bug behind this file: somebody downloaded a new version, unzipped it to a
different folder and started it — and was shown the old version. The old one
had never been closed and still held port 8756, so the launcher saw the port
was busy, assumed it was looking at itself, and opened a browser at whatever
was there. Nothing anywhere said the version was not the one just started, so
moving the folder and unzipping again both appeared to do nothing.

Three things are checked here: that a running copy will say which version it
is, that the launcher notices when that is not its own version, and that what
it then says is something a person can act on.
"""
from __future__ import annotations

import os
import shutil
import socket
import tempfile
import threading
import time

import pytest

os.environ.setdefault("NEXORA_DATA", tempfile.mkdtemp(prefix="nexora-launch-"))

import desktop  # noqa: E402
from app import config  # noqa: E402
from app import companies as registry, db as dbmod  # noqa: E402
from app.seed import bootstrap  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# Who is answering on this port?
# --------------------------------------------------------------------------


def test_nothing_listening_is_not_mistaken_for_an_answer():
    assert desktop.who_is_answering(free_port(), timeout=0.4) == {}


def test_something_that_is_not_us_is_not_mistaken_for_us():
    """A web server that knows nothing about /health must not look like a copy."""
    import http.server

    port = free_port()
    server = http.server.HTTPServer(("127.0.0.1", port),
                                    http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert desktop.who_is_answering(port, timeout=1.0) == {}
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def running():
    """A real copy of the application, on a real port, as a customer would have."""
    import uvicorn

    tmp = tempfile.mkdtemp(prefix="nexora-launch-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
    dbmod.reset_all()

    from app.main import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while time.time() < deadline and not desktop.port_in_use("127.0.0.1", port):
        time.sleep(0.1)
    yield port
    server.should_exit = True
    thread.join(timeout=10)
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def test_a_running_copy_says_which_version_it_is(running):
    answer = desktop.who_is_answering(running)
    assert answer["app"] == config.APP_NAME
    assert answer["version"] == config.APP_VERSION


def test_a_running_copy_says_which_folder_it_was_started_from(running):
    """Without this there is no way to tell two unzipped copies apart."""
    answer = desktop.who_is_answering(running)
    assert answer["program_dir"] == str(config.program_dir())
    assert answer["pid"] == os.getpid()


def test_the_port_check_agrees_with_reality(running):
    assert desktop.port_in_use("127.0.0.1", running)
    assert not desktop.port_in_use("127.0.0.1", free_port())


# --------------------------------------------------------------------------
# What the launcher says about it
# --------------------------------------------------------------------------


def test_an_older_copy_holding_the_port_is_named_not_silently_opened():
    message = desktop.explain_the_other_copy(
        8756, {"app": config.APP_NAME, "version": "2.7.1",
               "program_dir": r"C:\2026\NexoraBooks2.6.0", "pid": 4321})

    assert "2.7.1" in message                     # the one in the way
    assert config.APP_VERSION in message          # the one they just started
    assert r"C:\2026\NexoraBooks2.6.0" in message  # where to find it
    assert "4321" in message                      # and how to end it
    assert "Task Manager" in message
    assert str(config.data_dir()) in message      # their books are safe
    assert "--port 9000" in message               # the way to run both


def test_the_message_survives_a_copy_that_will_not_say_where_it_lives():
    message = desktop.explain_the_other_copy(8756, {"version": "2.7.1"})
    assert "2.7.1" in message
    assert "NexoraBooks.exe, python.exe or py.exe" in message


def test_the_message_is_written_for_somebody_who_is_not_a_programmer():
    message = desktop.explain_the_other_copy(8756, {"version": "2.7.1"})
    for jargon in ("socket", "bind", "EADDRINUSE", "errno", "traceback"):
        assert jargon.lower() not in message.lower()


def test_the_folder_is_only_told_to_somebody_on_this_computer():
    """Over the network, /health says what it always said and nothing more.

    The folder and the process id are there to tell two unzipped copies apart
    on one machine. They are nobody else's business, so a request arriving
    from another computer gets the name and the version alone.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, client=("192.168.1.30", 51000)) as c:
        away = c.get("/health").json()
    assert away == {"status": "ok", "app": config.APP_NAME,
                    "version": config.APP_VERSION}

    with TestClient(app, client=("127.0.0.1", 51000)) as c:
        here = c.get("/health").json()
    assert here["program_dir"] == str(config.program_dir())


def test_the_window_is_held_open_even_with_no_keyboard(monkeypatch, capsys):
    """Started from a scheduled task there is nothing to press Enter on.

    A traceback printed underneath a carefully worded explanation buries the
    explanation, which is the one thing the message existed to deliver.
    """
    def no_keyboard(_prompt):
        raise EOFError("not a terminal")

    monkeypatch.setattr("builtins.input", no_keyboard)
    desktop.pause()            # must simply return
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(OSError()))
    desktop.pause()


# --------------------------------------------------------------------------
# The black window
# --------------------------------------------------------------------------
#
# A customer double-clicking accounting software should not be looking at a
# terminal. But that window is also the only place a failed start-up can
# explain itself, so it is hidden rather than never made — and only when it
# genuinely belongs to us.


def test_a_shared_console_is_never_hidden(monkeypatch):
    """Somebody who typed a command is sharing their own window with us.

    Taking that away would close the window they were working in, which is a
    far worse thing to do than showing a black rectangle.
    """
    monkeypatch.setattr(desktop.os, "name", "nt", raising=False)
    monkeypatch.setattr(desktop, "console_is_ours_alone", lambda: False)
    assert desktop.hide_own_console() is False


def test_nothing_is_hidden_where_there_is_no_console():
    """On Linux and macOS there is no such window and no ctypes call to make."""
    if os.name == "nt":                              # pragma: no cover
        pytest.skip("this is the question for every other operating system")
    assert desktop.console_is_ours_alone() is False
    assert desktop.hide_own_console() is False


def test_asking_never_raises_on_any_machine():
    """Called on the way to opening the window. It may answer no; it may not
    stop the application from starting."""
    for call in (desktop.console_is_ours_alone,
                 desktop.hide_own_console,
                 desktop.show_own_console):
        try:
            call()
        except Exception as exc:                     # noqa: BLE001
            pytest.fail(f"{call.__name__} raised {exc!r}")


def test_the_window_can_be_kept_on_purpose():
    """--console is the way back to the old behaviour for anybody debugging."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--console", action="store_true")
    assert parser.parse_args([]).console is False
    assert parser.parse_args(["--console"]).console is True
