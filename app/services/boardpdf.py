"""The board pack, as a PDF somebody can put in front of a board.

The screen version and this share one source of figures — ``brief.board_pack``
builds the numbers, and neither renderer is allowed to compute anything of its
own. That is what stops the printed pack and the screen quietly disagreeing.

Laid out for reading rather than for density: one subject per page, headings a
person can find while somebody is talking, and every table in the same shape so
the eye learns it once.
"""
from __future__ import annotations

from ..models import Company
from ..pdfwriter import A4, HELVETICA, Canvas, truncate, width_of, wrap
from . import pdfdocs
from .pdfdocs import INK, LINE, QUIET, money, when
from .. import themes

#: Where a table's money column ends, measured from the right margin.
NUM_RIGHT = 0.0
SECOND_RIGHT = 118.0
THIRD_RIGHT = 236.0

BODY = 9.5
HEAD = 13.0


def _band(c: Canvas, y: float, height: float = 15.0) -> None:
    c.rect(c.margin - 4, y - 11, c.usable + 8, height, fill=(0.955, 0.965, 0.97))


class Sheet:
    """A page the layout can pour into, breaking to a new one when it fills.

    Written as a small object rather than passing ``y`` around because every
    board pack section needs the same "am I near the bottom?" question answered
    and getting it wrong produces a row printed over the footer.
    """

    def __init__(self, company: Company, slug: str | None = None):
        self.c = Canvas(A4)
        self.company = company
        self.slug = slug
        self.y = 0.0
        self.limit = self.c.height - 56

    # -- page handling ----------------------------------------------------
    def start_page(self, first: bool = False) -> None:
        if not first:
            self.c.new_page()
        self.y = self.c.margin + 6

    def room(self, needed: float = 24.0) -> None:
        if self.y + needed > self.limit:
            self.start_page()

    def section(self, number: int, title: str) -> None:
        """Every numbered section begins its own page. Boards flip by heading."""
        self.start_page(first=not self.c.page.content)
        brand = themes.accent_rgb(self.company)
        self.c.text(self.c.margin, self.y, f"{number}", size=22, bold=True, colour=brand)
        self.c.text(self.c.margin + 24, self.y + 3, title.upper(), size=11.5, bold=True,
                    colour=INK)
        self.y += 22
        self.c.rule(self.y, colour=LINE)
        self.y += 16

    # -- content ----------------------------------------------------------
    def note(self, text: str, size: float = 9.0, colour=QUIET) -> None:
        for line in wrap(text, size, self.c.usable, HELVETICA):
            self.room(14)
            self.c.text(self.c.margin, self.y, line, size=size, colour=colour)
            self.y += size + 3.5
        self.y += 4

    def head_row(self, label: str, *columns: str) -> None:
        self.room(26)
        _band(self.c, self.y)
        self.c.text(self.c.margin, self.y, label.upper(), size=7.6, bold=True, colour=QUIET)
        for text, offset in zip(columns, (NUM_RIGHT, SECOND_RIGHT, THIRD_RIGHT)):
            self.c.text(self.c.right - offset, self.y, text.upper(), size=7.6, bold=True,
                        colour=QUIET, align="right")
        self.y += 16

    def row(self, label: str, *values: str, bold: bool = False, indent: float = 0.0,
            rule: bool = False, colour=INK, sub: str = "") -> None:
        self.room(20 if not sub else 30)
        width = self.c.usable - 150 - indent
        self.c.text(self.c.margin + indent, self.y, truncate(label, BODY, width),
                    size=BODY, bold=bold, colour=colour)
        for text, offset in zip(values, (NUM_RIGHT, SECOND_RIGHT, THIRD_RIGHT)):
            if text == "":
                continue
            self.c.text(self.c.right - offset, self.y, text, size=BODY, bold=bold,
                        align="right", colour=colour)
        # A sub-line belongs to the row above it, so it sits closer to its own
        # label than to the next one. Equal gaps read as if it belonged to the
        # row below, which is exactly wrong.
        if sub:
            self.y += 9.5
            self.c.text(self.c.margin + indent, self.y, truncate(sub, 8.0, width),
                        size=8.0, colour=QUIET)
            self.y += 15
        else:
            self.y += 13
        if rule:
            self.c.rule(self.y - 3, colour=LINE)
            self.y += 4

    def group(self, title: str) -> None:
        self.room(24)
        self.c.text(self.c.margin, self.y, title, size=9.0, bold=True, colour=QUIET)
        self.y += 14


