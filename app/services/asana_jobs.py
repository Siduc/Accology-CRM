"""CRM Job ↔ Asana task mapping and sync."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models import Job
from app.services import asana_client
from app.services.client_connections import (
    client_ids_with_provider,
    is_connected,
)

STATUTORY_TYPES = ("Accounts", "Confirmation Statement")
CLOSED = frozenset(Job.CLOSED_STATUSES)


@dataclass
class PushResult:
    ok: bool
    message: str
    task_gid: Optional[str] = None
    created: bool = False


def crm_status_completed(status: Optional[str]) -> bool:
    return (status or "") in CLOSED


def job_task_name(job: Job) -> str:
    client_name = "Client"
    if job.client:
        client_name = job.client.display_name()
    elif job.client_id:
        client_name = f"Client #{job.client_id}"
    jtype = job.type or "Job"
    pe = f" — PE {job.period_end}" if job.period_end else ""
    return f"{client_name} — {jtype}{pe}"[:500]


def job_task_notes(job: Job) -> str:
    lines = [
        f"CRM job #{job.id}",
        f"Type: {job.type or '—'}",
        f"Status: {job.status or '—'}",
        f"Period end: {job.period_end or '—'}",
        f"Statutory due: {job.statutory_due_date or '—'}",
        f"Fee: £{float(job.fee or 0):.2f}",
    ]
    if job.client:
        lines.insert(
            1,
            f"Client: {job.client.display_name()} ({job.client.company_number or '—'})",
        )
    lines.append(f"CRM path: /jobs/{job.id}")
    if job.notes:
        lines.append("")
        lines.append(str(job.notes)[:1500])
    return "\n".join(lines)


def jobs_for_asana_push(
    db: Session,
    *,
    only_overdue: bool = True,
    types: Sequence[str] = STATUTORY_TYPES,
    today: Optional[date] = None,
    limit: int = 100,
) -> List[Job]:
    today = today or date.today()
    q = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(list(CLOSED)))
        .filter(Job.type.in_(list(types)))
    )
    jobs = q.limit(500).all()
    # Privacy: only clients with Asana connection enabled
    allowed = client_ids_with_provider(db, "asana")
    out: List[Job] = []
    for j in jobs:
        if j.client and (j.client.overall_status or "") == "Inactive":
            continue
        if not j.client_id or j.client_id not in allowed:
            continue
        if only_overdue:
            if not j.statutory_due_date or j.statutory_due_date >= today:
                continue
        out.append(j)
    out.sort(key=lambda j: j.statutory_due_date or date.max)
    return out[:limit]


def push_job(db: Session, job_id: int) -> PushResult:
    job = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.id == job_id)
        .first()
    )
    if not job:
        return PushResult(ok=False, message="Job not found.")

    # Opt-in per client first (privacy — Connections tab)
    if not is_connected(db, job.client_id, "asana"):
        return PushResult(
            ok=False,
            message="Asana is not enabled for this client. Turn it on under Client → Connections.",
        )

    if not asana_client.is_configured():
        return PushResult(ok=False, message="Asana not configured.")
    if not asana_client.workspace_configured():
        return PushResult(ok=False, message="Set ASANA_WORKSPACE_GID in .env.")

    name = job_task_name(job)
    notes = job_task_notes(job)
    due = job.statutory_due_date.isoformat() if job.statutory_due_date else None
    completed = crm_status_completed(job.status)

    if job.asana_task_gid:
        res = asana_client.update_task(
            job.asana_task_gid,
            completed=completed,
            due_on=due,
            name=name,
            notes=notes,
        )
        if not res.ok:
            return PushResult(ok=False, message=res.error, task_gid=job.asana_task_gid)
        job.asana_synced_at = datetime.utcnow()
        db.commit()
        return PushResult(
            ok=True,
            message="Asana task updated.",
            task_gid=job.asana_task_gid,
            created=False,
        )

    res = asana_client.create_task(
        name=name,
        notes=notes,
        due_on=due,
        assignee="me",
    )
    if not res.ok:
        return PushResult(ok=False, message=res.error)
    gid = str((res.data or {}).get("gid") or "")
    if not gid:
        return PushResult(ok=False, message="Asana create returned no task id.")
    if completed:
        asana_client.update_task(gid, completed=True)
    job.asana_task_gid = gid
    job.asana_synced_at = datetime.utcnow()
    db.commit()
    return PushResult(ok=True, message="Pushed to Asana.", task_gid=gid, created=True)


def pull_status_for_job(db: Session, job_id: int) -> PushResult:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return PushResult(ok=False, message="Job not found.")
    if not job.asana_task_gid:
        return PushResult(ok=False, message="Job is not linked to Asana.")
    res = asana_client.get_task(job.asana_task_gid)
    if not res.ok:
        return PushResult(ok=False, message=res.error, task_gid=job.asana_task_gid)
    completed = bool((res.data or {}).get("completed"))
    if completed and not crm_status_completed(job.status):
        job.status = "Completed"
        if not job.actual_completion:
            job.actual_completion = date.today()
        job.asana_synced_at = datetime.utcnow()
        db.commit()
        return PushResult(
            ok=True,
            message="CRM job marked Completed from Asana.",
            task_gid=job.asana_task_gid,
        )
    job.asana_synced_at = datetime.utcnow()
    db.commit()
    return PushResult(
        ok=True,
        message="No status change (Asana still open or already complete).",
        task_gid=job.asana_task_gid,
    )


def sync_status_from_crm(db: Session, job: Job) -> Optional[str]:
    """After CRM status change, push completed flag to Asana if linked and allowed."""
    if not job.asana_task_gid or not asana_client.is_configured():
        return None
    if not is_connected(db, job.client_id, "asana"):
        return None  # connection off — no outbound sync
    completed = crm_status_completed(job.status)
    due = job.statutory_due_date.isoformat() if job.statutory_due_date else None
    res = asana_client.update_task(
        job.asana_task_gid,
        completed=completed,
        due_on=due,
    )
    if res.ok:
        job.asana_synced_at = datetime.utcnow()
        db.commit()
        return None
    return res.error


def push_overdue_batch(db: Session, *, limit: int = 25) -> Tuple[int, int, List[str]]:
    """Push overdue Accounts + CS. Returns (ok_count, fail_count, messages)."""
    jobs = jobs_for_asana_push(db, only_overdue=True, limit=limit)
    ok_n = fail_n = 0
    msgs: List[str] = []
    for j in jobs:
        r = push_job(db, j.id)
        if r.ok:
            ok_n += 1
        else:
            fail_n += 1
            msgs.append(f"Job {j.id}: {r.message}")
    return ok_n, fail_n, msgs


def pull_linked_batch(db: Session, *, limit: int = 50) -> Tuple[int, int]:
    jobs = (
        db.query(Job)
        .filter(Job.asana_task_gid.isnot(None))
        .limit(limit)
        .all()
    )
    ok_n = fail_n = 0
    for j in jobs:
        r = pull_status_for_job(db, j.id)
        if r.ok:
            ok_n += 1
        else:
            fail_n += 1
    return ok_n, fail_n


def fetch_my_tasks_views(limit: int = 40):
    res = asana_client.list_my_incomplete_tasks(limit=limit)
    if not res.ok:
        return res, []
    tasks = (res.data or {}).get("tasks") or []
    if isinstance(res.data, list):
        tasks = res.data
    return res, asana_client.tasks_to_views(tasks)
