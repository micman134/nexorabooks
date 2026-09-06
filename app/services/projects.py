"""Job costing — what each project actually made.

The design rule here is the one that keeps job costing honest: **a project's
figures are the ledger's figures, filtered**. Nothing is kept in a second
place and reconciled later. Every number below is a sum of posted journal
lines carrying that project's id, which means a project's profit and the
company's profit cannot drift apart, and a project report can be traced to the
same transactions as the profit and loss.

What that buys, beyond profit per job:

  * **Work done and not billed.** A contract worth ten million with four
    million invoiced and seven million of cost booked is either badly
    under-priced or missing an invoice. Both are worth knowing this week
    rather than at the year end.
  * **Jobs eating money quietly.** Cost against budget, per job, ranked.
  * **The unallocated pile.** Whatever has not been coded to any job at all,
    so nobody can mistake a partial picture for a complete one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as Date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    EXPENSE,
    INCOME,
    PROJECT_OPEN,
    Account,
    JournalEntry,
    JournalLine,
    Project,
)


@dataclass
class Figures:
    """What one project has earned and cost, out of the ledger."""

    revenue: int = 0
    cost: int = 0

    @property
    def profit(self) -> int:
        return self.revenue - self.cost

    @property
    def margin(self) -> float:
        return (self.profit * 100.0 / self.revenue) if self.revenue else 0.0


@dataclass
class Standing:
    """A project, its figures, and what they mean."""

    project: Project
    figures: Figures = field(default_factory=Figures)
    #: Cost booked against what was budgeted.
    budget_used: float = 0.0
    #: Revenue billed against what the contract is worth.
    billed: float = 0.0
    concerns: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.project.name

    @property
    def over_budget(self) -> bool:
        return bool(self.project.budget_cost) and self.figures.cost > self.project.budget_cost

    @property
    def unbilled(self) -> int:
        """Contract value not yet invoiced. Zero when there is no contract."""
        if not self.project.contract_value:
            return 0
        return max(0, self.project.contract_value - self.figures.revenue)

    @property
    def expected_profit(self) -> int:
        """What it will make if it bills in full and costs what was budgeted."""
        if not self.project.contract_value:
            return self.figures.profit
        return self.project.contract_value - max(
            self.figures.cost, self.project.budget_cost or self.figures.cost
        )

    @property
    def losing_money(self) -> bool:
        return self.figures.profit < 0

    @property
    def needs_attention(self) -> bool:
        return bool(self.concerns)


def _totals(db: Session, start: Date | None = None,
            end: Date | None = None) -> dict[int, Figures]:
    """Revenue and cost per project, read straight from the ledger."""
    query = (
        select(
            JournalLine.project_id,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.is_posted.is_(True),
            JournalLine.project_id.is_not(None),
            Account.type.in_([INCOME, EXPENSE]),
        )
        .group_by(JournalLine.project_id, Account.type)
    )
    if start:
        query = query.where(JournalEntry.date >= start)
    if end:
        query = query.where(JournalEntry.date <= end)

    out: dict[int, Figures] = {}
    for project_id, acc_type, debit, credit in db.execute(query):
        figures = out.setdefault(project_id, Figures())
        if acc_type == INCOME:
            figures.revenue += int(credit) - int(debit)
        else:
            figures.cost += int(debit) - int(credit)
    return out


def _concerns(standing: Standing) -> None:
    """Say plainly what is wrong with a job, or say nothing."""
    from ..money import fmt

    project, figures = standing.project, standing.figures

    if standing.over_budget:
        over = figures.cost - project.budget_cost
        standing.concerns.append(
            f"Costs are {fmt(over)} over the budget of {fmt(project.budget_cost)}."
        )

    if project.contract_value and figures.cost:
        billed_share = figures.revenue / project.contract_value
        cost_share = figures.cost / (project.budget_cost or project.contract_value)
        if cost_share - billed_share > 0.25:
            standing.concerns.append(
                f"About {cost_share * 100:.0f}% of the expected cost has been "
                f"incurred but only {billed_share * 100:.0f}% has been invoiced. "
                "Either an invoice is owed or the job is running away."
            )

    if figures.revenue and figures.profit < 0:
        standing.concerns.append(
            f"It has cost {fmt(figures.cost)} and billed {fmt(figures.revenue)}. "
            "As it stands the job is losing money."
        )

    if project.due_on and project.is_open and project.due_on < Date.today():
        late = (Date.today() - project.due_on).days
        standing.concerns.append(
            f"It was due to finish {late} days ago and is still open."
        )


def standings(db: Session, start: Date | None = None, end: Date | None = None,
              include_finished: bool = True) -> list[Standing]:
    """Every project, worst first."""
    totals = _totals(db, start, end)
    query = select(Project)
    if not include_finished:
        query = query.where(Project.status == PROJECT_OPEN)

    out: list[Standing] = []
    for project in db.scalars(query.order_by(Project.code)):
        standing = Standing(project=project,
                            figures=totals.get(project.id, Figures()))
        if project.budget_cost:
            standing.budget_used = standing.figures.cost * 100.0 / project.budget_cost
        if project.contract_value:
            standing.billed = standing.figures.revenue * 100.0 / project.contract_value
        _concerns(standing)
        out.append(standing)

    out.sort(key=lambda s: (not s.needs_attention, -s.figures.cost))
    return out


def one(db: Session, project_id: int, start: Date | None = None,
        end: Date | None = None) -> Standing | None:
    project = db.get(Project, project_id)
    if project is None:
        return None
    standing = Standing(project=project,
                        figures=_totals(db, start, end).get(project_id, Figures()))
    if project.budget_cost:
        standing.budget_used = standing.figures.cost * 100.0 / project.budget_cost
    if project.contract_value:
        standing.billed = standing.figures.revenue * 100.0 / project.contract_value
    _concerns(standing)
    return standing


def ledger(db: Session, project_id: int, start: Date | None = None,
           end: Date | None = None):
    """Every posted line coded to this job, so the figures can be checked."""
    query = (
        select(JournalLine, JournalEntry, Account)
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.is_posted.is_(True),
            JournalLine.project_id == project_id,
            Account.type.in_([INCOME, EXPENSE]),
        )
        .order_by(JournalEntry.date, JournalEntry.id)
    )
    if start:
        query = query.where(JournalEntry.date >= start)
    if end:
        query = query.where(JournalEntry.date <= end)
    return db.execute(query).all()


def unallocated(db: Session, start: Date | None = None,
                end: Date | None = None) -> Figures:
    """Revenue and cost coded to no job at all.

    Shown on the list on purpose. Job costing where most of the money is
    uncoded looks complete and is not, and somebody will make a decision on it.
    """
    query = (
        select(
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
        .join(Account, JournalLine.account_id == Account.id)
        .where(
            JournalEntry.is_posted.is_(True),
            JournalLine.project_id.is_(None),
            Account.type.in_([INCOME, EXPENSE]),
        )
        .group_by(Account.type)
    )
    if start:
        query = query.where(JournalEntry.date >= start)
    if end:
        query = query.where(JournalEntry.date <= end)

    figures = Figures()
    for acc_type, debit, credit in db.execute(query):
        if acc_type == INCOME:
            figures.revenue += int(credit) - int(debit)
        else:
            figures.cost += int(debit) - int(credit)
    return figures


def choices(db: Session) -> list[Project]:
    """The jobs a person can code something to: open ones, newest first."""
    return list(db.scalars(
        select(Project).where(Project.status == PROJECT_OPEN).order_by(Project.code)
    ))
