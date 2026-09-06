"""Encrypting the connection, and making a certificate when there is none.

Nexora Books was built for one office network, where a password crossing a
cable between two desks in the same building is not much of a risk. The moment
a member of staff signs in from another state that stops being true: without
encryption their password, their session cookie and every figure they look at
travel in the clear past everybody between them and the office.

So this module does two things.

**It turns on TLS when a certificate is available.** A real one, from a real
authority, is always better — Tailscale hands them out for free on a private
network, and that is the arrangement to prefer, because the browser simply
works with no warning to teach people to click through.

**And it can make one when there is not.** A certificate this software signs
itself encrypts the traffic exactly as well as a purchased one; what it cannot
do is prove to a browser which computer it belongs to, so the browser warns the
first time. That warning is worth understanding rather than dismissing: it is
the difference between "nobody can read this" and "nobody can read this *and*
you know who you are talking to". On an office network where you can walk over
and look at the machine, the first is usually enough.

The certificate is written by hand, in DER, on top of ``rsa_lite``. That is
more work than calling a library, and it is done that way for the same reason
everything else here is: adding a dependency that needs a C compiler would mean
a customer cannot install this software without one, and no encryption is worth
that trade.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, fileguard, rsa_lite

#: How long a certificate this software makes is good for. Long, because
#: nobody in a small office is going to enjoy renewing it, and it is only ever
#: trusted by people who chose to trust it.
YEARS = 5


def _utc_now() -> datetime:
    """The only place in this software that is allowed to care about Greenwich.

    Everything a business does is recorded on the local clock — see
    ``app/clock.py`` for why. A certificate is the exception: X.509 says its
    validity dates are UTC, and a browser in Lagos and a browser in Manila both
    have to read the same answer out of the same file.

    Written with an aware datetime rather than the deprecated no-argument
    call Python is in the process of removing; the ``tzinfo`` is dropped again
    on the way out because the DER encoding carries the ``Z`` itself.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# The little bit of ASN.1 a certificate needs
# ---------------------------------------------------------------------------


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _int(value: int) -> bytes:
    if value == 0:
        return _tlv(0x02, b"\x00")
    body = value.to_bytes((value.bit_length() + 8) // 8, "big")
    return _tlv(0x02, body)


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _set(*parts: bytes) -> bytes:
    return _tlv(0x31, b"".join(parts))


def _oid(dotted: str) -> bytes:
    numbers = [int(n) for n in dotted.split(".")]
    body = bytes([40 * numbers[0] + numbers[1]])
    for number in numbers[2:]:
        chunk = [number & 0x7F]
        number >>= 7
        while number:
            chunk.append((number & 0x7F) | 0x80)
            number >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, body)


def _utf8(text: str) -> bytes:
    return _tlv(0x0C, text.encode("utf-8"))


def _bits(data: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + data)


def _time(when: datetime) -> bytes:
    """UTCTime while the year fits in two digits, as X.509 requires."""
    if when.year < 2050:
        return _tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode("ascii"))
    return _tlv(0x18, when.strftime("%Y%m%d%H%M%SZ").encode("ascii"))


OID_RSA = "1.2.840.113549.1.1.1"
OID_SHA256_RSA = "1.2.840.113549.1.1.11"
OID_CN = "2.5.4.3"
OID_O = "2.5.4.10"
OID_SAN = "2.5.29.17"
OID_BASIC = "2.5.29.19"
OID_KEY_USE = "2.5.29.15"
OID_EXT_KEY_USE = "2.5.29.37"
OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1"


def _name(common: str, organisation: str) -> bytes:
    return _seq(
        _set(_seq(_oid(OID_O), _utf8(organisation))),
        _set(_seq(_oid(OID_CN), _utf8(common))),
    )


def _public_key(n: int, e: int) -> bytes:
    return _seq(
        _seq(_oid(OID_RSA), _tlv(0x05, b"")),
        _bits(_seq(_int(n), _int(e))),
    )


def _alt_names(names: list[str]) -> bytes:
    parts = []
    for name in names:
        if _looks_like_an_address(name):
            parts.append(_tlv(0x87, bytes(int(p) for p in name.split("."))))
        else:
            parts.append(_tlv(0x82, name.encode("ascii")))
    return _tlv(0x04, _seq(*parts))


def _looks_like_an_address(name: str) -> bool:
    bits = name.split(".")
    return len(bits) == 4 and all(b.isdigit() and 0 <= int(b) <= 255 for b in bits)


def _extensions(names: list[str]) -> bytes:
    return _tlv(
        0xA3,
        _seq(
            _seq(_oid(OID_BASIC), _tlv(0x04, _seq())),
            _seq(_oid(OID_KEY_USE), _tlv(0x01, b"\xff"),
                 _tlv(0x04, _bits(b"\xa0"))),          # digitalSignature, keyEncipherment
            _seq(_oid(OID_EXT_KEY_USE), _tlv(0x04, _seq(_oid(OID_SERVER_AUTH)))),
            _seq(_oid(OID_SAN), _alt_names(names)),
        ),
    )


