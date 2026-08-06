"""Completed jobs list for invoicing control."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models.job import Job


def week_bounds(today: Optional[date] = None) -> Tuple[date, date]:
    """Monday–Sunday of the current week (ISO)."""
    today = today or date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    return start, end


def period_bounds(
    period: str, today: Optional[date] = None
) -> Tuple[Optional[date], Optional[date], str]:
    """
    Return (from_date, to_date, label) for a named period.
    from/to inclusive. None means unbounded.
    """
    today = today or date.today()
    period = (period or "week").strip().lower()

    if period in ("week", "this_week", "this-week"):
        start, end = week_bounds(today)
        return start, end, f"This week ({start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')})"

    if period in ("last_week", "last-week"):
        start, end = week_bounds(today)
        start = start - timedelta(days=7)
        end = end - timedelta(days=7)
        return start, end, f"Last week ({start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')})"

    if period in ("month", "this_month", "this-month"):
        start = today.replace(day=1)
        if today.month == 12:
            end = date(today.year, 12, 31)
        else:
            end = date(today.year, today.month + 1, 1) - timedelta(days=1)
        return start, end, f"This month ({start.strftime('%b %Y')})"

    if period in ("last_month", "last-month"):
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return start, end, f"Last month ({start.strftime('%b %Y')})"

    if period in ("unbilled", "to_invoice", "ready"):
        return None, None, "Unbilled completed jobs"

    if period in ("all", ""):
        return None, None, "All completed jobs"

    # Fallback: treat as this week
    start, end = week_bounds(today)
    return start, end, f"This week ({start.strftime('%d/%m')} – {end.strftime('%d/%m/%Y')})"


def completion_date(job: Job) -> Optional[date]:
    """Date used for chronological ordering / period filter."""
    if job.actual_completion:
        return job.actual_completion
    if job.updated_at:
        try:
            return job.updated_at.date() if hasattr(job.updated_at, "date") else job.updated_at
        except Exception:
            return None
    return None


def is_completion_candidate(job: Job) -> bool:
    """
    A job belongs on the invoicing control list if it is marked Completed
    or has an actual completion date (users often fill the date without
    changing status). Cancelled jobs are never included.
    """
    status = (job.status or "").strip()
    if status == "Cancelled":
        return False
    if status == "Completed":
        return True
    if job.actual_completion is not None:
        return True
    return False


def repair_completion_status(db: Session) -> int:
    """
    Jobs with actual_completion set but still open → mark Completed.
    Does not create next-year recurrence (that stays on explicit job save).
    """
    openish = (
        db.query(Job)
        .filter(
            Job.actual_completion.isnot(None),
            Job.status.notin_(["Completed", "Cancelled"]),
        )
        .all()
    )
    n = 0
    for job in openish:
        job.status = "Completed"
        n += 1
    if n:
        db.commit()
    return n


def list_completed_jobs(
    db: Session,
    *,
    period: str = "week",
    today: Optional[date] = None,
    job_type: str = "",
    auto_repair: bool = True,
) -> Dict[str, Any]:
    """
    Completed jobs for invoicing control, chronological by completion date.
    Net value = job.fee (ex-VAT). Invoice number = invoice_reference.

    Includes status=Completed and any job with actual_completion set
    (so date-only completions still appear).
    """
    today = today or date.today()
    if auto_repair:
        repair_completion_status(db)

    from_d, to_d, label = period_bounds(period, today)
    unbilled_only = (period or "").lower() in ("unbilled", "to_invoice", "ready")

    q = db.query(Job).options(joinedload(Job.client)).filter(
        Job.status != "Cancelled"
    )
    if job_type == "Accounts":
        q = q.filter(Job.type == "Accounts")
    elif job_type in ("CS", "Confirmation Statement", "Confirmation+Statement"):
        q = q.filter(Job.type == "Confirmation Statement")

    jobs = q.all()

    rows: List[Dict[str, Any]] = []
    for j in jobs:
        if not is_completion_candidate(j):
            continue
        cd = completion_date(j)
        if unbilled_only:
            if (j.invoice_reference or "").strip():
                continue
            # Explicitly marked not to bill (retainer / included / waived)
            bill = (j.billing_status or "").strip().lower()
            if bill in (
                "retainer",
                "not_billable",
                "not billed",
                "included",
                "waived",
                "no_charge",
                "no charge",
                "do_not_bill",
                "invoiced",
                "paid",
            ):
                continue
        elif from_d is not None or to_d is not None:
            if not cd:
                continue
            if from_d and cd < from_d:
                continue
            if to_d and cd > to_d:
                continue

        net = float(j.fee or 0)
        inv = (j.invoice_reference or "").strip()
        bill = (j.billing_status or "").strip().lower()
        settled_no_inv = bill in (
            "retainer",
            "not_billable",
            "not billed",
            "included",
            "waived",
            "no_charge",
            "no charge",
            "do_not_bill",
            "invoiced",
            "paid",
        )
        rows.append(
            {
                "job": j,
                "completed_on": cd,
                "invoice_number": inv,
                "net_value": net,
                "gross_value": float(j.gross_amount) if j.gross_amount is not None else None,
                "is_billed": bool(inv) or settled_no_inv,
            }
        )

    # Chronological: oldest first (control list order)
    rows.sort(
        key=lambda r: (
            r["completed_on"] or date.min,
            r["job"].id or 0,
        )
    )

    total_net = round(sum(r["net_value"] for r in rows), 2)
    billed_net = round(sum(r["net_value"] for r in rows if r["is_billed"]), 2)
    unbilled_net = round(total_net - billed_net, 2)
    billed_count = sum(1 for r in rows if r["is_billed"])

    return {
        "rows": rows,
        "period": period or "week",
        "period_label": label,
        "from_date": from_d,
        "to_date": to_d,
        "today": today,
        "count": len(rows),
        "total_net": total_net,
        "billed_net": billed_net,
        "unbilled_net": unbilled_net,
        "billed_count": billed_count,
        "unbilled_count": len(rows) - billed_count,
        "job_type": job_type or "",
    }


def update_invoicing_fields(
    db: Session,
    updates: List[Dict[str, Any]],
) -> int:
    """
    Apply invoice_number + net_value for job ids.
    Each update: {job_id, invoice_number, net_value}.
    Returns number of jobs updated.
    """
    n = 0
    for u in updates:
        jid = u.get("job_id")
        if not jid:
            continue
        job = db.query(Job).filter(Job.id == int(jid)).first()
        if not job:
            continue
        inv = (u.get("invoice_number") or "").strip() or None
        net_raw = u.get("net_value")
        try:
            if net_raw is None or net_raw == "":
                net = job.fee
            else:
                net = float(str(net_raw).replace("£", "").replace(",", "").strip() or 0)
        except (TypeError, ValueError):
            net = job.fee

        changed = False
        if (job.invoice_reference or None) != inv:
            job.invoice_reference = inv
            changed = True
        if net is not None and float(job.fee or 0) != float(net):
            job.fee = float(net)
            changed = True
        if inv and (job.billing_status or "") not in ("invoiced", "paid"):
            job.billing_status = "invoiced"
            changed = True
        elif not inv and job.billing_status == "invoiced":
            job.billing_status = "unbilled"
            changed = True
        if changed:
            n += 1
    if n:
        db.commit()
    return n
