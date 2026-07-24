"""Practice task ledger helpers."""

from __future__ import annotations

from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.practice_task import PracticeTask


TASK_STATUSES = [
    "Planned",
    "In Progress",
    "Overdue and Imminent",
    "Planning",
    "Pre Planning",
    "Completed",
    "Cancelled",
]


def open_tasks(db: Session) -> List[PracticeTask]:
    return (
        db.query(PracticeTask)
        .options(joinedload(PracticeTask.client), joinedload(PracticeTask.job))
        .filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
        .order_by(PracticeTask.due_on.asc(), PracticeTask.id.desc())
        .all()
    )


def list_tasks(
    db: Session,
    *,
    status: str = "",
    client_id: Optional[int] = None,
    include_closed: bool = False,
    limit: int = 200,
) -> List[PracticeTask]:
    q = db.query(PracticeTask).options(
        joinedload(PracticeTask.client), joinedload(PracticeTask.job)
    )
    if status:
        q = q.filter(PracticeTask.status == status)
    elif not include_closed:
        q = q.filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
    if client_id:
        q = q.filter(PracticeTask.client_id == client_id)
    return (
        q.order_by(PracticeTask.due_on.asc(), PracticeTask.id.desc())
        .limit(limit)
        .all()
    )


def task_horizon_key(task: PracticeTask, today: Optional[date] = None) -> str:
    """Align task buckets with WIP horizons."""
    from app.services.working_capital import job_horizon_key_for_due

    today = today or date.today()
    if task.is_closed():
        return "later"
    return job_horizon_key_for_due(task.due_on, today)


def compute_task_horizons(db: Session, today: Optional[date] = None) -> dict:
    today = today or date.today()
    tasks = open_tasks(db)
    buckets = {
        "imminent": {"key": "imminent", "label": "Overdue and Imminent", "count": 0, "amount": 0.0},
        "planning": {"key": "planning", "label": "Planning", "count": 0, "amount": 0.0},
        "pre_planning": {"key": "pre_planning", "label": "Pre Planning", "count": 0, "amount": 0.0},
        "later": {"key": "later", "label": "Everything else", "count": 0, "amount": 0.0},
    }
    for t in tasks:
        k = task_horizon_key(t, today)
        if k not in buckets:
            k = "later"
        buckets[k]["count"] += 1
        buckets[k]["amount"] += float(t.fee or 0)
    for b in buckets.values():
        b["amount"] = round(b["amount"], 2)
    return {
        "buckets": list(buckets.values()),
        "total_count": len(tasks),
        "total_amount": round(sum(float(t.fee or 0) for t in tasks), 2),
        "tasks": tasks,
    }
