from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ServiceFee
from app.models.sales import Service, ServicePrice
from app.services.fees import DEFAULT_SERVICES, seed_default_fees
from app.services.sales_ledger import seed_services
from app.templating import render

router = APIRouter(prefix="/services", tags=["services"])


@router.get("", response_class=HTMLResponse)
async def services_catalogue(request: Request, db: Session = Depends(get_db)):
    """Services Ledger — master catalogue."""
    seed_services(db)
    services = db.query(Service).order_by(Service.category, Service.name).all()
    return render(
        request,
        "services/catalogue.html",
        {"services": services},
    )


@router.post("/catalogue/add", response_class=HTMLResponse)
async def services_add(
    request: Request,
    code: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    default_fee: str = Form("0"),
    default_vat_rate: str = Form("0"),
    category: str = Form("compliance"),
    unit: str = Form("job"),
    is_sellable: str = Form("yes"),
    db: Session = Depends(get_db),
):
    code_c = (code or "").strip().upper().replace(" ", "_")
    if not code_c or db.query(Service).filter(Service.code == code_c).first():
        return RedirectResponse("/services?error=code", status_code=303)
    try:
        fee = float((default_fee or "0").replace("£", "").replace(",", ""))
    except ValueError:
        fee = 0.0
    try:
        vat = float(default_vat_rate or 0)
    except ValueError:
        vat = 0.0
    db.add(
        Service(
            code=code_c,
            name=(name or code_c).strip(),
            description=description or None,
            default_fee=fee,
            default_vat_rate=vat,
            category=category or "compliance",
            unit=unit or "job",
            is_active=True,
            is_sellable_to_clients=is_sellable == "yes",
        )
    )
    db.commit()
    return RedirectResponse("/services", status_code=303)


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
        suggested = get_suggested_fee(
            db,
            job.type or "",
            job.period_end,
            client_id=job.client_id,
        )
        if suggested is not None and suggested > 0:
            job.fee = suggested
            updated += 1
    db.commit()
    return RedirectResponse(f"/services/fees?applied={updated}", status_code=303)
