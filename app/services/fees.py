"""Look up suggested service fees by type and year."""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models.service_fee import ServiceFee

# Canonical service codes (match Job.type where possible)
SERVICE_ACCOUNTS = "Accounts"
SERVICE_CS = "Confirmation Statement"
SERVICE_CT = "Corporation Tax"
SERVICE_SA = "Self Assessment"

DEFAULT_SERVICES = [
    SERVICE_ACCOUNTS,
    SERVICE_CS,
    SERVICE_CT,
    SERVICE_SA,
]


def service_code_for_job_type(job_type: str) -> str:
    t = (job_type or "").strip()
    if "Confirmation" in t:
        return SERVICE_CS
    if "Accounts" in t:
        return SERVICE_ACCOUNTS
    if "Corporation" in t or t == "CT":
        return SERVICE_CT
    if "Self Assessment" in t or t == "SA":
        return SERVICE_SA
    return t or "Other"


def fee_year_from_period_end(period_end: Optional[date]) -> int:
    """Fee year = period end year (e.g. accounts to 31 Mar 2025 → 2025)."""
    if period_end:
        return period_end.year
    return date.today().year


def get_suggested_fee(
    db: Session,
    job_type: str,
    period_end: Optional[date] = None,
    year: Optional[int] = None,
) -> Optional[float]:
    code = service_code_for_job_type(job_type)
    y = year if year is not None else fee_year_from_period_end(period_end)
    row = (
        db.query(ServiceFee)
        .filter(ServiceFee.service_code == code, ServiceFee.year == y)
        .first()
    )
    if row is not None:
        return float(row.fee or 0)
    return None


def seed_default_fees(db: Session) -> int:
    """
    Insert starter fee rows if the table is empty / missing those years.
    Accounts: 2025=2000, 2026=2250, 2027=2500
    Confirmation Statement: 50 each year 2025–2027
    """
    defaults = [
        (SERVICE_ACCOUNTS, "Accounts", 2025, 2000.0),
        (SERVICE_ACCOUNTS, "Accounts", 2026, 2250.0),
        (SERVICE_ACCOUNTS, "Accounts", 2027, 2500.0),
        (SERVICE_CS, "Confirmation Statement", 2025, 50.0),
        (SERVICE_CS, "Confirmation Statement", 2026, 50.0),
        (SERVICE_CS, "Confirmation Statement", 2027, 50.0),
    ]
    added = 0
    for code, name, year, fee in defaults:
        exists = (
            db.query(ServiceFee)
            .filter(ServiceFee.service_code == code, ServiceFee.year == year)
            .first()
        )
        if exists:
            continue
        db.add(
            ServiceFee(
                service_code=code,
                service_name=name,
                year=year,
                fee=fee,
                notes="Default schedule",
            )
        )
        added += 1
    if added:
        db.commit()
    return added
