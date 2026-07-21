from datetime import datetime, date

from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models import Client, Job, Person
from app.models.person import person_clients
from app.services.import_csv import normalize_company_number
from app.services.company_numbers import normalize_company_number as norm_cn
from app.services.prior_import import client_fee_history
from app.templating import render

router = APIRouter(prefix="/clients", tags=["clients"])

STATUSES = ["Active", "Inactive", "Prospect", "Former"]
# Statuses that stay on the main (live) clients list
LIVE_STATUSES = ["Active", "Prospect", "Former"]
LOST_STATUSES = ["Inactive"]
CLIENT_TYPES = [
    "Limited Company",
    "LLP",
    "Sole Trader",
    "Partnership",
    "PLC",
    "Individual",
    "Other",
]


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


@router.get("", response_class=HTMLResponse)
async def list_clients(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    db: Session = Depends(get_db),
):
    """Live clients — excludes Inactive (those appear under Lost Clients)."""
    query = db.query(Client)
    query = _client_search(query, q)
    if status:
        if status == "Inactive":
            return RedirectResponse("/lost/clients", status_code=303)
        query = query.filter(Client.overall_status == status)
    else:
        # Default: everything except lost/inactive
        query = query.filter(
            (Client.overall_status.is_(None))
            | (Client.overall_status != "Inactive")
        )
    clients = query.order_by(Client.company_name).all()
    return render(
        request,
        "clients/list.html",
        {
            "clients": clients,
            "q": q,
            "status": status,
            "statuses": LIVE_STATUSES,
            "page_title": "Clients",
            "view": "live",
            "all_statuses": STATUSES,
            "lost_count": db.query(Client)
            .filter(Client.overall_status == "Inactive")
            .count(),
        },
    )


