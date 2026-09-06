"""Projects: setting them up, and seeing what each one made."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from ..models import (
    PROJECT_OPEN,
    PROJECT_STATUSES,
    Contact,
    Project,
    User,
)
from ..security import P_ENTRY, P_VIEW
from ..services import projects as P
from ..services.posting import audit, next_number
from ._common import (
    client_ip,
    db_of,
    need,
    parse_date,
    parse_id,
    parse_money,
    period_from_query,
    redirect,
)

router = APIRouter(prefix="/projects")


@router.get("")
def index(request: Request):
    from ..main import render
    from sqlalchemy import select

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)
    show_finished = request.query_params.get("finished") == "1"

    rows = P.standings(db, start, end, include_finished=show_finished)
    return render(
        request,
        "projects/index.html",
        rows=rows,
        start=start,
        end=end,
        preset=preset,
        show_finished=show_finished,
        unallocated=P.unallocated(db, start, end),
        contacts=list(db.scalars(select(Contact).where(Contact.is_customer.is_(True))
                                 .order_by(Contact.name))),
        users=list(db.scalars(select(User).where(User.is_active.is_(True))
                              .order_by(User.username))),
        statuses=PROJECT_STATUSES,
    )


@router.post("/save")
async def save(request: Request):
    from ..main import flash

    user = need(request, P_ENTRY)
    db = db_of(request)
    form = await request.form()

    project_id = parse_id(form.get("id"))
    project = db.get(Project, project_id) if project_id else None
    creating = project is None
    if creating:
        project = Project(code=(form.get("code") or "").strip()
                          or next_number(db, "PROJECT"))
        db.add(project)

    name = (form.get("name") or "").strip()
    if not name:
        flash(request, "A project needs a name.", "danger")
        return redirect("/projects")

    project.name = name[:200]
    project.contact_id = parse_id(form.get("contact_id"))
    project.manager_id = parse_id(form.get("manager_id"))
    project.status = (form.get("status") or PROJECT_OPEN).strip().upper()
    project.contract_value = parse_money(form.get("contract_value"))
    project.budget_cost = parse_money(form.get("budget_cost"))
    project.notes = (form.get("notes") or "")[:2000]
    for field, key in (("started_on", "started_on"), ("due_on", "due_on"),
                       ("finished_on", "finished_on")):
        raw = (form.get(key) or "").strip()
        setattr(project, field, parse_date(raw) if raw else None)

    db.flush()
    audit(db, user, "PROJECT_SAVE", "Project", project.id, detail=project.name,
          ip=client_ip(request))
    db.commit()
    flash(request, f"{'Created' if creating else 'Saved'} {project.name}.")
    return redirect(f"/projects/{project.id}")


@router.get("/{project_id}")
def detail(request: Request, project_id: int):
    from ..main import render
    from sqlalchemy import select

    need(request, P_VIEW)
    db = db_of(request)
    start, end, preset = period_from_query(request, db)

    standing = P.one(db, project_id, start, end)
    if standing is None:
        return redirect("/projects")

    return render(
        request,
        "projects/detail.html",
        s=standing,
        project=standing.project,
        rows=P.ledger(db, project_id, start, end),
        start=start,
        end=end,
        preset=preset,
        contacts=list(db.scalars(select(Contact).where(Contact.is_customer.is_(True))
                                 .order_by(Contact.name))),
        users=list(db.scalars(select(User).where(User.is_active.is_(True))
                              .order_by(User.username))),
        statuses=PROJECT_STATUSES,
    )
