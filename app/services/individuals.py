"""Individual clients: people who are clients in their own right (e.g. SA only)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Query, Session, joinedload

from app.models import Client, Job, Person
from app.models.sales import Invoice


def individual_client_number(person_id: int) -> str:
    """Stable pseudo company number for individual (non-company) clients."""
    return f"IND-{person_id:06d}"


def is_individual_shell(client: Any) -> bool:
    """True for person-backed shells (IND-… / client_type Individual) — not companies."""
    if client is None:
        return False
    ct = (getattr(client, "client_type", None) or "").strip().lower()
    cn = (getattr(client, "company_number", None) or "").strip().upper()
    return ct == "individual" or cn.startswith("IND-")


def filter_company_clients(query: Query) -> Query:
    """Restrict a Client query to company / firm records (exclude individual shells)."""
    return query.filter(
        or_(Client.client_type.is_(None), Client.client_type != "Individual"),
        or_(
            Client.company_number.is_(None),
            ~Client.company_number.ilike("IND-%"),
        ),
    )


def _shell_clients_for_person(person: Person) -> List[Client]:
    shells: List[Client] = []
    for c in person.clients or []:
        if is_individual_shell(c):
            shells.append(c)
    # Also by stable IND-###### even if link missing
    if person.id:
        ref = individual_client_number(person.id)
        # May already be in list
        if not any((c.company_number or "").upper() == ref for c in shells):
            # Caller may not have session; only check linked clients
            pass
    return shells


def person_delete_impact(db: Session, person: Person) -> Dict[str, Any]:
    """
    Summarise what will be affected if this person is deleted.

    Used on the confirm-delete screen (especially for cleaning up duplicates).
    """
    companies = [
        {
            "id": c.id,
            "name": c.display_name() if hasattr(c, "display_name") else (c.company_name or c.company_number),
            "number": c.company_number or "",
        }
        for c in (person.clients or [])
        if not is_individual_shell(c)
    ]

    shells: List[Client] = [c for c in (person.clients or []) if is_individual_shell(c)]
    # Ensure IND-{person_id} shell is included even if unlinked
    if person.id:
        ref = individual_client_number(person.id)
        extra = (
            db.query(Client)
            .filter(Client.company_number == ref)
            .first()
        )
        if extra and extra not in shells:
            shells.append(extra)

    shell_details = []
    total_open_jobs = 0
    total_jobs = 0
    total_invoices = 0
    shell_can_delete_all = True
    for shell in shells:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == shell.id)
            .order_by(Job.id.desc())
            .all()
        )
        open_jobs = [
            j
            for j in jobs
            if (j.status or "").strip() not in ("Completed", "Cancelled", "Filed")
        ]
        inv_count = int(
            db.query(Invoice)
            .filter(Invoice.client_id == shell.id)
            .count()
            or 0
        )
        total_jobs += len(jobs)
        total_open_jobs += len(open_jobs)
        total_invoices += inv_count
        can_delete_shell = len(jobs) == 0 and inv_count == 0
        if not can_delete_shell:
            shell_can_delete_all = False
        shell_details.append(
            {
                "id": shell.id,
                "name": shell.company_name or shell.company_number or f"#{shell.id}",
                "number": shell.company_number or "",
                "job_count": len(jobs),
                "open_job_count": len(open_jobs),
                "invoice_count": inv_count,
                "can_delete": can_delete_shell,
                "sample_jobs": [
                    {
                        "id": j.id,
                        "title": j.title or j.type or f"Job #{j.id}",
                        "type": j.type or "",
                        "status": j.status or "",
                        "fee": float(j.fee or 0),
                    }
                    for j in jobs[:8]
                ],
            }
        )

    # Possible keep candidates (same / similar name) — helpful for duplicates
    name = (person.full_name or "").strip()
    name_matches: List[Dict[str, Any]] = []
    if name and person.id:
        # Exact name (case-insensitive) other people
        others = (
            db.query(Person)
            .options(joinedload(Person.clients))
            .filter(Person.id != person.id, Person.full_name.ilike(name))
            .order_by(Person.id)
            .limit(10)
            .all()
        )
        for o in others:
            cos = [
                c.company_name or c.company_number or f"#{c.id}"
                for c in (o.clients or [])
                if not is_individual_shell(c)
            ]
            name_matches.append(
                {
                    "id": o.id,
                    "name": o.full_name or "—",
                    "is_individual": bool(o.is_individual_client),
                    "companies": cos[:5],
                    "has_ch_code": bool((o.ch_code or "").strip()),
                    "has_email": bool((o.email or "").strip()),
                }
            )

    warnings: List[str] = []
    if companies:
        warnings.append(
            f"Linked to {len(companies)} compan{'y' if len(companies) == 1 else 'ies'} — "
            "links will be removed (companies themselves stay)."
        )
    if total_open_jobs:
        warnings.append(
            f"Individual client shell has {total_open_jobs} open job(s) — "
            "the shell will NOT be deleted while jobs/invoices remain."
        )
    elif total_jobs or total_invoices:
        warnings.append(
            f"Individual shell has {total_jobs} job(s) and {total_invoices} invoice(s) — "
            "shell will not be deleted (move or complete them first)."
        )
    if (person.ch_code or "").strip():
        warnings.append("This person has a Companies House personal code on file.")
    if (person.utr or "").strip():
        warnings.append("This person has a UTR on file.")
    if (getattr(person, "gov_gateway_username", None) or "").strip() or (
        getattr(person, "gov_gateway_password", None) or ""
    ).strip():
        warnings.append("This person has Government Gateway credentials on file.")
    if name_matches:
        warnings.append(
            f"{len(name_matches)} other person record(s) share the same name — "
            "likely a duplicate cleanup; keep the better record."
        )

    return {
        "person_id": person.id,
        "full_name": person.full_name or "—",
        "is_individual": bool(person.is_individual_client),
        "email": person.email or "",
        "phone": person.phone or "",
        "role": person.role or "",
        "has_ch_code": bool((person.ch_code or "").strip()),
        "has_utr": bool((person.utr or "").strip()),
        "companies": companies,
        "shells": shell_details,
        "total_open_jobs": total_open_jobs,
        "total_jobs": total_jobs,
        "total_invoices": total_invoices,
        "can_delete_shell": shell_can_delete_all and bool(shells),
        "name_matches": name_matches,
        "warnings": warnings,
        # Person row can always be deleted; shell is optional when empty
        "can_delete_person": True,
    }


def delete_person(
    db: Session,
    person: Person,
    *,
    delete_empty_shell: bool = True,
) -> Dict[str, Any]:
    """
    Delete a person record (duplicate cleanup).

    - Always removes the person and person↔company links (CASCADE).
    - Optionally deletes empty IND- individual client shell(s) with no jobs/invoices.
    - Never deletes shells that still have jobs or invoices.
    """
    impact = person_delete_impact(db, person)
    deleted_shell_ids: List[int] = []
    kept_shells: List[Dict[str, Any]] = []

    if delete_empty_shell:
        for shell_info in impact.get("shells") or []:
            if not shell_info.get("can_delete"):
                kept_shells.append(shell_info)
                continue
            shell = db.query(Client).filter(Client.id == shell_info["id"]).first()
            if shell:
                db.delete(shell)
                deleted_shell_ids.append(shell_info["id"])
    else:
        kept_shells = list(impact.get("shells") or [])

    pid = person.id
    name = person.full_name
    db.delete(person)
    db.commit()
    return {
        "ok": True,
        "person_id": pid,
        "full_name": name,
        "deleted_shell_ids": deleted_shell_ids,
        "kept_shells": kept_shells,
        "companies_unlinked": len(impact.get("companies") or []),
    }


def ensure_individual_client(db: Session, person: Person) -> Client:
    """
    Ensure this person has a Client record for jobs/fees.

    Individuals (tax return only, sole traders without a company number) still
    need a client row so jobs and invoices can attach cleanly.
    """
    ref = individual_client_number(person.id)
    client = db.query(Client).filter(Client.company_number == ref).first()
    if not client:
        # Fallback: match by exact name + Individual type
        client = (
            db.query(Client)
            .filter(
                Client.client_type == "Individual",
                Client.company_name == person.full_name,
            )
            .first()
        )
    if not client:
        client = Client(
            company_name=person.full_name,
            company_number=ref,
            contact_name=person.full_name,
            email=person.email,
            phone=person.phone,
            utr=person.utr,
            client_type="Individual",
            overall_status="Active",
            source="individual",
            notes="Individual client (e.g. Self Assessment / tax only) — no limited company.",
        )
        db.add(client)
        db.flush()
    else:
        # Keep core details in sync
        client.company_name = person.full_name or client.company_name
        client.contact_name = person.full_name
        if person.email:
            client.email = person.email
        if person.phone:
            client.phone = person.phone
        if person.utr:
            client.utr = person.utr
        client.client_type = "Individual"

    if client not in person.clients:
        person.clients.append(client)

    if person.person_status in (None, "", "Contact"):
        person.person_status = "Individual Client"

    return client
