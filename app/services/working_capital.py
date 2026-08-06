"""Working capital metrics for the Accologise practice dashboard."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models import Client, Job
from app.models.finance import BankAccount, BankTransaction, CreditorBill

PAID_BILLING = {
    "paid",
    "written off",
    "written-off",
    "waived",
    "cancelled",
}


@dataclass
class AgeBucket:
    label: str
    count: int = 0
    amount: float = 0.0


@dataclass
class WipSnapshot:
    value: float  # total WIP: jobs + retainers + open task fees
    count: int  # open jobs count
    ageing: List[AgeBucket] = field(default_factory=list)
    jobs: List[Job] = field(default_factory=list)
    # Breakdown
    jobs_value: float = 0.0  # per-job fees only (non-retainer clients)
    retainer_count: int = 0
    retainer_monthly: float = 0.0
    retainer_annual: float = 0.0  # included in value
    retainer_job_count: int = 0
    tasks_value: float = 0.0  # open practice task fees (excl. Development / hold)
    tasks_count: int = 0


@dataclass
class WipHorizonBucket:
    """Forward-looking WIP bucket (count + fee total)."""

    key: str
    label: str
    count: int = 0
    amount: float = 0.0
    from_date: Optional[date] = None
    to_date: Optional[date] = None


@dataclass
class WipTypeHorizon:
    """One row of horizon tiles for a job type (Accounts or CS)."""

    job_type: str
    label: str
    buckets: List[WipHorizonBucket] = field(default_factory=list)
    total_count: int = 0
    total_amount: float = 0.0


@dataclass
class DebtorsSnapshot:
    total: float
    count: int
    ageing: List[AgeBucket] = field(default_factory=list)
    jobs: List[Job] = field(default_factory=list)


@dataclass
class CashSnapshot:
    balance: float
    account_id: Optional[int]
    account_name: str
    recent: List[BankTransaction] = field(default_factory=list)
    txn_count: int = 0


@dataclass
class CreditorsSnapshot:
    total: float
    supplier_total: float
    vat_total: float
    count: int
    ageing: List[AgeBucket] = field(default_factory=list)
    bills: List[CreditorBill] = field(default_factory=list)


@dataclass
class WorkingCapitalSnapshot:
    wip: WipSnapshot
    debtors: DebtorsSnapshot
    cash: CashSnapshot
    creditors: CreditorsSnapshot
    net: float

    def as_dict(self) -> dict:
        return {
            "wip_value": self.wip.value,
            "wip_count": self.wip.count,
            "wip_ageing": self.wip.ageing,
            "debtors_total": self.debtors.total,
            "debtors_count": self.debtors.count,
            "debtors_ageing": self.debtors.ageing,
            "cash_balance": self.cash.balance,
            "cash_account_name": self.cash.account_name,
            "cash_recent": self.cash.recent,
            "cash_txn_count": self.cash.txn_count,
            "creditors_total": self.creditors.total,
            "creditors_supplier": self.creditors.supplier_total,
            "creditors_vat": self.creditors.vat_total,
            "creditors_count": self.creditors.count,
            "creditors_ageing": self.creditors.ageing,
            "net_working_capital": self.net,
        }


def _client_is_lost(client: Optional[Client]) -> bool:
    return bool(client and (client.overall_status or "") == "Inactive")


def _job_amount(job: Job) -> float:
    if job.gross_amount is not None and float(job.gross_amount) > 0:
        return float(job.gross_amount)
    return float(job.fee or 0)


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _days_overdue(ref: Optional[date], today: date) -> int:
    if not ref:
        return 0
    delta = (today - ref).days
    return max(0, delta)


def _age_bucket_overdue(days: int) -> str:
    if days <= 0:
        return "Current"
    if days <= 30:
        return "1–30"
    if days <= 60:
        return "31–60"
    return "61+"


def _age_bucket_debtor(days: int) -> str:
    if days <= 30:
        return "0–30"
    if days <= 60:
        return "31–60"
    if days <= 90:
        return "61–90"
    return "90+"


def _empty_buckets(labels: Sequence[str]) -> Dict[str, AgeBucket]:
    return {lab: AgeBucket(label=lab) for lab in labels}


def ensure_default_bank_account(db: Session) -> BankAccount:
    from app.services.bank_ledger import ensure_default_bank_account as _ensure

    return _ensure(db)


def cash_balance(db: Session, account: Optional[BankAccount] = None) -> float:
    from app.services.bank_ledger import account_balance, total_cash

    if account is not None:
        return account_balance(db, account)
    return total_cash(db)


def is_open_job(job: Job) -> bool:
    """Active WIP job (excludes completed, cancelled, and on hold)."""
    if hasattr(job, "is_active"):
        return bool(job.is_active())
    return (job.status or "") not in ("Completed", "Cancelled", "On hold")


def is_debtor_job(job: Job) -> bool:
    """Outstanding AR: invoiced / completed work not marked Paid."""
    status = (job.billing_status or "").strip().lower()
    if status in PAID_BILLING:
        return False
    has_invoice = bool((job.invoice_reference or "").strip())
    amount = _job_amount(job)
    if has_invoice and amount > 0:
        return True
    if status and "invoice" in status and amount > 0:
        return True
    # Completed with fee but no paid status → treat as debtor if invoiced-like
    if (job.status or "") == "Completed" and amount > 0 and (
        has_invoice or status
    ):
        return True
    return False


def wip_jobs(db: Session) -> List[Job]:
    jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Completed", "Cancelled", "On hold"]))
        .all()
    )
    return [j for j in jobs if not _client_is_lost(j.client)]


def debtor_jobs(db: Session) -> List[Job]:
    jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .all()
    )
    out = []
    for j in jobs:
        if _client_is_lost(j.client):
            # Still include lost clients' unpaid bills — they are debtors
            pass
        if is_debtor_job(j):
            out.append(j)
    return out


def _client_is_retainer(client: Optional[Client]) -> bool:
    if not client:
        return False
    if hasattr(client, "is_retainer"):
        return bool(client.is_retainer())
    return float(getattr(client, "retainer_amount", 0) or 0) > 0


def retainer_book(db: Session) -> Dict[str, float]:
    """Active clients on retainer — monthly / annual book."""
    clients = (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive", "Former", "Prospect"]))
        .all()
    )
    count = 0
    monthly = 0.0
    by_id: Dict[int, float] = {}
    for c in clients:
        if not _client_is_retainer(c):
            continue
        count += 1
        if hasattr(c, "retainer_monthly_net"):
            m = float(c.retainer_monthly_net())
        else:
            m = float(c.retainer_amount or 0)
        monthly += m
        by_id[c.id] = m
    return {
        "count": count,
        "monthly": round(monthly, 2),
        "annual": round(monthly * 12.0, 2),
        "monthly_by_client": by_id,
    }


def _retainer_share_for_job(
    job: Job,
    open_counts: Dict[int, int],
    monthly_by_client: Dict[int, float],
) -> float:
    """
    Deprecated path: retainers are valued on the calendar (bank on 1st),
    not spread across open jobs. Always returns 0.
    """
    return 0.0


def wip_amount_for_job(
    job: Job,
    *,
    open_counts: Optional[Dict[int, int]] = None,
    monthly_by_client: Optional[Dict[int, float]] = None,
) -> float:
    """
    Fee that counts toward WIP for this job.

    Retainer clients: covered work at £0 stays out of job WIP (retainer book
    banks monthly). Any **positive** job fee still counts — extra billable
    work on a retainer client is real WIP.
    """
    fee = float(job.fee or 0)
    if _client_is_retainer(job.client):
        return fee if fee > 0 else 0.0
    return fee


def retainer_forward_month_starts(
    today: Optional[date] = None, *, months: int = 12
) -> List[date]:
    """
    1sts of months not yet banked.

    Retainers bank on the 1st: once ``today >= 1st of month``, that month has
    left WIP. Outstanding months always start at the **next** calendar month.
    """
    today = today or date.today()
    this_m = _month_start(today)
    # Current month already banked on its 1st (including when today is the 1st)
    first = _add_calendar_months(this_m, 1)
    return [_add_calendar_months(first, i) for i in range(max(0, int(months)))]


def _retainer_month_to_wip_band(month_start: date, today: date) -> str:
    """Map a banking month (1st) onto WIP calendar bands today|m1|m2|m3|later."""
    meta = wip_calendar_band_meta(today)
    bounds = meta.get("_bounds") or {}
    m1 = bounds.get("m1")
    m2 = bounds.get("m2")
    m3 = bounds.get("m3")
    m4 = bounds.get("m4")
    if m1 and month_start == m1:
        return "m1"
    if m2 and month_start == m2:
        return "m2"
    if m3 and month_start == m3:
        return "m3"
    if m4 and month_start >= m4:
        return "later"
    # Past / current month should not appear in outstanding
    if month_start <= _month_start(today):
        return "today"
    return "later"


def retainer_wip_band_amounts(
    db: Session, today: Optional[date] = None, *, forward_months: int = 12
) -> Dict[str, float]:
    """
    Allocate the active retainer book across WIP calendar bands.

    Rules:
      • Banks on the 1st of each month → never sits in **Today** once that
        1st has been reached (so mid-month, current month is already out).
      • Next three calendar months (m1/m2/m3) each get the full monthly book.
      • Remaining forward months (default 12-month horizon) sit in **later**.

    Example (today 29 Jul, monthly book £2,500):
      today £0 · Aug £2,500 · Sep £2,500 · Oct £2,500 · later 9×£2,500.
    """
    today = today or date.today()
    book = retainer_book(db)
    monthly = float(book.get("monthly") or 0)
    out = {"today": 0.0, "m1": 0.0, "m2": 0.0, "m3": 0.0, "later": 0.0}
    if monthly <= 0:
        return out
    for start in retainer_forward_month_starts(today, months=forward_months):
        band = _retainer_month_to_wip_band(start, today)
        if band not in out:
            band = "later"
        out[band] = round(out[band] + monthly, 2)
    return out


def retainer_outstanding_total(
    db: Session, today: Optional[date] = None, *, forward_months: int = 12
) -> float:
    """Sum of unbanked retainer months in the forward horizon."""
    return round(sum(retainer_wip_band_amounts(db, today, forward_months=forward_months).values()), 2)


def compute_wip(db: Session, today: Optional[date] = None) -> WipSnapshot:
    """
    WIP value = per-job fees (non-retainer clients)
              + unbanked retainer months (bank on 1st; next 12 months)
              + open task fees (practice tasks; not Development / On hold).

    Retainer £ is calendar-based, not attached to individual job due dates.
    """
    today = today or date.today()
    jobs = wip_jobs(db)
    book = retainer_book(db)

    # Dashboard ageing matches WIP page horizons (not debtor-style days late)
    horizon_labels = {
        "imminent": "Overdue and Imminent",
        "planning": "Planning",
        "pre_planning": "Pre Planning",
        "later": "Everything else",
    }
    buckets = _empty_buckets(
        [
            "Overdue and Imminent",
            "Planning",
            "Pre Planning",
            "Everything else",
        ]
    )
    total = 0.0
    jobs_value = 0.0
    retainer_job_count = 0

    for j in jobs:
        is_ret = _client_is_retainer(j.client)
        if is_ret:
            retainer_job_count += 1
            amt = 0.0  # valued via calendar months, not job due date
        else:
            amt = float(j.fee or 0)
            jobs_value += amt
        total += amt
        hkey = job_horizon_key(j, today) or "later"
        label = horizon_labels.get(hkey, "Everything else")
        buckets[label].count += 1
        buckets[label].amount += amt

    # Unbanked retainer months → map calendar WIP bands onto ageing labels
    ret_bands = retainer_wip_band_amounts(db, today)
    # today band is always £0 for retainers; m1≈Planning, m2≈Pre Planning, rest later
    buckets["Planning"].amount += ret_bands.get("m1", 0.0)
    buckets["Pre Planning"].amount += ret_bands.get("m2", 0.0)
    buckets["Everything else"].amount += (
        ret_bands.get("m3", 0.0) + ret_bands.get("later", 0.0)
    )
    retainer_outstanding = sum(ret_bands.values())
    total += retainer_outstanding

    # Open task ledger fees (excludes Completed / Cancelled / On hold / Development)
    # Included in total value; not mixed into horizon ageing (dashboard ageing
    # matches WIP page job tiles).
    tasks_value = 0.0
    tasks_count = 0
    try:
        from app.services.practice_tasks import open_tasks

        for t in open_tasks(db):
            fee = float(t.fee or 0)
            if fee <= 0:
                continue
            tasks_count += 1
            tasks_value += fee
            total += fee
    except Exception:
        pass

    for b in buckets.values():
        b.amount = round(b.amount, 2)

    return WipSnapshot(
        value=round(total, 2),
        count=len(jobs),
        ageing=list(buckets.values()),
        jobs=jobs,
        jobs_value=round(jobs_value, 2),
        retainer_count=int(book["count"]),
        retainer_monthly=float(book["monthly"]),
        retainer_annual=float(book["annual"]),
        retainer_job_count=retainer_job_count,
        tasks_value=round(tasks_value, 2),
        tasks_count=tasks_count,
    )


def _end_of_month(d: date) -> date:
    return date(d.year, d.month, monthrange(d.year, d.month)[1])


def _add_months(d: date, months: int) -> date:
    """Shift calendar month by *months* (same day, clamped to month end)."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last = monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def _job_due_for_horizon(job: Job) -> Optional[date]:
    """Prefer statutory due for Accounts/CS planning; fall back to target complete."""
    return _as_date(job.statutory_due_date) or _as_date(job.target_completion)


