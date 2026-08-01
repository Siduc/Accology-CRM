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


def _next_sort_order(db: Session) -> int:
    """New tasks go to the top of the ledger (lowest sort_order)."""
    from sqlalchemy import func

    mn = db.query(func.min(PracticeTask.sort_order)).scalar()
    if mn is None:
        return 0
    return int(mn) - 10


def open_tasks(db: Session) -> List[PracticeTask]:
    """Active open tasks (not completed, cancelled, or on hold) for WIP."""
    rows = (
        db.query(PracticeTask)
        .options(joinedload(PracticeTask.client), joinedload(PracticeTask.job))
        .filter(PracticeTask.status.notin_(list(_TASK_INACTIVE)))
        .all()
    )
    return _sort_ledger(rows)


def _sort_ledger(rows: List[PracticeTask]) -> List[PracticeTask]:
    """
    Manual drag order first (sort_order), then due date, id.
    Priority is no longer used for list order — users drag continuously.
    """
    rows = list(rows)
    rows.sort(
        key=lambda t: (
            0 if t.sort_order is not None else 1,
            int(t.sort_order) if t.sort_order is not None else 10**9,
            t.due_on or date.max,
            -(t.id or 0),
        )
    )
    return rows


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
    Order: drag-and-drop sort_order (manual), then priority / due.
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
    rows = q.limit(limit).all()
    return _sort_ledger(rows)


def reorder_tasks(db: Session, ordered_ids: List[int]) -> int:
    """
    Persist ledger order from drag-and-drop.
    ordered_ids = task ids top → bottom. Returns count updated.
    """
    if not ordered_ids:
        return 0
    # Keep ids unique, preserve order
    seen = set()
    ids: List[int] = []
    for raw in ordered_ids:
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid in seen:
            continue
        seen.add(tid)
        ids.append(tid)
    if not ids:
        return 0
    tasks = (
        db.query(PracticeTask)
        .filter(PracticeTask.id.in_(ids))
        .all()
    )
    by_id = {t.id: t for t in tasks}
    n = 0
    # 10, 20, 30… leaves room for inserts without renumbering everything
    for i, tid in enumerate(ids):
        t = by_id.get(tid)
        if not t:
            continue
        new_ord = (i + 1) * 10
        if t.sort_order != new_ord:
            t.sort_order = new_ord
            t.updated_at = datetime.utcnow()
            n += 1
    db.commit()
    return n


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
    email_from: Optional[str] = None,
    email_to: Optional[str] = None,
    email_preview: Optional[str] = None,
    outlook_message_id: Optional[str] = None,
    outlook_conversation_id: Optional[str] = None,
    outlook_web_link: Optional[str] = None,
    commit: bool = True,
) -> PracticeTask:
    pri = (priority or "").strip()
    if pri and pri not in TASK_PRIORITIES:
        pri = "Medium"
    if not pri:
        pri = "Medium"
    st = status if status in TASK_STATUSES else "Planned"
    oid = (outlook_message_id or "").strip() or None
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
        sort_order=_next_sort_order(db),
        source_email_date=source_email_date,
        import_source=import_source,
        import_hash=import_hash,
        import_batch_id=import_batch_id,
        email_from=(email_from or "").strip() or None,
        email_to=(email_to or "").strip() or None,
        email_preview=(email_preview or "").strip() or None,
        outlook_message_id=oid,
        outlook_conversation_id=(outlook_conversation_id or "").strip() or None,
        outlook_web_link=(outlook_web_link or "").strip() or None,
        outlook_archive_status="none" if oid else None,
    )
    db.add(task)
    if commit:
        db.commit()
        db.refresh(task)
    else:
        db.flush()
    return task


def complete_task(
    db: Session, task: PracticeTask, *, archive_outlook: bool = True
) -> tuple:
    """
    Mark task completed. Optionally archive linked Outlook message via Graph.
    Returns (task, archive_message).
    """
    task.status = "Completed"
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    archive_msg = ""
    if archive_outlook and (task.outlook_message_id or "").strip():
        try:
            from app.services.practice_emails import archive_outlook_for_task

            _ok, archive_msg = archive_outlook_for_task(db, task)
            db.refresh(task)
        except Exception as exc:  # noqa: BLE001
            archive_msg = f"Archive error: {exc}"
            task.outlook_archive_status = "failed"
            db.commit()
    return task, archive_msg


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