@router.get("/lost", response_class=HTMLResponse)
async def list_lost_clients_legacy(
    request: Request,
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    """Legacy URL — redirect to /lost/clients (avoids clash with {client_id})."""
    return RedirectResponse(
        "/lost/clients" + (f"?q={q}" if q else ""),
        status_code=303,
    )


@router.get("/new", response_class=HTMLResponse)
async def new_client_form(request: Request):
    return render(
        request,
        "clients/form.html",
        {
            "client": None,
            "statuses": STATUSES,
            "client_types": CLIENT_TYPES,
            "error": None,
        },
    )


@router.post("/new")
async def create_client(
    request: Request,
    company_name: str = Form(""),
    company_number: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    client_type: str = Form(""),
    overall_status: str = Form("Active"),
    vat_number: str = Form(""),
    utr: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    cn = normalize_company_number(company_number)
    if not cn:
        return render(
            request,
            "clients/form.html",
            {
                "client": None,
                "statuses": STATUSES,
                "client_types": CLIENT_TYPES,
                "error": "Company number is required.",
            },
            status_code=400,
        )
    existing = db.query(Client).filter(Client.company_number == cn).first()
    if existing:
        return render(
            request,
            "clients/form.html",
            {
                "client": None,
                "statuses": STATUSES,
                "client_types": CLIENT_TYPES,
                "error": f"Company number {cn} already exists (client #{existing.id}).",
            },
            status_code=400,
        )

    client = Client(
        company_name=company_name or None,
        company_number=cn,
        contact_name=contact_name or None,
        email=email or None,
        phone=phone or None,
        address_line1=address_line1 or None,
        address_line2=address_line2 or None,
        town=town or None,
        postcode=postcode or None,
        client_type=client_type or None,
        overall_status=overall_status or "Active",
        vat_number=vat_number or None,
        utr=utr or None,
        notes=notes or None,
        source="manual",
        created_at=datetime.utcnow(),
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@router.get("/{client_id:int}", response_class=HTMLResponse)
async def client_detail(
    client_id: int,
    request: Request,
    saved: str = Query(""),
    contact_added: str = Query(""),
    contact_linked: str = Query(""),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)
    jobs = (
        db.query(Job)
        .filter(Job.client_id == client_id)
        .order_by(Job.statutory_due_date)
        .all()
    )
    people = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .join(person_clients, person_clients.c.person_id == Person.id)
        .filter(person_clients.c.client_id == client_id)
        .order_by(Person.full_name)
        .all()
    )
    try:
        fee_history = client_fee_history(db, client_id)
    except Exception:
        fee_history = {
            "rows": [],
            "year_totals": [],
            "average_per_year": 0,
            "historical_average": 0,
            "current_year": date.today().year,
            "current_year_fee": 0,
            "variance": 0,
            "variance_pct": None,
            "job_count": 0,
            "chart_years": [],
            "chart_datasets": [],
            "chart_average": 0,
        }
    linked_ids = {p.id for p in people}
    try:
        other_people = [
            p
            for p in db.query(Person).order_by(Person.full_name).all()
            if p.id not in linked_ids
        ][:200]
    except Exception:
        other_people = []
    message = None
    if saved:
        message = "Details saved."
    elif contact_added:
        message = "Contact added to people list and linked to this client."
    elif contact_linked:
        message = "Existing person linked to this client."
    # Pre-serialize chart JSON so template never fails on tojson edge cases
    import json

    chart_json = json.dumps(
        {
            "years": fee_history.get("chart_years") or [],
            "datasets": fee_history.get("chart_datasets") or [],
            "average": fee_history.get("chart_average") or 0,
        }
    )
    return render(
        request,
        "clients/detail.html",
        {
            "client": client,
            "jobs": jobs or [],
            "people": people or [],
            "other_people": other_people,
            "statuses": STATUSES,
            "client_types": CLIENT_TYPES,
            "fee_history": fee_history,
            "chart_json": chart_json,
            "message": message,
            "today": date.today(),
        },
    )


@router.post("/{client_id:int}/details")
async def update_client_details(
    client_id: int,
    request: Request,
    company_name: str = Form(""),
    company_number: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    client_type: str = Form(""),
    vat_number: str = Form(""),
    utr: str = Form(""),
    paye_reference: str = Form(""),
    accounts_office_reference: str = Form(""),
    gov_gateway_username: str = Form(""),
    gov_gateway_password: str = Form(""),
    accounts_software_id: str = Form(""),
    accounts_software_password: str = Form(""),
    ch_authentication_code: str = Form(""),
    ch_personal_code: str = Form(""),
    notes: str = Form(""),
    primary_person_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save editable details from the client detail screen."""
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    cn_raw = (company_number or "").strip()
    if cn_raw:
        cn = norm_cn(cn_raw) or cn_raw
        dup = (
            db.query(Client)
            .filter(Client.company_number == cn, Client.id != client_id)
            .first()
        )
        if not dup:
            client.company_number = cn

    client.company_name = company_name or client.company_name
    client.contact_name = contact_name or None
    client.email = email or None
    client.phone = phone or None
    client.address_line1 = address_line1 or None
    client.address_line2 = address_line2 or None
    client.town = town or None
    client.postcode = postcode or None
    client.client_type = client_type or None
    client.vat_number = vat_number or None
    client.utr = utr or None
    client.paye_reference = paye_reference or None
    client.accounts_office_reference = accounts_office_reference or None
    client.gov_gateway_username = gov_gateway_username or None
    client.gov_gateway_password = gov_gateway_password or None
    client.accounts_software_id = accounts_software_id or None
    client.accounts_software_password = accounts_software_password or None
    client.ch_authentication_code = ch_authentication_code or None
    client.ch_personal_code = ch_personal_code or None
    client.notes = notes or None
    client.updated_at = datetime.utcnow()

    # Set primary contact from people list
    if primary_person_id:
        try:
            pid = int(primary_person_id)
        except ValueError:
            pid = None
        if pid:
            person = db.query(Person).filter(Person.id == pid).first()
            if person:
                if client not in person.clients:
                    person.clients.append(client)
                for p in db.query(Person).join(
                    person_clients, person_clients.c.person_id == Person.id
                ).filter(person_clients.c.client_id == client_id).all():
                    p.is_primary = p.id == pid
                client.contact_name = person.full_name
                if person.email:
                    client.email = person.email
                if person.phone:
                    client.phone = person.phone

    db.commit()
    return RedirectResponse(f"/clients/{client_id}?saved=1", status_code=303)


@router.post("/{client_id:int}/contacts/add")
async def add_client_contact(
    client_id: int,
    full_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    role: str = Form(""),
    set_primary: str = Form(""),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    person = Person(
        full_name=full_name.strip(),
        email=email or None,
        phone=phone or None,
        role=role or "Contact",
        person_status="Contact",
        is_primary=set_primary == "yes",
    )
    person.clients.append(client)
    db.add(person)
    db.flush()

    if set_primary == "yes":
        for p in (
            db.query(Person)
            .join(person_clients, person_clients.c.person_id == Person.id)
            .filter(person_clients.c.client_id == client_id)
            .all()
        ):
            p.is_primary = p.id == person.id
        client.contact_name = person.full_name
        if person.email:
            client.email = person.email
        if person.phone:
            client.phone = person.phone
        client.updated_at = datetime.utcnow()

    db.commit()
    return RedirectResponse(f"/clients/{client_id}?contact_added=1", status_code=303)


@router.post("/{client_id:int}/contacts/link")
async def link_existing_contact(
    client_id: int,
    person_id: int = Form(...),
    set_primary: str = Form(""),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    person = db.query(Person).filter(Person.id == person_id).first()
    if not client or not person:
        return RedirectResponse("/clients", status_code=303)
    if client not in person.clients:
        person.clients.append(client)
    if set_primary == "yes":
        for p in (
            db.query(Person)
            .join(person_clients, person_clients.c.person_id == Person.id)
            .filter(person_clients.c.client_id == client_id)
            .all()
        ):
            p.is_primary = p.id == person.id
        person.is_primary = True
        client.contact_name = person.full_name
        if person.email:
            client.email = person.email
        if person.phone:
            client.phone = person.phone
    db.commit()
    return RedirectResponse(f"/clients/{client_id}?contact_linked=1", status_code=303)


@router.post("/{client_id:int}/status")
async def update_client_status(
    client_id: int,
    overall_status: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    """Quick status change from list or detail (no full edit form)."""
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    status = (overall_status or "Active").strip()
    if status not in STATUSES:
        status = "Active"
    client.overall_status = status
    client.updated_at = datetime.utcnow()
    db.commit()

    # Where to return after change
    if next == "lost":
        return RedirectResponse("/lost/clients", status_code=303)
    if next == "list":
        if status == "Inactive":
            return RedirectResponse("/lost/clients", status_code=303)
        return RedirectResponse("/clients", status_code=303)
    # default: stay on client detail
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.get("/{client_id:int}/edit", response_class=HTMLResponse)
async def edit_client_form(
    client_id: int, request: Request, db: Session = Depends(get_db)
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)
    return render(
        request,
        "clients/form.html",
        {
            "client": client,
            "statuses": STATUSES,
            "client_types": CLIENT_TYPES,
            "error": None,
        },
    )


@router.post("/{client_id:int}/edit")
async def update_client(
    client_id: int,
    request: Request,
    company_name: str = Form(""),
    company_number: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    client_type: str = Form(""),
    overall_status: str = Form("Active"),
    vat_number: str = Form(""),
    utr: str = Form(""),
    paye_reference: str = Form(""),
    accounts_office_reference: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    cn = normalize_company_number(company_number)
    dup = (
        db.query(Client)
        .filter(Client.company_number == cn, Client.id != client_id)
        .first()
    )
    if dup:
        return render(
            request,
            "clients/form.html",
            {
                "client": client,
                "statuses": STATUSES,
                "client_types": CLIENT_TYPES,
                "error": f"Company number {cn} already used by client #{dup.id}.",
            },
            status_code=400,
        )

    client.company_name = company_name or None
    client.company_number = cn
    client.contact_name = contact_name or None
    client.email = email or None
    client.phone = phone or None
    client.address_line1 = address_line1 or None
    client.address_line2 = address_line2 or None
    client.town = town or None
    client.postcode = postcode or None
    client.client_type = client_type or None
    client.overall_status = overall_status or "Active"
    client.vat_number = vat_number or None
    client.utr = utr or None
    client.paye_reference = paye_reference or None
    client.accounts_office_reference = accounts_office_reference or None
    client.notes = notes or None
    # Also accept new fields if present on full edit form later
    form = await request.form()
    if "gov_gateway_username" in form:
        client.gov_gateway_username = form.get("gov_gateway_username") or None
        client.gov_gateway_password = form.get("gov_gateway_password") or None
        client.accounts_software_id = form.get("accounts_software_id") or None
        client.accounts_software_password = form.get("accounts_software_password") or None
        client.ch_authentication_code = form.get("ch_authentication_code") or None
        client.ch_personal_code = form.get("ch_personal_code") or None
    client.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
