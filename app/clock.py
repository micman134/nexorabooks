"""What time it is, from the point of view of the business.

Every timestamp in these books used to be recorded with ``datetime.utcnow()``
and then printed unchanged. In Lagos that made the audit trail an hour behind
the clock on the wall. In Manila it made it eight hours behind — which at half
past seven in the morning means the audit trail says *yesterday*.

An audit trail exists to answer one question: when did this happen. One that
disagrees with the clock in the room, and on the far side of the world with the
calendar as well, does not answer it. It also disagreed with the software's own
business dates, which have always come from ``date.today()`` and so were local
all along; an invoice dated today with an audit entry dated yesterday is not a
discrepancy anybody should have to explain.

So: one clock, and it is the local one. Nexora Books runs on a single computer
in a single office, and the time everybody in that office means when they say
"half past two" is the time on that computer. Recording anything else is
recording a fact about Greenwich, which is nobody's question.

Naive, deliberately. Attaching a zone would imply the software knows how to
convert between them, and it has no business doing that when every reader of
every timestamp is in the same building as the machine that wrote it.

The one exception in the codebase is ``app/tls.py``, where a certificate's
validity dates are UTC because the X.509 standard says they are. That is a fact
about a certificate, not about a business, and it keeps ``utcnow``.
"""
from __future__ import annotations

from datetime import date, datetime


def now() -> datetime:
    """The moment this is happening, on this computer's clock."""
    return datetime.now()


def today() -> date:
    """The business day, which is whatever day it is where the books are kept."""
    return date.today()
