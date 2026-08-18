"""Create VAT Return jobs from a client's VAT frequency / stagger."""

from __future__ import annotations

import logging
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.models import Client, Job
from app.services.dates import calculate_dates
from app.services.fees import get_suggested_fee
from app.services.sales_ledger import (
    normalise_quarterly_pattern,
    normalise_service_recurrence,
    quarterly_period_end_months,
    seed_services,
)

logger = logging.getLogger("accountant_crm.vat_jobs")

# Bump when create semantics change — appears in logs so we know which code ran
VAT_CREATE_VERSION = "single-current-v3"

JOB_TYPE = "VAT Return"
# Prior imports / older rows sometimes store type as plain "VAT"
VAT_JOB_TYPES = ("VAT Return", "VAT")


def _vat_type_filter():
    return Job.type.in_(VAT_JOB_TYPES)


@dataclass
class VatJobsResult:
    created: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    created_jobs: List[str] = field(default_factory=list)
    skipped_jobs: List[str] = field(default_factory=list)
    period_ends: List[date] = field(default_factory=list)


def normalise_client_vat_frequency(raw: Optional[str]) -> str:
    return normalise_service_recurrence(raw)


def normalise_client_vat_pattern(
    raw: Optional[str], *, frequency: str = ""
) -> Optional[str]:
    return normalise_quarterly_pattern(raw, recurrence=frequency)


def normalise_year_end_month(raw) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        m = int(raw)
    except (TypeError, ValueError):
        return None
    if 1 <= m <= 12:
        return m
    return None


def _last_day(year: int, month: int) -> date:
    return date(year, month, monthrange(year, month)[1])


def _month_add(d: date, months: int) -> date:
    """Add months keeping day as last day of month when d is month-end-ish."""
    y = d.year
    m = d.month + months
    while m > 12:
        m -= 12
        y += 1
    while m < 1:
        m += 12
        y -= 1
    return _last_day(y, m)


def next_vat_period_end(
    after: date,
    frequency: str,
    *,
    pattern: Optional[str] = None,
    year_end_month: Optional[int] = None,
) -> Optional[date]:
    """First period end strictly after `after` for this scheme."""
    freq = normalise_client_vat_frequency(frequency)
    if freq in ("none", ""):
        return None
    if freq == "monthly":
        return _month_add(after, 1)
    if freq == "quarterly":
        months = list(quarterly_period_end_months(pattern or "stagger_1"))
        # Walk forward month by month until we land on a pattern month-end after `after`
        y, m = after.year, after.month
        for _ in range(24):
            m += 1
            if m > 12:
                m = 1
                y += 1
            if m in months:
                pe = _last_day(y, m)
                if pe > after:
                    return pe
        return None
    if freq in ("annually", "annual"):
        ye_m = year_end_month
        if not ye_m and pattern:
            ye_m = quarterly_period_end_months(pattern)[0]
        ye_m = int(ye_m or 3)
        pe = _last_day(after.year, ye_m)
        if pe > after:
            return pe
        return _last_day(after.year + 1, ye_m)
    return None


def planned_vat_period_ends(
    frequency: str,
    *,
    pattern: Optional[str] = None,
    year_end_month: Optional[int] = None,
    today: Optional[date] = None,
    lookback_days: int = 120,
    horizon_days: int = 400,
) -> List[date]:
    """
    Period ends to create jobs for.

    Quarterly: matching stagger months in [today-lookback, today+horizon].
    Monthly: month-ends in that window (typically ~4 past + ~13 forward).
    Annually: year-end month in window (current/next YE).
    """
    today = today or date.today()
    freq = normalise_client_vat_frequency(frequency)
    if freq in ("none", ""):
        return []

    start = today - timedelta(days=lookback_days)
    end = today + timedelta(days=horizon_days)
    ends: List[date] = []

    if freq == "monthly":
        # Walk month-ends from start's month through end's month
        y, m = start.year, start.month
        while True:
            pe = _last_day(y, m)
            if pe > end:
                break
            if pe >= start:
                ends.append(pe)
            m += 1
            if m > 12:
                m = 1
                y += 1
        return ends

    if freq == "quarterly":
        months = quarterly_period_end_months(pattern or "stagger_1")
        # Cover years spanning the window
        for y in range(start.year - 1, end.year + 2):
            for mon in months:
                pe = _last_day(y, mon)
                if start <= pe <= end:
                    ends.append(pe)
        return sorted(set(ends))

    if freq in ("annually", "annual"):
        ye_m = year_end_month
        if not ye_m and pattern:
            # Fall back to first month of pattern (e.g. Mar for stagger_1)
            ye_m = quarterly_period_end_months(pattern)[0]
        ye_m = ye_m or 3
        for y in range(start.year - 1, end.year + 2):
            pe = _last_day(y, int(ye_m))
            if start <= pe <= end:
                ends.append(pe)
        return sorted(set(ends))

    return []


