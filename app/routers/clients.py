from datetime import datetime, date
from typing import Optional
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from sqlalchemy.orm import joinedload

from app.database import get_db
from app.models import Client, Job, Person
from app.models.person import person_clients
from app.services.import_csv import normalize_company_number
from app.services.company_numbers import normalize_company_number as norm_cn
from app.services.individuals import filter_company_clients
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


def _parse_date(value: str):
    """Parse YYYY-MM-DD form date; empty → None."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse)
async def list_clients(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    book: str = Query(""),
    as_of: str = Query(""),
    cohort: str = Query(""),
    db: Session = Depends(get_db),
):
    """Live **companies** — excludes Inactive and individual/person shells.

    Individual clients (IND-… / client_type Individual) belong on People, not here.

    book=closing — dashboard Clients tile (joined, not lost) = New − Lost set.
    book=on&as_of= — on the book at a date (engagement/invoice stock).
    cohort=all|YYYY — New tile (ever joined, or joined in year).
    """
    query = db.query(Client)
    query = filter_company_clients(query)
    query = _client_search(query, q)
    page_title = "Companies"
    book_note = ""
    book_key = (book or "").strip().lower()
    cohort_key = (cohort or "").strip().lower()

    if book_key in ("closing", "close", "clients"):
        from app.routers.dashboard import _closing_client_ids

        ids = _closing_client_ids(db)
        if ids:
            query = query.filter(Client.id.in_(ids))
        else:
            query = query.filter(Client.id == -1)
        page_title = "Companies · closing stock"
        book_note = (
            f"{len(ids)} companies = New − Lost (joined via engagement/first invoice, "
            "not currently lost). Matches the dashboard Clients tile on Overall. "
            "Individual tax clients are under People."
        )
    elif book_key in ("on", "1", "true", "yes"):
        from app.routers.dashboard import _on_books_client_ids

        as_of_d = _parse_date(as_of) or date.today()
        ids = _on_books_client_ids(db, as_of_d)
        if ids:
            query = query.filter(Client.id.in_(ids))
        else:
            query = query.filter(Client.id == -1)
        page_title = f"Companies on books · {as_of_d.strftime('%d-%m-%Y')}"
        book_note = (
            f"Practice book at {as_of_d.strftime('%d-%m-%Y')} — "
            f"engagement/first invoice through before leave/disengagement. "
            "Companies only (no individual shells)."
        )
    elif cohort_key:
        from app.routers.dashboard import _new_client_ids

        year = int(cohort_key) if cohort_key.isdigit() else None
        ids = _new_client_ids(db, year)
        if ids:
            query = query.filter(Client.id.in_(ids))
        else:
            query = query.filter(Client.id == -1)
        page_title = (
            f"New companies · {year}" if year else "New companies · all time"
        )
        book_note = (
            f"{len(ids)} companies with a join date"
            + (f" in {year}" if year else " (engagement or first invoice).")
            + " Matches the dashboard New tile."
        )
    elif status:
        if status == "Inactive":
            return RedirectResponse("/lost/clients", status_code=303)
        query = query.filter(Client.overall_status == status)
        if status == "Prospect":
            page_title = "Prospect companies"
    else:
        # Default: companies except lost/inactive
        query = query.filter(
            (Client.overall_status.is_(None))
            | (Client.overall_status != "Inactive")
        )
    clients = query.order_by(Client.company_name).all()
    lost_q = filter_company_clients(
        db.query(Client).filter(Client.overall_status == "Inactive")
    )
    return render(
        request,
        "clients/list.html",
        {
            "clients": clients,
            "q": q,
            "status": status,
            "book": book,
            "as_of": as_of,
            "book_note": book_note,
            "statuses": LIVE_STATUSES,
            "page_title": page_title,
            "view": "live",
            "all_statuses": STATUSES,
            "lost_count": lost_q.count(),
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


def _new_client_form_ctx(
    *,
    default_status: str = "Active",
    error: Optional[str] = None,
    draft: Optional[dict] = None,
    ch_msg: str = "",
    ch_error: str = "",
    ch_search_q: str = "",
    ch_search_items: Optional[list] = None,
    create_jobs_from_ch: bool = True,
):
    from app.services.companies_house import has_api_key

    return {
        "client": None,
        "statuses": STATUSES,
        "client_types": CLIENT_TYPES,
        "default_status": default_status,
        "error": error,
        "draft": draft or {},
        "ch_key": has_api_key(),
        "ch_msg": ch_msg,
        "ch_error": ch_error,
        "ch_search_q": ch_search_q,
        "ch_search_items": ch_search_items or [],
        "create_jobs_from_ch": create_jobs_from_ch,
    }


@router.get("/new", response_class=HTMLResponse)
async def new_client_form(
    request: Request,
    status: str = Query(""),
    cn: str = Query(""),
    db: Session = Depends(get_db),
):
    # Allow /clients/new?status=Prospect for hub “New Prospect” action
    default_status = status if status in STATUSES else "Active"
    draft = {}
    ch_msg = ""
    ch_error = ""
    # Optional deep-link: /clients/new?cn=12345678 pulls profile immediately
    raw_cn = (cn or "").strip()
    if raw_cn:
        from app.services.companies_house import (
            client_fields_from_profile,
            fetch_company_profile,
            has_api_key,
        )

        if not has_api_key():
            ch_error = "Companies House API key not configured (Settings)."
        else:
            num = normalize_company_number(raw_cn) or raw_cn
            fetch = fetch_company_profile(num)
            if fetch.ok and fetch.profile:
                draft = client_fields_from_profile(fetch.profile)
                default_status = draft.get("overall_status") or default_status
                existing = (
                    db.query(Client)
                    .filter(Client.company_number == (draft.get("company_number") or num))
                    .first()
                )
                if existing:
                    ch_error = (
                        f"Already a client: #{existing.id} "
                        f"{existing.company_name or existing.company_number}. "
                        f"Open that record instead of creating a duplicate."
                    )
                else:
                    ch_msg = (
                        f"Pulled from Companies House: {draft.get('company_name') or num}"
                    )
            else:
                ch_error = fetch.error or "Companies House lookup failed."
                draft = {"company_number": num}

    return render(
        request,
        "clients/form.html",
        _new_client_form_ctx(
            default_status=default_status,
            draft=draft,
            ch_msg=ch_msg,
            ch_error=ch_error,
        ),
    )


@router.post("/new/save-ch-key", response_class=HTMLResponse)
async def new_client_save_ch_key(
    request: Request,
    api_key: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Save Companies House public REST API key (same store as Jobs → CH).
    Then return to New Client so lookup UI appears.
    """
    from app.services.companies_house import has_api_key, save_api_key, test_api_key

    err = save_api_key(api_key)
    if err:
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(ch_error=err),
            status_code=400,
        )
    test = test_api_key()
    if test.ok:
        ch_msg = (
            "Companies House API key saved and tested "
            f"({test.profile.get('company_name', 'OK')}). "
            "You can pull company details below."
        )
        ch_error = ""
    else:
        ch_msg = "API key saved, but the test call failed."
        ch_error = test.error or "Companies House rejected the key."
    return render(
        request,
        "clients/form.html",
        _new_client_form_ctx(ch_msg=ch_msg, ch_error=ch_error),
    )


