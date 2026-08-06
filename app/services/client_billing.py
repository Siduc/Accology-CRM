"""Resolve per-client job fee and Done-billing patterns."""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import Job
from app.models.client_billing import PATTERN_ON_DONE, ClientJobPattern
from app.services.dates import JOB_TYPES


def normalise_job_type(job_type: str) -> str:
    t = (job_type or "").strip()
    if not t:
        return ""
    # Align common aliases with catalogue / jobs
    low = t.lower()
    if "vat" in low or low == "vat":
        return "VAT Return"
    if "confirmation" in low:
        return "Confirmation Statement"
    if "self assessment" in low or low in ("sa", "sar"):
        return "Self Assessment"
    if "corporation" in low or low == "ct":
        return "Corporation Tax"
    if "accounts" in low:
        return "Accounts"
    if "payroll" in low:
        return "Payroll"
    if "bookkeep" in low:
        return "Bookkeeping"
    return t


def get_client_job_pattern(
    db: Session,
    client_id: int,
    job_type: str,
    *,
    active_only: bool = True,
) -> Optional[ClientJobPattern]:
    if not client_id:
        return None
    jt = normalise_job_type(job_type)
    if not jt:
        return None
    q = db.query(ClientJobPattern).filter(
        ClientJobPattern.client_id == client_id,
        ClientJobPattern.job_type == jt,
    )
    if active_only:
        q = q.filter(ClientJobPattern.is_active.is_(True))
    return q.first()


def list_client_job_patterns(db: Session, client_id: int) -> List[ClientJobPattern]:
    return (
        db.query(ClientJobPattern)
        .filter(ClientJobPattern.client_id == client_id)
        .order_by(ClientJobPattern.job_type)
        .all()
    )


def pattern_fixed_fee(
    db: Session, client_id: Optional[int], job_type: str
) -> Optional[float]:
    """
    Return fee if client has an active pattern with a fixed fee (incl. £0).
    Return None if no pattern or pattern uses standard schedule (fee is null).
    """
    if not client_id:
        return None
    pat = get_client_job_pattern(db, client_id, job_type)
    if not pat:
        return None
    if pat.fee is None:
        return None
    return round(float(pat.fee), 2)


def pattern_on_done(
    db: Session, client_id: Optional[int], job_type: str
) -> Optional[str]:
    """Return draft|sent|none if pattern sets Done behaviour; else None."""
    if not client_id:
        return None
    pat = get_client_job_pattern(db, client_id, job_type)
    if not pat:
        return None
    v = (pat.on_done or "default").strip().lower()
    if v in ("draft", "sent", "none"):
        return v
    return None


def upsert_client_job_pattern(
    db: Session,
    client_id: int,
    job_type: str,
    *,
    fee: Optional[float] = None,
    fee_blank: bool = False,
    on_done: str = "default",
    notes: Optional[str] = None,
    is_active: bool = True,
    commit: bool = False,
) -> ClientJobPattern:
    """
    Create or update a pattern.

    fee_blank=True → store fee=None (use standard schedule).
    fee=0 → free / covered by retainer.
    """
    jt = normalise_job_type(job_type)
    if not jt:
        raise ValueError("Job type is required")
    od = (on_done or "default").strip().lower()
    if od not in PATTERN_ON_DONE:
        od = "default"

    row = (
        db.query(ClientJobPattern)
        .filter(
            ClientJobPattern.client_id == client_id,
            ClientJobPattern.job_type == jt,
        )
        .first()
    )
    if not row:
        row = ClientJobPattern(client_id=client_id, job_type=jt)
        db.add(row)

    if fee_blank:
        row.fee = None
    elif fee is not None:
        row.fee = round(float(fee), 2)
    # Only change on_done when caller passes a real choice (not "default" on update
    # that means "leave unchanged") — use force via explicit value.
    if od != "default" or row.id is None or not (row.on_done or "").strip():
        row.on_done = od
    elif od == "default" and not row.on_done:
        row.on_done = "default"
    if notes is not None:
        row.notes = (notes or "").strip() or None
    row.is_active = bool(is_active)
    row.updated_at = datetime.utcnow()
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return row


def remember_from_job(
    db: Session,
    *,
    client_id: int,
    job_type: str,
    fee: Optional[float],
    on_done: str,
    commit: bool = False,
) -> Optional[ClientJobPattern]:
    """Persist pattern from a completed job (optional 'remember' on Done)."""
    if not client_id or not job_type:
        return None
    od = (on_done or "default").strip().lower()
    if od not in ("draft", "sent", "none"):
        od = "default"
    return upsert_client_job_pattern(
        db,
        client_id,
        job_type,
        fee=float(fee) if fee is not None else 0.0,
        fee_blank=False,
        on_done=od if od != "default" else "none",
        notes="Set from job completion",
        is_active=True,
        commit=commit,
    )


def apply_pattern_fee_to_open_jobs(
    db: Session,
    client_id: int,
    job_type: str,
    fee: float,
    *,
    from_period_end: Optional[date] = None,
    include_current: bool = True,
) -> int:
    """
    Set fee on open (non-completed/cancelled) jobs of this type for the client.

    from_period_end: if set, only jobs with period_end >= that date (or null PE).
    Returns count updated.
    """
    jt = normalise_job_type(job_type)
    if not client_id or not jt:
        return 0
    q = (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            Job.type == jt,
            Job.status.notin_(["Completed", "Cancelled"]),
        )
    )
    if from_period_end is not None:
        if include_current:
            q = q.filter(
                (Job.period_end.is_(None)) | (Job.period_end >= from_period_end)
            )
        else:
            q = q.filter(
                (Job.period_end.is_(None)) | (Job.period_end > from_period_end)
            )
    n = 0
    target = round(float(fee), 2)
    for job in q.all():
        if float(job.fee or 0) != target:
            job.fee = target
            job.updated_at = datetime.utcnow()
            n += 1
    return n


def apply_saved_pattern_to_open_jobs(
    db: Session, pattern: ClientJobPattern
) -> int:
    """If pattern has a fixed fee (incl. £0), push it to open jobs of that type."""
    if pattern is None or not pattern.is_active:
        return 0
    if pattern.fee is None:
        return 0
    return apply_pattern_fee_to_open_jobs(
        db,
        int(pattern.client_id),
        pattern.job_type or "",
        float(pattern.fee),
        from_period_end=None,
        include_current=True,
    )


def pattern_types_for_ui() -> List[str]:
    """Job types offered on the client billing form."""
    base = list(JOB_TYPES)
    extras = ["Bookkeeping"]
    for e in extras:
        if e not in base:
            base.append(e)
    return base


def parse_fee_form(raw: str) -> Tuple[Optional[float], bool]:
    """
    Parse fee input from form.
    Returns (fee, fee_blank).
    blank / 'standard' → (None, True)
    '0' / '0.00' → (0.0, False)
    """
    s = (raw or "").strip().lower().replace("£", "").replace(",", "")
    if s in ("", "standard", "std", "default", "-"):
        return None, True
    try:
        return float(s), False
    except ValueError:
        return None, True
