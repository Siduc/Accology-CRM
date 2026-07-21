from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ServiceFee
from app.services.fees import DEFAULT_SERVICES, seed_default_fees
from app.templating import render

router = APIRouter(prefix="/services", tags=["services"])


@router.get("/fees", response_class=HTMLResponse)
async def list_fees(
    request: Request,
    service: str = Query(""),
    applied: str = Query(""),
    db: Session = Depends(get_db),
):
    seed_default_fees(db)
    q = db.query(ServiceFee)
    if service:
        q = q.filter(ServiceFee.service_code == service)
    fees = q.order_by(ServiceFee.service_code, ServiceFee.year).all()
    services = (
        db.query(ServiceFee.service_code)
        .distinct()
        .order_by(ServiceFee.service_code)
        .all()
    )
    service_codes = [s[0] for s in services] or list(DEFAULT_SERVICES)
    message = None
    if applied != "":
        message = f"Updated fees on {applied} open job(s) that had £0."
    return render(
        request,
        "services/fees.html",
        {
            "fees": fees,
            "service": service,
            "service_codes": service_codes,
            "default_services": DEFAULT_SERVICES,
            "message": message,
        },
    )


@router.post("/fees", response_class=HTMLResponse)
async def save_fee(
    request: Request,
    service_code: str = Form(...),
    service_name: str = Form(""),
    year: str = Form(...),
    fee: str = Form(...),
    notes: str = Form(""),
    fee_id: str = Form(""),
    db: Session = Depends(get_db),
):
    code = service_code.strip()
    name = (service_name or code).strip()
    try:
        year_i = int(year)
    except ValueError:
        year_i = datetime.utcnow().year
    try:
        fee_f = float(str(fee).replace("£", "").replace(",", ""))
    except ValueError:
        fee_f = 0.0

    if fee_id:
        row = db.query(ServiceFee).filter(ServiceFee.id == int(fee_id)).first()
        if row:
            row.service_code = code
            row.service_name = name
            row.year = year_i
            row.fee = fee_f
            row.notes = notes or None
            row.updated_at = datetime.utcnow()
    else:
        existing = (
            db.query(ServiceFee)
            .filter(ServiceFee.service_code == code, ServiceFee.year == year_i)
            .first()
        )
        if existing:
            existing.fee = fee_f
            existing.service_name = name
            existing.notes = notes or existing.notes
            existing.updated_at = datetime.utcnow()
        else:
            db.add(
                ServiceFee(
                    service_code=code,
                    service_name=name,
                    year=year_i,
                    fee=fee_f,
                    notes=notes or None,
                )
            )
    db.commit()
    return RedirectResponse("/services/fees", status_code=303)


@router.post("/fees/{fee_id}/delete")
async def delete_fee(fee_id: int, db: Session = Depends(get_db)):
    row = db.query(ServiceFee).filter(ServiceFee.id == fee_id).first()
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse("/services/fees", status_code=303)


@router.post("/fees/seed-defaults")
async def seed_defaults(db: Session = Depends(get_db)):
    seed_default_fees(db)
    return RedirectResponse("/services/fees", status_code=303)


@router.post("/fees/apply-to-zero-jobs")
async def apply_fees_to_zero_jobs(db: Session = Depends(get_db)):
    """Set suggested fee on open jobs that currently have fee 0."""
    from app.models import Job
    from app.services.fees import get_suggested_fee

    jobs = (
        db.query(Job)
        .filter(
            Job.status.notin_(["Completed", "Cancelled"]),
            (Job.fee.is_(None)) | (Job.fee == 0),
        )
        .all()
    )
    updated = 0
    for job in jobs:
        suggested = get_suggested_fee(db, job.type or "", job.period_end)
        if suggested is not None and suggested > 0:
            job.fee = suggested
            updated += 1
    db.commit()
    return RedirectResponse(f"/services/fees?applied={updated}", status_code=303)
