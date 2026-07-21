from fastapi import APIRouter, Depends, Form, Request, Query, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Client, Person
from app.services.import_csv import (
    normalize_company_number,
    parse_rows,
    excel_bytes_to_csv_text,
    PERSON_HEADER_MAP,
)
from app.services.individuals import ensure_individual_client
from app.templating import render

router = APIRouter(prefix="/people", tags=["people"])


def _resolve_clients_from_ids(db: Session, client_ids: list[int]) -> list[Client]:
    if not client_ids:
        return []
    return db.query(Client).filter(Client.id.in_(client_ids)).all()


def _parse_client_ids(form_values) -> list[int]:
    """Accept multi-select list or single value."""
    if form_values is None:
        return []
    if isinstance(form_values, str):
        form_values = [form_values] if form_values else []
    ids = []
    for v in form_values:
        if not v:
            continue
        try:
            ids.append(int(v))
        except (TypeError, ValueError):
            continue
    return ids


def _company_clients_of(person: Person) -> list[Client]:
    return [
        c
        for c in (person.clients or [])
        if (c.client_type or "").lower() != "individual"
        and not (c.company_number or "").upper().startswith("IND-")
    ]


def _person_row(person: Person) -> dict:
    """Plain dict for templates (avoids Jinja method-call issues)."""
    cos = _company_clients_of(person)
    individual = bool(person.is_individual_client)
    return {
        "id": person.id,
        "full_name": person.full_name,
        "email": person.email,
        "phone": person.phone,
        "role": person.role,
        "person_status": person.person_status,
        "is_individual_client": individual,
        "company_clients": cos,
        "needs_company_link": (not individual) and (not cos),
    }


@router.get("", response_class=HTMLResponse)
async def list_people(
    request: Request,
    filter: str = Query(""),
    db: Session = Depends(get_db),
):
    people = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .order_by(Person.full_name)
        .all()
    )
    rows = [_person_row(p) for p in people]
    if filter == "unlinked":
        rows = [r for r in rows if r["needs_company_link"]]
    elif filter == "linked":
        rows = [r for r in rows if r["company_clients"]]
    elif filter == "individual":
        rows = [r for r in rows if r["is_individual_client"]]

    linked = sum(1 for r in (_person_row(p) for p in people) if r["company_clients"])
    individual = sum(1 for p in people if p.is_individual_client)
    needs_link = sum(
        1 for r in (_person_row(p) for p in people) if r["needs_company_link"]
    )
    return render(
        request,
        "people/list.html",
        {
            "people": rows,
            "filter": filter,
            "linked_count": linked,
            "individual_count": individual,
            "unlinked_count": needs_link,
        },
    )


def _is_company_client(c: Client) -> bool:
    if (c.client_type or "").lower() == "individual":
        return False
    if (c.company_number or "").upper().startswith("IND-"):
        return False
    return True


def _link_page_people(db: Session) -> list[dict]:
    people = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .order_by(Person.full_name)
        .all()
    )
    rows = []
    for p in people:
        linked = [c for c in (p.clients or []) if _is_company_client(c)]
        rows.append(
            {
                "id": p.id,
                "full_name": p.full_name,
                "role": p.role,
                "is_individual_client": bool(p.is_individual_client),
                "linked_companies": [
                    {"id": c.id, "name": c.company_name or c.company_number}
                    for c in linked
                ],
                "linked_ids": {c.id for c in linked},
            }
        )
    rows.sort(key=lambda r: (len(r["linked_companies"]) > 0, r["full_name"] or ""))
    return rows


@router.get("/link", response_class=HTMLResponse)
async def link_people_page(request: Request, db: Session = Depends(get_db)):
    people = _link_page_people(db)
    clients = [
        c
        for c in db.query(Client).order_by(Client.company_name).all()
        if _is_company_client(c)
    ]
    return render(
        request,
        "people/link.html",
        {
            "people": people,
            "clients": clients,
            "result": None,
        },
    )


