"""Nexora Books desktop launcher.

Double-clicking NexoraBooks.exe runs this. It starts the application server on
this machine and opens it in a normal desktop window. The same server is what
staff on the office network connect to with a browser, so one running copy
serves everybody.

    NexoraBooks.exe              open the desktop window (normal use)
    NexoraBooks.exe --server     run without a window, e.g. as a background service
    NexoraBooks.exe --port 9000  listen on a different port
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser

from app import config

WINDOW_TITLE = f"{config.APP_NAME} — Accounting"

SW_HIDE = 0
SW_SHOW = 5


def console_is_ours_alone() -> bool:
    """Is that black window one Windows made for us, and nobody else?

    Worth asking carefully. Double-clicking the application gives it a console
    of its own, and hiding that is a kindness — a customer running accounting
    software should not be looking at a terminal. But somebody who started it
    by typing a command is *sharing* a window with their command prompt, and
    hiding that would take away the window they were working in. Windows can
    tell the two apart: a console of our own has exactly one program attached.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        if not kernel32.GetConsoleWindow():
            return False                            # no console at all
        buffer = (ctypes.c_uint32 * 8)()
        attached = kernel32.GetConsoleProcessList(buffer, 8)
        return attached == 1
    except Exception:                               # noqa: BLE001
        return False


def _show_console(state: int) -> bool:
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()   # type: ignore[attr-defined]
        if not window:
            return False
        ctypes.windll.user32.ShowWindow(window, state)       # type: ignore[attr-defined]
        return True
    except Exception:                               # noqa: BLE001
        return False


def hide_own_console() -> bool:
    """Put the black window away. True only if there was one to put away."""
    if not console_is_ours_alone():
        return False
    return _show_console(SW_HIDE)


def show_own_console() -> bool:
    """Bring it back — on the way out, so a parting message can be read."""
    return _show_console(SW_SHOW)


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        try:
            return s.connect_ex(("127.0.0.1", port)) == 0
        except OSError:
            return False


def who_is_answering(port: int, timeout: float = 2.0) -> dict:
    """Ask whatever holds this port to identify itself. ``{}`` if it will not.

    Only the standard library is used, because this runs before the check for
    missing libraries — a copy that cannot import anything must still be able
    to work out what is going on.
    """
    import json
    import ssl
    import urllib.request

    # Both, because the copy already running may have been started with
    # encryption on while this one has not, or the other way about.
    relaxed = ssl.create_default_context()
    relaxed.check_hostname = False
    relaxed.verify_mode = ssl.CERT_NONE
    for prefix, context in (("http", None), ("https", relaxed)):
        try:
            with urllib.request.urlopen(
                f"{prefix}://127.0.0.1:{port}/health", timeout=timeout, context=context
            ) as answer:
                body = json.loads(answer.read().decode("utf-8", "replace"))
            if isinstance(body, dict) and body.get("app"):
                return body
        except Exception:                           # noqa: BLE001 — try the other one
            continue
    return {}


def explain_the_other_copy(port: int, running: dict) -> str:
    """What to say when the copy already running is not the one just started.

    This exists because of a real afternoon lost to it. Somebody downloaded a
    new version, unzipped it to a different folder, started it — and was shown
    the *old* version, because the old one had never been closed and still held
    the port. The launcher saw the port was busy, assumed it was looking at
    itself, and opened a browser at whatever was there. Nothing on screen said
    the version was not the one they had just started, so moving the folder,
    unzipping again and restarting all appeared to change nothing.
    """
    theirs = running.get("version") or "an unknown version"
    where = running.get("program_dir")
    pid = running.get("pid")

    lines = [
        f"You started {config.APP_NAME} {config.APP_VERSION}, but version "
        f"{theirs} is already running on this computer and is using port "
        f"{port}.",
        "",
        "Two copies cannot share one port, so nothing has been started. If a "
        "browser is opened now it would show you the older copy — which is "
        "exactly the confusion this message exists to prevent.",
        "",
        "To use the version you just started:",
        "",
        f"  1. Close every {config.APP_NAME} window, including any black "
        "command window behind them.",
    ]
    if where:
        lines.append(f"     The copy that is running was started from:\n"
                     f"       {where}")
    lines += [
        "  2. If it is still running, press Ctrl+Shift+Esc for Task Manager "
        "and end",
        f"     {'the process with id ' + str(pid) if pid else 'NexoraBooks.exe, python.exe or py.exe'}"
        ". Restarting the computer does the same job.",
        "  3. Start this copy again.",
        "",
        "Your books are not affected either way — they are kept outside the "
        f"program folder, in:\n  {config.data_dir()}",
        "",
        f"To run both at once instead, give this one its own port:\n"
        f"    NexoraBooks.exe --port 9000",
    ]
    return "\n".join(lines)


