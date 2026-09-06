"""Just enough RSA to check a signature, in plain Python.

A licence has to be checked on a computer with no internet connection, and it
has to be impossible for the person holding the copy to mint more of them. That
rules out a shared secret — anything the application can use to *make* a key, a
determined customer can extract and use to make their own. It needs a signature:
the seller keeps the private half, every copy of the application carries only
the public half, and the public half can verify but never forge.

RSA verification is one modular exponentiation and a padding check, so it is
written out here rather than pulled in. That keeps the Windows build free of
compiled extensions, which is the reason the whole application has no binary
dependencies. It is slower than a C library and it does not matter: this runs
once, at start-up, on a message of about two hundred bytes.

The signing and key-generation halves are here too, because the seller has to
be able to issue keys — but they are used only by the two command-line tools,
never by the application itself.
"""
from __future__ import annotations

import hashlib
import secrets

#: DER prefix for a SHA-256 digest inside a PKCS#1 v1.5 signature block.
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _int_to_bytes(value: int, length: int) -> bytes:
    return value.to_bytes(length, "big")


def _bytes_to_int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _pkcs1_encode(message: bytes, key_len: int) -> bytes:
    """EMSA-PKCS1-v1_5: 0x00 0x01 0xFF... 0x00 DigestInfo."""
    digest = hashlib.sha256(message).digest()
    tail = SHA256_PREFIX + digest
    padding_len = key_len - len(tail) - 3
    if padding_len < 8:
        raise ValueError("key too small for a SHA-256 signature")
    return b"\x00\x01" + b"\xff" * padding_len + b"\x00" + tail


def verify(message: bytes, signature: bytes, n: int, e: int = 65537) -> bool:
    """True when this signature was made over this message by the private key."""
    key_len = (n.bit_length() + 7) // 8
    if len(signature) > key_len:
        return False
    try:
        recovered = _int_to_bytes(pow(_bytes_to_int(signature), e, n), key_len)
        expected = _pkcs1_encode(message, key_len)
    except (ValueError, OverflowError):
        return False
    # The comparison is not secret — the message and the public key both are
    # public — but constant time costs nothing and avoids a bad habit.
    return secrets.compare_digest(recovered, expected)


# --------------------------------------------------------------------------
# The seller's half. Never called by the application.
# --------------------------------------------------------------------------


def sign(message: bytes, n: int, d: int) -> bytes:
    key_len = (n.bit_length() + 7) // 8
    block = _pkcs1_encode(message, key_len)
    return _int_to_bytes(pow(_bytes_to_int(block), d, n), key_len)


def _is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        candidate = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def generate(bits: int = 2048, e: int = 65537) -> tuple[int, int, int]:
    """A fresh keypair as (n, e, d). Slow — it is run once, ever."""
    half = bits // 2
    while True:
        p, q = _prime(half), _prime(half)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        if phi % e == 0:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        return n, e, pow(e, -1, phi)
