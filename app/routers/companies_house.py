from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client
from app.services.companies_house import (
    get_api_key,
    has_api_key,
    save_api_key,
    fetch_company_profile,
    summarize_profile_dates,
    normalize_company_number,
    test_api_key,
    API_KEY_FILE,
)
from app.services.ch_jobs import create_jobs_for_client_from_ch, create_jobs_for_clients_from_ch
from app.services.company_numbers import pad_all_client_company_numbers
from app.services.job_profiles import default_profiles, drafts_from_companies_house
from app.templating import render

router = APIRouter(tags=["companies-house"])


def _ch_clients(db: Session) -> list[Client]:
    clients = (
        db.query(Client)
        .filter(Client.company_number.isnot(None), Client.company_number != "")
        .order_by(Client.company_name)
        .all()
    )
    return [
        c
        for c in clients
        if not (c.company_number or "").upper().startswith("IND-")
        and (c.client_type or "").lower() != "individual"
    ]


# Use /companies-house/... (not /jobs/...) so we never clash with /jobs/{job_id}
CH_JOBS_URL = "/companies-house/jobs"


@router.get("/companies-house/jobs", response_class=HTMLResponse)
async def ch_jobs_page(request: Request, db: Session = Depends(get_db)):
    profiles = default_profiles()
    clients = _ch_clients(db)
    return render(
        request,
        "jobs/from_ch.html",
        {
            "has_api_key": has_api_key(),
            "api_key_set": bool(get_api_key()),
            "profiles": profiles,
            "clients": clients,
            "result": None,
            "preview": None,
            "key_message": None,
        },
    )


@router.post("/companies-house/jobs/save-key", response_class=HTMLResponse)
async def ch_save_key(
    request: Request,
    api_key: str = Form(...),
    db: Session = Depends(get_db),
):
    err = save_api_key(api_key)
    profiles = default_profiles()
    clients = _ch_clients(db)
    if err:
        return render(
            request,
            "jobs/from_ch.html",
            {
                "has_api_key": has_api_key(),
                "api_key_set": bool(get_api_key()),
                "profiles": profiles,
                "clients": clients,
                "result": None,
                "preview": None,
                "key_message": {"ok": False, "text": err},
            },
        )

    # Quick live test against a known company number
    test = test_api_key()
    if test.ok:
        msg = {
            "ok": True,
            "text": (
                "API key saved and tested successfully "
                f"(Companies House returned: {test.profile.get('company_name', 'OK')})."
            ),
        }
    else:
        msg = {
            "ok": False,
            "text": (
                f"Key was saved but Companies House rejected a test call: {test.error} "
                "Create a REST API key (not Streaming/Web) and paste only the key string."
            ),
        }
    return render(
        request,
        "jobs/from_ch.html",
        {
            "has_api_key": has_api_key(),
            "api_key_set": bool(get_api_key()),
            "profiles": profiles,
            "clients": clients,
            "result": None,
            "preview": None,
            "key_message": msg,
        },
    )


@router.post("/companies-house/jobs/preview", response_class=HTMLResponse)
async def ch_preview(
    request: Request,
    company_number: str = Form(""),
    client_id: str = Form(""),
    db: Session = Depends(get_db),
):
    profiles = default_profiles()
    clients = _ch_clients(db)
    cn = normalize_company_number(company_number)
    if client_id and not cn:
        client = db.query(Client).filter(Client.id == int(client_id)).first()
        if client:
            cn = normalize_company_number(client.company_number or "")

    if not cn:
        return render(
            request,
            "jobs/from_ch.html",
            {
                "has_api_key": has_api_key(),
                "api_key_set": bool(get_api_key()),
                "profiles": profiles,
                "clients": clients,
                "result": None,
                "preview": {"error": "Enter a company number or choose a client."},
            },
        )

    fetch = fetch_company_profile(cn)
    if not fetch.ok:
        return render(
            request,
            "jobs/from_ch.html",
            {
                "has_api_key": has_api_key(),
                "api_key_set": bool(get_api_key()),
                "profiles": profiles,
                "clients": clients,
                "result": None,
                "preview": {"error": fetch.error, "company_number": cn},
            },
        )

    dates = summarize_profile_dates(fetch.profile)
    drafts = drafts_from_companies_house(
        fetch.profile, company_name=dates.get("company_name") or ""
    )
    return render(
        request,
        "jobs/from_ch.html",
        {
            "has_api_key": has_api_key(),
            "api_key_set": bool(get_api_key()),
            "profiles": profiles,
            "clients": clients,
            "result": None,
            "preview": {
                "company_number": cn,
                "dates": dates,
                "drafts": drafts,
            },
        },
    )


