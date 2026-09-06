"""One clock, and it is the one on the wall in the room.

Timestamps used to be recorded in UTC and printed unchanged. In Lagos that put
the audit trail an hour behind everybody's watch. In Manila it put it eight
hours behind, which before eight in the morning means the audit trail says the
work was done *yesterday*.

That is not a cosmetic problem. An audit trail exists to answer when something
happened, and it also has to agree with the software's own business dates,
which have always been local. These tests pin both.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

os.environ["NEXORA_DATA"] = tempfile.mkdtemp(prefix="nexora-clock-")

from sqlalchemy import select  # noqa: E402

from app import clock, companies as registry, db as dbmod  # noqa: E402
from app.models import AuditLog, Company, User  # noqa: E402
from app.seed import bootstrap  # noqa: E402
from app.services import invites  # noqa: E402
from app.services.posting import audit  # noqa: E402

SOURCE = Path(__file__).resolve().parent.parent


@pytest.fixture()
def home():
    tmp = tempfile.mkdtemp(prefix="nexora-clock-")
    os.environ["NEXORA_DATA"] = tmp
    dbmod.reset_all()
    ref = registry.ensure_at_least_one()
    dbmod.init_db(ref.slug)
    with dbmod.session_scope_for(ref.slug) as db:
        bootstrap(db)
        db.get(Company, 1).setup_complete = True
    dbmod.reset_all()
    yield tmp
    dbmod.reset_all()
    shutil.rmtree(tmp, ignore_errors=True)


def test_the_clock_is_the_local_one():
    assert abs((clock.now() - datetime.now()).total_seconds()) < 2
    assert clock.today() == date.today()


def test_an_audit_entry_agrees_with_the_clock_in_the_room(home):
    with dbmod.session_scope_for(registry.default_slug()) as db:
        user = db.scalar(select(User))
        audit(db, user, "TEST", "User", user.id, detail="what time is it")
        db.commit()
        entry = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()

    assert abs((entry.at - datetime.now()).total_seconds()) < 120, (
        "the audit trail must not be hours away from the wall clock")


def test_an_audit_entry_agrees_with_the_business_date(home):
    """An invoice dated today with an audit entry dated yesterday is indefensible."""
    with dbmod.session_scope_for(registry.default_slug()) as db:
        user = db.scalar(select(User))
        audit(db, user, "TEST", "User", user.id)
        db.commit()
        entry = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    assert entry.at.date() == date.today()


#: Three, not a dozen. Each one starts a whole new process, and these are the
#: cases that differ: eight hours ahead, seven behind, and the one hour that
#: made the original bug easy to shrug at.
@pytest.mark.parametrize("zone", ["Asia/Manila", "America/Los_Angeles", "Africa/Lagos"])
def test_it_holds_in_every_part_of_the_world(zone):
    """Run the check again in a fresh process, pretending to be somewhere else.

    The old code passed in London and nowhere else, which is exactly the kind
    of bug that only shows up once the software is sold abroad.
    """
    script = (
        "import os, tempfile, sys\n"
        "os.environ['NEXORA_DATA'] = tempfile.mkdtemp()\n"
        f"sys.path.insert(0, {str(SOURCE)!r})\n"
        "from datetime import datetime, date\n"
        "from sqlalchemy import select\n"
        "from app import companies as registry, db as dbmod\n"
        "from app.models import AuditLog, User\n"
        "from app.seed import bootstrap\n"
        "from app.services.posting import audit\n"
        "ref = registry.ensure_at_least_one(); dbmod.init_db(ref.slug)\n"
        "with dbmod.session_scope_for(ref.slug) as db:\n"
        "    bootstrap(db); db.commit()\n"
        "with dbmod.session_scope_for(ref.slug) as db:\n"
        "    u = db.scalar(select(User))\n"
        "    audit(db, u, 'TEST', 'User', u.id); db.commit()\n"
        "    e = db.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()\n"
        "    gap = abs((e.at - datetime.now()).total_seconds())\n"
        "    print('GAP', int(gap), 'DATE', e.at.date() == date.today())\n"
    )
    env = dict(os.environ, TZ=zone)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, env=env, cwd=str(SOURCE))
    assert "GAP" in result.stdout, result.stderr[-800:]
    line = [l for l in result.stdout.splitlines() if l.startswith("GAP")][0]
    gap = int(line.split()[1])
    same_day = line.split()[3] == "True"
    assert gap < 120, f"in {zone} the audit trail is {gap} seconds off the clock"
    assert same_day, f"in {zone} the audit trail records the wrong day"


def test_an_invitation_expires_by_the_same_clock_it_was_made_by(home):
    """Mixing clocks here would shift every expiry by the time zone offset."""
    with dbmod.session_scope_for(registry.default_slug()) as db:
        user = db.scalar(select(User))
        token = invites.create(db, user)
        db.commit()
        assert invites.find(db, token) is not None

        user.invite_expires = clock.now() - timedelta(minutes=1)
        db.commit()
        assert invites.find(db, token) is None, "an expired invitation must be refused"


def test_nothing_records_business_time_in_utc_any_more():
    """The rule, enforced. tls.py keeps UTC because X.509 requires it."""
    offenders = []
    for path in (SOURCE / "app").rglob("*.py"):
        if path.name in ("clock.py", "tls.py"):
            continue
        # encoding, explicitly: Windows reads text as cp1252 unless told
        # otherwise, and six files here carry a ₦ or a curly quote. Without
        # this the test does not fail honestly — it dies before it can look.
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if "utcnow" in line:
                offenders.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
    assert not offenders, (
        "these record a fact about Greenwich, which is nobody's question:\n  "
        + "\n  ".join(offenders))


def test_the_certificate_still_uses_utc_because_the_standard_says_so():
    text = (SOURCE / "app" / "tls.py").read_text(encoding="utf-8")
    assert "timezone.utc" in text, "the certificate's dates must still be UTC"
    assert "utcnow" not in text, (
        "UTC yes, but not through datetime.utcnow() — Python has deprecated it "
        "and a build on a newer Python would stop working")
