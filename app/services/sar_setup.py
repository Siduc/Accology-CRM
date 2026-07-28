"""
One-shot / repair: SAR people → individual tax clients + SA jobs for tax year.

- person_status SAR + not individual → mark is_individual_client
- ensure Individual client row (IND-######)
- ensure Self Assessment service + fee schedule
- create recurring SA job PE 5 Apr 2026 / statutory due 31 Jan 2027
- fee from prior SA history (+5%) when available
- if prior PE year already Completed, note that; 2026 job stays open unless already completed
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import Job, Person
from app.models.sales import Service, ServicePrice
from app.models.service_fee import ServiceFee
from app.services.fees import (
    PRIOR_YEAR_UPLIFT,
    SERVICE_SA,
    get_prior_client_job_fee,
    get_suggested_fee,
)
from app.services.individuals import ensure_individual_client
from app.services.sales_ledger import seed_services

# Tax year 2025/26
SA_PE_2026 = date(2026, 4, 5)
SA_DUE_2027 = date(2027, 1, 31)
SA_PE_2025 = date(2025, 4, 5)

# Standard Self Assessment fee (no history)
SA_DEFAULT_FEE = 250.0


def ensure_sa_service_and_fees(db: Session) -> Dict[str, Any]:
    """Ensure Self Assessment is on Services Ledger and fee schedule."""
    seed_services(db)
    svc = db.query(Service).filter(Service.code == "SA").first()
    if not svc:
        svc = Service(
            code="SA",
            name="Self Assessment",
            description="Personal tax return (Self Assessment).",
            default_fee=SA_DEFAULT_FEE,
            default_vat_rate=0.0,
            unit="job",
            category="compliance",
            is_active=True,
            is_sellable_to_clients=True,
        )
        db.add(svc)
        db.flush()
    else:
        if not svc.is_active:
            svc.is_active = True
        # Standard SA fee is flat £250 (not prior-year uplift schedule)
        svc.default_fee = SA_DEFAULT_FEE
        if (svc.name or "").strip() in ("", "Self Assessment"):
            svc.name = "Self Assessment"
            svc.description = svc.description or "Personal tax return (Self Assessment)."

    fees_added = 0
    # Flat standard fee for all schedule years (second service by value after Accounts)
    for year, fee in (
        (2025, SA_DEFAULT_FEE),
        (2026, SA_DEFAULT_FEE),
        (2027, SA_DEFAULT_FEE),
    ):
        row = (
            db.query(ServiceFee)
            .filter(ServiceFee.service_code == SERVICE_SA, ServiceFee.year == year)
            .first()
        )
        if not row:
            db.add(
                ServiceFee(
                    service_code=SERVICE_SA,
                    service_name="Self Assessment",
                    year=year,
                    fee=fee,
                )
            )
            fees_added += 1
        elif float(row.fee or 0) != fee:
            row.fee = fee
            row.service_name = "Self Assessment"
            fees_added += 1
        # ServicePrice year rows
        if svc.id:
            sp = (
                db.query(ServicePrice)
                .filter(ServicePrice.service_id == svc.id, ServicePrice.year == year)
                .first()
            )
            if not sp:
                db.add(ServicePrice(service_id=svc.id, year=year, fee=fee))
                fees_added += 1
            elif float(sp.fee or 0) != fee:
                sp.fee = fee
                fees_added += 1

    db.commit()
    return {"service_id": svc.id, "fees_added": fees_added}


def _sar_people(db: Session) -> List[Person]:
    people = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .order_by(Person.id)
        .all()
    )
    out = []
    for p in people:
        st = (p.person_status or "").strip().upper()
        if st == "SAR" or st in ("SA", "SELF ASSESSMENT"):
            out.append(p)
            continue
        # Also notes/role markers
        blob = f"{p.person_status or ''} {p.role or ''} {p.notes or ''}".lower()
        if "self assessment" in blob or " sa " in f" {blob} " or blob.strip() == "sa":
            out.append(p)
    return out


def _fee_for_client(db: Session, client_id: int) -> float:
    prior = get_prior_client_job_fee(
        db, client_id, "Self Assessment", SA_PE_2026
    )
    if prior and prior > 0:
        return round(float(prior) * (1.0 + PRIOR_YEAR_UPLIFT), 2)
    suggested = get_suggested_fee(
        db, "Self Assessment", period_end=SA_PE_2026, client_id=client_id
    )
    if suggested and suggested > 0:
        return float(suggested)
    return float(SA_DEFAULT_FEE)


def _existing_sa_job(db: Session, client_id: int, pe: date) -> Optional[Job]:
    return (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == "Self Assessment",
            Job.period_end == pe,
            Job.status.notin_(["Cancelled"]),
        )
        .first()
    )


def _prior_year_done(db: Session, client_id: int) -> Optional[Job]:
    """Most recent completed SA for PE 5 Apr 2025 (or any earlier completed SA)."""
    j = (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == "Self Assessment",
            Job.period_end == SA_PE_2025,
            Job.status.in_(["Completed", "Filed"]),
        )
        .first()
    )
    if j:
        return j
    return (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == "Self Assessment",
            Job.status.in_(["Completed", "Filed"]),
            Job.period_end.isnot(None),
            Job.period_end < SA_PE_2026,
        )
        .order_by(Job.period_end.desc())
        .first()
    )


def setup_sar_individuals_and_jobs(
    db: Session, *, dry_run: bool = False
) -> Dict[str, Any]:
    """
    Fix SAR people flags, ensure SA service, create 2025/26 SA jobs.
    """
    svc_info = ensure_sa_service_and_fees(db)

    people = _sar_people(db)
    flagged = 0
    clients_ensured = 0
    jobs_created = 0
    jobs_existed = 0
    jobs_completed_prior = 0
    details: List[str] = []

    for person in people:
        needs_flag = not bool(person.is_individual_client)
        if dry_run:
            details.append(
                f"person #{person.id} {person.full_name!r}: "
                f"{'flag individual + ' if needs_flag else ''}"
                f"ensure client + SA job PE {SA_PE_2026.isoformat()}"
            )
            if needs_flag:
                flagged += 1
            continue

        if needs_flag:
            person.is_individual_client = True
            flagged += 1
        if (person.person_status or "").strip() in ("", "Contact"):
            person.person_status = "SAR"

        client = ensure_individual_client(db, person)
        clients_ensured += 1

        # Prior year done note
        prior_done = _prior_year_done(db, client.id)
        if prior_done:
            jobs_completed_prior += 1

        existing = _existing_sa_job(db, client.id, SA_PE_2026)
        if existing:
            jobs_existed += 1
            # Ensure recurring + statutory if missing/wrong
            changed = False
            if (existing.is_recurring or "").strip() != "Yes":
                existing.is_recurring = "Yes"
                changed = True
            if existing.statutory_due_date != SA_DUE_2027:
                existing.statutory_due_date = SA_DUE_2027
                changed = True
            if changed:
                details.append(
                    f"updated existing SA job #{existing.id} for {person.full_name}"
                )
            continue

        fee = _fee_for_client(db, client.id)
        notes_bits = [
            "Self Assessment tax year 2025/26",
            "Created from SAR person setup",
        ]
        if prior_done and prior_done.period_end:
            notes_bits.append(
                f"Prior SA to {prior_done.period_end.isoformat()} status={prior_done.status}"
                f" fee={float(prior_done.fee or 0):.2f}"
            )

        job = Job(
            title=f"{person.full_name} — Self Assessment 2025/26",
            type="Self Assessment",
            client_id=client.id,
            period_end=SA_PE_2026,
            statutory_due_date=SA_DUE_2027,
            target_start=None,
            target_completion=None,
            fee=fee,
            status="Planned",
            is_recurring="Yes",
            source="sar_setup",
            notes="; ".join(notes_bits),
            import_key=f"sar-sa-2026-{person.id}",
        )
        db.add(job)
        jobs_created += 1
        details.append(
            f"created SA job for person #{person.id} {person.full_name!r} "
            f"client={client.id} fee={fee:.2f}"
        )

    if not dry_run:
        db.commit()

    return {
        "dry_run": dry_run,
        "sar_people": len(people),
        "flagged_individual": flagged,
        "clients_ensured": clients_ensured,
        "jobs_created": jobs_created,
        "jobs_already_existed": jobs_existed,
        "prior_year_completed_count": jobs_completed_prior,
        "service": svc_info,
        "period_end": SA_PE_2026.isoformat(),
        "statutory_due": SA_DUE_2027.isoformat(),
        "details": details[:50],
        "detail_truncated": max(0, len(details) - 50),
    }