@router.post("/new/from-ch", response_class=HTMLResponse)
async def new_client_from_ch(
    request: Request,
    company_number: str = Form(""),
    overall_status: str = Form("Active"),
    db: Session = Depends(get_db),
):
    """Pull company profile from CH and re-show New Client form pre-filled."""
    from app.services.companies_house import (
        client_fields_from_profile,
        fetch_company_profile,
        has_api_key,
    )

    if not has_api_key():
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(
                default_status=overall_status or "Active",
                ch_error=(
                    "Companies House public REST API key not found. "
                    "Paste it in the box above (same key as Jobs → Companies House)."
                ),
                draft={"company_number": company_number},
            ),
            status_code=400,
        )

    cn = normalize_company_number(company_number) or (company_number or "").strip()
    if not cn:
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(
                default_status=overall_status or "Active",
                ch_error="Enter a company number to pull from Companies House.",
            ),
            status_code=400,
        )

    fetch = fetch_company_profile(cn)
    if not fetch.ok or not fetch.profile:
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(
                default_status=overall_status or "Active",
                draft={"company_number": cn},
                ch_error=fetch.error or "Companies House lookup failed.",
            ),
            status_code=400,
        )

    draft = client_fields_from_profile(fetch.profile)
    existing = (
        db.query(Client)
        .filter(Client.company_number == (draft.get("company_number") or cn))
        .first()
    )
    ch_error = ""
    ch_msg = f"Pulled from Companies House: {draft.get('company_name') or cn}"
    if existing:
        ch_error = (
            f"Already a client: #{existing.id} {existing.company_name or existing.company_number}. "
            "Open that record instead of creating a duplicate."
        )
        ch_msg = ""

    return render(
        request,
        "clients/form.html",
        _new_client_form_ctx(
            default_status=draft.get("overall_status") or overall_status or "Active",
            draft=draft,
            ch_msg=ch_msg,
            ch_error=ch_error,
        ),
    )


