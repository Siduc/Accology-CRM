"""Public PAYE onboarding form (no login)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.client import Client
from app.models.prospecting import Prospect
from app.services.paye_onboarding import apply_onboarding, parse_token
from app.services.prospecting import log_activity
from app.templating import render

router = APIRouter(tags=["payroll-onboarding"])


def _ctx(request: Request, client: Client, token: str, error: str = "", saved: bool = False):
    return {
        "client": client,
        "token": token,
        "error": error,
        "saved": saved,
    }


@router.get("/payroll/paye/{token}", response_class=HTMLResponse)
async def paye_form(token: str, request: Request, db: Session = Depends(get_db)):
    cid, pid, err = parse_token(token)
    if err or not cid:
        return render(request, "website/paye_form.html", {"error": err or "Invalid link.", "client": None})
    client = db.query(Client).filter(Client.id == cid).first()
    if not client:
        return render(request, "website/paye_form.html", {"error": "Client not found.", "client": None})
    return render(request, "website/paye_form.html", _ctx(request, client, token))


@router.post("/payroll/paye/{token}", response_class=HTMLResponse)
async def paye_form_post(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    director_name: str = Form(""),
    ni_number: str = Form(""),
    date_of_birth: str = Form(""),
    home_address: str = Form(""),
    trading_name: str = Form(""),
    trading_address: str = Form(""),
    nature_of_business: str = Form(""),
    first_pay_date: str = Form(""),
    staff_count: str = Form(""),
    already_paye: str = Form("no"),
    existing_paye_ref: str = Form(""),
    existing_ao_ref: str = Form(""),
    gg_username: str = Form(""),
):
    cid, pid, err = parse_token(token)
    if err or not cid:
        return render(request, "website/paye_form.html", {"error": err or "Invalid link.", "client": None})
    client = db.query(Client).filter(Client.id == cid).first()
    if not client:
        return render(request, "website/paye_form.html", {"error": "Client not found.", "client": None})
    prospect = db.query(Prospect).filter(Prospect.id == pid).first() if pid else None
    data = {
        "contact_name": contact_name,
        "email": email,
        "phone": phone,
        "director_name": director_name or contact_name,
        "ni_number": ni_number,
        "date_of_birth": date_of_birth,
        "home_address": home_address,
        "trading_name": trading_name,
        "trading_address": trading_address,
        "nature_of_business": nature_of_business,
        "first_pay_date": first_pay_date,
        "staff_count": staff_count,
        "already_paye": already_paye,
        "existing_paye_ref": existing_paye_ref,
        "existing_ao_ref": existing_ao_ref,
        "gg_username": gg_username,
        "source": "web_form",
    }
    apply_onboarding(db, client, data, prospect=prospect)
    if prospect:
        log_activity(
            db,
            prospect.id,
            activity_type="email",
            subject="PAYE onboarding form submitted",
            body="Client completed the Accology Pays PAYE registration form.",
            direction="inbound",
            outcome="interested",
            campaign_id=2,
            commit=False,
        )
    db.commit()
    return RedirectResponse(f"/payroll/paye/{token}/thanks", status_code=303)


@router.get("/payroll/paye/{token}/thanks", response_class=HTMLResponse)
async def paye_thanks(token: str, request: Request, db: Session = Depends(get_db)):
    cid, _pid, err = parse_token(token)
    client = db.query(Client).filter(Client.id == cid).first() if cid else None
    return render(
        request,
        "website/paye_thanks.html",
        {"client": client, "error": err if not client else ""},
    )