def wip_horizon_boundaries(today: date) -> Dict[str, date]:
    """
    WIP horizons (practice view):
    - imminent: through end of current month (includes overdue)
    - planning: next 3 calendar months after EOM
    - pre_planning: 3 months after that
    - later: everything else / undated
    """
    eom = _end_of_month(today)
    plan_end = _end_of_month(_add_months(today, 3))
    pre_end = _end_of_month(_add_months(today, 6))
    return {
        "today": today,
        "eom": eom,
        "plan_end": plan_end,
        "pre_end": pre_end,
        # aliases kept for older templates
        "eom_next": plan_end,
        "eom_plus4": plan_end,
        "eom_plus7": pre_end,
    }


def _empty_horizon_buckets(today: date) -> List[WipHorizonBucket]:
    from datetime import timedelta

    b = wip_horizon_boundaries(today)

    def day_after(d: date) -> date:
        return d + timedelta(days=1)

    return [
        WipHorizonBucket(
            key="imminent",
            label="Overdue and Imminent",
            from_date=None,
            to_date=b["eom"],
        ),
        WipHorizonBucket(
            key="planning",
            label="Planning",
            from_date=day_after(b["eom"]),
            to_date=b["plan_end"],
        ),
        WipHorizonBucket(
            key="pre_planning",
            label="Pre Planning",
            from_date=day_after(b["plan_end"]),
            to_date=b["pre_end"],
        ),
        WipHorizonBucket(
            key="later",
            label="Everything else",
            from_date=day_after(b["pre_end"]),
            to_date=None,
        ),
    ]


