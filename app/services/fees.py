"""Look up suggested service fees by type and year.

Accounts fees prefer the client's previous-year fee + 5% when available.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Job
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

# Uplift applied to prior-year fee when suggesting this year's fee
PRIOR_YEAR_UPLIFT = 0.05  # +5%

# Baseline only when no prior client fee and no schedule row exists
ACCOUNTS_BASELINE_FEE = 2000.0


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


def _round_fee(value: float) -> float:
    return round(float(value), 2)


def _schedule_fee(db: Session, code: str, year: int) -> Optional[float]:
    row = (
        db.query(ServiceFee)
        .filter(ServiceFee.service_code == code, ServiceFee.year == year)
        .first()
    )
    if row is not None:
        return float(row.fee or 0)
    return None


def get_prior_client_job_fee(
    db: Session,
    client_id: int,
    job_type: str,
    period_end: Optional[date] = None,
) -> Optional[float]:
    """
    Fee from the most recent earlier job of the same type for this client.

    Prefer previous calendar year of period_end; otherwise any earlier period_end.
    """
    if not client_id:
        return None

    code_type = (job_type or "").strip()
    q = (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == code_type,
            Job.fee.isnot(None),
            Job.fee > 0,
        )
    )
    if period_end is not None:
        # Prefer same client, earlier period end
        q = q.filter(Job.period_end.isnot(None), Job.period_end < period_end)
        # Prefer previous year first
        prior_year = period_end.year - 1
        same_year = (
            q.filter(Job.period_end >= date(prior_year, 1, 1))
            .filter(Job.period_end <= date(prior_year, 12, 31))
            .order_by(Job.period_end.desc())
            .first()
        )
        if same_year and same_year.fee:
            return float(same_year.fee)
    else:
        q = q.filter(Job.period_end.isnot(None))

    prior = q.order_by(Job.period_end.desc()).first()
    if prior and prior.fee:
        return float(prior.fee)
    return None


def get_suggested_fee(
    db: Session,
    job_type: str,
    period_end: Optional[date] = None,
    year: Optional[int] = None,
    client_id: Optional[int] = None,
) -> Optional[float]:
    """
    Suggested fee for a new/open job.

    Accounts (and other types with history):
      1. Client's previous-year job fee × 1.05
      2. Service fee schedule for the year
      3. Previous schedule year × 1.05
      4. Accounts only: baseline £2000 (then uplifted chain via seed)
    """
    code = service_code_for_job_type(job_type)
    y = year if year is not None else fee_year_from_period_end(period_end)

    # 1) Client-specific prior fee + 5%
    if client_id:
        prior = get_prior_client_job_fee(db, client_id, job_type, period_end)
        if prior is not None and prior > 0:
            return _round_fee(prior * (1.0 + PRIOR_YEAR_UPLIFT))

    # 2) Explicit schedule for this year
    scheduled = _schedule_fee(db, code, y)
    if scheduled is not None and scheduled > 0:
        return _round_fee(scheduled)

    # 3) Previous year schedule + 5%
    prev_scheduled = _schedule_fee(db, code, y - 1)
    if prev_scheduled is not None and prev_scheduled > 0:
        return _round_fee(prev_scheduled * (1.0 + PRIOR_YEAR_UPLIFT))

    # 4) Accounts baseline
    if code == SERVICE_ACCOUNTS:
        # Walk back from baseline year if needed: 2000 * 1.05^(y-2025)
        years_ahead = max(0, y - 2025)
        fee = ACCOUNTS_BASELINE_FEE
        for _ in range(years_ahead):
            fee *= 1.0 + PRIOR_YEAR_UPLIFT
        return _round_fee(fee)

    return None


def seed_default_fees(db: Session) -> int:
    """
    Insert starter fee rows if missing.

    Accounts: base 2025 = £2000, then each later year = prior × 1.05
    Confirmation Statement: £50 flat 2025–2027
    """
    accounts_2025 = ACCOUNTS_BASELINE_FEE
    accounts_2026 = _round_fee(accounts_2025 * (1.0 + PRIOR_YEAR_UPLIFT))
    accounts_2027 = _round_fee(accounts_2026 * (1.0 + PRIOR_YEAR_UPLIFT))

    defaults = [
        (SERVICE_ACCOUNTS, "Accounts", 2025, accounts_2025),
        (SERVICE_ACCOUNTS, "Accounts", 2026, accounts_2026),
        (SERVICE_ACCOUNTS, "Accounts", 2027, accounts_2027),
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
            # Keep existing user-edited schedule rows; do not overwrite
            continue
        db.add(
            ServiceFee(
                service_code=code,
                service_name=name,
                year=year,
                fee=fee,
                notes="Default schedule (accounts: prior year + 5%)",
            )
        )
        added += 1
    if added:
        db.commit()
    return added
