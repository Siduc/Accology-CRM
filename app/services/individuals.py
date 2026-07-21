"""Individual clients: people who are clients in their own right (e.g. SA only)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Client, Person


def individual_client_number(person_id: int) -> str:
    """Stable pseudo company number for individual (non-company) clients."""
    return f"IND-{person_id:06d}"


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