def pause(message: str = "\nPress Enter to close…") -> None:
    """Hold the window open so the message above can be read.

    Wrapped because there is not always a keyboard: started from a scheduled
    task, a service, or a pipe, ``input`` raises rather than waiting, and a
    traceback printed underneath a carefully worded explanation buries it.
    """
    try:
        input(message)
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def local_addresses() -> list[str]:
    """Best guess at the addresses staff should type into their browsers."""
    found: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        found.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except socket.gaierror:
        pass
    return found


#: What went wrong inside the server thread, if anything did. A thread that
#: dies takes its traceback with it, and the launcher used to notice only that
#: the port never opened — so a missing library was reported as a port clash,
#: which sent people looking in entirely the wrong place.
_startup_error: BaseException | None = None

#: What the software cannot run without, and the name to install it by.
REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("sqlalchemy", "sqlalchemy"),
    ("jinja2", "jinja2"),
    ("multipart", "python-multipart"),
    ("itsdangerous", "itsdangerous"),
]


def missing_libraries() -> list[str]:
    """Anything needed that is not installed, by the name pip knows it as."""
    import importlib.util

    missing = []
    for module, package in REQUIRED:
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(package)
        except (ImportError, ValueError):
            missing.append(package)
    return missing


def explain_missing(missing: list[str]) -> str:
    """Say what is missing and exactly what to type to fix it."""
    here = os.path.dirname(os.path.abspath(__file__))
    return (
        f"{config.APP_NAME} cannot start because "
        + ("a library it needs is" if len(missing) == 1 else
           "some libraries it needs are")
        + " not installed:\n\n    "
        + ", ".join(missing)
        + "\n\nThis is a one-off. Open a command prompt in\n    "
        + here
        + "\nand run:\n\n    pip install -r requirements.txt\n\n"
        "Then start it again. (If pip is not recognised either, install Python\n"
        "from python.org and tick 'Add python.exe to PATH' during setup.)"
    )


def serve(host: str, port: int) -> None:
    global _startup_error
    try:
        import uvicorn

        from app.main import app

        extra = {}
        if config.serving_over_tls():
            settings = config.tls_settings()
            extra = {"ssl_certfile": settings["cert"], "ssl_keyfile": settings["key"]}
        uvicorn.run(app, host=host, port=port, log_level="warning",
                    access_log=False, **extra)
    except BaseException as exc:                    # noqa: BLE001 — reported below
        _startup_error = exc


def scheme() -> str:
    return "https" if config.serving_over_tls() else "http"


