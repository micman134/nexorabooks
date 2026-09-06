"""Which address other computers can actually reach this one at.

There is a difference between the address in the person's own browser and the
address that means anything to anybody else, and getting it wrong is not a
cosmetic mistake. An invitation emailed to a member of staff carrying
``http://127.0.0.1:8756/invite/...`` sends them to *their own* computer, which
is not running anything, so the browser says the connection was refused. The
link is perfectly valid; it just points at the wrong machine. That was a real
bug and this module exists to make it impossible to repeat.

The rules, in order:

  1. If an administrator has written down the address staff use — because
     there is a fixed IP, a name on the office network, or something in front
     of it — that is the truth and nothing here second-guesses it.
  2. Otherwise, if the person doing the inviting is themselves looking at a
     real network address, that address demonstrably works from another
     computer, because they are on one.
  3. Otherwise, find this computer's address on the local network.
  4. And if none of that produces an address other people can reach, say so
     rather than sending somebody a link that cannot work.
"""
from __future__ import annotations

import re
import socket
from urllib.parse import urlsplit

from . import config

#: Names and addresses that mean "the computer I am typing on" and therefore
#: mean something different on every computer they are read on.
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "::1", "0.0.0.0", ""}


def is_loopback(host: str | None) -> bool:
    """True when this address only ever means 'here'."""
    name = str(host or "").strip().lower().strip("[]")
    if name in LOOPBACK_NAMES:
        return True
    return name.startswith("127.")


def host_of(url: str) -> str:
    parts = urlsplit(str(url or ""))
    return (parts.hostname or "").lower()


def lan_addresses() -> list[str]:
    """This computer's addresses on the local network, best guess first.

    Two ways of asking, because neither is reliable on its own: a machine with
    no DNS entry for its own name answers nothing to the first, and a machine
    with no route out answers nothing to the second.
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not is_loopback(ip):
                found.append(ip)
    except (socket.gaierror, OSError):
        pass
    if not found:
        try:
            # Nothing is sent: connecting a UDP socket only picks the route,
            # which is how the operating system is asked "which of my
            # addresses would you use to talk to the world?"
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                ip = probe.getsockname()[0]
            finally:
                probe.close()
            if not is_loopback(ip):
                found.append(ip)
        except OSError:
            pass
    return found


def port_of(base_url: str | None = None) -> int:
    """The port this is actually being served on, not the one in the settings."""
    if base_url:
        parts = urlsplit(str(base_url))
        if parts.port:
            return parts.port
        if parts.scheme == "https":
            return 443
        if parts.scheme == "http" and parts.hostname:
            return 80
    return config.SERVER_PORT


#: A host name or an IP address and nothing else. Deliberately strict: what
#: this produces is pasted into an email, and "something that looked like an
#: address" is exactly how the original bug got out.
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def tidy(address: str, fallback_port: int | None = None) -> str:
    """Turn whatever an administrator typed into a usable base address, or ''.

    People write ``192.168.1.20``, ``192.168.1.20:8756/``, ``books.local`` and
    ``http://books.local:8756``. All four mean the same thing and all four
    should work. Anything that is not an address at all comes back empty, so
    the caller can say so instead of emailing it to somebody.

    The port is only filled in when they did not name a scheme themselves.
    Somebody typing ``https://accounts.example.com`` means the ordinary web
    address, not that address with this application's port stuck on the end.
    """
    text = str(address or "").strip()
    if not text:
        return ""
    named_scheme = "://" in text
    if not named_scheme:
        text = "http://" + text

    parts = urlsplit(text)
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https"):
        return ""
    try:
        host, port = parts.hostname or "", parts.port
    except ValueError:                      # a port that is not a number
        return ""
    if not host or not _HOST.match(host):
        return ""
    if port is None and not named_scheme:
        port = fallback_port

    default = 443 if scheme == "https" else 80
    tail = "" if not port or port == default else f":{port}"
    return f"{scheme}://{host}{tail}" + parts.path.rstrip("/")


def reachable_base(base_url: str | None = None, stated: str = "") -> tuple[str, str]:
    """The address to put in a link somebody else will click.

    Returns ``(url, how)``, where ``how`` is one of ``"stated"`` (an
    administrator wrote it down), ``"browser"`` (the address in use is already
    a real one), ``"detected"`` (found on the local network) or ``""`` — the
    last meaning no address was found that would work anywhere but here, and
    the caller should say so rather than send a broken link.
    """
    port = port_of(base_url)

    settled = tidy(stated, port)
    if settled and not is_loopback(host_of(settled)):
        return settled, "stated"

    if base_url and not is_loopback(host_of(base_url)):
        return str(base_url).rstrip("/"), "browser"

    for ip in lan_addresses():
        return f"http://{ip}:{port}", "detected"

    return "", ""