# --------------------------------------------------------------------------
# The sections
# --------------------------------------------------------------------------


def _cover(s: Sheet, pack) -> None:
    c = s.c
    s.start_page(first=True)
    s.y = pdfdocs.letterhead(c, s.company, "Board pack", s.slug) + 10
    right = pdfdocs.facts(c, pdfdocs.FACTS_TOP, [
        ("Period", f"{when(pack.cur.start)} to {when(pack.cur.end)}"),
        ("Compared with", f"{when(pack.prior.start)} to {when(pack.prior.end)}"),
    ])
    s.y = max(s.y, right) + 14
    c.rule(s.y, colour=LINE)
    s.y += 18
    for line in pack.story:
        for text in wrap(line, 11.0, c.usable, HELVETICA):
            s.c.text(c.margin, s.y, text, size=11.0, colour=INK)
            s.y += 15
        s.y += 5
    s.y += 6
    s.note(
        "Every figure in this pack is the sum of posted transactions in the general "
        "ledger for the periods shown. Nothing in it is estimated, forecast or "
        "adjusted. Each figure can be traced through the software to the "
        "transactions that make it up."
    )


def _standing(s: Sheet, pack) -> None:
    s.section(1, "Where the business stands")
    # Two columns, with the comparison set under the name rather than in a
    # column of its own: a reader scanning down should meet the figure before
    # anything qualifying it.
    s.head_row("Measure", "This period")
    for name, value, note in pack.kpis:
        s.row(name, value, sub=(f"{note} against last period" if note else ""))


def _movers(s: Sheet, pack) -> None:
    s.section(2, "What moved, and by how much")
    s.note("Ranked by the effect each one had on profit.")
    s.head_row("Account", "Effect on profit", "Last period", "This period")
    for m in pack.movers:
        s.row(m.label, money(m.profit_effect), money(m.prior), money(m.current))
    if not pack.movers:
        s.row("Nothing moved between the two periods.", colour=QUIET)


def _profit_and_loss(s: Sheet, pack) -> None:
    pl = pack.pl
    s.section(3, "Profit and loss")
    s.head_row("Account", "This period", "Last period")
    for sec in (pl.revenue, pl.cogs, pl.expenses, pl.other_income, pl.tax):
        if not sec.rows:
            continue
        s.group(sec.title)
        for acc, value, prior in sec.rows:
            s.row(acc.name, money(value), money(prior), indent=10)
        s.row(sec.title, money(sec.total), money(sec.total_prior), bold=True, rule=True)
    s.y += 4
    s.row("Net profit", money(pl.net_profit), money(pl.net_profit_prior), bold=True,
          rule=True)


def _balance_sheet(s: Sheet, pack) -> None:
    bs = pack.bs
    s.section(4, f"Balance sheet at {when(bs.as_of)}")
    s.head_row("Account", "Amount")
    for sec in (bs.current_assets, bs.fixed_assets, bs.current_liabilities,
                bs.long_term_liabilities, bs.equity):
        if not sec.rows:
            continue
        s.group(sec.title)
        for acc, value in sec.rows:
            s.row(acc.name, money(value), indent=10)
        s.row(sec.title, money(sec.total), bold=True, rule=True)
    s.group("Earnings")
    s.row("Brought forward", money(bs.retained_brought_forward), indent=10)
    s.row("This year so far", money(bs.current_earnings), indent=10)
    s.y += 4
    s.row("Total assets", money(bs.total_assets), bold=True)
    s.row("Liabilities and equity", money(bs.total_liabilities + bs.total_equity),
          bold=True, rule=True)
    if not bs.balances_ok:
        s.note(
            f"WARNING: the balance sheet is out by {money(bs.difference)}. "
            "Do not circulate this pack until that has been resolved.",
            colour=(0.72, 0.11, 0.11),
        )