def job_horizon_key_for_due(due: Optional[date], today: Optional[date] = None) -> str:
    """Map a due date into WIP horizon key (also used for tasks)."""
    today = today or date.today()
    if due is None:
        return "later"
    b = wip_horizon_boundaries(today)
    if due <= b["eom"]:
        return "imminent"
    if due <= b["plan_end"]:
        return "planning"
    if due <= b["pre_end"]:
        return "pre_planning"
    return "later"


def _match_job_type(job_type: Optional[str], wanted: str) -> bool:
    t = (job_type or "").strip().lower()
    w = wanted.strip().lower()
    if not t:
        return False
    if w == "accounts":
        return t == "accounts" or t.startswith("accounts ")
    if w in ("confirmation statement", "cs"):
        return "confirmation" in t
    return t == w


def job_horizon_key(job: Job, today: Optional[date] = None) -> Optional[str]:
    """
    Which WIP horizon bucket a job falls in.
    Keys: imminent | planning | pre_planning | later
    """
    today = today or date.today()
    due = _job_due_for_horizon(job)
    return job_horizon_key_for_due(due, today)


# Status label applied to open jobs by horizon (display + optional persist)
# Note: imminent horizon is further split to Overdue | Imminent on lists.
HORIZON_STATUS = {
    "imminent": "Imminent",
    "planning": "Planning",
    "pre_planning": "Pre Planning",
    "later": "Later",
}


def wip_list_status(job: Job, today: Optional[date] = None) -> str:
    """
    List status:
      Today | Tomorrow | This week | Overdue | Imminent | Planning | Pre Planning | Later

    Explicit pins (Today / Tomorrow / This week) win.
    Auto: overdue → Overdue (shown in Today band); due tomorrow → Imminent
    (shown in Tomorrow band); due later this calendar week → This week.
    """
    from datetime import timedelta

    today = today or date.today()
    if getattr(job, "is_closed", lambda: False)():
        return job.status or "—"
    if getattr(job, "is_on_hold", lambda: False)():
        return "On hold"
    st = (job.status or "").strip().lower()
    if st == "today":
        return "Today"
    if st == "tomorrow":
        return "Tomorrow"
    if st in ("this week", "thisweek"):
        return "This week"
    due = _job_due_for_horizon(job)
    if due and due < today:
        return "Overdue"
    if due and due == today:
        return "Today"
    if due and due == today + timedelta(days=1):
        return "Imminent"  # appears under Tomorrow band
    # Rest of calendar week (Mon–Sun) after tomorrow
    if due:
        # Week ends on Sunday
        days_until_sunday = 6 - today.weekday()
        week_end = today + timedelta(days=days_until_sunday)
        if today < due <= week_end:
            return "This week"
    key = job_horizon_key_for_due(due, today)
    return HORIZON_STATUS.get(key, "Later")


def job_focus_band(job: Job, today: Optional[date] = None) -> str:
    """
    WIP focus tiles: today | tomorrow | this_week | later

    - Status pin Today / Tomorrow / This week wins
    - Overdue (and due today) → today
    - Due tomorrow / Imminent → tomorrow
    - Due later this calendar week → this_week
    """
    from datetime import timedelta

    today = today or date.today()
    if getattr(job, "is_closed", lambda: False)() or getattr(
        job, "is_on_hold", lambda: False
    )():
        return "later"
    st = (job.status or "").strip().lower()
    if st == "today":
        return "today"
    if st == "tomorrow":
        return "tomorrow"
    if st in ("this week", "thisweek"):
        return "this_week"

    due = _job_due_for_horizon(job)
    if due is None:
        return "later"
    if due <= today:
        return "today"  # overdue + due today
    if due == today + timedelta(days=1):
        return "tomorrow"
    days_until_sunday = 6 - today.weekday()
    week_end = today + timedelta(days=days_until_sunday)
    if due <= week_end:
        return "this_week"
    return "later"


def _month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def _add_calendar_months(d: date, months: int) -> date:
    """First day of month shifted by *months*."""
    return _month_start(_add_months(d, months))


def _month_label(d: date) -> str:
    return d.strftime("%B %Y")


def wip_calendar_band_meta(today: Optional[date] = None) -> Dict[str, dict]:
    """
    Calendar WIP bands:
      today — overdue, due this month (imminent), or status Today
      m1 — next calendar month (e.g. August if today is in July)
      m2 — month after that
      m3 — month after that
      later — everything else / undated
    """
    today = today or date.today()
    this_m = _month_start(today)
    m1 = _add_calendar_months(today, 1)
    m2 = _add_calendar_months(today, 2)
    m3 = _add_calendar_months(today, 3)
    m4 = _add_calendar_months(today, 4)
    return {
        "today": {
            "key": "today",
            "label": "Today",
            "detail": "Overdue · imminent · status Today",
            "from_date": None,
            "to_date": _end_of_month(today),
        },
        "m1": {
            "key": "m1",
            "label": _month_label(m1),
            "detail": f"Deadlines in {_month_label(m1)}",
            "from_date": m1,
            "to_date": _end_of_month(m1),
        },
        "m2": {
            "key": "m2",
            "label": _month_label(m2),
            "detail": f"Deadlines in {_month_label(m2)}",
            "from_date": m2,
            "to_date": _end_of_month(m2),
        },
        "m3": {
            "key": "m3",
            "label": _month_label(m3),
            "detail": f"Deadlines in {_month_label(m3)}",
            "from_date": m3,
            "to_date": _end_of_month(m3),
        },
        "later": {
            "key": "later",
            "label": "Everything else",
            "detail": f"From {_month_label(m4)} · undated",
            "from_date": m4,
            "to_date": None,
        },
        "_bounds": {
            "this_month": this_m,
            "m1": m1,
            "m2": m2,
            "m3": m3,
            "m4": m4,
        },
    }