@router.post("/companies-house/jobs/create", response_class=HTMLResponse)
async def ch_create_jobs(
    request: Request,
    scope: str = Form("selected"),
    client_ids: list[str] = Form(default=[]),
    profiles: list[str] = Form(default=[]),
    accounts_fee: str = Form("0"),
    cs_fee: str = Form("0"),
    db: Session = Depends(get_db),
):
    profile_list = default_profiles()
    all_clients = _ch_clients(db)

    try:
        acc_fee = float((accounts_fee or "0").replace("£", "").replace(",", ""))
    except ValueError:
        acc_fee = 0.0
    try:
        conf_fee = float((cs_fee or "0").replace("£", "").replace(",", ""))
    except ValueError:
        conf_fee = 0.0

    selected_profiles = profiles or ["accounts", "confirmation_statement"]

    if scope == "all":
        targets = all_clients
    else:
        ids = []
        for v in client_ids or []:
            try:
                ids.append(int(v))
            except ValueError:
                pass
        targets = [c for c in all_clients if c.id in ids]

    if not targets:
        return render(
            request,
            "jobs/from_ch.html",
            {
                "has_api_key": has_api_key(),
                "api_key_set": bool(get_api_key()),
                "profiles": profile_list,
                "clients": all_clients,
                "result": {
                    "error": "Select at least one client, or choose all clients."
                },
                "preview": None,
            },
        )

    if not has_api_key():
        return render(
            request,
            "jobs/from_ch.html",
            {
                "has_api_key": False,
                "api_key_set": False,
                "profiles": profile_list,
                "clients": all_clients,
                "result": {
                    "error": "Set your Companies House API key first (form above)."
                },
                "preview": None,
            },
        )

    results = create_jobs_for_clients_from_ch(
        db,
        targets,
        profile_keys=selected_profiles,
        accounts_fee=acc_fee,
        cs_fee=conf_fee,
        skip_duplicates=True,
    )
    total_created = sum(r.created for r in results)
    total_skipped = sum(r.skipped for r in results)
    total_errors = sum(1 for r in results if r.errors)

    return render(
        request,
        "jobs/from_ch.html",
        {
            "has_api_key": has_api_key(),
            "api_key_set": bool(get_api_key()),
            "profiles": profile_list,
            "clients": all_clients,
            "result": {
                "total_created": total_created,
                "total_skipped": total_skipped,
                "total_errors": total_errors,
                "rows": results,
            },
            "preview": None,
        },
    )


@router.post("/companies-house/jobs/pad-numbers", response_class=HTMLResponse)
async def pad_company_numbers(request: Request, db: Session = Depends(get_db)):
    """Zero-pad short company numbers (8056337 → 08056337) then show CH jobs page."""
    updated, unchanged, errors = pad_all_client_company_numbers(db)
    profiles = default_profiles()
    clients = _ch_clients(db)
    return render(
        request,
        "jobs/from_ch.html",
        {
            "has_api_key": has_api_key(),
            "api_key_set": bool(get_api_key()),
            "profiles": profiles,
            "clients": clients,
            "result": None,
            "preview": None,
            "key_message": {
                "ok": True if not errors else False,
                "text": (
                    f"Padded {updated} company number(s); {unchanged} unchanged."
                    + (
                        f" Notes: {'; '.join(errors[:5])}"
                        if errors
                        else " You can re-run Create jobs for clients that failed earlier."
                    )
                ),
            },
        },
    )


@router.post("/clients/{client_id}/jobs-from-ch")
async def client_jobs_from_ch(
    client_id: int,
    accounts: str = Form(""),
    confirmation_statement: str = Form(""),
    accounts_fee: str = Form("0"),
    cs_fee: str = Form("0"),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    keys = []
    if accounts == "yes":
        keys.append("accounts")
    if confirmation_statement == "yes":
        keys.append("confirmation_statement")
    if not keys:
        keys = ["accounts", "confirmation_statement"]

    try:
        acc_fee = float((accounts_fee or "0").replace("£", "").replace(",", ""))
    except ValueError:
        acc_fee = 0.0
    try:
        conf_fee = float((cs_fee or "0").replace("£", "").replace(",", ""))
    except ValueError:
        conf_fee = 0.0

    create_jobs_for_client_from_ch(
        db,
        client,
        profile_keys=keys,
        accounts_fee=acc_fee,
        cs_fee=conf_fee,
    )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