def wait_until_ready(port: int, thread: threading.Thread | None = None,
                     timeout: float = 40.0) -> bool:
    """Wait for the server, but stop the moment it is clear it is not coming.

    Sitting out a forty-second timeout when the server thread died in the first
    half-second is just a slower way of saying the same thing.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use("127.0.0.1", port):
            return True
        if _startup_error is not None:
            return False
        if thread is not None and not thread.is_alive():
            return False
        time.sleep(0.2)
    return False


def banner(port: int) -> None:
    addresses = local_addresses()
    line = "=" * 62
    print(line)
    print(f"  {config.APP_NAME} {config.APP_VERSION} is running")
    print(line)
    print(f"  On this computer:  {scheme()}://localhost:{port}")
    for ip in addresses:
        print(f"  On the network:    {scheme()}://{ip}:{port}")
    print()
    print(f"  Started from:\n    {config.program_dir()}")
    print(f"  Your books are stored in:\n    {config.data_dir()}")
    print()
    print("  Leave this window open while anyone is using Nexora Books.")
    print("  Closing it signs everyone out.")
    print(line, flush=True)


def _why_it_did_not_start(port: int) -> str:
    """The real reason, when there is one, rather than a guess about the port."""
    error = _startup_error
    if isinstance(error, ModuleNotFoundError):
        return explain_missing([error.name or "a library it needs"])
    if isinstance(error, OSError) and getattr(error, "errno", None) in (98, 10048):
        return (f"{config.APP_NAME} could not start: something else on this "
                f"computer is already using port {port}.\n"
                f"Close it, or start with a different port:\n"
                f"    NexoraBooks.exe --port 9000")
    if error is not None:
        return (f"{config.APP_NAME} could not start.\n\n    "
                f"{type(error).__name__}: {error}\n\n"
                "The details above are what went wrong. Nothing in your books "
                "has been changed.")
    return (f"{config.APP_NAME} did not finish starting up.\n"
            f"Check that no other program is using port {port}, then try again.")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{config.APP_NAME} accounting")
    parser.add_argument("--server", action="store_true",
                        help="run without opening a desktop window")
    parser.add_argument("--browser", action="store_true",
                        help="open in your normal web browser instead of a window")
    parser.add_argument("--console", action="store_true",
                        help="keep the black status window open behind the "
                             "application (it is hidden by default once the "
                             "application window is up)")
    parser.add_argument("--host", default=config.SERVER_HOST,
                        help="address to listen on (default: every network address)")
    parser.add_argument("--port", type=int, default=config.SERVER_PORT)
    parser.add_argument("--reset-two-factor", action="store_true",
                        help="turn two-factor sign-in off for somebody who is "
                             "locked out, without signing in")
    args, rest = parser.parse_known_args()

    # The one thing that has to work when the application itself cannot be
    # reached. It is here as well as in reset_two_factor.py because a customer
    # running the built .exe has no Python to run that script with, and the
    # person who most needs this is by definition the one who cannot get in.
    if args.reset_two_factor:
        from reset_two_factor import main as rescue

        raise SystemExit(rescue(rest))
    if rest:
        parser.error("unrecognised arguments: " + " ".join(rest))

    port = args.port
    url = f"{scheme()}://127.0.0.1:{port}"

    # If a copy is already running, join it rather than fighting over the port —
    # but only once it has said that it is the same version. Opening a browser
    # at an older copy still holding the port is how somebody spends an
    # afternoon wondering why their update changed nothing.
    if port_in_use(args.host, port):
        running = who_is_answering(port)
        if running.get("version") and running["version"] != config.APP_VERSION:
            print(explain_the_other_copy(port, running))
            pause()
            sys.exit(1)
        if running.get("app") and running["app"] != config.APP_NAME:
            print(f"Something else on this computer is already using port {port}. "
                  f"Start {config.APP_NAME} on another one:\n"
                  f"    NexoraBooks.exe --port 9000")
            pause()
            sys.exit(1)
        print(f"{config.APP_NAME} {config.APP_VERSION} is already running — "
              "opening the existing window.")
        webbrowser.open(url)
        return

    # Say what is actually wrong before starting anything. A missing library is
    # the commonest way a first run fails, and it deserves an instruction
    # rather than a traceback.
    missing = missing_libraries()
    if missing:
        print(explain_missing(missing))
        pause()
        sys.exit(1)

    server = threading.Thread(target=serve, args=(args.host, port), daemon=True)
    server.start()

    if not wait_until_ready(port, server):
        print(_why_it_did_not_start(port))
        pause("Press Enter to close…")
        sys.exit(1)

    banner(port)

    if args.server:
        try:
            while server.is_alive():
                server.join(1)
        except KeyboardInterrupt:
            print("\nShutting down. Goodbye.")
        return

    if not args.browser:
        try:
            import webview  # type: ignore

            webview.create_window(
                WINDOW_TITLE, url,
                width=1360, height=880, min_size=(1024, 680),
                confirm_close=True,
            )
            # There is a real window now, so the black one behind it is just
            # clutter. Hidden rather than never created: if starting up had
            # failed, the messages above are the only explanation anybody gets.
            hidden = args.console is False and hide_own_console()
            try:
                webview.start()
            finally:
                if hidden:
                    show_own_console()
            return
        except Exception:
            # No native window available — a browser works just as well
            pass

    webbrowser.open(url)
    try:
        while server.is_alive():
            server.join(1)
    except KeyboardInterrupt:
        print("\nShutting down. Goodbye.")


if __name__ == "__main__":
    multiprocessing.freeze_support()  # required for a PyInstaller build on Windows
    main()