def job_wip_band(job: Job, today: Optional[date] = None) -> str:
    """
    Mutually exclusive calendar band:
      today | m1 | m2 | m3 | later
    Today = status Today OR overdue OR due on/before end of current month.
    """
    today = today or date.today()
    if getattr(job, "is_closed", lambda: False)() or getattr(
        job, "is_on_hold", lambda: False
    )():
        return "later"
    if (job.status or "").strip().lower() == "today":
        return "today"
    due = _job_due_for_horizon(job)
    if due is None:
        return "later"
    if due < today:
        return "today"
    eom = _end_of_month(today)
    if due <= eom:
        return "today"
    m1 = _add_calendar_months(today, 1)
    m2 = _add_calendar_months(today, 2)
    m3 = _add_calendar_months(today, 3)
    m4 = _add_calendar_months(today, 4)
    if m1 <= due <= _end_of_month(m1):
        return "m1"
    if m2 <= due <= _end_of_month(m2):
        return "m2"
    if m3 <= due <= _end_of_month(m3):
        return "m3"
    if due >= m4:
        return "later"
    return "later"


def job_type_bucket(job: Job) -> str:
    """Accounts | Self Assessment | Confirmation Statement | Other."""
    t = (job.type or "").strip()
    if _match_job_type(t, "Accounts"):
        return "Accounts"
    if _match_job_type(t, "Self Assessment") or t in ("SA", "SAR"):
        return "Self Assessment"
    if _match_job_type(t, "Confirmation Statement"):
        return "Confirmation Statement"
    return "Other"


def compute_wip_age_home(db: Session, today: Optional[date] = None) -> dict:
    """
    WIP home focus tiles:
      Today (overdue + pin) · Tomorrow (imminent + pin) · This week · Total

    Also still computes legacy calendar month bands (m1–later) for retainers /
    older links. Job fees use :func:`job_focus_band`. Retainers stay on calendar
    months (bank on 1st) and are included in Total only (not Today/Tomorrow).
    """
    from datetime import timedelta

    today = today or date.today()
    meta = wip_calendar_band_meta(today)
    jobs = wip_jobs(db)

    # Focus bands for the WIP home desk
    tomorrow_d = today + timedelta(days=1)
    days_until_sunday = 6 - today.weekday()
    week_end = today + timedelta(days=days_until_sunday)
    focus = {
        "today": {
            "key": "today",
            "label": "Today",
            "detail": "Overdue · due today · status Today",
            "count": 0,
            "amount": 0.0,
        },
        "tomorrow": {
            "key": "tomorrow",
            "label": "Tomorrow",
            "detail": f"{tomorrow_d.strftime('%d %b')} · imminent · status Tomorrow",
            "count": 0,
            "amount": 0.0,
        },
        "this_week": {
            "key": "this_week",
            "label": "This week",
            "detail": f"To {week_end.strftime('%d %b')} · status This week",
            "count": 0,
            "amount": 0.0,
        },
    }

    # Legacy calendar bands (still used by drill links / retainers)
    bands = {}
    for key in ("today", "m1", "m2", "m3", "later"):
        m = meta[key]
        bands[key] = {
            "key": key,
            "label": m["label"],
            "detail": m["detail"],
            "count": 0,
            "amount": 0.0,
        }

    for j in jobs:
        # Focus tile
        fb = job_focus_band(j, today)
        amt = wip_amount_for_job(j)
        if fb in focus:
            focus[fb]["count"] += 1
            focus[fb]["amount"] += amt
        # Legacy calendar band (due-date months)
        band = job_wip_band(j, today)
        if band not in bands:
            band = "later"
        bands[band]["count"] += 1
        bands[band]["amount"] += amt

    # Calendar retainer book — not on Today/Tomorrow focus; on month bands + total
    ret = retainer_wip_band_amounts(db, today)
    for key, amt in ret.items():
        if key in bands and amt:
            bands[key]["amount"] = round(bands[key]["amount"] + amt, 2)
            if key != "today":
                detail = bands[key].get("detail") or ""
                if "retainer" not in detail.lower():
                    bands[key]["detail"] = (
                        f"{detail} · retainers".strip(" ·")
                        if detail
                        else "Retainers bank on 1st"
                    )

    for b in bands.values():
        b["amount"] = round(b["amount"], 2)
    for b in focus.values():
        b["amount"] = round(b["amount"], 2)

    # Calendar aged strip only (never Tomorrow / This week — those are focus tiles)
    calendar_aged = [
        bands["m1"],
        bands["m2"],
        bands["m3"],
        bands["later"],
    ]

    return {
        "today": focus["today"],
        "tomorrow": focus["tomorrow"],
        "this_week": focus["this_week"],
        "focus": focus,
        "calendar_bands": calendar_aged,
        # mid = calendar aged only (do not put focus bands here — avoids duplicates)
        "mid": calendar_aged,
        "bands": bands,
        "meta": meta,
        "retainer_by_band": ret,
    }


def _pe_year_bucket_key(pe: Optional[date]) -> str:
    if pe is None:
        return "pe_2024_prior"
    if pe.year >= 2027:
        return "pe_2027"
    if pe.year == 2026:
        return "pe_2026"
    if pe.year == 2025:
        return "pe_2025"
    return "pe_2024_prior"


def _job_fee(job: Job) -> float:
    fee = float(job.fee or 0)
    if fee <= 0 and job.gross_amount:
        fee = float(job.gross_amount or 0)
    return fee


def _job_is_open(job: Job) -> bool:
    st = (job.status or "").strip()
    return st not in {"Completed", "Cancelled", "On hold", "Filed"}


def _job_is_completed(job: Job) -> bool:
    st = (job.status or "").strip()
    return st in {"Completed", "Filed"}


def job_service_kind(job: Job) -> str:
    """accounts | sa | cs | vat | other — for PE-year drill tiles."""
    t = (job.type or "").strip().lower()
    if t == "accounts" or t.startswith("accounts "):
        return "accounts"
    if "self assessment" in t or t in ("sa", "sar"):
        return "sa"
    if "confirmation" in t:
        return "cs"
    if "vat" in t:
        return "vat"
    return "other"


