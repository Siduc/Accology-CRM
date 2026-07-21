"""Create jobs from Companies House using job profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from sqlalchemy.orm import Session

from app.models import Client, Job
from app.services.companies_house import (
    fetch_company_profile,
    summarize_profile_dates,
)
from app.services.company_numbers import normalize_company_number
from app.services.job_profiles import JobDraft, drafts_from_companies_house
from app.services.fees import get_suggested_fee


@dataclass
class ClientJobCreateResult:
    client_id: int
    company_number: str
    company_name: str
    created: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    created_jobs: List[str] = field(default_factory=list)
    skipped_jobs: List[str] = field(default_factory=list)
    preview_dates: dict = field(default_factory=dict)


def _existing_open_job(
    db: Session, client_id: int, job_type: str, period_end
) -> Optional[Job]:
    q = (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == job_type,
            Job.status.notin_(["Completed", "Cancelled"]),
        )
    )
    if period_end is not None:
        q = q.filter(Job.period_end == period_end)
    return q.first()


def create_jobs_from_drafts(
    db: Session,
    client: Client,
    drafts: Sequence[JobDraft],
    *,
    skip_duplicates: bool = True,
) -> ClientJobCreateResult:
    result = ClientJobCreateResult(
        client_id=client.id,
        company_number=client.company_number or "",
        company_name=client.company_name or "",
    )
    for draft in drafts:
        if skip_duplicates:
            existing = _existing_open_job(
                db, client.id, draft.type, draft.period_end
            )
            if existing:
                result.skipped += 1
                result.skipped_jobs.append(
                    f"{draft.type} period {draft.period_end} (job #{existing.id})"
                )
                continue
        fee = draft.fee or 0.0
        if not fee:
            suggested = get_suggested_fee(
                db, draft.type, draft.period_end, client_id=client.id
            )
            if suggested is not None:
                fee = suggested
        job = Job(
            title=draft.title,
            type=draft.type,
            client_id=client.id,
            period_end=draft.period_end,
            statutory_due_date=draft.statutory_due_date,
            target_start=draft.target_start,
            target_completion=draft.target_completion,
            fee=fee,
            status=draft.status,
            is_recurring=draft.is_recurring,
            notes=draft.notes,
        )
        db.add(job)
        result.created += 1
        result.created_jobs.append(
            f"{draft.type}: period {draft.period_end}, due {draft.statutory_due_date}, £{fee:.2f}"
        )
    if result.created:
        db.commit()
    return result


def create_jobs_for_client_from_ch(
    db: Session,
    client: Client,
    profile_keys: Optional[List[str]] = None,
    accounts_fee: float = 0.0,
    cs_fee: float = 0.0,
    skip_duplicates: bool = True,
) -> ClientJobCreateResult:
    raw_cn = client.company_number or ""
    cn = normalize_company_number(raw_cn) or ""
    if not cn or cn.startswith("IND-"):
        return ClientJobCreateResult(
            client_id=client.id,
            company_number=cn,
            company_name=client.company_name or "",
            errors=["Not a Companies House company number"],
        )

    # Persist padded number (e.g. 8056337 → 08056337) so future lookups work
    if cn != (raw_cn or "").strip():
        client.company_number = cn

    fetch = fetch_company_profile(cn)
    if not fetch.ok:
        return ClientJobCreateResult(
            client_id=client.id,
            company_number=cn,
            company_name=client.company_name or "",
            errors=[fetch.error],
        )

    name = fetch.profile.get("company_name") or client.company_name or ""
    # Refresh company name from CH when available
    if fetch.profile.get("company_name"):
        client.company_name = fetch.profile["company_name"]

    drafts = drafts_from_companies_house(
        fetch.profile,
        company_name=name,
        profile_keys=profile_keys,
        accounts_fee=accounts_fee,
        cs_fee=cs_fee,
    )
    if not drafts:
        return ClientJobCreateResult(
            client_id=client.id,
            company_number=cn,
            company_name=name,
            errors=["No Accounts or CS dates found on Companies House profile"],
            preview_dates=summarize_profile_dates(fetch.profile),
        )

    result = create_jobs_from_drafts(
        db, client, drafts, skip_duplicates=skip_duplicates
    )
    result.preview_dates = summarize_profile_dates(fetch.profile)
    result.company_name = name
    return result


def create_jobs_for_clients_from_ch(
    db: Session,
    clients: Sequence[Client],
    profile_keys: Optional[List[str]] = None,
    accounts_fee: float = 0.0,
    cs_fee: float = 0.0,
    skip_duplicates: bool = True,
) -> List[ClientJobCreateResult]:
    results = []
    for client in clients:
        results.append(
            create_jobs_for_client_from_ch(
                db,
                client,
                profile_keys=profile_keys,
                accounts_fee=accounts_fee,
                cs_fee=cs_fee,
                skip_duplicates=skip_duplicates,
            )
        )
    return results
