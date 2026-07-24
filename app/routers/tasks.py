"""Practice task ledger routes."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.models.practice_task import PracticeTask
from app.services.practice_tasks import TASK_STATUSES, list_tasks
from app.templating import render

router = APIRouter(tags=["tasks"])


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_money(value: str) -> float:
    try:
        return float((value or "0").replace("£", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    status: str = "",
    client_id: str = "",
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    tasks = list_tasks(
        db,
        status=status or "",
        client_id=cid,
        include_closed=bool(status),
    )
    clients = (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive"]))
        .order_by(Client.company_name)
        .limit(400)
        .all()
    )
    total_fees = round(sum(float(t.fee or 0) for t in tasks if not t.is_closed()), 2)
    return render(
        request,
        "tasks/list.html",
        {
            "tasks": tasks,
            "statuses": TASK_STATUSES,
            "status": status,
            "clients": clients,
            "filter_client_id": cid,
            "total_fees": total_fees,
            "today": date.today(),
        },
    )


@router.get("/tasks/new", response_class=HTMLResponse)
async def task_new_form(
    request: Request,
    client_id: str = "",
    job_id: str = "",
    db: Session = Depends(get_db),
):
    clients = (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive"]))
        .order_by(Client.company_name)
        .limit(400)
        .all()
    )
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    jobs = []
    if cid:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == cid)
            .filter(Job.status.notin_(["Cancelled"]))
            .order_by(Job.id.desc())
            .limit(50)
            .all()
        )
    return render(
        request,
        "tasks/form.html",
        {
            "task": None,
            "clients": clients,
            "jobs": jobs,
            "statuses": TASK_STATUSES,
            "pre_client_id": cid,
            "pre_job_id": jid,
        },
    )


@router.post("/tasks/new")
async def task_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    due_on: str = Form(""),
    period_end: str = Form(""),
    client_id: str = Form(""),
    job_id: str = Form(""),
    notes: str = Form(""),
    next: str = Form("/tasks"),
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    task = PracticeTask(
        title=(title or "").strip() or "Task",
        description=(description or "").strip() or None,
        fee=_parse_money(fee),
        status=status if status in TASK_STATUSES else "Planned",
        due_on=_parse_date(due_on),
        period_end=_parse_date(period_end),
        client_id=cid,
        job_id=jid,
        notes=(notes or "").strip() or None,
    )
    db.add(task)
    db.commit()
    dest = (next or "/tasks").strip() or "/tasks"
    return RedirectResponse(dest, status_code=303)


@router.get("/tasks/{task_id:int}/edit", response_class=HTMLResponse)
async def task_edit_form(
    task_id: int, request: Request, db: Session = Depends(get_db)
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if not task:
        return RedirectResponse("/tasks", status_code=303)
    clients = (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive"]))
        .order_by(Client.company_name)
        .limit(400)
        .all()
    )
    jobs = []
    if task.client_id:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == task.client_id)
            .order_by(Job.id.desc())
            .limit(50)
            .all()
        )
    return render(
        request,
        "tasks/form.html",
        {
            "task": task,
            "clients": clients,
            "jobs": jobs,
            "statuses": TASK_STATUSES,
            "pre_client_id": task.client_id,
            "pre_job_id": task.job_id,
        },
    )


@router.post("/tasks/{task_id:int}/edit")
async def task_update(
    task_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    due_on: str = Form(""),
    period_end: str = Form(""),
    client_id: str = Form(""),
    job_id: str = Form(""),
    notes: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if not task:
        return RedirectResponse("/tasks", status_code=303)
    task.title = (title or "").strip() or task.title
    task.description = (description or "").strip() or None
    task.fee = _parse_money(fee)
    task.status = status if status in TASK_STATUSES else task.status
    task.due_on = _parse_date(due_on)
    task.period_end = _parse_date(period_end)
    task.client_id = int(client_id) if (client_id or "").isdigit() else None
    task.job_id = int(job_id) if (job_id or "").isdigit() else None
    task.notes = (notes or "").strip() or None
    task.updated_at = datetime.utcnow()
    db.commit()
    dest = (next or f"/tasks/{task_id}/edit").strip()
    return RedirectResponse(dest, status_code=303)


@router.post("/tasks/{task_id:int}/complete")
async def task_complete(
    task_id: int,
    next: str = Form("/tasks"),
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if task:
        task.status = "Completed"
        task.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse((next or "/tasks").strip(), status_code=303)