def retainer_calendar_year_split(
    db: Session, year: int, today: Optional[date] = None
) -> dict:
    """
    Split active retainer book across calendar months of *year*.

    A month is **completed** once its start date has been reached
    (today >= 1st of month); otherwise it is **outstanding**.
    """
    today = today or date.today()
    book = retainer_book(db)
    monthly_by: Dict[int, float] = book.get("monthly_by_client") or {}
    completed_amt = 0.0
    outstanding_amt = 0.0
    completed_months = 0
    outstanding_months = 0
    for _cid, monthly in monthly_by.items():
        m = float(monthly or 0)
        if m <= 0:
            continue
        for month in range(1, 13):
            start = date(year, month, 1)
            if today >= start:
                completed_amt += m
                completed_months += 1
            else:
                outstanding_amt += m
                outstanding_months += 1
    return {
        "completed_amount": round(completed_amt, 2),
        "outstanding_amount": round(outstanding_amt, 2),
        "completed_months": completed_months,
        "outstanding_months": outstanding_months,
        "annual": round(completed_amt + outstanding_amt, 2),
        "client_count": int(book.get("count") or 0),
        "monthly_total": float(book.get("monthly") or 0),
    }


def _pe_year_calendar(pe_key: str) -> Optional[int]:
    """Map pe_* key to a calendar year for retainer month splits (None = prior)."""
    return {
        "pe_2027": 2027,
        "pe_2026": 2026,
        "pe_2025": 2025,
    }.get(pe_key)


def _pe_display_year(pe_key: str) -> str:
    return {
        "pe_2027": "2027",
        "pe_2026": "2026",
        "pe_2025": "2025",
        "pe_2024_prior": "2024 & earlier",
    }.get(pe_key, pe_key)


def _clients_with_2026_accounts(jobs: Sequence[Job]) -> set:
    out = set()
    for j in jobs:
        pe = _as_date(j.period_end)
        if pe and pe.year == 2026 and job_service_kind(j) == "accounts":
            if j.client_id:
                out.add(int(j.client_id))
    return out


def _converted_without_2026_accounts(
    db: Session,
    clients_2026_accounts: set,
    open_client_ids: set,
) -> Tuple[float, int]:
    """
    Converted prospects → clients who never had PE 2026 Accounts, and who are
    not already in open WIP (so their fee is not already in the ledger).
    """
    extra_amt = 0.0
    extra_n = 0
    try:
        from app.models.prospecting import Prospect

        rows = (
            db.query(Prospect)
            .filter(
                (Prospect.converted_at.isnot(None))
                | (Prospect.pipeline_status == "won")
            )
            .all()
        )
        for p in rows:
            cid = int(p.client_id) if p.client_id else None
            if not cid:
                continue
            if cid in clients_2026_accounts:
                continue
            if cid in open_client_ids:
                continue
            val = float(p.estimated_value or 0)
            if val <= 0:
                continue
            extra_amt += val
            extra_n += 1
    except Exception:
        pass
    return round(extra_amt, 2), extra_n


def _pe_job_fee(job: Job) -> float:
    """Fee for PE book tiles — retainer clients are valued via monthly splits."""
    if _client_is_retainer(job.client):
        return 0.0
    return _job_fee(job)


def compute_wip_book(db: Session, today: Optional[date] = None) -> dict:
    """
    WIP book (main WIP desk only).

    2027 = live Total WIP ledger (jobs + retainers + tasks) count & value,
           plus converted clients with no PE 2026 Accounts who are not already
           in open WIP.

    Earlier PE years = non-retainer job fees for that PE
           + retainer calendar months for that year
             (completed once month start reached; else outstanding).
    """
    today = today or date.today()
    from app.models import Job

    snap = compute_wip(db, today)
    all_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Cancelled"]))
        .all()
    )

    def empty_bucket(key: str, label: str, href: str, unit: str = "jobs") -> dict:
        return {
            "key": key,
            "label": label,
            "href": href,
            "count": 0,
            "amount": 0.0,
            "expected": 0.0,
            "unit": unit,
        }

    buckets = {
        "prospects": empty_bucket(
            "prospects", "Prospects", "/prospecting", unit="leads"
        ),
        "pe_2027": empty_bucket(
            "pe_2027", "2027 period end", "/working-capital/wip?pe_year=2027"
        ),
        "pe_2026": empty_bucket(
            "pe_2026", "2026 period end", "/working-capital/wip?pe_year=2026"
        ),
        "pe_2025": empty_bucket(
            "pe_2025", "2025 period end", "/working-capital/wip?pe_year=2025"
        ),
        "pe_2024_prior": empty_bucket(
            "pe_2024_prior",
            "2024 & earlier",
            "/working-capital/wip?pe_year=2024prior",
        ),
    }

    try:
        from app.services.prospecting import hub_stats

        hs = hub_stats(db)
        p_count = int(hs.get("open_count") or 0)
        p_amount = float(hs.get("open_value") or 0)
        buckets["prospects"]["count"] = p_count
        buckets["prospects"]["amount"] = round(p_amount, 2)
        buckets["prospects"]["expected"] = round(p_amount, 2)
    except Exception:
        pass

    by_key: Dict[str, List[Job]] = {
        "pe_2027": [],
        "pe_2026": [],
        "pe_2025": [],
        "pe_2024_prior": [],
    }
    for j in all_jobs:
        by_key[_pe_year_bucket_key(_as_date(j.period_end))].append(j)

    clients_2026_accounts = _clients_with_2026_accounts(all_jobs)
    open_client_ids = {int(j.client_id) for j in snap.jobs if j.client_id}

    # --- Historical PE years: job fees + retainer month split for that year ---
    for key in ("pe_2026", "pe_2025", "pe_2024_prior"):
        for j in by_key[key]:
            fee = _pe_job_fee(j)
            buckets[key]["expected"] += fee
            if _job_is_open(j):
                buckets[key]["amount"] += fee
                buckets[key]["count"] += 1

        cal = _pe_year_calendar(key)
        if cal is not None:
            split = retainer_calendar_year_split(db, cal, today)
            buckets[key]["expected"] += float(split["annual"])
            buckets[key]["amount"] += float(split["outstanding_amount"])
            # Retainers affect £ value only; job count stays open jobs
    # --- 2027 = Total WIP ledger + converted without 2026 Accounts (not in WIP) ---
    extra_amt, extra_n = _converted_without_2026_accounts(
        db, clients_2026_accounts, open_client_ids
    )
    buckets["pe_2027"]["amount"] = float(snap.value) + extra_amt
    buckets["pe_2027"]["expected"] = float(snap.value) + extra_amt
    buckets["pe_2027"]["count"] = int(snap.count) + extra_n

    for b in buckets.values():
        b["amount"] = round(float(b["amount"]), 2)
        b["expected"] = round(float(b["expected"]), 2)
        exp = float(b["expected"])
        rem = float(b["amount"])
        if exp > 0:
            b["pct"] = round(min(100.0, 100.0 * rem / exp), 1)
        elif rem > 0:
            b["pct"] = 100.0
        else:
            b["pct"] = 0.0
        b["pct_bar"] = min(100.0, max(0.0, float(b["pct"])))

    order = ["prospects", "pe_2027", "pe_2026", "pe_2025", "pe_2024_prior"]
    rem_total = sum(float(buckets[k]["amount"]) for k in order)
    exp_total = sum(float(buckets[k]["expected"]) for k in order)
    return {
        "tiles": [buckets[k] for k in order],
        "total_value": round(rem_total, 2),
        "total_expected": round(exp_total, 2),
        "total_count": sum(int(buckets[k]["count"]) for k in order),
    }