@router.post("/link", response_class=HTMLResponse)
async def link_people_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    # Collect every company ticked for each person.
    # Checkboxes share name person_{id} with different values — use getlist.
    by_person: dict[int, list[int]] = {}
    seen_keys = set()
    for key in form.keys():
        if not key.startswith("person_") or key in seen_keys:
            continue
        seen_keys.add(key)
        try:
            person_id = int(key.replace("person_", "", 1))
        except ValueError:
            continue
        raw_values = form.getlist(key)
        ids: list[int] = []
        for value in raw_values:
            if value is None or value == "":
                continue
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        # preserve order, drop duplicates
        by_person[person_id] = list(dict.fromkeys(ids))

    linked_ops = 0
    for person_id, client_ids in by_person.items():
        if not client_ids:
            continue
        person = (
            db.query(Person)
            .options(joinedload(Person.clients))
            .filter(Person.id == person_id)
            .first()
        )
        if not person:
            continue
        clients = _resolve_clients_from_ids(db, client_ids)
        existing_ids = {c.id for c in person.clients}
        for c in clients:
            if c.id not in existing_ids:
                person.clients.append(c)
                existing_ids.add(c.id)
                linked_ops += 1
        # ensure relationship flush for this person before next
        db.add(person)
    db.commit()

    people = _link_page_people(db)
    clients = [
        c
        for c in db.query(Client).order_by(Client.company_name).all()
        if _is_company_client(c)
    ]
    return render(
        request,
        "people/link.html",
        {
            "people": people,
            "clients": clients,
            "result": {"linked": linked_ops},
        },
    )


@router.get("/link-file", response_class=HTMLResponse)
async def link_from_file_page(request: Request):
    return render(request, "people/link_file.html", {"result": None})


