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
    # Horizon statuses are computed from due dates — not always stored on the row
    _computed = {
        "Overdue",
        "Overdue and Imminent",
        "Planning",
        "Pre Planning",
        "Later",
    }
    if status and status not in _computed:
        query = query.filter(Job.status == status)
    if job_type == "Accounts":
        query = query.filter(Job.type == "Accounts")
    elif job_type == "Self Assessment":
        query = query.filter(Job.type == "Self Assessment")
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

    if status == "Overdue" or status == "Overdue and Imminent":
        jobs = [
            j
            for j in jobs
            if j.display_status(today) == "Overdue and Imminent" or j.is_overdue(today)
        ]
    elif status in ("Planning", "Pre Planning", "Later"):
        jobs = [j for j in jobs if j.display_status(today) == status]

    if filter == "overdue":
        jobs = [
            j
            for j in jobs
            if j.is_overdue(today) or j.display_status(today) == "Overdue and Imminent"
        ]
    elif filter == "due_soon":
        soon = today + timedelta(days=30)
        jobs = [
            j
            for j in jobs
            if not j.is_closed()
            and j.due_date()
            and today <= j.due_date() <= soon
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


@router.get("/self-assessment", response_class=HTMLResponse)
async def list_sa_jobs(
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
        job_type="Self Assessment",
        title="Self Assessment jobs",
        view="sa",
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


@router.get("/completion", response_class=HTMLResponse)
async def jobs_completion_list(
    request: Request,
    period: str = Query("week"),
    type: str = Query(""),
    db: Session = Depends(get_db),
):
    """
    Invoicing control: completed jobs in chronological order.
    Defaults to jobs completed this week. Invoice number + net value
    are editable and will be filled automatically when raising invoices in-app.
    """
    from app.services.job_completion import list_completed_jobs

    snap = list_completed_jobs(db, period=period or "week", job_type=type or "")
    return render(
        request,
        "jobs/completion.html",
        {
            **snap,
            "saved": request.query_params.get("saved", ""),
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/completion/save")
async def jobs_completion_save(
    request: Request,
    db: Session = Depends(get_db),
):
    """Bulk-save invoice number + net value from the completion control list."""
    from app.services.job_completion import update_invoicing_fields

    form = await request.form()
    period = (form.get("period") or "week").strip()
    job_type = (form.get("type") or "").strip()

    # Collect job_id[] / invoice_number[] / net_value[] parallel lists
    ids = form.getlist("job_id")
    invs = form.getlist("invoice_number")
    nets = form.getlist("net_value")
    updates = []
    for i, jid in enumerate(ids):
        updates.append(
            {
                "job_id": jid,
                "invoice_number": invs[i] if i < len(invs) else "",
                "net_value": nets[i] if i < len(nets) else "",
            }
        )
    n = update_invoicing_fields(db, updates)
    q = f"period={period}"
    if job_type:
        q += f"&type={job_type}"
    q += f"&saved=1&msg={n}+updated"
    return RedirectResponse(f"/jobs/completion?{q}", status_code=303)


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
    target_start: str = Form(""),
    target_completion: str = Form(""),
    actual_start: str = Form(""),
    actual_completion: str = Form(""),
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
    statutory, calc_ts, calc_tc = calculate_dates(type, pe)
    try:
        fee_val = float(fee.replace("£", "").replace(",", "") or 0)
    except ValueError:
        fee_val = 0.0
    if fee_val == 0:
        suggested = get_suggested_fee(db, type, pe, client_id=client_id)
        if suggested is not None:
            fee_val = suggested

    ts = _parse_date(target_start) if target_start else calc_ts
    tc = _parse_date(target_completion) if target_completion else calc_tc
    act_s = _parse_date(actual_start) if actual_start else None
    act_c = _parse_date(actual_completion) if actual_completion else None
    status_val = status or "Planned"
    if status_val == "Completed" and not act_c:
        act_c = date.today()
    # Filling actual completion implies done — not when deliberately parked
    if act_c and status_val not in ("Completed", "Cancelled", "On hold"):
        status_val = "Completed"

    job_title = title or f"{type}" + (f" — {pe.isoformat()}" if pe else "")
    job = Job(
        title=job_title,
        type=type,
        client_id=client_id,
        period_end=pe,
        statutory_due_date=statutory,
        target_start=ts,
        target_completion=tc,
        actual_start=act_s,
        actual_completion=act_c,
        fee=fee_val,
        status=status_val,
        is_recurring=is_recurring or "Yes",
        notes=notes or None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.post("/{job_id:int}/status-quick")
async def job_status_quick(
    job_id: int,
    status: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Change job workflow status from any list (returns to next or job page)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job and status in JOB_STATUSES:
        job.status = status
        job.updated_at = datetime.utcnow()
        db.commit()
    dest = (next or "").strip() or f"/jobs/{job_id}"
    if not dest.startswith("/"):
        dest = f"/jobs/{job_id}"
    return RedirectResponse(dest, status_code=303)


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
    from app.services.client_connections import is_connected

    asana_enabled = is_connected(db, job.client_id, "asana") if job.client_id else False
    documents = []
    docs_conn = {
        "configured": False,
        "connected": False,
        "fresh": False,
    }
    try:
        from app.services import documents as docs_svc

        documents = docs_svc.list_documents(db, job_id=job_id, limit=100)
        docs_conn = docs_svc.docs_connection(db)
    except Exception:
        documents = []
    job_tasks = []
    try:
        from app.services.practice_tasks import list_tasks

        job_tasks = list_tasks(db, job_id=job_id, include_closed=False, limit=50)
    except Exception:
        job_tasks = []
    job_emails = []
    try:
        from app.services import practice_emails as practice_mail

        practice_mail.seed_email_templates(db)
        job_emails = practice_mail.list_messages(db, job_id=job_id, limit=40)
    except Exception:
        job_emails = []
    return render(
        request,
        "jobs/detail.html",
        {
            "job": job,
            "today": date.today(),
            "asana_msg": request.query_params.get("asana_msg", ""),
            "asana_error": request.query_params.get("asana_error", ""),
            "asana_enabled": asana_enabled,
            "documents": documents,
            "docs_conn": docs_conn,
            "job_tasks": job_tasks,
            "job_emails": job_emails,
            "msg": request.query_params.get("msg", ""),
        },
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
    target_start: str = Form(""),
    target_completion: str = Form(""),
    actual_start: str = Form(""),
    actual_completion: str = Form(""),
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
        statutory, calc_ts, calc_tc = calculate_dates(type, pe)
        job.statutory_due_date = statutory
        job.target_start = calc_ts
        job.target_completion = calc_tc
    else:
        if target_start:
            job.target_start = _parse_date(target_start)
        if target_completion:
            job.target_completion = _parse_date(target_completion)

    job.actual_start = _parse_date(actual_start) if actual_start else None
    job.actual_completion = _parse_date(actual_completion) if actual_completion else None
    # Completing: status ↔ actual_completion stay in sync
    if status == "Completed" and not job.actual_completion:
        job.actual_completion = date.today()
    if job.actual_completion and status not in ("Completed", "Cancelled", "On hold"):
        # Date filled = treated as complete for invoicing control
        status = "Completed"
        job.status = "Completed"

    if status == "Completed" and (is_recurring or "").lower() in (
        "yes",
        "y",
        "true",
        "1",
    ):
        if pe:
            next_pe = date(pe.year + 1, pe.month, pe.day)
            statutory, ts, tc = calculate_dates(type, next_pe)
            next_fee = get_suggested_fee(
                db, type, next_pe, client_id=client_id
            )
            if next_fee is None:
                # Fall back: this job's fee + 5%
                next_fee = round((fee_val or 0) * 1.05, 2) if fee_val else 0.0
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
                notes=f"Auto-created from job #{job.id} (prior fee + 5%)",
            )
            db.add(next_job)

    db.commit()
    # Push completion/due to Asana if linked
    try:
        from app.services.asana_jobs import sync_status_from_crm

        sync_status_from_crm(db, job)
    except Exception:
        pass
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)