def _cash_flow(s: Sheet, pack) -> None:
    cf = pack.cf
    s.section(5, "Cash flow")
    s.head_row("", "Amount")
    s.row("Cash at the start of the period", money(cf.opening_cash), bold=True, rule=True)
    for title, rows, total in (
        ("Operating", cf.operating, cf.operating_total),
        ("Investing", cf.investing, cf.investing_total),
        ("Financing", cf.financing, cf.financing_total),
    ):
        if not rows:
            continue
        s.group(title)
        for label, amount in rows:
            s.row(label, money(amount), indent=10)
        s.row(title, money(total), bold=True, rule=True)
    s.y += 4
    s.row("Cash at the end of the period", money(cf.closing_cash), bold=True, rule=True)
    if cf.difference:
        s.note(
            f"{money(cf.difference)} of the movement in cash could not be classified. "
            "It is shown here rather than hidden so the statement still adds up."
        )


def _against_plan(s: Sheet, pack, number: int) -> None:
    v = pack.budget
    s.section(number, "Against the plan")
    if pack.budget_name:
        s.note(pack.budget_name)
    s.head_row("Account", "Difference", "Actual", "Budget")
    for sec in v.sections:
        if not sec.rows:
            continue
        s.group(sec.title)
        for row in sec.rows:
            s.row(row.account.name, money(row.variance), money(row.actual),
                  money(row.budget), indent=10)
    s.y += 4
    s.row("Profit against plan", money(v.profit_variance), money(v.actual_profit),
          money(v.budget_profit), bold=True, rule=True)


def _risks(s: Sheet, pack, number: int) -> None:
    s.section(number, "What to raise")
    s.note(f"Conditions found in the books at {when(pack.cur.end)}.")
    if not pack.risks:
        s.row("Nothing was flagged for this period.", colour=QUIET)
        return
    for p in pack.risks:
        s.room(34)
        s.row(p.title, money(p.amount) if p.amount else "", bold=True)
        if p.detail:
            for line in wrap(p.detail, 8.5, s.c.usable - 150, HELVETICA):
                s.c.text(s.c.margin + 10, s.y, line, size=8.5, colour=QUIET)
                s.y += 11
        s.y += 6


def _questions(s: Sheet, pack, number: int) -> None:
    s.section(number, "Questions you will be asked")
    s.note("With the answers already worked out.")
    if not pack.questions:
        s.row("There is not enough in the books yet to anticipate anything useful.",
              colour=QUIET)
        return
    for q in pack.questions:
        s.room(40)
        s.c.text(s.c.margin, s.y, q.question, size=10.0, bold=True, colour=INK)
        s.y += 14
        for line in wrap(q.answer, BODY, s.c.usable, HELVETICA):
            s.room(16)
            s.c.text(s.c.margin, s.y, line, size=BODY, colour=INK)
            s.y += 12.5
        s.y += 10


# --------------------------------------------------------------------------
# Putting the pack together
# --------------------------------------------------------------------------


def render(pack, company: Company, slug: str | None = None) -> bytes:
    s = Sheet(company, slug)
    _cover(s, pack)
    _standing(s, pack)
    _movers(s, pack)
    _profit_and_loss(s, pack)
    _balance_sheet(s, pack)
    _cash_flow(s, pack)

    number = 6
    if pack.budget is not None:
        _against_plan(s, pack, number)
        number += 1
    _risks(s, pack, number)
    _questions(s, pack, number + 1)

    name = company.name if company else ""
    pdfdocs.stamp_pages(
        s.c, company,
        f"{name} · board pack · {when(pack.cur.start)} to {when(pack.cur.end)}".strip(" ·"),
    )
    return s.c.output()