def _existing_vat_job(
    db: Session, client_id: int, period_end: date
) -> Optional[Job]:
    return (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            _vat_type_filter(),
            Job.period_end == period_end,
            Job.status != "Cancelled",
        )
        .order_by(Job.id.asc())
        .first()
    )


def _resolve_vat_job_fee(db: Session, client: Client, pe: date) -> float:
    """Client pattern (incl. £0) wins; never treat 0 as missing."""
    suggested = get_suggested_fee(db, JOB_TYPE, pe, client_id=client.id)
    if suggested is not None:
        return float(suggested)
    try:
        from app.models.sales import Service

        svc = db.query(Service).filter(Service.code == "VAT").first()
        if svc and svc.default_fee is not None:
            return float(svc.default_fee or 0)
    except Exception:
        pass
    return 0.0


def _completed_vat_for_period(
    db: Session, client_id: int, period_end: date
) -> Optional[Job]:
    return (
        db.query(Job)
        .filter(
            Job.client_id == client_id,
            _vat_type_filter(),
            Job.period_end == period_end,
            Job.status == "Completed",
        )
        .order_by(Job.id.asc())
        .first()
    )


def current_open_vat_period_end(
    client: Client,
    db: Session,
    *,
    today: Optional[date] = None,
) -> Optional[date]:
    """
    The single VAT period that should have an open job.

    Never returns a PE that already has a Completed job.
    Prefers the earliest open job that is not a duplicate of history;
    otherwise the next scheme PE after the latest completed (or due now).
    """
    today = today or date.today()

    freq = normalise_client_vat_frequency(getattr(client, "vat_frequency", None))
    if freq in ("none", ""):
        return None
    pattern = normalise_client_vat_pattern(
        getattr(client, "vat_quarterly_pattern", None), frequency=freq
    )
    ye_m = normalise_year_end_month(getattr(client, "vat_year_end_month", None))
    if freq == "monthly":
        lookback, horizon = 100, 45
    elif freq in ("annually", "annual"):
        lookback, horizon = 400, 45
    else:
        # Current quarter only — not a year of periods
        lookback, horizon = 120, 45

    ends = planned_vat_period_ends(
        freq,
        pattern=pattern,
        year_end_month=ye_m,
        today=today,
        lookback_days=lookback,
        horizon_days=horizon,
    )
    # Latest PE already due and not completed = the return you should be filing
    due = [
        pe
        for pe in ends
        if pe <= today and not _completed_vat_for_period(db, client.id, pe)
    ]
    if due:
        return due[-1]
    upcoming = [
        pe
        for pe in ends
        if pe > today and not _completed_vat_for_period(db, client.id, pe)
    ]
    if upcoming:
        return upcoming[0]
    nxt = next_vat_period_end(
        today,
        freq,
        pattern=pattern,
        year_end_month=ye_m,
    )
    if nxt and not _completed_vat_for_period(db, client.id, nxt):
        return nxt
    return None


def prune_future_vat_jobs(
    db: Session,
    client: Client,
    *,
    keep_period_end: Optional[date] = None,
    today: Optional[date] = None,
) -> int:
    """
    Cancel extra Planned VAT jobs beyond the current open period.

    Also cancels open jobs that duplicate a Completed PE for the same period.
    Later periods are recreated when the current job is marked Done.
    """
    today = today or date.today()
    keep = keep_period_end or current_open_vat_period_end(client, db, today=today)
    open_jobs = (
        db.query(Job)
        .filter(
            Job.client_id == client.id,
            _vat_type_filter(),
            Job.status.notin_(["Completed", "Cancelled"]),
        )
        .order_by(Job.period_end.asc(), Job.id.asc())
        .all()
    )
    if not open_jobs:
        return 0

    keep_job = None
    if keep is not None:
        for job in open_jobs:
            if job.period_end == keep and not _completed_vat_for_period(
                db, client.id, keep
            ):
                keep_job = job
                break
    if keep_job is None:
        for job in open_jobs:
            if job.period_end and not _completed_vat_for_period(
                db, client.id, job.period_end
            ):
                keep_job = job
                break

    cancelled = 0
    tag = (
        "Superseded: only the current VAT period stays open; "
        "the next period is created when that job is Done."
    )
    for job in open_jobs:
        if keep_job is not None and job.id == keep_job.id:
            continue
        # Drop duplicates of completed periods and any extra future opens
        job.status = "Cancelled"
        note = (job.notes or "").strip()
        job.notes = f"{note} {tag}".strip() if note else tag
        job.updated_at = datetime.utcnow()
        cancelled += 1
    return cancelled