def compute_pe_year_layout(
    db: Session, pe_key: str, today: Optional[date] = None
) -> dict:
    """
    1–2–2–1 layout for a period-end year drill:
      top wide: Jobs completed (year shown prominently)
      mid: Accounts / Self Assessment / CS / VAT / Other (outstanding)
      bottom wide: Jobs for the year

    Values include retainers: each calendar month is completed once its start
    has been reached, otherwise outstanding (added to Accounts outstanding).

    2027 drill mirrors the live Total WIP ledger (all open jobs + retainers).
    """
    today = today or date.today()
    from app.models import Job

    all_jobs = (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.status.notin_(["Cancelled"]))
        .all()
    )

    pe_q = {
        "pe_2027": "2027",
        "pe_2026": "2026",
        "pe_2025": "2025",
        "pe_2024_prior": "2024prior",
    }.get(pe_key, pe_key)

    year_label = _pe_display_year(pe_key)
    labels = {
        "pe_2027": "2027 period end",
        "pe_2026": "2026 period end",
        "pe_2025": "2025 period end",
        "pe_2024_prior": "2024 & earlier",
    }
    cal = _pe_year_calendar(pe_key)

    # Retainer month split for this calendar year (if any)
    ret_completed_amt = 0.0
    ret_outstanding_amt = 0.0
    ret_completed_months = 0
    ret_outstanding_months = 0
    if cal is not None:
        split = retainer_calendar_year_split(db, cal, today)
        ret_completed_amt = float(split["completed_amount"])
        ret_outstanding_amt = float(split["outstanding_amount"])
        ret_completed_months = int(split["completed_months"])
        ret_outstanding_months = int(split["outstanding_months"])

    if pe_key == "pe_2027":
        # 2027 = full open WIP ledger (plus converted not already in WIP)
        snap = compute_wip(db, today)
        clients_2026_accounts = _clients_with_2026_accounts(all_jobs)
        open_client_ids = {int(j.client_id) for j in snap.jobs if j.client_id}
        extra_amt, extra_n = _converted_without_2026_accounts(
            db, clients_2026_accounts, open_client_ids
        )

        # Completed PE-2027 jobs (rare early) + earned retainer months in 2027
        pe2027_jobs = [j for j in all_jobs if job_period_end_bucket(j) == "pe_2027"]
        completed_jobs = [j for j in pe2027_jobs if _job_is_completed(j)]
        completed_amt = sum(_pe_job_fee(j) for j in completed_jobs) + ret_completed_amt
        completed_count = len(completed_jobs)

        # Outstanding: open WIP by service (non-retainer fees; retainer months → accounts)
        book = retainer_book(db)
        monthly_by = book.get("monthly_by_client") or {}
        open_counts: Dict[int, int] = {}
        for j in snap.jobs:
            if _client_is_retainer(j.client) and j.client_id:
                open_counts[j.client_id] = open_counts.get(j.client_id, 0) + 1

        by_kind: Dict[str, List[Job]] = {
            "accounts": [],
            "sa": [],
            "cs": [],
            "vat": [],
            "other": [],
        }
        kind_amt = {k: 0.0 for k in by_kind}
        for j in snap.jobs:
            kind = job_service_kind(j)
            if kind not in by_kind:
                kind = "other"
            by_kind[kind].append(j)
            if _client_is_retainer(j.client):
                continue  # valued via retainer months
            kind_amt[kind] += float(j.fee or 0)

        # Outstanding retainer months sit under Accounts (fixed-fee cover)
        kind_amt["accounts"] += ret_outstanding_amt

        year_amt = float(snap.value) + extra_amt
        year_count = int(snap.count) + extra_n

        def pack_amt(
            key: str, label: str, count: int, amount: float, href: str
        ) -> dict:
            return {
                "key": key,
                "label": label,
                "count": count,
                "amount": round(amount, 2),
                "href": href,
            }

        base_list = f"/working-capital/wip?pe_year={pe_q}"
        return {
            "pe_key": pe_key,
            "pe_q": pe_q,
            "label": labels.get(pe_key, pe_key),
            "year_label": year_label,
            "year_num": cal,
            "retainer_completed_months": ret_completed_months,
            "retainer_outstanding_months": ret_outstanding_months,
            "retainer_completed_amount": ret_completed_amt,
            "retainer_outstanding_amount": ret_outstanding_amt,
            "completed": pack_amt(
                "completed",
                "Jobs completed",
                completed_count,
                completed_amt,
                f"{base_list}&pe_slice=completed#wip-list",
            ),
            "mid": [
                pack_amt(
                    "accounts",
                    "Accounts outstanding",
                    len(by_kind["accounts"]),
                    kind_amt["accounts"],
                    f"{base_list}&pe_slice=accounts#wip-list",
                ),
                pack_amt(
                    "sa",
                    "Self Assessment outstanding",
                    len(by_kind["sa"]),
                    kind_amt["sa"],
                    f"{base_list}&pe_slice=sa#wip-list",
                ),
                pack_amt(
                    "cs",
                    "Confirmation statements outstanding",
                    len(by_kind["cs"]),
                    kind_amt["cs"],
                    f"{base_list}&pe_slice=cs#wip-list",
                ),
                pack_amt(
                    "vat",
                    "VAT outstanding",
                    len(by_kind["vat"]),
                    kind_amt["vat"],
                    f"{base_list}&pe_slice=vat#wip-list",
                ),
                pack_amt(
                    "other",
                    "Other outstanding",
                    len(by_kind["other"]),
                    kind_amt["other"],
                    f"{base_list}&pe_slice=other#wip-list",
                ),
            ],
            "year_total": pack_amt(
                "year",
                "Jobs for the year",
                year_count,
                year_amt,
                f"{base_list}&pe_slice=year#wip-list",
            ),
        }

    # --- 2026 / 2025 / 2024 prior: jobs with that PE + retainer months ---
    year_jobs = [j for j in all_jobs if job_period_end_bucket(j) == pe_key]
    completed_jobs = [j for j in year_jobs if _job_is_completed(j)]
    outstanding = [j for j in year_jobs if _job_is_open(j)]

    completed_amt = sum(_pe_job_fee(j) for j in completed_jobs) + ret_completed_amt
    completed_count = len(completed_jobs)

    by_kind_jobs: Dict[str, List[Job]] = {
        "accounts": [],
        "sa": [],
        "cs": [],
        "vat": [],
        "other": [],
    }
    kind_amt = {k: 0.0 for k in by_kind_jobs}
    for j in outstanding:
        kind = job_service_kind(j)
        if kind not in by_kind_jobs:
            kind = "other"
        by_kind_jobs[kind].append(j)
        kind_amt[kind] += _pe_job_fee(j)

    kind_amt["accounts"] += ret_outstanding_amt

    year_job_fees = sum(_pe_job_fee(j) for j in year_jobs)
    year_amt = year_job_fees + ret_completed_amt + ret_outstanding_amt
    year_count = len(year_jobs)

    def pack_amt(key: str, label: str, count: int, amount: float, href: str) -> dict:
        return {
            "key": key,
            "label": label,
            "count": count,
            "amount": round(amount, 2),
            "href": href,
        }

    base_list = f"/working-capital/wip?pe_year={pe_q}"
    return {
        "pe_key": pe_key,
        "pe_q": pe_q,
        "label": labels.get(pe_key, pe_key),
        "year_label": year_label,
        "year_num": cal,
        "retainer_completed_months": ret_completed_months,
        "retainer_outstanding_months": ret_outstanding_months,
        "retainer_completed_amount": ret_completed_amt,
        "retainer_outstanding_amount": ret_outstanding_amt,
        "completed": pack_amt(
            "completed",
            "Jobs completed",
            completed_count,
            completed_amt,
            f"{base_list}&pe_slice=completed#wip-list",
        ),
        "mid": [
            pack_amt(
                "accounts",
                "Accounts outstanding",
                len(by_kind_jobs["accounts"]),
                kind_amt["accounts"],
                f"{base_list}&pe_slice=accounts#wip-list",
            ),
            pack_amt(
                "sa",
                "Self Assessment outstanding",
                len(by_kind_jobs["sa"]),
                kind_amt["sa"],
                f"{base_list}&pe_slice=sa#wip-list",
            ),
            pack_amt(
                "cs",
                "Confirmation statements outstanding",
                len(by_kind_jobs["cs"]),
                kind_amt["cs"],
                f"{base_list}&pe_slice=cs#wip-list",
            ),
            pack_amt(
                "vat",
                "VAT outstanding",
                len(by_kind_jobs["vat"]),
                kind_amt["vat"],
                f"{base_list}&pe_slice=vat#wip-list",
            ),
            pack_amt(
                "other",
                "Other outstanding",
                len(by_kind_jobs["other"]),
                kind_amt["other"],
                f"{base_list}&pe_slice=other#wip-list",
            ),
        ],
        "year_total": pack_amt(
            "year",
            "Jobs for the year",
            year_count,
            year_amt,
            f"{base_list}&pe_slice=year#wip-list",
        ),
    }