def _pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


def _private_key_pem(n: int, e: int, d: int, p: int, q: int) -> str:
    """PKCS#1, which is what every server library reads without complaint."""
    der = _seq(
        _int(0), _int(n), _int(e), _int(d), _int(p), _int(q),
        _int(d % (p - 1)), _int(d % (q - 1)), _int(pow(q, -1, p)),
    )
    return _pem("RSA PRIVATE KEY", der)


# ---------------------------------------------------------------------------
# Making one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Certificate:
    cert_path: Path
    key_path: Path
    names: tuple[str, ...]
    fingerprint: str
    expires: datetime

    @property
    def exists(self) -> bool:
        return self.cert_path.exists() and self.key_path.exists()


def folder() -> Path:
    place = config.data_dir() / "certificate"
    place.mkdir(parents=True, exist_ok=True)
    return place


def paths() -> tuple[Path, Path]:
    place = folder()
    return place / "server.crt", place / "server.key"


def make(names: list[str], organisation: str = "", bits: int = 2048) -> Certificate:
    """Write a self-signed certificate covering these names. Returns where it went.

    ``names`` is every address a browser might use to reach this computer — the
    machine name, its address on the office network, and localhost. A name that
    is not in here produces a second, louder warning in the browser, so it is
    better to be generous.
    """
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n]
    if not names:
        names = ["localhost", "127.0.0.1"]
    organisation = organisation or config.APP_NAME

    n, e, d = rsa_lite.generate(bits)
    p, q = _factors(n, e, d)

    start = _utc_now() - timedelta(days=1)
    end = start + timedelta(days=365 * YEARS)
    serial = int.from_bytes(os.urandom(16), "big") >> 1 or 1

    algorithm = _seq(_oid(OID_SHA256_RSA), _tlv(0x05, b""))
    tbs = _seq(
        _tlv(0xA0, _int(2)),                       # version 3
        _int(serial),
        algorithm,
        _name(names[0], organisation),             # self-signed: issuer is subject
        _seq(_time(start), _time(end)),
        _name(names[0], organisation),
        _public_key(n, e),
        _extensions(names),
    )
    signature = rsa_lite.sign(tbs, n, d)
    der = _seq(tbs, algorithm, _bits(signature))

    cert_path, key_path = paths()
    cert_path.write_text(_pem("CERTIFICATE", der), encoding="utf-8")
    key_path.write_text(_private_key_pem(n, e, d, p, q), encoding="utf-8")
    fileguard.restrict_to_owner(key_path)

    return Certificate(
        cert_path=cert_path, key_path=key_path, names=tuple(names),
        fingerprint=fingerprint(der), expires=end)


def _factors(n: int, e: int, d: int) -> tuple[int, int]:
    """Recover the two primes from the key, which PKCS#1 needs and RSA hides.

    The standard method from the RSA specification: ``ed - 1`` is a multiple of
    the order of the group, and halving it repeatedly finds a square root of one
    that is not one, which shares a factor with n.
    """
    k = d * e - 1
    t = k
    while t % 2 == 0:
        t //= 2
        for base in (2, 3, 5, 7, 11, 13, 17, 19, 23):
            candidate = pow(base, t, n)
            if candidate in (1, n - 1):
                continue
            factor = _gcd(candidate - 1, n)
            if 1 < factor < n:
                return factor, n // factor
    raise ValueError("could not recover the key's factors")   # pragma: no cover


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


def fingerprint(der: bytes) -> str:
    """The SHA-256 fingerprint, written the way a browser writes it.

    Worth showing on screen: it is how somebody confirms that the certificate
    their browser is warning about is the one this computer actually made, and
    not somebody sitting in the middle.
    """
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def read(cert_path: Path | str) -> bytes | None:
    """The DER inside a PEM file, or None if it is not one."""
    try:
        text = Path(cert_path).read_text(encoding="utf-8")
    except OSError:
        return None
    if "-----BEGIN CERTIFICATE-----" not in text:
        return None
    body = text.split("-----BEGIN CERTIFICATE-----", 1)[1]
    body = body.split("-----END CERTIFICATE-----", 1)[0]
    try:
        return base64.b64decode("".join(body.split()))
    except Exception:                               # noqa: BLE001
        return None


def existing() -> Certificate | None:
    """The certificate already on this computer, if there is a usable one."""
    cert_path, key_path = paths()
    if not (cert_path.exists() and key_path.exists()):
        return None
    der = read(cert_path)
    if der is None:
        return None
    return Certificate(cert_path=cert_path, key_path=key_path, names=(),
                       fingerprint=fingerprint(der), expires=_utc_now())