def _force_single_open_vat(
    db: Session,
    client: Client,
    *,
    keep_period_end: Optional[date],
    today: Optional[date] = None,
) -> List[str]:
    """Cancel every open VAT job except one for keep_period_end (or best current)."""
    today = today or date.today()
    keep = keep_period_end or current_open_vat_period_end(client, db, today=today)
    open_jobs = (
        db.query(Job)
        .filter(
            Job.client_id == client.id,
            _vat_type_filter(),
            Job.status.notin_(["Completed", "Cancelled"]),
        )
        .order_by(Job.period_end.asc(), Job.id.asc())
        .all()
    )
    if not open_jobs:
        return []
    keep_job = None
    if keep is not None:
        for j in open_jobs:
            if j.period_end == keep:
                keep_job = j
                break
    if keep_job is None:
        # Prefer latest PE <= today among open, else earliest open
        due = [j for j in open_jobs if j.period_end and j.period_end <= today]
        keep_job = due[-1] if due else open_jobs[0]

    msgs: List[str] = []
    for j in open_jobs:
        if j.id == keep_job.id:
            continue
        j.status = "Cancelled"
        # Free import_key so a later Done/recurrence can recreate this period
        j.import_key = None
        j.notes = (
            ((j.notes or "").strip() + " ")
            + f"[{VAT_CREATE_VERSION}] Surplus open VAT cancelled — only current period kept."
        ).strip()
        j.updated_at = datetime.utcnow()
        msgs.append(f"Cancelled surplus VAT #{j.id} PE={j.period_end}")
    if msgs:
        db.flush()  # ensure count()/next query sees Cancelled in this session
    return msgs