@router.post("/link-file", response_class=HTMLResponse)
async def link_from_file(
    request: Request,
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add company memberships from spreadsheet (name + company number/name).

    Same person can appear on multiple rows with different companies.
    Links are additive (does not remove existing companies).
    """
    text = ""
    try:
        if csv_file and csv_file.filename:
            content = await csv_file.read()
            name = (csv_file.filename or "").lower()
            if name.endswith((".xlsx", ".xlsm")):
                text = excel_bytes_to_csv_text(content)
            else:
                text = content.decode("utf-8-sig", errors="replace")
        else:
            text = csv_data or ""
    except Exception as exc:  # noqa: BLE001
        return render(
            request,
            "people/link_file.html",
            {"result": {"error": str(exc)}},
        )

    if not text.strip():
        return render(
            request,
            "people/link_file.html",
            {"result": {"error": "No file or data provided."}},
        )

    rows, warnings = parse_rows(text)
    linked = 0
    skipped = 0
    errors = list(warnings)

    for idx, row in enumerate(rows, start=1):
        data = {}
        for raw_key, value in row.items():
            key = PERSON_HEADER_MAP.get(raw_key, raw_key)
            data[key] = value if value else None

        full_name = data.get("full_name")
        if not full_name:
            first = (data.get("first_name") or "").strip()
            last = (data.get("last_name") or "").strip()
            if first or last:
                full_name = f"{first} {last}".strip()
        if not full_name:
            vals = [v for v in row.values() if v]
            full_name = vals[0] if vals else None

        if not full_name:
            skipped += 1
            errors.append(f"Row {idx}: no name")
            continue

        client = None
        if data.get("company_number"):
            cn = normalize_company_number(data["company_number"])
            client = db.query(Client).filter(Client.company_number == cn).first()
        if not client and data.get("company_name_link"):
            client = (
                db.query(Client)
                .filter(Client.company_name.ilike(data["company_name_link"].strip()))
                .first()
            )
        if not client:
            for alt in ("company_name", "company", "client_name", "client"):
                if row.get(alt):
                    client = (
                        db.query(Client)
                        .filter(Client.company_name.ilike(str(row[alt]).strip()))
                        .first()
                    )
                    if client:
                        break

        if not client:
            skipped += 1
            errors.append(f"Row {idx} ({full_name}): no matching client")
            continue

        person = (
            db.query(Person)
            .options(joinedload(Person.clients))
            .filter(Person.full_name.ilike(full_name.strip()))
            .first()
        )
        if not person:
            skipped += 1
            errors.append(f"Row {idx}: person not found: {full_name}")
            continue

        if client not in person.clients:
            person.clients.append(client)
            linked += 1
        else:
            skipped += 1

    db.commit()
    return render(
        request,
        "people/link_file.html",
        {
            "result": {
                "linked": linked,
                "skipped": skipped,
                "errors": errors[:40],
            }
        },
    )


def _company_client_choices(db: Session) -> list[Client]:
    """Companies for the multi-select (hide auto Individual shell records)."""
    return (
        db.query(Client)
        .filter(
            (Client.client_type.is_(None))
            | (Client.client_type != "Individual")
        )
        .order_by(Client.company_name)
        .all()
    )


@router.get("/new", response_class=HTMLResponse)
async def new_person_form(
    request: Request,
    client_id: int = Query(None),
    db: Session = Depends(get_db),
):
    clients = _company_client_choices(db)
    return render(
        request,
        "people/form.html",
        {
            "person": None,
            "clients": clients,
            "selected_client_ids": [client_id] if client_id else [],
            "error": None,
        },
    )


@router.post("/new")
async def create_person(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    role: str = Form(""),
    person_status: str = Form("Contact"),
    notes: str = Form(""),
    is_primary: str = Form(""),
    is_individual_client: str = Form(""),
    utr: str = Form(""),
    db: Session = Depends(get_db),
):
    form = await request.form()
    client_ids = _parse_client_ids(form.getlist("client_ids"))
    individual = is_individual_client == "yes"

    person = Person(
        full_name=full_name,
        email=email or None,
        phone=phone or None,
        role=role or None,
        person_status=(
            "Individual Client"
            if individual
            else (person_status or "Contact")
        ),
        notes=notes or None,
        is_primary=is_primary == "yes",
        is_individual_client=individual,
        utr=utr or None,
    )
    person.clients = _resolve_clients_from_ids(db, client_ids)
    db.add(person)
    db.flush()
    if individual:
        ensure_individual_client(db, person)
    db.commit()
    db.refresh(person)
    if person.clients:
        return RedirectResponse(f"/clients/{person.clients[0].id}", status_code=303)
    return RedirectResponse("/people", status_code=303)


@router.get("/{person_id:int}/edit", response_class=HTMLResponse)
async def edit_person_form(
    person_id: int, request: Request, db: Session = Depends(get_db)
):
    person = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .filter(Person.id == person_id)
        .first()
    )
    if not person:
        return RedirectResponse("/people", status_code=303)
    clients = _company_client_choices(db)
    selected = [c.id for c in person.company_clients()]
    return render(
        request,
        "people/form.html",
        {
            "person": person,
            "clients": clients,
            "selected_client_ids": selected,
            "error": None,
        },
    )


@router.post("/{person_id:int}/edit")
async def update_person(
    person_id: int,
    request: Request,
    full_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    role: str = Form(""),
    person_status: str = Form("Contact"),
    notes: str = Form(""),
    is_primary: str = Form(""),
    is_individual_client: str = Form(""),
    utr: str = Form(""),
    db: Session = Depends(get_db),
):
    person = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .filter(Person.id == person_id)
        .first()
    )
    if not person:
        return RedirectResponse("/people", status_code=303)

    form = await request.form()
    client_ids = _parse_client_ids(form.getlist("client_ids"))
    individual = is_individual_client == "yes"

    person.full_name = full_name
    person.email = email or None
    person.phone = phone or None
    person.role = role or None
    person.notes = notes or None
    person.is_primary = is_primary == "yes"
    person.is_individual_client = individual
    person.utr = utr or None
    # Keep company links from form; preserve any Individual shell client
    company_clients = _resolve_clients_from_ids(db, client_ids)
    individual_shells = [
        c
        for c in person.clients
        if (c.client_type or "").lower() == "individual"
        or (c.company_number or "").upper().startswith("IND-")
    ]
    person.clients = company_clients + [
        c for c in individual_shells if c not in company_clients
    ]

    if individual:
        ensure_individual_client(db, person)
        if person.person_status in (None, "", "Contact"):
            person.person_status = "Individual Client"
    else:
        person.person_status = person_status or person.person_status or "Contact"

    db.commit()

    if person.clients:
        return RedirectResponse(f"/clients/{person.clients[0].id}", status_code=303)
    return RedirectResponse("/people", status_code=303)
