"""Make your own signing keypair. Run this once, before you sell anything.

    python make_licence_keys.py

It writes seller/private-key.json and prints the public half for you to paste
into app/licensing.py.

Why you must run it
-------------------
This copy of Nexora Books ships with a working keypair so that licensing runs
out of the box and can be tested. That keypair is not secret — it came with the
software, so anybody who has the software has it. Until you replace it, anybody
could issue licences for your product.

The private key is the whole business. Keep it on one computer you control.
Never put it in the folder you send to customers, never put it in the .exe, and
never email it. If it leaks, every licence you have ever sold becomes forgeable
and the only fix is a new key and a new build.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import fileguard  # noqa: E402
from app.rsa_lite import generate, sign, verify  # noqa: E402

OUT = Path(__file__).resolve().parent / "seller" / "private-key.json"


def main() -> int:
    if OUT.exists():
        print(f"\n  {OUT} already exists.")
        print("  Replacing it makes every licence you have already issued stop working.")
        if input("  Type REPLACE to go ahead: ").strip() != "REPLACE":
            print("  Nothing changed.\n")
            return 1

    print("\n  Generating a 2048-bit key. This takes a few seconds...\n")
    n, e, d = generate(2048)

    probe = b"nexora-books-selftest"
    if not verify(probe, sign(probe, n, d), n, e):
        print("  The generated key did not verify its own signature. Nothing written.")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"n": str(n), "e": e, "d": str(d)}, indent=2),
                   encoding="utf-8")
    fileguard.restrict_to_owner(OUT)

    print(f"  Private key written to {OUT}")
    print("  Keep it. Back it up somewhere only you can reach. Never ship it.\n")
    print("  Now paste these lines into app/licensing.py, replacing PUBLIC_KEY_N:\n")
    print("PUBLIC_KEY_N = int(")
    for line in textwrap.wrap(str(n), 70):
        print(f'    "{line}"')
    print(")")
    print(f"PUBLIC_KEY_E = {e}\n")
    print("  Then rebuild. Licences issued with the old key will stop working,")
    print("  which is what you want if the old key was ever anywhere else.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
