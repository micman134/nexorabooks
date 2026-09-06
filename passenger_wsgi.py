"""The way cPanel starts Nexora Books.

cPanel's "Setup Python App" runs applications through Passenger, which speaks
WSGI. Nexora Books is an ASGI application, so a thin adapter sits between them.
That adapter is the whole of this file, plus one safety check that is more
important than the adapter itself.

Set this file as the "Application startup file" and ``application`` as the
"Application Entry point". See deploy/CPANEL.txt for the rest.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


#: Folder names that a web server hands out to anybody who asks for them.
SERVED_TO_THE_WORLD = {"public_html", "public", "www", "htdocs", "web"}


def refuse_if_the_books_would_be_downloadable(data: Path) -> None:
    """Stop dead rather than put a company's ledger on the open web.

    On shared hosting the temptation is to keep everything in one folder under
    public_html, because that is the folder people know about. Do that here and
    ``company.db`` — every invoice, every salary, every customer's details —
    becomes a file anybody can download by guessing its name. No password is
    involved; it is simply a file on a web server.

    There is no warning that would be strong enough, so this raises instead.
    An application that will not start is a bad afternoon. A ledger quietly
    readable by the internet is a different kind of problem entirely.
    """
    parts = {part.lower() for part in data.resolve().parts}
    exposed = parts & SERVED_TO_THE_WORLD
    if exposed:
        raise RuntimeError(
            f"Nexora Books will not start: the data folder ({data}) is inside "
            f"'{exposed.pop()}', which your web server publishes to anyone who "
            f"asks. Your company database would be downloadable by strangers. "
            f"Move it outside that folder — set the NEXORA_DATA environment "
            f"variable in cPanel to something like /home/<your account>/"
            f"nexorabooks-data — and start it again."
        )


# One level above the application, which on cPanel is the account's home
# folder rather than anything the web server publishes.
DEFAULT_DATA = HERE.parent / "nexorabooks-data"
data_dir = Path(os.environ.setdefault("NEXORA_DATA", str(DEFAULT_DATA)))
data_dir.mkdir(parents=True, exist_ok=True)
refuse_if_the_books_would_be_downloadable(data_dir)

try:                                        # keep it out of other accounts' reach
    os.chmod(data_dir, 0o700)
except OSError:
    pass

from a2wsgi import ASGIMiddleware       # noqa: E402
from app.main import app                # noqa: E402

#: Passenger looks for this name. It runs the application's start-up work —
#: creating and migrating each company's books — on the first request.
application = ASGIMiddleware(app)