def job_period_end_bucket(job: Job) -> str:
    """Map open job to WIP-book period key."""
    pe = _as_date(job.period_end)
    if pe is None:
        return "pe_2024_prior"
    if pe.year >= 2027:
        return "pe_2027"
    if pe.year == 2026:
        return "pe_2026"
    if pe.year == 2025:
        return "pe_2025"
    return "pe_2024_prior"


def compute_wip_type_totals_for_band(
    db: Session, band: str, today: Optional[date] = None
) -> List[dict]:
    """
    Per job-type totals within one age band (for drill-down tiles).

    Buckets: Accounts · Self Assessment · Confirmation Statement · Other.
    Calendar retainer WIP sits in **Other** (never Accounts), so Accounts only
    lists real Accounts jobs.
    """
    today = today or date.today()
    jobs = wip_jobs(db)

    buckets = {
        "Accounts": {"key": "Accounts", "label": "Accounts", "count": 0, "amount": 0.0},
        "Self Assessment": {
            "key": "Self Assessment",
            "label": "Self Assessment",
            "count": 0,
            "amount": 0.0,
        },
        "Confirmation Statement": {
            "key": "Confirmation Statement",
            "label": "Confirmation statements",
            "count": 0,
            "amount": 0.0,
        },
        "Other": {"key": "Other", "label": "Other", "count": 0, "amount": 0.0},
    }
    focus_keys = {"today", "tomorrow", "this_week"}
    for j in jobs:
        if band not in ("all", "total", ""):
            if band in focus_keys:
                if job_focus_band(j, today) != band:
                    continue
            elif job_wip_band(j, today) != band:
                continue
        # Retainer clients valued via calendar months under Other (below)
        if _client_is_retainer(j.client):
            continue
        tb = job_type_bucket(j)
        if tb not in buckets:
            tb = "Other"
        # SAR bucket is personal tax only — skip Ltd / firm clients
        if tb == "Self Assessment":
            from app.services.individuals import is_individual_shell
            import re as _re

            c = j.client
            if c and not is_individual_shell(c):
                name = (c.company_name or "").lower()
                cn = (c.company_number or "").strip().upper()
                if _re.search(r"\b(limited|ltd|llp|plc)\b", name) or (
                    cn and not cn.startswith("IND-")
                ):
                    tb = "Other"
        amt = wip_amount_for_job(j)
        buckets[tb]["count"] += 1
        buckets[tb]["amount"] += amt

    # Retainers bank on 1st → Other (not Accounts)
    ret = retainer_wip_band_amounts(db, today)
    book = retainer_book(db)
    ret_clients = int(book.get("count") or 0)
    if band in ("all", "total", ""):
        ret_amt = sum(ret.values())
        months_n = sum(1 for v in ret.values() if float(v or 0) > 0)
        ret_count = ret_clients if ret_amt > 0 else 0
    elif band in focus_keys:
        ret_amt = 0.0
        ret_count = 0
    else:
        ret_amt = float(ret.get(band, 0.0) or 0.0)
        ret_count = ret_clients if ret_amt > 0 else 0
    if ret_amt > 0:
        buckets["Other"]["amount"] = round(
            float(buckets["Other"]["amount"]) + ret_amt, 2
        )
        # Count retainer clients once (plus any non-retainer “other” jobs already counted)
        buckets["Other"]["count"] = int(buckets["Other"]["count"]) + int(ret_count)

    out = []
    for b in buckets.values():
        b["amount"] = round(b["amount"], 2)
        out.append(b)
    return out