@router.get("/new/ch-search", response_class=HTMLResponse)
async def new_client_ch_search(
    request: Request,
    q: str = Query(""),
    status: str = Query("Active"),
):
    """Search Companies House by name/number for the new-client flow."""
    from app.services.companies_house import has_api_key, search_companies

    items = []
    ch_error = ""
    if not (q or "").strip():
        ch_error = "Enter a company name or number to search."
    elif not has_api_key():
        ch_error = "Companies House API key not configured (Settings)."
    else:
        res = search_companies(q.strip())
        if not res.ok:
            ch_error = res.error or "Search failed."
        else:
            items = (res.profile or {}).get("items") or []

    return render(
        request,
        "clients/form.html",
        _new_client_form_ctx(
            default_status=status if status in STATUSES else "Active",
            ch_search_q=q,
            ch_search_items=items,
            ch_error=ch_error,
        ),
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
    engagement_date: str = Form(""),
    disengagement_date: str = Form(""),
    vat_number: str = Form(""),
    utr: str = Form(""),
    notes: str = Form(""),
    create_jobs_from_ch: str = Form(""),
    source: str = Form(""),
    ch_authentication_code: str = Form(""),
    db: Session = Depends(get_db),
):
    cn = normalize_company_number(company_number)
    if not cn:
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(
                default_status=overall_status or "Active",
                error="Company number is required.",
                draft={
                    "company_name": company_name,
                    "company_number": company_number,
                    "contact_name": contact_name,
                    "email": email,
                    "phone": phone,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "town": town,
                    "postcode": postcode,
                    "client_type": client_type,
                    "notes": notes,
                    "vat_number": vat_number,
                    "utr": utr,
                    "ch_authentication_code": ch_authentication_code,
                },
            ),
            status_code=400,
        )
    existing = db.query(Client).filter(Client.company_number == cn).first()
    if existing:
        return render(
            request,
            "clients/form.html",
            _new_client_form_ctx(
                default_status=overall_status or "Active",
                error=f"Company number {cn} already exists (client #{existing.id}).",
                draft={
                    "company_name": company_name,
                    "company_number": cn,
                    "contact_name": contact_name,
                    "email": email,
                    "phone": phone,
                    "address_line1": address_line1,
                    "address_line2": address_line2,
                    "town": town,
                    "postcode": postcode,
                    "client_type": client_type,
                    "notes": notes,
                },
            ),
            status_code=400,
        )

    eng = _parse_date(engagement_date)
    dis = _parse_date(disengagement_date)
    status = overall_status or "Active"
    # Completing disengagement marks the client lost unless already a prospect
    if dis and status not in ("Prospect", "Inactive"):
        status = "Inactive"

    src = (source or "").strip() or "manual"
    if src not in ("manual", "companies_house", "ch"):
        src = "manual"
    if src == "ch":
        src = "companies_house"

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
        overall_status=status,
        engagement_date=eng,
        disengagement_date=dis,
        vat_number=vat_number or None,
        utr=utr or None,
        notes=notes or None,
        source=src,
        ch_authentication_code=(ch_authentication_code or "").strip() or None,
        created_at=datetime.utcnow(),
    )
    db.add(client)
    db.commit()
    db.refresh(client)

    # Optional: create Accounts + CS jobs from CH dates (same as Jobs → CH)
    jobs_q = ""
    if (create_jobs_from_ch or "").strip().lower() in ("1", "yes", "on", "true"):
        try:
            from app.services.ch_jobs import create_jobs_for_client_from_ch

            result = create_jobs_for_client_from_ch(db, client)
            db.commit()
            if result.errors:
                jobs_q = f"?jobs_msg={url_quote('; '.join(result.errors)[:200])}"
            elif result.created:
                jobs_q = f"?jobs_created={result.created}"
            elif result.skipped:
                jobs_q = f"?jobs_msg={url_quote(f'{result.skipped} job(s) already existed')}"
            else:
                jobs_q = f"?jobs_msg={url_quote('No CH job dates found to create')}"
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            jobs_q = f"?jobs_msg={url_quote(f'Jobs not created: {exc}')[:220]}"

    return RedirectResponse(f"/clients/{client.id}{jobs_q}", status_code=303)


