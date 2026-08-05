import re
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, quote, unquote

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


def _safe_return_to(value: str, fallback: str = "/jobs") -> str:
    """Only allow same-app relative paths (no open redirects)."""
    v = unquote((value or "").strip())
    if not v:
        return fallback
    if v.startswith("/") and not v.startswith("//") and "://" not in v and "\n" not in v:
        return v
    return fallback


def _return_to_from_request(request: Request, explicit: str = "") -> str:
    """Prefer explicit return_to, else safe Referer path (list you came from)."""
    explicit = (explicit or request.query_params.get("return_to") or "").strip()
    if explicit:
        return _safe_return_to(explicit)
    ref = (request.headers.get("referer") or "").strip()
    if not ref:
        return ""
    try:
        parsed = urlparse(ref)
        # Same host only
        host = (request.url.hostname or "").lower()
        ref_host = (parsed.hostname or "").lower()
        if ref_host and host and ref_host != host:
            return ""
        path = parsed.path or ""
        if not path.startswith("/") or path.startswith("//"):
            return ""
        # Prefer list/filter screens over job detail itself
        if re.match(r"^/jobs/\d+", path):
            return ""
        q = f"?{parsed.query}" if parsed.query else ""
        return _safe_return_to(path + q, "")
    except Exception:
        return ""


def _client_is_lost(client) -> bool:
    return bool(client and (client.overall_status or "") == "Inactive")