def compute_wip_type_horizons(
    db: Session, today: Optional[date] = None
) -> List[WipTypeHorizon]:
    """
    Rows for WIP page: Accounts, Self Assessment, Confirmation Statements.
    Buckets: Overdue and Imminent · Planning · Pre Planning · Everything else.

    Retainer months (bank on 1st) sit under Accounts only — not on CS/SA jobs.
    """
    today = today or date.today()
    jobs = wip_jobs(db)
    ret = retainer_wip_band_amounts(db, today)
    # Map calendar bands → legacy horizon keys used by this view
    ret_to_horizon = {
        "m1": "planning",
        "m2": "pre_planning",
        "m3": "later",
        "later": "later",
        "today": "imminent",  # always 0 in practice
    }

    rows_spec = [
        ("Accounts", "Accounts"),
        ("Self Assessment", "Self Assessment"),
        ("Confirmation Statement", "Confirmation statements"),
    ]
    titles = {
        "imminent": "Overdue and Imminent",
        "planning": "Planning",
        "pre_planning": "Pre Planning",
        "later": "Everything else",
    }
    out: List[WipTypeHorizon] = []
    for type_key, label in rows_spec:
        buckets = _empty_horizon_buckets(today)
        by_key = {b.key: b for b in buckets}
        total_c = 0
        total_a = 0.0
        for j in jobs:
            if not _match_job_type(j.type, type_key):
                continue
            amt = wip_amount_for_job(j)
            total_c += 1
            total_a += amt
            key = job_horizon_key(j, today) or "later"
            if key not in by_key:
                key = "later"
            by_key[key].count += 1
            by_key[key].amount += amt

        # Calendar retainers only on Accounts row
        if type_key == "Accounts":
            for band_key, amt in ret.items():
                if not amt:
                    continue
                hkey = ret_to_horizon.get(band_key, "later")
                if hkey not in by_key:
                    hkey = "later"
                by_key[hkey].amount += amt
                total_a += amt

        for b in buckets:
            b.amount = round(b.amount, 2)

        display_buckets = [
            WipHorizonBucket(
                key=bk.key,
                label=titles.get(bk.key, bk.label),
                count=bk.count,
                amount=bk.amount,
                from_date=bk.from_date,
                to_date=bk.to_date,
            )
            for bk in buckets
        ]

        out.append(
            WipTypeHorizon(
                job_type=type_key,
                label=label,
                buckets=display_buckets,
                total_count=total_c,
                total_amount=round(total_a, 2),
            )
        )
    return out


def compute_debtors(db: Session, today: Optional[date] = None) -> DebtorsSnapshot:
    """Prefer Sales Ledger open invoices; fall back to job billing if none."""
    today = today or date.today()
    try:
        from app.models.sales import Invoice
        from app.services.sales_ledger import ageing_report, outstanding_invoices

        invs = outstanding_invoices(db)
        if invs:
            ageing = ageing_report(db, today)
            total = round(sum(float(i.balance or 0) for i in invs), 2)
            # Keep jobs list empty when using invoices (drill-down is /sales)
            return DebtorsSnapshot(
                total=total, count=len(invs), ageing=ageing, jobs=[]
            )
    except Exception:
        pass

    jobs = debtor_jobs(db)
    buckets = _empty_buckets(["0–30", "31–60", "61–90", "90+"])
    total = 0.0
    for j in jobs:
        amt = _job_amount(j)
        total += amt
        inv_date = (
            _as_date(j.period_end)
            or _as_date(j.actual_completion)
            or _as_date(j.updated_at)
            or _as_date(j.created_at)
            or today
        )
        days = max(0, (today - inv_date).days)
        label = _age_bucket_debtor(days)
        buckets[label].count += 1
        buckets[label].amount += amt
    return DebtorsSnapshot(
        total=round(total, 2),
        count=len(jobs),
        ageing=list(buckets.values()),
        jobs=jobs,
    )


def compute_cash(db: Session) -> CashSnapshot:
    from app.services.bank_ledger import (
        ensure_default_bank_account as primary_acc,
        list_accounts,
        recent_transactions,
        total_cash,
    )

    acc = primary_acc(db)
    accounts = list_accounts(db, active_only=True)
    bal = total_cash(db)
    recent = recent_transactions(db, limit=5)
    txn_count = sum(r.txn_count for r in accounts)
    n_acc = len(accounts)
    name = acc.name or "Practice account"
    if n_acc > 1:
        name = f"{name} · {n_acc} accounts"
    return CashSnapshot(
        balance=bal,
        account_id=acc.id,
        account_name=name,
        recent=recent,
        txn_count=int(txn_count or 0),
    )


def compute_creditors(db: Session, today: Optional[date] = None) -> CreditorsSnapshot:
    """Creditors from Purchase Ledger open balances (not category=vat input VAT)."""
    today = today or date.today()
    try:
        from app.services.purchase_ledger import outstanding_bills

        bills = outstanding_bills(db)
    except Exception:
        bills = (
            db.query(CreditorBill)
            .filter(CreditorBill.status.in_(["outstanding", "part_paid"]))
            .all()
        )
    bills = sorted(bills, key=lambda b: b.due_date or date.max)

    buckets = _empty_buckets(["Current", "1–30", "31–60", "61+"])
    total = 0.0
    supplier = 0.0
    vat = 0.0  # HMRC/VAT liability bills (category=vat), not reclaimable input VAT
    for b in bills:
        amt = float(
            b.balance
            if b.balance is not None
            else (b.total or b.amount or 0)
        )
        if amt <= 0.001:
            continue
        total += amt
        cat = (b.category or "supplier").lower()
        if cat == "vat":
            vat += amt
        else:
            supplier += amt
        due = _as_date(b.due_date)
        if due and due < today:
            days = _days_overdue(due, today)
            label = _age_bucket_overdue(days)
        else:
            label = "Current"
        buckets[label].count += 1
        buckets[label].amount += amt

    return CreditorsSnapshot(
        total=round(total, 2),
        supplier_total=round(supplier, 2),
        vat_total=round(vat, 2),
        count=len(bills),
        ageing=list(buckets.values()),
        bills=bills,
    )


def compute_working_capital(db: Session, today: Optional[date] = None) -> WorkingCapitalSnapshot:
    today = today or date.today()
    wip = compute_wip(db, today)
    debtors = compute_debtors(db, today)
    cash = compute_cash(db)
    creditors = compute_creditors(db, today)
    net = round(wip.value + debtors.total + cash.balance - creditors.total, 2)
    return WorkingCapitalSnapshot(
        wip=wip,
        debtors=debtors,
        cash=cash,
        creditors=creditors,
        net=net,
    )
