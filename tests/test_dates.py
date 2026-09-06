"""Writing dates the same way on every operating system.

A customer in Lagos, on Windows 11, uploaded a bank statement and got an error
page. Their diagnostic report ended:

    app\\services\\charts.py, line 76, in _label
        return f"{when:%-d %b}"
    ValueError: Invalid format string

``%-d`` means "day of the month, no leading zero" — on Linux and macOS, where
Python hands the format to the C library and the GNU extension is understood.
Windows has never had it. The statement had been read correctly and every line
of it parsed; the crash came afterwards, in the one line whose only job was to
write "3 Aug" rather than "03 Aug" under a chart nobody had asked for.

It was invisible here because everything is developed and tested on Linux. So
these tests do two things: prove the formatter works when the platform refuses
the extension, and refuse to let ``%-`` reach a platform call ever again.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from app import prefs
from app.main import date_fmt
from app.services import charts

SOURCE = Path(__file__).resolve().parent.parent


class WindowsDate(date):
    """A date that behaves the way Windows does, on purpose.

    Not a mock of our own code — a mock of the platform underneath it, which
    is the part that was never exercised in this project's tests.
    """

    def strftime(self, fmt: str) -> str:                      # noqa: D102
        if re.search(r"%[-#]", fmt):
            raise ValueError("Invalid format string")
        return super().strftime(fmt)


class WindowsDateTime(datetime):
    def strftime(self, fmt: str) -> str:                      # noqa: D102
        if re.search(r"%[-#]", fmt):
            raise ValueError("Invalid format string")
        return super().strftime(fmt)


def test_the_stand_in_really_does_refuse_what_windows_refuses():
    """If this passes trivially the rest of the file proves nothing."""
    with pytest.raises(ValueError):
        WindowsDate(2026, 8, 3).strftime("%-d %b")
    assert WindowsDate(2026, 8, 3).strftime("%d %b") == "03 Aug"


# --------------------------------------------------------------------------
# The formatter
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt,expected", [
    ("%-d %b", "3 Aug"),
    ("%-d %B", "3 August"),
    ("%B %-d", "August 3"),
    ("%-d/%-m/%Y", "3/8/2026"),
    ("%d %b %Y", "03 Aug 2026"),          # ordinary formats are untouched
    ("%b %y", "Aug 26"),
    ("%#d %b", "3 Aug"),                  # the Windows spelling of the same idea
    ("%-y", "26"),
])
def test_dates_come_out_right_even_where_the_platform_refuses(fmt, expected):
    assert prefs.strftime(WindowsDate(2026, 8, 3), fmt) == expected


def _the_platform_understands_a_dash() -> bool:
    """Does this computer's own strftime accept "%-d"?

    Asked by trying it, not by looking at the operating system's name. It is
    the behaviour that matters, and asking directly is how this stays true on
    whatever Python does next.
    """
    try:
        date(2026, 8, 3).strftime("%-d")
    except ValueError:
        return False
    return True


@pytest.mark.skipif(not _the_platform_understands_a_dash(),
                    reason="this platform refuses %-d, which is the whole "
                           "reason prefs.strftime exists — there is no native "
                           "answer here to compare against")
def test_the_answer_is_the_same_on_a_platform_that_would_have_coped():
    """Nothing about the fix may change what Linux and macOS already printed."""
    for fmt in ("%-d %b", "%-d %B", "%B %-d", "%-d/%-m/%Y"):
        assert prefs.strftime(date(2026, 8, 3), fmt) == date(2026, 8, 3).strftime(fmt)


def test_a_double_percent_is_left_alone():
    assert prefs.strftime(WindowsDate(2026, 8, 3), "100%% on %-d") == "100% on 3"


def test_midnight_does_not_become_an_empty_string():
    """"%-H" of zero is "0", not nothing — stripping the zero must not eat it."""
    assert prefs.strftime(WindowsDateTime(2026, 8, 3, 0, 5), "%-H:%M") == "0:05"


def test_nothing_in_gives_nothing_out():
    assert prefs.strftime(None, "%-d %b") == ""


def test_an_ordinary_date_is_not_slowed_down_by_being_wrong():
    assert prefs.strftime(date(2026, 12, 25), "%d/%m/%Y") == "25/12/2026"


# --------------------------------------------------------------------------
# The two places it actually broke
# --------------------------------------------------------------------------


def test_a_chart_label_no_longer_ends_the_import(monkeypatch):
    """This is the exact call in the customer's traceback."""
    assert charts._label(WindowsDate(2026, 8, 3), 1) == "3 Aug"
    assert charts._label(WindowsDate(2026, 8, 3), 7) == "3 Aug"
    assert charts._label(WindowsDate(2026, 8, 3), 30) == "Aug 26"


class _Line:
    def __init__(self, when, amount):
        self.date, self.amount = when, amount


def test_a_whole_statement_is_bucketed_on_a_windows_machine():
    lines = [_Line(WindowsDate(2026, 8, day), amount)
             for day, amount in ((3, 250_000_00), (4, -80_000_00), (7, 15_000_00))]
    buckets = charts.bucket_lines(lines, opening=100_000_00)

    assert buckets, "the import preview draws nothing without these"
    assert buckets[0].label == "3 Aug"
    assert buckets[-1].closing == 100_000_00 + 250_000_00 - 80_000_00 + 15_000_00
    # And the chart itself is drawn without touching the platform's strftime
    assert charts.cash_chart(buckets).startswith("<svg")


def test_the_template_filter_survives_it_too():
    """`{{ some_date|dt('%-d %B') }}` appears on the cash and import screens."""
    assert date_fmt(WindowsDate(2026, 8, 3), "%-d %B") == "3 August"
    assert date_fmt(WindowsDate(2026, 8, 3), "%B %-d") == "August 3"
    assert date_fmt(None, "%-d %B") == ""


# --------------------------------------------------------------------------
# Making sure it cannot come back
# --------------------------------------------------------------------------


def _python_files():
    for path in (SOURCE / "app").rglob("*.py"):
        yield path
    for name in ("desktop.py", "run.py", "seed_demo.py", "reset_two_factor.py"):
        if (SOURCE / name).exists():
            yield SOURCE / name


def test_no_code_asks_the_platform_for_a_format_windows_refuses():
    """The rule: ``%-`` may be written, but only on its way to prefs.strftime.

    Two spellings are banned outright — an f-string format spec ``{when:%-d}``
    and a direct ``.strftime("%-d")`` — because both go straight to the
    operating system. Every other use goes through the helper, which handles
    it on any platform.
    """
    in_a_format_spec = re.compile(r":%[-#]")
    in_a_direct_call = re.compile(r"\.strftime\(\s*[\"'][^\"']*%[-#]")

    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if in_a_format_spec.search(line) or in_a_direct_call.search(line):
                offenders.append(f"{path.relative_to(SOURCE)}:{number}: {line.strip()}")
    assert not offenders, (
        "These go straight to the platform and raise on Windows. Use "
        "prefs.strftime instead:\n  " + "\n  ".join(offenders))


def test_every_date_format_a_company_can_choose_works_everywhere():
    """The settings screen must not be able to hand Windows something it hates."""
    for fmt, example in prefs.DATE_FORMATS:
        assert not re.search(r"%[-#]", fmt), fmt
        assert prefs.strftime(WindowsDate(2026, 8, 23), fmt) == example
