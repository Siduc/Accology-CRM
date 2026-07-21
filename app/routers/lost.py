"""Lost clients & jobs — paths under /lost/... so they never clash with /clients/{id}."""

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Client, Job
from app.services.lost_analysis import analyse_lost_jobs, apply_lost_filters
from app.templating import render

router = APIRouter(prefix="/lost", tags=["lost"])


def _client_search(query, q: str):
    if not q:
        return query
    like = f"%{q}%"
    return query.filter(
        (Client.company_name.ilike(like))
        | (Client.company_number.ilike(like))
        | (Client.email.ilike(like))
        | (Client.contact_name.ilike(like))
    )


@router.get("/clients", response_class=HTMLResponse)
async def lost_clients(
    request: Request,
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    query = db.query(Client).filter(Client.overall_status == "Inactive")
    query = _client_search(query, q)
    clients = query.order_by(Client.company_name).all()
    return render(
        request,
        "clients/list.html",
        {
            "clients": clients,
            "q": q,
            "status": "Inactive",
            "statuses": ["Inactive"],
            "page_title": "Lost clients",
            "view": "lost",
            "all_statuses": ["Active", "Inactive", "Prospect", "Former"],
            "lost_count": len(clients),
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def lost_jobs(
    request: Request,
    job_type: str = Query(""),
    year: str = Query(""),
    fee_band: str = Query(""),
    billing: str = Query(""),
    q: str = Query(""),
    late: str = Query(""),
    include_completed: str = Query("yes"),
    db: Session = Depends(get_db),
):
    """Jobs for Inactive clients + analysis filters."""
    from datetime import date

    all_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .join(Client, Job.client_id == Client.id)
        .filter(Client.overall_status == "Inactive")
        .order_by(Job.period_end.desc())
        .all()
    )

    include_comp = include_completed != "no"
    filtered = apply_lost_filters(
        all_jobs,
        job_type=job_type,
        year=year,
        fee_band_key=fee_band,
        billing=billing,
        q=q,
        late=late,
        include_completed=include_comp,
    )
    stats = analyse_lost_jobs(filtered)

    # Filter option lists from full lost set
    types = sorted({j.type for j in all_jobs if j.type})
    years = sorted(
        {j.period_end.year for j in all_jobs if j.period_end}, reverse=True
    )
    billings = sorted({j.billing_status for j in all_jobs if j.billing_status})

    return render(
        request,
        "jobs/lost.html",
        {
            "jobs": filtered,
            "stats": stats,
            "job_type": job_type,
            "year": year,
            "fee_band": fee_band,
            "billing": billing,
            "q": q,
            "late": late,
            "include_completed": include_completed,
            "types": types,
            "years": years,
            "billings": billings,
            "today": date.today(),
        },
    )
