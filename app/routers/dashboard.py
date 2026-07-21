"""Dashboard — headcount tiles + workload overview."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Client, Job, Person
from app.services.grouping import (
    client_ids_for_period_and_statuses,
    count_groups,
)
from app.templating import render

router = APIRouter(tags=["dashboard"])

# Status mapping for practice headcount tiles
CLIENT_STATUSES = ("Active", "Former")
PROSPECT_STATUSES = ("Prospect",)
LOST_STATUSES = ("Inactive",)
GROUP_STATUSES = ("Active", "Former", "Prospect")

# Earliest year we have invoice / period-end history for
EARLIEST_INVOICE_YEAR = 2022


def _available_years(today: date, db: Session) -> List[int]:
    """
    Year tabs: EARLIEST_INVOICE_YEAR .. current year (inclusive), newest first.
    If data has an earlier min year, still floor at EARLIEST_INVOICE_YEAR.
    """
    end_y = today.year
    start_y = EARLIEST_INVOICE_YEAR
    # Optionally extend upward only — never before 2022 per product request
    return list(range(end_y, start_y - 1, -1))


def _parse_period(period: str, today: date) -> Tuple[str, Optional[int]]:
    """
    Returns (mode, year).
    mode = 'overall' | 'year'
    year = calendar year when mode == 'year'
    Accepts legacy this_year / last_year aliases.
    """
    raw = (period or "overall").strip().lower()
    if raw in ("", "overall", "all"):
        return "overall", None
    if raw == "this_year":
        return "year", today.year
    if raw == "last_year":
        return "year", today.year - 1
    if raw.isdigit():
        y = int(raw)
        if EARLIEST_INVOICE_YEAR <= y <= today.year + 1:
            return "year", y
    return "overall", None


def _year_datetime_bounds(year: int) -> Tuple[datetime, datetime]:
    return (
        datetime.combine(date(year, 1, 1), time.min),
        datetime.combine(date(year, 12, 31), time.max),
    )


def _year_date_bounds(year: int) -> Tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def _count_clients_by_statuses(
    db: Session,
    statuses: Sequence[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> int:
    q = db.query(func.count(Client.id)).filter(
        Client.overall_status.in_(list(statuses))
    )
    if start is not None and end is not None:
        q = q.filter(Client.created_at >= start, Client.created_at <= end)
    return int(q.scalar() or 0)


def _is_billable_job(job: Job) -> bool:
    if job.fee and float(job.fee) > 0:
        return True
    if job.gross_amount is not None and float(job.gross_amount) > 0:
        return True
    if (job.invoice_reference or "").strip():
        return True
    return False


def _invoice_date(job: Job) -> Optional[date]:
    if job.period_end:
        return job.period_end
    if job.actual_completion:
        return job.actual_completion
    if job.created_at:
        if isinstance(job.created_at, datetime):
            return job.created_at.date()
        return job.created_at
    return None


def _client_invoice_bounds(
    db: Session,
) -> Dict[int, Tuple[Optional[date], Optional[date]]]:
    jobs = db.query(Job).filter(Job.client_id.isnot(None)).all()
    bounds: Dict[int, Tuple[Optional[date], Optional[date]]] = {}
    for job in jobs:
        if not _is_billable_job(job):
            continue
        inv = _invoice_date(job)
        if not inv or not job.client_id:
            continue
        cid = int(job.client_id)
        first, last = bounds.get(cid, (None, None))
        if first is None or inv < first:
            first = inv
        if last is None or inv > last:
            last = inv
        bounds[cid] = (first, last)
    return bounds


def _count_stock_clients(db: Session) -> int:
    return int(
        db.query(func.count(Client.id))
        .filter(Client.overall_status.in_(list(CLIENT_STATUSES)))
        .scalar()
        or 0
    )


def _count_stock_lost(db: Session) -> int:
    return int(
        db.query(func.count(Client.id))
        .filter(Client.overall_status.in_(list(LOST_STATUSES)))
        .scalar()
        or 0
    )


def _count_new_by_first_invoice_year(db: Session, year: int) -> int:
    bounds = _client_invoice_bounds(db)
    d0, d1 = _year_date_bounds(year)
    return sum(
        1
        for first, _last in bounds.values()
        if first is not None and d0 <= first <= d1
    )


def _count_lost_by_last_invoice_year(db: Session, year: int) -> int:
    inactive_ids = {
        int(r[0])
        for r in db.query(Client.id)
        .filter(Client.overall_status.in_(list(LOST_STATUSES)))
        .all()
    }
    if not inactive_ids:
        return 0
    bounds = _client_invoice_bounds(db)
    d0, d1 = _year_date_bounds(year)
    return sum(
        1
        for cid in inactive_ids
        if (bounds.get(cid, (None, None))[1] is not None
            and d0 <= bounds[cid][1] <= d1)
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    period: str = Query("overall"),
):
    today = date.today()
    soon = today + timedelta(days=30)
    mode, year = _parse_period(period, today)
    years = _available_years(today, db)

    if mode == "year" and year is not None:
        period_key = str(year)
        year_label = str(year)
        start, end = _year_datetime_bounds(year)
    else:
        mode = "overall"
        year = None
        period_key = "overall"
        year_label = "Overall"
        start, end = None, None

    # —— Headcount tiles ——
    # Order: Groups | Clients | New clients | Lost | Prospects
    #
    # Overall: Clients = New − Lost (New = stock clients + stock lost)
    # Year:    New / Lost by first / last invoice; Clients = current book
    group_client_ids = client_ids_for_period_and_statuses(
        db, statuses=GROUP_STATUSES, start=start, end=end
    )
    total_groups = int(count_groups(db, client_ids=group_client_ids))

    stock_clients = _count_stock_clients(db)
    stock_lost = _count_stock_lost(db)

    if mode == "overall":
        total_clients = stock_clients
        total_lost = stock_lost
        total_new_clients = total_clients + total_lost
    else:
        assert year is not None
        total_clients = stock_clients
        total_new_clients = int(_count_new_by_first_invoice_year(db, year))
        total_lost = int(_count_lost_by_last_invoice_year(db, year))

    total_prospects = int(
        _count_clients_by_statuses(db, PROSPECT_STATUSES, start, end)
    )

    # —— Workload (current live view) ——
    people_count = int(db.query(func.count(Person.id)).scalar() or 0)
    jobs_count = int(db.query(func.count(Job.id)).scalar() or 0)
    individual_clients = int(
        db.query(func.count(Person.id))
        .filter(Person.is_individual_client.is_(True))
        .scalar()
        or 0
    )
    company_clients = int(
        db.query(func.count(Client.id))
        .filter(
            Client.overall_status == "Active",
            Client.client_type != "Individual",
        )
        .scalar()
        or 0
    )
    active_clients = int(
        db.query(func.count(Client.id))
        .filter(Client.overall_status == "Active")
        .scalar()
        or 0
    )

    open_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
        .all()
    )
    live_jobs = [
        j
        for j in open_jobs
        if not j.client or (j.client.overall_status or "") != "Inactive"
    ]
    total_fees = sum(j.fee or 0 for j in live_jobs)
    overdue_jobs = [
        j
        for j in live_jobs
        if j.statutory_due_date and j.statutory_due_date < today
    ]
    due_soon = [
        j
        for j in live_jobs
        if j.statutory_due_date and today <= j.statutory_due_date <= soon
    ]
    accounts_open = len([j for j in live_jobs if j.type == "Accounts"])
    cs_open = len(
        [j for j in live_jobs if j.type == "Confirmation Statement"]
    )

    recent_clients = (
        db.query(Client)
        .filter(
            (Client.overall_status.is_(None))
            | (Client.overall_status != "Inactive")
        )
        .order_by(Client.id.desc())
        .limit(8)
        .all()
    )
    upcoming = sorted(
        [j for j in live_jobs if j.statutory_due_date],
        key=lambda j: j.statutory_due_date,
    )[:10]

    return render(
        request,
        "dashboard.html",
        {
            "period": period_key,
            "year_label": year_label,
            "years": years,
            "earliest_year": EARLIEST_INVOICE_YEAR,
            "total_groups": total_groups,
            "total_clients": total_clients,
            "total_new_clients": total_new_clients,
            "total_prospects": total_prospects,
            "total_lost": total_lost,
            "active_clients": active_clients,
            "company_clients": company_clients,
            "individual_clients": individual_clients,
            "people_count": people_count,
            "jobs_count": jobs_count,
            "total_fees": round(total_fees, 2),
            "overdue_count": len(overdue_jobs),
            "due_soon_count": len(due_soon),
            "accounts_open": accounts_open,
            "cs_open": cs_open,
            "recent_clients": recent_clients,
            "upcoming_jobs": upcoming,
            "today": today,
        },
    )
