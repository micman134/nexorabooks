"""Issue a licence for one customer's computer.

    python issue_licence.py

It asks for the customer's name and the machine code they sent you, signs a
licence with your private key, and writes it to a text file you can email back.
The customer opens Settings > Licence, pastes it in, and that is the whole
transaction.

The machine code is on the customer's Settings > Licence screen. It is a hash of
their installation — it tells you nothing about them, and a licence signed for
one machine code will not work on any other.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from app import licensing  # noqa: E402
from app.rsa_lite import sign  # noqa: E402

KEY = HERE / "seller" / "private-key.json"


def ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def main() -> int:
    if not KEY.exists():
        print(f"\n  No private key at {KEY}.")
        print("  Run  python make_licence_keys.py  first.\n")
        return 1
    key = json.loads(KEY.read_text(encoding="utf-8"))
    n, d = int(key["n"]), int(key["d"])

    if n != licensing.PUBLIC_KEY_N:
        print("\n  WARNING: this private key does not match the public key built into")
        print("  app/licensing.py. Licences signed with it will be rejected by the")
        print("  application. Paste the public half from make_licence_keys.py into")
        print("  app/licensing.py and rebuild before issuing anything.\n")
        if ask("Type CONTINUE to sign anyway") != "CONTINUE":
            return 1

    print("\n  Issue a Nexora Books licence")
    print("  ----------------------------\n")
    name = ask("  Customer or business name")
    machine = ask("  Machine code from their Settings > Licence screen").upper()
    if not name or not machine:
        print("\n  Both are needed. Nothing issued.\n")
        return 1

    print("\n  How long is it good for?")
    print("    1  Perpetual — never expires")
    print("    2  One year")
    print("    3  A number of days")
    choice = ask("  Choose", "1")
    if choice == "2":
        expires = date.today() + timedelta(days=365)
    elif choice == "3":
        expires = date.today() + timedelta(days=int(ask("  Days", "30") or 30))
    else:
        expires = None

    users = int(ask("  Number of users they paid for (0 for no limit)", "0") or 0)
    companies = int(ask("  Maximum companies (0 for no limit)", "0") or 0)
    edition = ask("  Edition", "Standard")
    note = ask("  Note (order number, anything — optional)", "")

    payload = {
        "name": name,
        "machine": machine,
        "issued": date.today().isoformat(),
        "expires": expires.isoformat() if expires else None,
        "companies": companies,
        "users": users,
        "edition": edition,
        "note": note,
    }
    message = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    text = licensing.build(payload, sign(message, n, d))

    check = licensing.read(text)
    if check is None:
        print("\n  The licence just signed does not verify against the built-in public")
        print("  key. Do not send it. Check that app/licensing.py carries the public")
        print("  half of this private key.\n")
        return 1

    safe = "".join(c if c.isalnum() else "-" for c in name).strip("-").lower()
    out = HERE / f"licence-{safe or 'customer'}-{date.today().isoformat()}.txt"
    out.write_text(
        f"Nexora Books licence for {name}\n"
        f"Computer: {machine}\n"
        f"Users: {'no limit' if not users else users}\n"
        f"{'Never expires' if expires is None else 'Valid until ' + expires.isoformat()}\n"
        f"\nOpen Settings > Licence in Nexora Books and paste everything below the line.\n"
        f"{'-' * 68}\n{text}\n",
        encoding="utf-8",
    )
    print(f"\n  Written to {out.name}")
    print("  Email that file to the customer. It works only on their computer.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
