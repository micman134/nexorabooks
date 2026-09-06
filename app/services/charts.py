"""Small SVG charts, drawn here rather than by a library.

Three reasons this is a hundred lines of Python instead of a script tag. A
charting library would be another dependency the Windows build could fail on.
Loading one from the internet would break the promise that this software
fetches nothing. And a canvas chart prints as an empty box, where an SVG prints
exactly as it appears.

Everything below uses ``var(--...)`` for its colours, so a chart follows
whichever of the eleven themes the person is using without knowing anything
about them, and the print stylesheet's light override applies to charts too.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import timedelta
from html import escape

from .. import prefs


@dataclass
class Bucket:
    """One column of a chart: a period, what went in, what went out."""

    label: str
    start: Date
    end: Date
    money_in: int = 0
    money_out: int = 0
    closing: int | None = None

    @property
    def net(self) -> int:
        return self.money_in - self.money_out


def bucket_lines(lines, opening: int | None = None, target: int = 24) -> list[Bucket]:
    """Group statement lines into a sensible number of columns.

    By day for a short statement, by week for a long one, so a month reads as
    four bars and a year does not read as three hundred and sixty-five.
    """
    dated = sorted((line for line in lines if line.date), key=lambda line: line.date)
    if not dated:
        return []

    first, last = dated[0].date, dated[-1].date
    span = (last - first).days + 1
    step = 1 if span <= target else (7 if span <= target * 7 else 30)

    buckets: list[Bucket] = []
    cursor = first
    while cursor <= last:
        end = cursor + timedelta(days=step - 1)
        buckets.append(Bucket(label=_label(cursor, step), start=cursor, end=min(end, last)))
        cursor = end + timedelta(days=1)

    running = opening or 0
    at = 0
    for bucket in buckets:
        for line in dated[at:]:
            if line.date > bucket.end:
                break
            if line.amount > 0:
                bucket.money_in += line.amount
            else:
                bucket.money_out += -line.amount
            at += 1
        running += bucket.net
        bucket.closing = running
    return buckets


def _label(when: Date, step: int) -> str:
    if not hasattr(when, "strftime"):
        return str(when)
    # prefs.strftime, not strftime: "%-d" is a GNU extension that Windows
    # rejects outright, and this one line used to end every Windows customer's
    # bank statement import with "Invalid format string".
    if step in (1, 7):
        return prefs.strftime(when, "%-d %b")
    return prefs.strftime(when, "%b %y")


# --------------------------------------------------------------------------
# Money in, money out, and where the balance went
# --------------------------------------------------------------------------


def cash_chart(buckets: list[Bucket], *, height: int = 170, show_balance: bool = True) -> str:
    """Paired bars per period, with the running balance drawn over them."""
    if not buckets:
        return ""

    width = max(360, len(buckets) * 46)
    pad_top, pad_bottom = 12, 26
    plot = height - pad_top - pad_bottom
    peak = max(
        [b.money_in for b in buckets] + [b.money_out for b in buckets] + [1]
    )

    slot = width / len(buckets)
    bar = min(15.0, slot / 3.2)
    parts = [
        f'<svg class="cash-chart" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="Money in and out over the statement period">'
    ]

    # A baseline, so bars have something to stand on.
    base = pad_top + plot
    parts.append(
        f'<line x1="0" y1="{base:.1f}" x2="{width}" y2="{base:.1f}" '
        f'stroke="var(--line)" stroke-width="1"/>'
    )

    for index, bucket in enumerate(buckets):
        middle = slot * (index + 0.5)
        for value, offset, colour, what in (
            (bucket.money_in, -bar - 1.5, "var(--good)", "in"),
            (bucket.money_out, 1.5, "var(--danger)", "out"),
        ):
            if value <= 0:
                continue
            tall = max(1.5, value / peak * plot)
            parts.append(
                f'<rect x="{middle + offset:.1f}" y="{base - tall:.1f}" '
                f'width="{bar:.1f}" height="{tall:.1f}" rx="1.5" fill="{colour}">'
                f'<title>{escape(bucket.label)}: {what} {value / 100:,.2f}</title></rect>'
            )

    if show_balance and any(b.closing is not None for b in buckets):
        closings = [b.closing for b in buckets if b.closing is not None]
        low, high = min(closings + [0]), max(closings + [0])
        spread = (high - low) or 1

        def y_of(value: int) -> float:
            return pad_top + plot - (value - low) / spread * plot

        points = " ".join(
            f"{slot * (i + 0.5):.1f},{y_of(b.closing):.1f}"
            for i, b in enumerate(buckets) if b.closing is not None
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="var(--accent)" '
            f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round" '
            f'vector-effect="non-scaling-stroke" opacity=".85"/>'
        )
        if low < 0:
            zero = y_of(0)
            parts.append(
                f'<line x1="0" y1="{zero:.1f}" x2="{width}" y2="{zero:.1f}" '
                f'stroke="var(--danger)" stroke-width="1" stroke-dasharray="3 3" '
                f'vector-effect="non-scaling-stroke"/>'
            )

    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------
# How much of it the software recognised
# --------------------------------------------------------------------------


@dataclass
class Slice_:
    label: str
    count: int
    colour: str
    hint: str = ""


def split_bar(slices: list[Slice_]) -> str:
    """One bar divided into parts. Reads as a proportion at a glance."""
    total = sum(s.count for s in slices) or 1
    parts = ['<div class="split-bar">']
    for piece in slices:
        if piece.count <= 0:
            continue
        share = piece.count * 100 / total
        parts.append(
            f'<span style="width:{share:.2f}%; background:{piece.colour}" '
            f'title="{escape(piece.label)}: {piece.count}"></span>'
        )
    parts.append("</div>")
    return "".join(parts)
