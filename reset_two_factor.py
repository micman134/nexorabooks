"""Turn two-factor sign-in off for somebody who cannot sign in to turn it off.

Every other way back in — a recovery code, an administrator clearing it from
Settings › Users — needs somebody to be signed in. When the only administrator
is the one holding the broken phone, none of them help, and the books are shut
with nobody able to open them. That is not an acceptable state for software
that holds a business's accounts, so this exists.

It is not a back door. It runs on this computer, against the company file
sitting on this computer's disk, and anybody who can run it could already open
that file with any SQLite tool and do far worse. What it buys is that the
person who owns the books does not need to be the sort of person who owns a
SQLite tool.

Run it with no arguments and it will ask; or:

    python reset_two_factor.py --list
    python reset_two_factor.py --user ade
    python reset_two_factor.py --company acme-limited --user ade
    python reset_two_factor.py --company acme-limited --all

Every clearing is written to that company's audit trail, so it shows up later
in Reports › Audit trail with the date it happened.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import companies as registry, config, db as dbmod   # noqa: E402
from app.models import User                                   # noqa: E402
from app.services import twofactor                            # noqa: E402
from app.services.posting import audit                        # noqa: E402


def _companies() -> list:
    found = registry.all_companies(include_archived=True)
    if not found:
        print(f"\n  No company files found in {config.data_dir()}")
        print("  Start Nexora Books once first, or set NEXORA_DATA to where your books are.\n")
    return found


def _users(slug: str) -> list[tuple[int, str, str, bool]]:
    dbmod.init_db(slug)
    with dbmod.session_scope_for(slug) as db:
        return [
            (u.id, u.username, u.full_name or "", twofactor.is_on(u))
            for u in db.query(User).order_by(User.username).all()
        ]


def _clear(slug: str, usernames: list[str]) -> int:
    """Turn it off, then write it down — in that order, and separately.

    The order matters. This tool is the last resort, reached by somebody who
    cannot get into their own books; if the audit write were to fail for any
    reason, rolling the rescue back with it would leave them exactly as shut
    out as before. So the clearing is committed on its own and the record of it
    is a second, best-effort transaction.
    """
    dbmod.init_db(slug)
    cleared: list[tuple[int, str]] = []
    with dbmod.session_scope_for(slug) as db:
        for name in usernames:
            user = db.query(User).filter(User.username == name).one_or_none()
            if user is None:
                print(f"  ! no user called '{name}' in this company")
                continue
            if not (user.totp_secret or user.totp_enabled):
                print(f"  - {name}: two-factor was not on; nothing to do")
                continue
            twofactor.turn_off(user)
            cleared.append((user.id, name))
            print(f"  * {name}: two-factor sign-in cleared")
        db.commit()

    if cleared:
        try:
            with dbmod.session_scope_for(slug) as db:
                for user_id, name in cleared:
                    audit(db, db.get(User, user_id), "TWOFACTOR_CLEARED", "User", user_id,
                          detail=f"{name} — cleared with reset_two_factor.py on this computer")
                db.commit()
        except Exception as problem:            # a damaged file must not undo the rescue
            first_line = str(problem).splitlines()[0]
            print("\n  Note: the clearing worked, but it could not be written to this")
            print(f"  company's audit trail — {first_line}")
    return len(cleared)


def _choose(prompt: str, options: list[str]) -> int | None:
    for number, label in enumerate(options, start=1):
        print(f"   {number}. {label}")
    answer = input(f"\n{prompt} (or press Enter to stop): ").strip()
    if not answer.isdigit() or not (1 <= int(answer) <= len(options)):
        return None
    return int(answer) - 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn two-factor sign-in off for a user, without signing in.")
    parser.add_argument("--company", help="company folder name (see --list)")
    parser.add_argument("--user", action="append", default=[],
                        help="username to clear; may be given more than once")
    parser.add_argument("--all", action="store_true",
                        help="clear it for everybody in that company")
    parser.add_argument("--list", action="store_true",
                        help="show the companies and who has two-factor on")
    args = parser.parse_args(argv)

    print("\n  Nexora Books — two-factor rescue")
    print(f"  Books folder: {config.data_dir()}\n")

    found = _companies()
    if not found:
        return 1

    if args.list:
        for ref in found:
            print(f"  {ref.name}  [{ref.slug}]")
            for _id, username, full_name, on in _users(ref.slug):
                mark = "two-factor ON " if on else "              "
                print(f"      {mark} {username}" + (f"  ({full_name})" if full_name else ""))
            print()
        return 0

    ref = None
    if args.company:
        ref = registry.get(args.company)
        if ref is None:
            print(f"  No company called '{args.company}'. Try --list.")
            return 1
    elif len(found) == 1:
        ref = found[0]
    else:
        print("  Which company?")
        picked = _choose("Number", [f"{c.name}  [{c.slug}]" for c in found])
        if picked is None:
            print("\n  Nothing was changed.\n")
            return 1
        ref = found[picked]

    people = _users(ref.slug)
    names = [u[1] for u in people]

    if args.all:
        wanted = names
    elif args.user:
        wanted = args.user
    else:
        print(f"\n  {ref.name} — who is locked out?")
        labels = [
            f"{username}" + (f"  ({full_name})" if full_name else "")
            + ("   — two-factor is ON" if on else "")
            for _id, username, full_name, on in people
        ]
        picked = _choose("Number", labels)
        if picked is None:
            print("\n  Nothing was changed.\n")
            return 1
        wanted = [names[picked]]

    print()
    cleared = _clear(ref.slug, wanted)
    if cleared:
        print("\n  Done. Those people now sign in with their username and password")
        print("  alone. Ask them to set two-factor up again from their account page")
        print("  once the phone is sorted out.\n")
    else:
        print("\n  Nothing was changed.\n")
    return 0


if __name__ == "__main__":                                    # pragma: no cover
    raise SystemExit(main())
