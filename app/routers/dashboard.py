"""Dashboard — headcount tiles + workload overview."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.config import CHASE_LIVE_MODE
from app.database import get_db
from app.models import Client, Job, Person
from app.services.grouping import count_groups
from app.services.practice_groups import count_practice_groups
from app.services.sales_ledger import chase_status_summary
from app.services.scrap_notes import pinned_for_live_tiles
from app.services.working_capital import compute_working_capital
from app.templating import render

router = APIRouter(tags=["dashboard"])

# Status mapping for practice headcount tiles
CLIENT_STATUSES = ("Active", "Former")
PROSPECT_STATUSES = ("Prospect",)
LOST_STATUSES = ("Inactive",)
GROUP_STATUSES = ("Active", "Former", "Prospect")

# Earliest year we have invoice / period-end history for
EARLIEST_INVOICE_YEAR = 2022


def _available_years(today: date, db: Session) -> List[int]:
    """
    Year tabs: EARLIEST_INVOICE_YEAR .. current year (inclusive), newest first.
    If data has an earlier min year, still floor at EARLIEST_INVOICE_YEAR.
    """
    end_y = today.year
    start_y = EARLIEST_INVOICE_YEAR
    # Optionally extend upward only — never before 2022 per product request
    return list(range(end_y, start_y - 1, -1))


def _parse_period(period: str, today: date) -> Tuple[str, Optional[int]]:
    """
    Returns (mode, year).
    mode = 'overall' | 'year'
    year = calendar year when mode == 'year'
    Accepts legacy this_year / last_year aliases.
    """
    raw = (period or "overall").strip().lower()
    if raw in ("", "overall", "all"):
        return "overall", None
    if raw == "this_year":
        return "year", today.year
    if raw == "last_year":
        return "year", today.year - 1
    if raw.isdigit():
        y = int(raw)
        if EARLIEST_INVOICE_YEAR <= y <= today.year + 1:
            return "year", y
    return "overall", None


def _year_datetime_bounds(year: int) -> Tuple[datetime, datetime]:
    return (
        datetime.combine(date(year, 1, 1), time.min),
        datetime.combine(date(year, 12, 31), time.max),
    )


def _year_date_bounds(year: int) -> Tuple[date, date]:
    return date(year, 1, 1), date(year, 12, 31)


def _count_clients_by_statuses(
    db: Session,
    statuses: Sequence[str],
    start: Optional[datetime],
    end: Optional[datetime],
) -> int:
    q = db.query(func.count(Client.id)).filter(
        Client.overall_status.in_(list(statuses))
    )
    if start is not None and end is not None:
        q = q.filter(Client.created_at >= start, Client.created_at <= end)
    return int(q.scalar() or 0)


def _is_billable_job(job: Job) -> bool:
    if job.fee and float(job.fee) > 0:
        return True
    if job.gross_amount is not None and float(job.gross_amount) > 0:
        return True
    if (job.invoice_reference or "").strip():
        return True
    return False


def _invoice_date(job: Job) -> Optional[date]:
    if job.period_end:
        return job.period_end
    if job.actual_completion:
        return job.actual_completion
    if job.created_at:
        if isinstance(job.created_at, datetime):
            return job.created_at.date()
        return job.created_at
    return None


def _client_invoice_bounds(
    db: Session,
) -> Dict[int, Tuple[Optional[date], Optional[date]]]:
    jobs = db.query(Job).filter(Job.client_id.isnot(None)).all()
    bounds: Dict[int, Tuple[Optional[date], Optional[date]]] = {}
    for job in jobs:
        if not _is_billable_job(job):
            continue
        inv = _invoice_date(job)
        if not inv or not job.client_id:
            continue
        cid = int(job.client_id)
        first, last = bounds.get(cid, (None, None))
        if first is None or inv < first:
            first = inv
        if last is None or inv > last:
            last = inv
        bounds[cid] = (first, last)
    return bounds


def _client_rows(db: Session) -> List[Tuple[int, str]]:
    """(client_id, overall_status) for non-prospect clients."""
    rows = db.query(Client.id, Client.overall_status).all()
    out: List[Tuple[int, str]] = []
    for cid, status in rows:
        st = (status or "Active").strip()
        if st == "Prospect":
            continue
        out.append((int(cid), st))
    return out


def _is_currently_lost(status: str) -> bool:
    return (status or "") in LOST_STATUSES


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _client_lifecycle_rows(
    db: Session,
) -> List[Tuple[int, str, Optional[date], Optional[date]]]:
    """
    (client_id, status, join_date, leave_date) for non-prospects.

    Join  = engagement_date if set, else first billable invoice date.
    Leave = disengagement_date if set, else last invoice when status is lost.
    Explicit engagement / disengagement override invoice proxies as soon as filled.
    """
    inv_bounds = _client_invoice_bounds(db)
    rows = db.query(
        Client.id,
        Client.overall_status,
        Client.engagement_date,
        Client.disengagement_date,
    ).all()
    out: List[Tuple[int, str, Optional[date], Optional[date]]] = []
    for cid, status, eng, dis in rows:
        st = (status or "Active").strip()
        if st == "Prospect":
            continue
        inv_first, inv_last = inv_bounds.get(int(cid), (None, None))
        join = _as_date(eng) or inv_first
        if _as_date(dis) is not None:
            leave = _as_date(dis)
        elif _is_currently_lost(st):
            leave = inv_last
        else:
            leave = None
        out.append((int(cid), st, join, leave))
    return out


def _on_books_at_fixed(
    client_id: int,
    status: str,
    first: Optional[date],
    last: Optional[date],
    as_of: date,
) -> bool:
    """
    On the book at end of day `as_of`?

    Join = engagement (or first invoice). Leave = disengagement (or last invoice
    if lost). Off books from leave date inclusive.
    """
    if first is None or first > as_of:
        return False
    if last is not None and as_of >= last:
        return False
    return True


def _count_on_books(db: Session, as_of: date) -> int:
    n = 0
    for cid, status, join, leave in _client_lifecycle_rows(db):
        if _on_books_at_fixed(cid, status, join, leave, as_of):
            n += 1
    return n


def _count_new_in_year(db: Session, year: int) -> int:
    """New clients = join date (engagement or first invoice) in calendar year."""
    d0, d1 = _year_date_bounds(year)
    return sum(
        1
        for _cid, _st, join, _leave in _client_lifecycle_rows(db)
        if join is not None and d0 <= join <= d1
    )


def _count_lost_in_year(db: Session, year: int) -> int:
    """
    Lost in year = leave date falls in that year
    (disengagement_date when set, else last invoice for Inactive clients).
    """
    d0, d1 = _year_date_bounds(year)
    return sum(
        1
        for _cid, _st, _join, leave in _client_lifecycle_rows(db)
        if leave is not None and d0 <= leave <= d1
    )


def _on_books_client_ids(db: Session, as_of: date) -> List[int]:
    """Client ids on the book at as_of (for Groups point-in-time)."""
    ids: List[int] = []
    for cid, status, join, leave in _client_lifecycle_rows(db):
        if _on_books_at_fixed(cid, status, join, leave, as_of):
            ids.append(cid)
    return ids


def _practice_book_metrics(
    db: Session,
    mode: str,
    year: Optional[int],
    today: date,
) -> Tuple[int, int, int, int, Optional[int], int]:
    """
    Returns:
      total_groups, total_clients (closing), total_new, total_lost,
      opening_clients, closing_clients
    """
    life = _client_lifecycle_rows(db)

    if mode == "overall" or year is None:
        # New = ever joined; Lost = ever left; Clients = New − Lost
        new_all = 0
        lost_all = 0
        for _cid, status, join, leave in life:
            if join is None:
                continue
            new_all += 1
            if leave is not None or _is_currently_lost(status):
                # Prefer leave date; still count currently lost without leave
                lost_all += 1
        closing = new_all - lost_all
        on_books_ids = _on_books_client_ids(db, today)
        prospect_ids = [
            int(r[0])
            for r in db.query(Client.id)
            .filter(Client.overall_status == "Prospect")
            .all()
        ]
        group_ids = list(set(on_books_ids) | set(prospect_ids))
        groups = int(count_groups(db, client_ids=group_ids)) if group_ids else 0
        return groups, closing, new_all, lost_all, None, closing

    # Year view: stock roll-forward
    y = year
    d_close = date(y, 12, 31)
    opening = _count_on_books(db, date(y - 1, 12, 31))
    new_y = _count_new_in_year(db, y)
    lost_y = _count_lost_in_year(db, y)
    closing = opening + new_y - lost_y

    on_books_eoy = _on_books_client_ids(db, d_close)
    groups = int(count_groups(db, client_ids=on_books_eoy)) if on_books_eoy else 0

    return groups, closing, new_y, lost_y, opening, closing


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    period: str = Query("overall"),
):
    today = date.today()
    soon = today + timedelta(days=30)
    mode, year = _parse_period(period, today)
    years = _available_years(today, db)

    if mode == "year" and year is not None:
        period_key = str(year)
        year_label = str(year)
        start, end = _year_datetime_bounds(year)
    else:
        mode = "overall"
        year = None
        period_key = "overall"
        year_label = "Overall"
        start, end = None, None

    # —— Practice book (invoice stock roll-forward) ——
    # Year Y: Opening(Y) = Closing(Y-1)
    #         Closing(Y) = Opening(Y) + New(Y) − Lost(Y)
    # Groups: point-in-time at year end (or today for Overall)
    (
        total_groups,
        total_clients,
        total_new_clients,
        total_lost,
        opening_clients,
        closing_clients,
    ) = _practice_book_metrics(db, mode, year, today)

    # Prefer persisted practice groups (editable board) for the Groups tile
    try:
        total_groups = count_practice_groups(db)
    except Exception:
        pass

    total_prospects = int(
        _count_clients_by_statuses(db, PROSPECT_STATUSES, None, None)
    )

    try:
        live_notes = pinned_for_live_tiles(db, limit=6)
    except Exception:
        live_notes = []

    # —— Workload (current live view) ——
    people_count = int(db.query(func.count(Person.id)).scalar() or 0)
    jobs_count = int(db.query(func.count(Job.id)).scalar() or 0)
    individual_clients = int(
        db.query(func.count(Person.id))
        .filter(Person.is_individual_client.is_(True))
        .scalar()
        or 0
    )
    company_clients = int(
        db.query(func.count(Client.id))
        .filter(
            Client.overall_status == "Active",
            Client.client_type != "Individual",
        )
        .scalar()
        or 0
    )
    active_clients = int(
        db.query(func.count(Client.id))
        .filter(Client.overall_status == "Active")
        .scalar()
        or 0
    )

    open_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
        .all()
    )
    live_jobs = [
        j
        for j in open_jobs
        if not j.client or (j.client.overall_status or "") != "Inactive"
    ]
    total_fees = sum(j.fee or 0 for j in live_jobs)
    overdue_jobs = [
        j
        for j in live_jobs
        if j.statutory_due_date and j.statutory_due_date < today
    ]
    due_soon = [
        j
        for j in live_jobs
        if j.statutory_due_date and today <= j.statutory_due_date <= soon
    ]
    accounts_open = len([j for j in live_jobs if j.type == "Accounts"])
    cs_open = len(
        [j for j in live_jobs if j.type == "Confirmation Statement"]
    )

    recent_clients = (
        db.query(Client)
        .filter(
            (Client.overall_status.is_(None))
            | (Client.overall_status != "Inactive")
        )
        .order_by(Client.id.desc())
        .limit(8)
        .all()
    )
    upcoming = sorted(
        [j for j in live_jobs if j.statutory_due_date],
        key=lambda j: j.statutory_due_date,
    )[:10]

    # —— Working capital cycle (primary dashboard panel) ——
    wc = compute_working_capital(db, today)
    chase_sum = chase_status_summary(db, today)

    return render(
        request,
        "dashboard.html",
        {
            "hide_nav": True,
            "period": period_key,
            "year_label": year_label,
            "years": years,
            "earliest_year": EARLIEST_INVOICE_YEAR,
            "total_groups": total_groups,
            "total_clients": total_clients,
            "total_new_clients": total_new_clients,
            "total_prospects": total_prospects,
            "total_lost": total_lost,
            "opening_clients": opening_clients,
            "closing_clients": closing_clients,
            "active_clients": active_clients,
            "company_clients": company_clients,
            "individual_clients": individual_clients,
            "people_count": people_count,
            "jobs_count": jobs_count,
            "total_fees": round(total_fees, 2),
            "overdue_count": len(overdue_jobs),
            "due_soon_count": len(due_soon),
            "accounts_open": accounts_open,
            "cs_open": cs_open,
            "recent_clients": recent_clients,
            "upcoming_jobs": upcoming,
            "today": today,
            # Working capital
            "wc_net": wc.net,
            "wc_wip_value": wc.wip.value,
            "wc_wip_count": wc.wip.count,
            "wc_wip_ageing": wc.wip.ageing,
            "wc_debtors_total": wc.debtors.total,
            "wc_debtors_count": wc.debtors.count,
            "wc_debtors_ageing": wc.debtors.ageing,
            "wc_cash_balance": wc.cash.balance,
            "wc_cash_name": wc.cash.account_name,
            "wc_cash_recent": wc.cash.recent,
            "wc_cash_txn_count": wc.cash.txn_count,
            "wc_creditors_total": wc.creditors.total,
            "wc_creditors_supplier": wc.creditors.supplier_total,
            "wc_creditors_vat": wc.creditors.vat_total,
            "wc_creditors_count": wc.creditors.count,
            "wc_creditors_ageing": wc.creditors.ageing,
            # Debt chase (Working Capital · Debtors)
            "chase_pipeline_count": chase_sum.get("pipeline_count", 0),
            "chase_pipeline_amount": chase_sum.get("pipeline_amount", 0),
            "chase_by_stage": chase_sum.get("by_stage", {}),
            "chase_actions_week": chase_sum.get("actions_this_week", 0),
            "chase_live": CHASE_LIVE_MODE,
            "live_notes": live_notes,
        },
    )
