"""Practice task ledger helpers."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.practice_task import PracticeTask


TASK_STATUSES = [
    "Planned",
    "In Progress",
    "On hold",
    "Development",
    "Overdue and Imminent",
    "Planning",
    "Pre Planning",
    "Completed",
    "Cancelled",
]

TASK_PRIORITIES = ["High", "Medium", "Low"]

# WIP / active pipeline — exclude finished, parked, and development thinking
_TASK_INACTIVE = ("Completed", "Cancelled", "On hold", "Development")

_PRIORITY_SORT = {"High": 0, "Medium": 1, "Low": 2, "": 3, None: 3}


def open_tasks(db: Session) -> List[PracticeTask]:
    """Active open tasks (not completed, cancelled, or on hold) for WIP."""
    return (
        db.query(PracticeTask)
        .options(joinedload(PracticeTask.client), joinedload(PracticeTask.job))
        .filter(PracticeTask.status.notin_(list(_TASK_INACTIVE)))
        .order_by(PracticeTask.due_on.asc(), PracticeTask.id.desc())
        .all()
    )


def list_tasks(
    db: Session,
    *,
    status: str = "",
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    priority: str = "",
    include_closed: bool = False,
    include_hold: bool = True,
    limit: int = 200,
) -> List[PracticeTask]:
    """
    Task ledger query.
    Default open list: everything except Completed/Cancelled (On hold included).
    WIP uses open_tasks() which excludes On hold.
    """
    q = db.query(PracticeTask).options(
        joinedload(PracticeTask.client), joinedload(PracticeTask.job)
    )
    if status:
        q = q.filter(PracticeTask.status == status)
    elif not include_closed:
        q = q.filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
        if not include_hold:
            q = q.filter(PracticeTask.status != "On hold")
    if client_id:
        q = q.filter(PracticeTask.client_id == client_id)
    if job_id:
        q = q.filter(PracticeTask.job_id == job_id)
    if priority and priority in TASK_PRIORITIES:
        q = q.filter(PracticeTask.priority == priority)
    rows = (
        q.order_by(PracticeTask.due_on.asc(), PracticeTask.id.desc())
        .limit(limit)
        .all()
    )
    # Priority sort: High first, then due date (stable secondary)
    rows.sort(
        key=lambda t: (
            _PRIORITY_SORT.get(t.priority or "", 3),
            t.due_on or date.max,
            -(t.id or 0),
        )
    )
    return rows


def create_task(
    db: Session,
    *,
    title: str,
    description: Optional[str] = None,
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    fee: float = 0.0,
    status: str = "Planned",
    due_on: Optional[date] = None,
    period_end: Optional[date] = None,
    notes: Optional[str] = None,
    priority: Optional[str] = None,
    source_email_date: Optional[date] = None,
    import_source: Optional[str] = None,
    import_hash: Optional[str] = None,
    import_batch_id: Optional[str] = None,
) -> PracticeTask:
    pri = (priority or "").strip()
    if pri and pri not in TASK_PRIORITIES:
        pri = "Medium"
    if not pri:
        pri = "Medium"
    st = status if status in TASK_STATUSES else "Planned"
    task = PracticeTask(
        title=(title or "").strip() or "Task",
        description=(description or "").strip() or None,
        client_id=client_id,
        job_id=job_id,
        fee=float(fee or 0),
        status=st,
        due_on=due_on,
        period_end=period_end,
        notes=(notes or "").strip() or None,
        priority=pri,
        source_email_date=source_email_date,
        import_source=import_source,
        import_hash=import_hash,
        import_batch_id=import_batch_id,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def complete_task(db: Session, task: PracticeTask) -> PracticeTask:
    task.status = "Completed"
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task


def task_horizon_key(task: PracticeTask, today: Optional[date] = None) -> str:
    """Align task buckets with WIP horizons."""
    from app.services.working_capital import job_horizon_key_for_due

    today = today or date.today()
    if task.is_closed() or task.is_on_hold() or task.is_development():
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
