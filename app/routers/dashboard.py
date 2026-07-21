from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models import Client, Person, Job
from app.templating import render

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    soon = today + timedelta(days=30)

    total_clients = db.query(Client).count()
    active_clients = (
        db.query(Client).filter(Client.overall_status == "Active").count()
    )
    company_clients = (
        db.query(Client)
        .filter(
            Client.overall_status == "Active",
            Client.client_type != "Individual",
        )
        .count()
    )
    individual_clients = (
        db.query(Person).filter(Person.is_individual_client.is_(True)).count()
    )
    people_count = db.query(Person).count()
    jobs_count = db.query(Job).count()

    open_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
        .all()
    )
    # Live workload only (exclude jobs for Inactive / lost clients)
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
    cs_open = len([j for j in live_jobs if j.type == "Confirmation Statement"])
    prospects_count = (
        db.query(Client).filter(Client.overall_status == "Prospect").count()
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
            "total_clients": total_clients,
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
            "prospects_count": prospects_count,
            "pipeline_value": 0,
            "overdue_invoices": 0,
            "outstanding_ar": 0,
            "recent_clients": recent_clients,
            "upcoming_jobs": upcoming,
            "today": today,
        },
    )
