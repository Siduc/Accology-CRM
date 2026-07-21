from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Client, Job
from app.services.dates import calculate_dates, JOB_TYPES, JOB_STATUSES
from app.services.fees import get_suggested_fee
from app.templating import render

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _parse_date(value: str):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _client_is_lost(client) -> bool:
    return bool(client and (client.overall_status or "") == "Inactive")


def _list_jobs_page(
    request: Request,
    db: Session,
    *,
    status: str = "",
    filter: str = "",
    job_type: str = "",
    title: str = "Jobs",
    view: str = "all",
    lost_only: bool = False,
):
    today = date.today()
    query = db.query(Job).options(joinedload(Job.client))
    if status:
        query = query.filter(Job.status == status)
    if job_type == "Accounts":
        query = query.filter(Job.type == "Accounts")
    elif job_type == "Confirmation Statement":
        query = query.filter(Job.type == "Confirmation Statement")
    jobs = query.order_by(Job.statutory_due_date.asc()).all()

    # Split live vs lost by parent client status
    if lost_only:
        jobs = [j for j in jobs if _client_is_lost(j.client)]
        # Lost jobs list focuses on work that was still live when client was lost
        if not status and not filter:
            jobs = [j for j in jobs if j.status not in ("Completed", "Cancelled")]
    else:
        jobs = [j for j in jobs if not _client_is_lost(j.client)]

    if filter == "overdue":
        jobs = [
            j
            for j in jobs
            if j.statutory_due_date
            and j.statutory_due_date < today
            and j.status not in ("Completed", "Cancelled")
        ]
    elif filter == "due_soon":
        soon = today + timedelta(days=30)
        jobs = [
            j
            for j in jobs
            if j.statutory_due_date
            and today <= j.statutory_due_date <= soon
            and j.status not in ("Completed", "Cancelled")
        ]
    elif filter == "open":
        jobs = [j for j in jobs if j.status not in ("Completed", "Cancelled")]

    total_fees = sum(j.fee or 0 for j in jobs if j.status not in ("Cancelled",))
    return render(
        request,
        "jobs/list.html",
        {
            "jobs": jobs,
            "status": status,
            "filter": filter,
            "job_type": job_type,
            "statuses": JOB_STATUSES,
            "today": today,
            "page_title": title,
            "view": view,
            "total_fees": round(total_fees, 2),
        },
    )


@router.get("/from-companies-house")
async def legacy_ch_jobs_redirect():
    """Old URL collided with /jobs/{job_id}; send users to the fixed path."""
    return RedirectResponse("/companies-house/jobs", status_code=303)


@router.get("/accounts", response_class=HTMLResponse)
async def list_accounts_jobs(
    request: Request,
    status: str = Query(""),
    filter: str = Query(""),
    db: Session = Depends(get_db),
):
    return _list_jobs_page(
        request,
        db,
        status=status,
        filter=filter,
        job_type="Accounts",
        title="Accounts jobs",
        view="accounts",
    )


@router.get("/confirmation-statements", response_class=HTMLResponse)
async def list_cs_jobs(
    request: Request,
    status: str = Query(""),
    filter: str = Query(""),
    db: Session = Depends(get_db),
):
    return _list_jobs_page(
        request,
        db,
        status=status,
        filter=filter,
        job_type="Confirmation Statement",
        title="Confirmation Statement jobs",
        view="cs",
    )


@router.get("/lost", response_class=HTMLResponse)
async def list_lost_jobs_legacy(
    request: Request,
    status: str = Query(""),
    filter: str = Query(""),
    type: str = Query(""),
    db: Session = Depends(get_db),
):
    """Legacy URL — prefer /lost/jobs to avoid any {job_id} clash."""
    from fastapi.responses import RedirectResponse as RR

    return RR("/lost/jobs", status_code=303)


@router.get("", response_class=HTMLResponse)
async def list_jobs(
    request: Request,
    status: str = Query(""),
    filter: str = Query(""),
    type: str = Query(""),
    db: Session = Depends(get_db),
):
    return _list_jobs_page(
        request,
        db,
        status=status,
        filter=filter,
        job_type=type,
        title="All jobs",
        view="all",
    )