def create_vat_jobs_for_client(
    db: Session,
    client: Client,
    *,
    today: Optional[date] = None,
    commit: bool = False,
    prune_future: bool = True,
) -> VatJobsResult:
    """
    Ensure exactly one open Planned VAT job — the current period only.

    HARD RULE: creates at most ONE job. Later periods appear only on Done.
    Always force-prunes any surplus open VAT jobs at the end.
    """
    result = VatJobsResult()
    today = today or date.today()
    logger.info(
        "VAT create %s client_id=%s create_v=%s",
        VAT_CREATE_VERSION,
        getattr(client, "id", None),
        VAT_CREATE_VERSION,
    )
    freq = normalise_client_vat_frequency(getattr(client, "vat_frequency", None))
    if freq in ("none", ""):
        result.errors.append("VAT frequency not set")
        return result

    if freq == "quarterly" and not (client.vat_quarterly_pattern or "").strip():
        result.errors.append("Choose a quarterly pattern (stagger) for this client")
        return result

    pattern = normalise_client_vat_pattern(
        getattr(client, "vat_quarterly_pattern", None), frequency=freq
    )
    ye_m = normalise_year_end_month(getattr(client, "vat_year_end_month", None))

    pe = current_open_vat_period_end(client, db, today=today)
    if pe and _completed_vat_for_period(db, client.id, pe):
        pe = next_vat_period_end(pe, freq, pattern=pattern, year_end_month=ye_m)
        if pe and _completed_vat_for_period(db, client.id, pe):
            pe = None

    result.period_ends = [pe] if pe else []

    try:
        seed_services(db)
    except Exception:
        pass

    if pe:
        ikey = f"vat:{client.id}:{pe.isoformat()}"
        existing = (
            db.query(Job)
            .filter(
                Job.client_id == client.id,
                _vat_type_filter(),
                Job.period_end == pe,
                Job.status.notin_(["Completed", "Cancelled"]),
            )
            .order_by(Job.id.asc())
            .first()
        )
        if existing:
            result.skipped += 1
            result.skipped_jobs.append(
                f"VAT {pe.isoformat()} (job #{existing.id}, {existing.status})"
            )
            fee = _resolve_vat_job_fee(db, client, pe)
            if float(existing.fee or 0) != float(fee):
                existing.fee = fee
                existing.updated_at = datetime.utcnow()
        else:
            held = db.query(Job).filter(Job.import_key == ikey).first()
            if held and (held.status or "") == "Cancelled":
                held.import_key = None

            statutory, t_start, t_comp = calculate_dates(JOB_TYPE, pe)
            fee = _resolve_vat_job_fee(db, client, pe)
            from app.services.dates import uk_date

            title = f"{JOB_TYPE} — {uk_date(pe)}"
            # SINGLE job only — never loop periods
            job = Job(
                title=title,
                type=JOB_TYPE,
                client_id=client.id,
                period_end=pe,
                statutory_due_date=statutory,
                target_start=t_start,
                target_completion=t_comp,
                fee=fee,
                status="Planned",
                is_recurring="Yes",
                notes=(
                    f"[{VAT_CREATE_VERSION}] Current VAT only ({freq}"
                    + (f", {pattern}" if pattern else "")
                    + "). Next period opens when this job is Done."
                ),
                source="vat_scheme",
                import_key=ikey,
            )
            db.add(job)
            db.flush()
            result.created = 1  # hard-set, never increment in a loop
            result.created_jobs.append(
                f"{title} · due {uk_date(statutory, empty='—')} · £{fee:.2f}"
            )
            logger.info(
                "VAT single-create client=%s pe=%s job_id=%s",
                client.id,
                pe,
                job.id,
            )
    else:
        result.errors.append("No open VAT period to create")

    # ALWAYS leave at most one open VAT job
    if prune_future:
        result.skipped_jobs.extend(
            _force_single_open_vat(db, client, keep_period_end=pe, today=today)
        )

    db.flush()
    open_left = (
        db.query(Job)
        .filter(
            Job.client_id == client.id,
            _vat_type_filter(),
            Job.status.notin_(["Completed", "Cancelled"]),
        )
        .all()
    )
    if len(open_left) > 1:
        logger.error(
            "VAT create left %s open jobs for client %s — force prune again",
            len(open_left),
            client.id,
        )
        result.skipped_jobs.extend(
            _force_single_open_vat(db, client, keep_period_end=pe, today=today)
        )
        db.flush()

    if commit:
        db.commit()
    return result


def apply_vat_scheme_and_jobs(
    db: Session,
    client: Client,
    *,
    frequency: str,
    pattern: str = "",
    year_end_month=None,
    create_jobs: bool = True,
    commit: bool = False,
) -> VatJobsResult:
    """Persist scheme fields on client and optionally create the current VAT job only."""
    freq = normalise_client_vat_frequency(frequency)
    client.vat_frequency = freq if freq != "none" else None
    if freq == "quarterly":
        client.vat_quarterly_pattern = normalise_client_vat_pattern(
            pattern, frequency=freq
        )
    elif freq in ("annually", "annual"):
        client.vat_year_end_month = normalise_year_end_month(year_end_month)
        client.vat_quarterly_pattern = normalise_client_vat_pattern(
            pattern, frequency=freq
        )
        if not client.vat_year_end_month and client.vat_quarterly_pattern:
            client.vat_year_end_month = quarterly_period_end_months(
                client.vat_quarterly_pattern
            )[0]
    else:
        if freq == "monthly":
            client.vat_quarterly_pattern = None
        else:
            client.vat_quarterly_pattern = None
            client.vat_year_end_month = None

    if not create_jobs or freq in ("none", ""):
        # Still prune surplus if scheme cleared or jobs not requested but open clutter exists
        if freq not in ("none", "") and getattr(client, "id", None):
            _force_single_open_vat(db, client, keep_period_end=None)
        return VatJobsResult()

    result = create_vat_jobs_for_client(db, client, commit=False, prune_future=True)
    # Absolute guarantee after any scheme save
    extra = _force_single_open_vat(
        db,
        client,
        keep_period_end=result.period_ends[0] if result.period_ends else None,
    )
    result.skipped_jobs.extend(extra)
    logger.info(
        "VAT scheme apply client=%s created=%s open_msgs=%s v=%s",
        client.id,
        result.created,
        len(result.skipped_jobs),
        VAT_CREATE_VERSION,
    )
    if commit:
        db.commit()
    return result