def _client_looks_like_limited_company(client) -> bool:
    """True for firm-style clients (Ltd / Limited / real CH number), not IND- people."""
    if not client:
        return False
    from app.services.individuals import is_individual_shell

    if is_individual_shell(client):
        return False
    name = (client.company_name or "").lower()
    if re.search(r"\b(limited|ltd|llp|plc)\b", name):
        return True
    cn = (client.company_number or "").strip().upper()
    # Real company numbers (not blank / IND-)
    if cn and not cn.startswith("IND-") and re.fullmatch(r"[A-Z]{0,2}\d{6,8}", cn):
        return True
    return False


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
    # Focus / horizon statuses are often computed from due dates
    _computed = {
        "Overdue",
        "Overdue and Imminent",
        "Imminent",
        "Planning",
        "Pre Planning",
        "Later",
        "Today",
        "Tomorrow",
        "This week",
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
        if not status and not filter:
            jobs = [j for j in jobs if j.status not in ("Completed", "Cancelled")]
    else:
        jobs = [j for j in jobs if not _client_is_lost(j.client)]

    # Other = not Accounts / Self Assessment (includes Confirmation Statement + rest)
    if job_type == "Other":
        jobs = [
            j
            for j in jobs
            if (j.type or "").strip() not in ("Accounts", "Self Assessment")
        ]

    # Type book lists (Accounts / SAR / CS / Other) default to open WIP —
    # never mix in Completed / Cancelled unless status explicitly asks for them.
    _type_books = {
        "Accounts",
        "Self Assessment",
        "Confirmation Statement",
        "Other",
    }
    if (
        job_type in _type_books
        and (not status or status in _computed)
        and (filter or "open") == "open"
    ):
        jobs = [j for j in jobs if not j.is_closed()]

    # SAR is personal tax — hide Limited / firm clients (data quality)
    if job_type == "Self Assessment":
        jobs = [
            j
            for j in jobs
            if not _client_looks_like_limited_company(j.client)
        ]

    if status == "Overdue" or status == "Overdue and Imminent":
        jobs = [
            j
            for j in jobs
            if j.display_status(today) in ("Overdue", "Overdue and Imminent")
            or j.is_overdue(today)
        ]
    elif status == "Imminent":
        jobs = [j for j in jobs if j.display_status(today) == "Imminent"]
    elif status == "Today":
        jobs = [
            j
            for j in jobs
            if j.display_status(today) in ("Today", "Overdue")
            or (j.status or "") == "Today"
        ]
    elif status == "Tomorrow":
        jobs = [
            j
            for j in jobs
            if j.display_status(today) in ("Tomorrow", "Imminent")
            or (j.status or "") == "Tomorrow"
        ]
    elif status == "This week":
        jobs = [
            j
            for j in jobs
            if j.display_status(today) == "This week"
            or (j.status or "") == "This week"
        ]
    elif status in ("Planning", "Pre Planning", "Later"):
        jobs = [j for j in jobs if j.display_status(today) == status]

    if filter == "overdue":
        jobs = [
            j
            for j in jobs
            if j.is_overdue(today) or j.display_status(today) == "Overdue"
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
    q = request.url.query
    list_return = request.url.path + (f"?{q}" if q else "")
    list_return_q = "?return_to=" + quote(list_return, safe="")
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
            "list_return": list_return,
            "list_return_q": list_return_q,
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
    # Type lists from WIP tiles = open book only (not completed)
    return _list_jobs_page(
        request,
        db,
        status=status,
        filter=filter or "open",
        job_type="Accounts",
        title="Accounts jobs",
        view="accounts",
    )


@router.get("/other", response_class=HTMLResponse)
async def list_other_jobs(
    request: Request,
    status: str = Query(""),
    filter: str = Query(""),
    db: Session = Depends(get_db),
):
    return _list_jobs_page(
        request,
        db,
        status=status,
        filter=filter or "open",
        job_type="Other",
        title="Other jobs",
        view="other",
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
        filter=filter or "open",
        job_type="Self Assessment",
        title="SAR jobs",
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
        filter=filter or "open",
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
    request: Request,
    status: str = Form(...),
    next: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Change job workflow status from any list (returns to the list you were on)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job and status in JOB_STATUSES:
        job.status = status
        job.updated_at = datetime.utcnow()
        db.commit()
    dest = _safe_return_to(
        next or return_to or _return_to_from_request(request),
        f"/jobs/{job_id}",
    )
    return RedirectResponse(dest, status_code=303)


@router.post("/bulk-status")
async def jobs_bulk_status(
    request: Request,
    status: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Set status on many jobs at once (Today / Tomorrow / This week / SAR lists).
    Expects form fields job_ids (repeated) or job_ids[] style multi-select.
    """
    form = await request.form()
    raw_ids = form.getlist("job_ids") or form.getlist("job_ids[]")
    ids: list[int] = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    # de-dupe preserve order
    seen = set()
    ids = [i for i in ids if not (i in seen or seen.add(i))]

    updated = 0
    if status in JOB_STATUSES and ids:
        jobs = db.query(Job).filter(Job.id.in_(ids)).all()
        now = datetime.utcnow()
        for job in jobs:
            job.status = status
            job.updated_at = now
            if status == "Completed" and not job.actual_completion:
                job.actual_completion = date.today()
            updated += 1
        db.commit()

    dest = _safe_return_to(
        return_to or _return_to_from_request(request),
        "/jobs",
    )
    # Preserve flash via query
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(
        f"{dest}{sep}bulk_msg={updated}+jobs+set+to+{quote(status)}",
        status_code=303,
    )


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
    client_docs_unlinked = []
    docs_conn = {
        "configured": False,
        "connected": False,
        "fresh": False,
    }
    try:
        from app.services import documents as docs_svc

        documents = docs_svc.list_documents(db, job_id=job_id, limit=100)
        docs_conn = docs_svc.docs_connection(db)
        if job.client_id:
            # Files on the client from OneDrive scan that are not on this job yet
            for d in docs_svc.list_documents(db, client_id=job.client_id, limit=100):
                if not d.job_id or d.job_id != job.id:
                    if not d.job_id:
                        client_docs_unlinked.append(d)
    except Exception:
        documents = []
        client_docs_unlinked = []
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
    return_to = _return_to_from_request(request)
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
            "client_docs_unlinked": client_docs_unlinked,
            "docs_conn": docs_conn,
            "job_tasks": job_tasks,
            "job_emails": job_emails,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "return_to": return_to,
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
    return_to = _return_to_from_request(request)
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
            "return_to": return_to,
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
    return_to: str = Form(""),
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
    # Back to the list you started from (Today / WIP / SAR list…), not always All jobs
    dest = _safe_return_to(return_to, f"/jobs/{job_id}")
    return RedirectResponse(dest, status_code=303)


def _spawn_next_recurring_job(db: Session, job: Job) -> Optional[Job]:
    """
    When a recurring job is completed, create next year's job if missing.
    Returns the next job if created (or already present), else None.
    """
    is_rec = (job.is_recurring or "").strip().lower() in ("yes", "y", "true", "1")
    if not is_rec:
        return None
    pe = job.period_end
    if not pe or not job.client_id:
        return None
    try:
        next_pe = date(pe.year + 1, pe.month, pe.day)
    except ValueError:
        # 29 Feb → 28 Feb next year
        next_pe = date(pe.year + 1, pe.month, 28)

    existing = (
        db.query(Job)
        .filter(
            Job.client_id == job.client_id,
            Job.type == job.type,
            Job.period_end == next_pe,
        )
        .first()
    )
    if existing:
        return existing

    statutory, ts, tc = calculate_dates(job.type or "", next_pe)
    next_fee = get_suggested_fee(
        db, job.type or "", next_pe, client_id=job.client_id
    )
    if next_fee is None:
        fee_val = float(job.fee or 0)
        next_fee = round(fee_val * 1.05, 2) if fee_val else 0.0
    next_job = Job(
        title=f"{job.type} — {next_pe.isoformat()}",
        type=job.type,
        client_id=job.client_id,
        period_end=next_pe,
        statutory_due_date=statutory,
        target_start=ts,
        target_completion=tc,
        fee=next_fee,
        status="Planned",
        is_recurring=job.is_recurring or "Yes",
        notes=f"Auto-created from job #{job.id} (prior fee + 5%)",
    )
    db.add(next_job)
    return next_job


@router.post("/{job_id:int}/done")
async def job_done(
    job_id: int,
    request: Request,
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Task-ledger style Done for jobs:
    mark Completed, spawn next recurring job, raise invoice, open that invoice.
    Safe if already completed — still opens / creates the invoice.
    """
    from app.services.sales_ledger import invoice_from_job

    job = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.id == job_id)
        .first()
    )
    if not job:
        return RedirectResponse("/jobs", status_code=303)

    if (job.status or "") == "Cancelled":
        dest = _safe_return_to(next or _return_to_from_request(request), f"/jobs/{job_id}")
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(
            f"{dest}{sep}msg={quote('Job is cancelled — not completed')}",
            status_code=303,
        )

    job.status = "Completed"
    if not job.actual_completion:
        job.actual_completion = date.today()
    job.updated_at = datetime.utcnow()

    # Ensure next year exists for recurring jobs (idempotent — skips if present)
    try:
        _spawn_next_recurring_job(db, job)
    except Exception:
        pass

    db.commit()

    try:
        from app.services.asana_jobs import sync_status_from_crm

        sync_status_from_crm(db, job)
    except Exception:
        pass

    if not job.client_id:
        dest = _safe_return_to(next or _return_to_from_request(request), f"/jobs/{job_id}")
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(
            f"{dest}{sep}msg={quote('Job completed but has no client — cannot invoice')}",
            status_code=303,
        )

    try:
        inv = invoice_from_job(db, job)
    except Exception as e:
        dest = _safe_return_to(next or _return_to_from_request(request), f"/jobs/{job_id}")
        sep = "&" if "?" in dest else "?"
        return RedirectResponse(
            f"{dest}{sep}msg={quote(f'Job completed; invoice failed: {e}')}",
            status_code=303,
        )

    return RedirectResponse(f"/sales/invoices/{inv.id}", status_code=303)
