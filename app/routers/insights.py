"""The screens that explain the figures rather than just listing them.

Three things live here and they share one engine:

  * **Why did that change?** — the drill-down through a movement in profit.
  * **Today's brief** — the same engine pointed at "what should I know now".
  * **Board pack** — the same engine, printed.

Nothing on these screens is estimated, predicted or generated. Every figure is
a sum of posted journal lines and every one of them can be opened until the
person is looking at the transaction itself. That is the whole design: the
software is allowed to draw attention to something, and it is never allowed to
tell somebody a number it cannot show them the source of.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..money import fmt
from ..services import brief as brief_service
from ..services import variance as V
from ..security import P_VIEW
from ._common import db_of, need, parse_date

router = APIRouter(prefix="/insights")


# --------------------------------------------------------------------------
# Choosing the two periods
# --------------------------------------------------------------------------


def _periods(request: Request) -> tuple[V.Period, V.Period, str]:
    """Work out which two periods the screen is comparing.

    Falls back to "last month against the month before" whenever the query
    string is missing or makes no sense, because a screen that errors on a
    typed URL is worse than one that quietly shows something sensible.
    """
    q = request.query_params
    key = q.get("compare", "month_prev")
    today = date.today()

    if key == "custom":
        cs = parse_date(q.get("cs"), today.replace(day=1))
        ce = parse_date(q.get("ce"), today)
        ps = parse_date(q.get("ps"), cs)
        pe = parse_date(q.get("pe"), ce)
        if ce < cs:
            cs, ce = ce, cs
        if pe < ps:
            ps, pe = pe, ps
        return (
            V.Period("Chosen period", cs, ce),
            V.Period("Compared with", ps, pe),
            "custom",
        )

    pair = V.choice(key, today) or V.choice("month_prev", today)
    cur, prior = pair
    return cur, prior, key if V.choice(key, today) else "month_prev"


# --------------------------------------------------------------------------
# Why did that change?
# --------------------------------------------------------------------------


@router.get("/why")
def why(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    cur, prior, key = _periods(request)
    path = [p for p in request.query_params.getlist("p") if p]

    level = V.explore(db, cur, prior, path)
    # The crumbs came back without their own paths; give each one the trail
    # that leads to it so the template can just link them.
    trail = []
    for depth, node in enumerate(level.crumbs):
        trail.append((node, path[:depth]))

    return render(
        request,
        "insights/why.html",
        cur=cur,
        prior=prior,
        compare=key,
        choices=V.compare_choices(),
        path=path,
        level=level,
        trail=trail,
        story=V.narrate(level, fmt),
        dim_label=V.DIMENSION_LABELS.get(level.child_dim, ""),
        col_label=V.COLUMN_LABELS.get(level.child_dim, ""),
    )


# --------------------------------------------------------------------------
# Today's brief
# --------------------------------------------------------------------------


@router.get("/brief")
def daily_brief(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    on = parse_date(request.query_params.get("on"), date.today())
    return render(request, "insights/brief.html", b=brief_service.build(db, on), on=on)


# --------------------------------------------------------------------------
# Board pack
# --------------------------------------------------------------------------


@router.get("/board-pack")
def board_pack(request: Request):
    from ..main import render

    need(request, P_VIEW)
    db = db_of(request)
    cur, prior, key = _periods(request)

    if request.query_params.get("format") == "pdf":
        # Built with the PDF's own money formatter: the built-in fonts have no
        # glyph for every currency symbol, so the pack writes the ISO code
        # where the screen writes the symbol.
        from ..services import boardpdf, pdfdocs

        pack = brief_service.board_pack(db, cur, prior, fmt=pdfdocs.money)
        data = boardpdf.render(pack, request.state.company, request.state.company_slug)
        stamp = f"{cur.end:%Y-%m}"
        return Response(
            data,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="board-pack-{stamp}.pdf"',
            },
        )

    return render(
        request,
        "insights/board_pack.html",
        pack=brief_service.board_pack(db, cur, prior),
        cur=cur,
        prior=prior,
        compare=key,
        choices=V.compare_choices(),
    )
