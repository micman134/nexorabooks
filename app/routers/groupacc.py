"""The group: several companies read as one, and the settings that allow it.

Everything under here reads. No screen in this file writes to a member
company's books — the only thing it saves is the group's own settings, and
those live in a file of their own outside every company.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from .. import companies as registry
from .. import currency as currency_mod
from .. import db as dbmod
from .. import group as group_mod
from ..models import Contact
from ..security import P_ADMIN, P_VIEW
from ..services import consolidation as C
from ._common import db_of, need, period_from_query, redirect

router = APIRouter(prefix="/group")


def _period(request: Request):
    db = db_of(request)
    return period_from_query(request, db)


@router.get("")
def index(request: Request):
    from ..main import render

    need(request, P_VIEW)
    group = group_mod.load()
    start, end, preset = _period(request)

    if not group.is_set_up:
        return render(request, "group/setup_first.html", group=group,
                      start=start, end=end, preset=preset)

    out = C.build(group, start, end)
    with currency_mod.using(out.currency):
        return render(request, "group/index.html", g=out, group=group,
                      names={r.slug: r.name for r in out.members},
                      start=start, end=end, preset=preset)


@router.get("/settings")
def settings(request: Request):
    from ..main import render

    need(request, P_ADMIN)
    group = group_mod.load()
    return render(request, "group/settings.html", group=group,
                  currencies=currency_mod.choices(),
                  contacts=_contact_names(group),
                  refs={ref.slug: ref for ref in registry.all_companies()})


def _contact_names(group: group_mod.Group) -> dict[str, list[tuple[int, str]]]:
    """Every customer and supplier in each member, for the mapping screen."""
    from sqlalchemy import select

    out: dict[str, list[tuple[int, str]]] = {}
    for member in group.members:
        if not member.include or not member.exists:
            continue
        try:
            with dbmod.session_scope_for(member.slug) as db:
                out[member.slug] = [
                    (c.id, c.name)
                    for c in db.scalars(select(Contact).order_by(Contact.name))
                ]
        except Exception:
            out[member.slug] = []
    return out


@router.post("/settings")
async def save_settings(request: Request):
    from ..main import flash

    need(request, P_ADMIN)
    form = await request.form()
    group = group_mod.load()

    group.name = (form.get("name") or "").strip()[:120]
    group.currency = (form.get("currency") or "").strip().upper()[:6]

    for member in group.members:
        member.include = form.get(f"include:{member.slug}") is not None
        member.closing_rate = str(form.get(f"closing:{member.slug}") or "1").strip() or "1"
        member.average_rate = str(form.get(f"average:{member.slug}") or "1").strip() or "1"

    group_mod.save(group)
    flash(request, "Group settings saved.")
    return redirect("/group/settings")


@router.post("/settings/internal")
async def save_internal(request: Request):
    """Which contact in each company stands for another company in the group."""
    from ..main import flash

    need(request, P_ADMIN)
    form = await request.form()
    group = group_mod.load()
    known = group.internal_slugs()

    for member in group.members:
        chosen: dict[str, str] = {}
        for key, value in form.multi_items():
            prefix = f"internal:{member.slug}:"
            if key.startswith(prefix) and value and value in known:
                contact_id = key[len(prefix):]
                if contact_id.isdigit() and value != member.slug:
                    chosen[contact_id] = value
        member.internal = chosen

    group_mod.save(group)
    flash(request, "Saved which contacts are other companies in the group.")
    return redirect("/group/settings#internal")


@router.post("/settings/suggest")
async def suggest(request: Request):
    """Fill the mapping in from the names, for a person to check and save."""
    from ..main import flash

    need(request, P_ADMIN)
    group = group_mod.load()
    found = 0
    for member in group.chosen:
        guesses = C.suggest_internal(member.slug, group.chosen)
        if guesses:
            member.internal = {**guesses, **member.internal}
            found += len(guesses)
    group_mod.save(group)

    flash(request,
          f"Matched {found} contact{'' if found == 1 else 's'} by name. "
          "Check them before relying on the figures — a wrong match takes real "
          "trade out of the group's revenue."
          if found else
          "No contact in any company is named like another company in the group.",
          "success" if found else "info")
    return redirect("/group/settings#internal")


@router.get("/csv")
def csv_export(request: Request):
    """The consolidated figures as a spreadsheet, one column per company."""
    import csv
    import io

    from fastapi.responses import Response

    need(request, P_VIEW)
    group = group_mod.load()
    if not group.is_set_up:
        return redirect("/group")

    start, end, _preset = _period(request)
    out = C.build(group, start, end)
    names = [reading.slug for reading in out.members]

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([f"{group.name or 'Group'} — consolidated"])
    writer.writerow([f"{start} to {end}", f"in {out.currency.code}"])
    writer.writerow([])
    writer.writerow(["Section", "Code", "Account"]
                    + [reading.name for reading in out.members]
                    + ["Combined", "Eliminated", "Group"])

    def major(value: int) -> str:
        from ..money import to_major
        return f"{to_major(value, out.currency):.{out.currency.decimals}f}"

    for section in (out.revenue, out.cogs, out.expenses, out.other_income,
                    out.tax, out.current_assets, out.fixed_assets,
                    out.current_liabilities, out.long_liabilities, out.equity):
        for row in section.rows:
            writer.writerow(
                [section.title, row.code, row.name]
                + [major(row.parts.get(slug, 0)) for slug in names]
                + [major(row.combined), major(row.eliminated), major(row.total)])
        writer.writerow([section.title, "", f"Total {section.title.lower()}"]
                        + [major(section.of(slug)) for slug in names]
                        + [major(section.combined), major(section.eliminated),
                           major(section.total)])
        writer.writerow([])

    writer.writerow(["Group profit for the period", "", "", *[""] * len(names),
                     "", "", major(out.net_profit)])
    writer.writerow([])
    for note in out.notes:
        writer.writerow(["Note", note])

    stamp = date.today().isoformat()
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="group-accounts-{stamp}.csv"'},
    )