@router.get("/new", response_class=HTMLResponse)
async def new_job_form(
    request: Request,
    client_id: int = Query(None),
    db: Session = Depends(get_db),
):
    clients = db.query(Client).order_by(Client.company_name).all()
    return render(
        request,
        "jobs/form.html",
        {
            "job": None,
            "clients": clients,
            "job_types": JOB_TYPES,
            "statuses": JOB_STATUSES,
            "selected_client_id": client_id,
            "error": None,
        },
    )


@router.post("/new")
async def create_job(
    request: Request,
    client_id: int = Form(...),
    title: str = Form(""),
    type: str = Form(...),
    period_end: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    is_recurring: str = Form("Yes"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    clients = db.query(Client).order_by(Client.company_name).all()
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return render(
            request,
            "jobs/form.html",
            {
                "job": None,
                "clients": clients,
                "job_types": JOB_TYPES,
                "statuses": JOB_STATUSES,
                "selected_client_id": client_id,
                "error": "Client not found.",
            },
            status_code=400,
        )

    pe = _parse_date(period_end) if period_end else None
    statutory, target_start, target_completion = calculate_dates(type, pe)
    try:
        fee_val = float(fee.replace("£", "").replace(",", "") or 0)
    except ValueError:
        fee_val = 0.0
    if fee_val == 0:
        suggested = get_suggested_fee(db, type, pe)
        if suggested is not None:
            fee_val = suggested

    job_title = title or f"{type}" + (f" — {pe.isoformat()}" if pe else "")
    job = Job(
        title=job_title,
        type=type,
        client_id=client_id,
        period_end=pe,
        statutory_due_date=statutory,
        target_start=target_start,
        target_completion=target_completion,
        fee=fee_val,
        status=status or "Planned",
        is_recurring=is_recurring or "Yes",
        notes=notes or None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/{job_id:int}", response_class=HTMLResponse)
async def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.id == job_id)
        .first()
    )
    if not job:
        return RedirectResponse("/jobs", status_code=303)
    return render(
        request,
        "jobs/detail.html",
        {"job": job, "today": date.today()},
    )


@router.get("/{job_id:int}/edit", response_class=HTMLResponse)
async def edit_job_form(
    job_id: int, request: Request, db: Session = Depends(get_db)
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return RedirectResponse("/jobs", status_code=303)
    clients = db.query(Client).order_by(Client.company_name).all()
    return render(
        request,
        "jobs/form.html",
        {
            "job": job,
            "clients": clients,
            "job_types": JOB_TYPES,
            "statuses": JOB_STATUSES,
            "selected_client_id": job.client_id,
            "error": None,
        },
    )


@router.post("/{job_id:int}/edit")
async def update_job(
    job_id: int,
    request: Request,
    client_id: int = Form(...),
    title: str = Form(""),
    type: str = Form(...),
    period_end: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    is_recurring: str = Form("Yes"),
    notes: str = Form(""),
    recalculate_dates: str = Form(""),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return RedirectResponse("/jobs", status_code=303)

    pe = _parse_date(period_end) if period_end else None
    try:
        fee_val = float(fee.replace("£", "").replace(",", "") or 0)
    except ValueError:
        fee_val = 0.0

    job.client_id = client_id
    job.title = title or job.title
    job.type = type
    job.period_end = pe
    job.fee = fee_val
    job.status = status
    job.is_recurring = is_recurring
    job.notes = notes or None
    job.updated_at = datetime.utcnow()

    if recalculate_dates == "yes" or not job.statutory_due_date:
        statutory, target_start, target_completion = calculate_dates(type, pe)
        job.statutory_due_date = statutory
        job.target_start = target_start
        job.target_completion = target_completion

    if status == "Completed" and (is_recurring or "").lower() in (
        "yes",
        "y",
        "true",
        "1",
    ):
        if pe:
            next_pe = date(pe.year + 1, pe.month, pe.day)
            statutory, ts, tc = calculate_dates(type, next_pe)
            next_fee = get_suggested_fee(db, type, next_pe)
            if next_fee is None:
                next_fee = fee_val
            next_job = Job(
                title=f"{type} — {next_pe.isoformat()}",
                type=type,
                client_id=client_id,
                period_end=next_pe,
                statutory_due_date=statutory,
                target_start=ts,
                target_completion=tc,
                fee=next_fee,
                status="Planned",
                is_recurring=is_recurring,
                notes=f"Auto-created from job #{job.id}",
            )
            db.add(next_job)

    db.commit()
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
