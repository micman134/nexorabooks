"""Request-scoped display preferences that are not money.

At the moment that means the date format. A Nigerian, a Briton and an
Australian all read 03/04/2026 as the third of April; an American reads it as
the fourth of March. On an invoice sent to a customer that is not a cosmetic
difference, so the format is a company setting rather than a constant, and it
is held here where the template filter can reach it without being handed the
company row on every call.
"""
from __future__ import annotations

from contextvars import ContextVar

#: Sensible choices, shown on the settings screen with a live example.
DATE_FORMATS: list[tuple[str, str]] = [
    ("%d %b %Y", "23 Aug 2026"),
    ("%d/%m/%Y", "23/08/2026"),
    ("%m/%d/%Y", "08/23/2026"),
    ("%Y-%m-%d", "2026-08-23"),
    ("%d-%m-%Y", "23-08-2026"),
    ("%b %d, %Y", "Aug 23, 2026"),
    ("%d.%m.%Y", "23.08.2026"),
    ("%Y.%m.%d", "2026.08.23"),
]

DEFAULT_DATE_FORMAT = "%d %b %Y"
_VALID = {f for f, _ in DATE_FORMATS}

_date_format: ContextVar[str] = ContextVar("date_format", default=DEFAULT_DATE_FORMAT)


#: The fields ``%-d`` and friends can drop a leading zero from, and how to get
#: the same answer without asking the operating system for something it may not
#: know about.
_UNPADDED = {"d": "%d", "m": "%m", "H": "%H", "I": "%I", "j": "%j",
             "M": "%M", "S": "%S", "y": "%y"}


def strftime(value, fmt: str) -> str:
    """Format a date, including ``%-d``, on every operating system.

    ``%-d`` means "the day of the month without a leading zero". It is a GNU
    extension: Python hands the format straight to the C library, so it works
    on Linux and macOS and raises ``ValueError: Invalid format string`` on
    Windows. That is not a cosmetic difference — it took down bank statement
    importing for every Windows customer, from a line of code whose only job
    was to write "3 Aug" instead of "03 Aug" under a chart.

    So the unpadded fields are worked out here and everything else is passed
    on unchanged. ``%%`` is left alone, and an unknown code is handed to the
    platform exactly as before, because inventing behaviour for it would be a
    worse surprise than the platform's own error.
    """
    if value is None:
        return ""
    out: list[str] = []
    i, size = 0, len(fmt or "")
    while i < size:
        ch = fmt[i]
        if ch != "%":
            out.append(ch)
            i += 1
            continue
        nxt = fmt[i + 1: i + 2]
        if nxt == "%":
            out.append("%")
            i += 2
            continue
        # "%-d" on any platform, and "%#d" for anybody who has typed the
        # Windows spelling of the same idea.
        if nxt in "-#" and fmt[i + 2: i + 3] in _UNPADDED:
            code = _UNPADDED[fmt[i + 2]]
            out.append(value.strftime(code).lstrip("0") or "0")
            i += 3
            continue
        out.append(value.strftime("%" + nxt) if nxt else "%")
        i += 2
    return "".join(out)


def date_format() -> str:
    return _date_format.get()


def set_date_format(fmt: str | None) -> None:
    _date_format.set(fmt if fmt in _VALID else DEFAULT_DATE_FORMAT)


def example(fmt: str) -> str:
    for candidate, shown in DATE_FORMATS:
        if candidate == fmt:
            return shown
    return DATE_FORMATS[0][1]