@router.get("/{client_id:int}", response_class=HTMLResponse)
async def client_detail(
    client_id: int,
    request: Request,
    saved: str = Query(""),
    contact_added: str = Query(""),
    contact_linked: str = Query(""),
    tab: str = Query(""),
    jobs_created: str = Query(""),
    jobs_msg: str = Query(""),
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
    if saved == "connections":
        message = "Connections saved."
    elif saved:
        message = "Details saved."
    elif contact_added:
        message = "Contact added to people list and linked to this client."
    elif contact_linked:
        message = "Existing person linked to this client."
    elif jobs_created and str(jobs_created).isdigit() and int(jobs_created) > 0:
        n = int(jobs_created)
        message = (
            f"Client created. {n} job(s) set up from Companies House dates "
            "(Accounts / Confirmation Statement)."
        )
    elif jobs_msg:
        message = f"Client created. Jobs: {jobs_msg}"

    from app.services.client_connections import list_connections_for_client
    from app.services.cs_automation import latest_pack_for_client
    from app.services.ch_oauth import latest_token_for_client, token_is_fresh

    connections = list_connections_for_client(db, client_id)
    asana_on = any(c["provider"] == "asana" and c["enabled"] for c in connections)
    try:
        latest_cs_pack = latest_pack_for_client(db, client_id)
    except Exception:
        latest_cs_pack = None
    ch_oauth_connected = False
    try:
        tok = latest_token_for_client(db, client_id)
        ch_oauth_connected = bool(tok and token_is_fresh(tok))
    except Exception:
        ch_oauth_connected = False

    documents = []
    docs_conn = {
        "configured": False,
        "connected": False,
        "fresh": False,
    }
    try:
        from app.services import documents as docs_svc

        documents = docs_svc.list_documents(db, client_id=client_id, limit=100)
        docs_conn = docs_svc.docs_connection(db)
    except Exception:
        documents = []

    client_tasks = []
    try:
        from app.services.practice_tasks import list_tasks

        client_tasks = list_tasks(db, client_id=client_id, include_closed=False, limit=50)
    except Exception:
        client_tasks = []

    client_emails = []
    try:
        from app.services import practice_emails as practice_mail

        practice_mail.seed_email_templates(db)
        client_emails = practice_mail.list_messages(db, client_id=client_id, limit=40)
    except Exception:
        client_emails = []

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
            "connections": connections,
            "asana_on": asana_on,
            "active_tab": tab or "overview",
            "latest_cs_pack": latest_cs_pack,
            "ch_oauth_connected": ch_oauth_connected,
            "documents": documents,
            "docs_conn": docs_conn,
            "client_tasks": client_tasks,
            "client_emails": client_emails,
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/{client_id:int}/connections")
async def update_client_connections(
    client_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save per-client integration toggles (opt-in)."""
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    form = await request.form()
    from app.services.client_connections import CONNECTION_PROVIDERS, save_connection_toggles

    enabled_map = {}
    for code, _label, _desc in CONNECTION_PROVIDERS:
        # checkbox present when on
        enabled_map[code] = form.get(f"conn_{code}") == "yes"
    save_connection_toggles(db, client_id, enabled_map)
    return RedirectResponse(
        f"/clients/{client_id}?tab=connections&saved=connections",
        status_code=303,
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
    engagement_date: str = Form(""),
    disengagement_date: str = Form(""),
    billing_model: str = Form("Per job"),
    retainer_amount: str = Form(""),
    retainer_frequency: str = Form("Monthly"),
    retainer_notes: str = Form(""),
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

    eng = _parse_date(engagement_date)
    dis = _parse_date(disengagement_date)

    client.company_name = company_name or client.company_name
    client.contact_name = contact_name or None
    client.email = email or None
    client.phone = phone or None
    client.address_line1 = address_line1 or None
    client.address_line2 = address_line2 or None
    client.town = town or None
    client.postcode = postcode or None
    client.client_type = client_type or None
    client.engagement_date = eng
    client.disengagement_date = dis
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
    model = (billing_model or "Per job").strip() or "Per job"
    if model not in ("Per job", "Retainer"):
        model = "Per job"
    client.billing_model = model
    try:
        ra = float(
            (retainer_amount or "")
            .replace("£", "")
            .replace(",", "")
            .strip()
            or 0
        )
    except ValueError:
        ra = 0.0
    client.retainer_amount = ra if ra > 0 else None
    freq = (retainer_frequency or "Monthly").strip() or "Monthly"
    if freq not in ("Monthly", "Quarterly", "Annual"):
        freq = "Monthly"
    client.retainer_frequency = freq if client.retainer_amount or model == "Retainer" else None
    client.retainer_notes = (retainer_notes or "").strip() or None
    if model == "Retainer" and not client.retainer_amount:
        # still mark as retainer even if amount to fill later
        client.billing_model = "Retainer"
    client.notes = notes or None
    # Completing disengagement → lost (practice book leave date)
    if dis and (client.overall_status or "") not in ("Prospect", "Inactive"):
        client.overall_status = "Inactive"
    client.updated_at = datetime.utcnow()

    # Lost / disengaged clients leave practice groups
    if (client.overall_status or "") == "Inactive" or client.disengagement_date:
        try:
            from app.services.practice_groups import remove_client_from_groups

            remove_client_from_groups(db, client_id)
        except Exception:
            pass

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

    # Inactive = Lost → remove from groups board
    # Former stays live (not lost) and can remain in groups
    if status == "Inactive":
        try:
            from app.services.practice_groups import remove_client_from_groups

            remove_client_from_groups(db, client_id)
        except Exception:
            pass

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
    engagement_date: str = Form(""),
    disengagement_date: str = Form(""),
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

    eng = _parse_date(engagement_date)
    dis = _parse_date(disengagement_date)
    status = overall_status or "Active"
    if dis and status not in ("Prospect", "Inactive"):
        status = "Inactive"

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
    client.overall_status = status
    client.engagement_date = eng
    client.disengagement_date = dis
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
    if status == "Inactive" or dis:
        try:
            from app.services.practice_groups import remove_client_from_groups

            remove_client_from_groups(db, client_id)
        except Exception:
            pass
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
