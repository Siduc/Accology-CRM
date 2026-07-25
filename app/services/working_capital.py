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
    Spread a retainer client's annual book across their open WIP jobs
    so horizons / list amounts include retainers without double-counting.
    """
    cid = job.client_id
    if not cid:
        return 0.0
    monthly = float(monthly_by_client.get(cid, 0) or 0)
    if monthly <= 0 and job.client and hasattr(job.client, "retainer_monthly_net"):
        monthly = float(job.client.retainer_monthly_net())
    annual = monthly * 12.0
    n = int(open_counts.get(cid, 0) or 0)
    if n <= 0:
        return round(annual, 2)
    return round(annual / n, 2)


def wip_amount_for_job(
    job: Job,
    *,
    open_counts: Optional[Dict[int, int]] = None,
    monthly_by_client: Optional[Dict[int, float]] = None,
) -> float:
    """Fee that counts toward WIP for this job (retainer share or job fee)."""
    if _client_is_retainer(job.client):
        return _retainer_share_for_job(
            job, open_counts or {}, monthly_by_client or {}
        )
    return float(job.fee or 0)


def compute_wip(db: Session, today: Optional[date] = None) -> WipSnapshot:
    """
    WIP value = per-job fees (non-retainer clients)
              + annualised retainer book
              + open task fees (practice tasks; not Development / On hold).

    Retainer clients' listed jobs carry a share of annual retainer so horizon
    tiles and lists include them; retainer clients with no open jobs still add
    their full annual into Current.
    """
    today = today or date.today()
    jobs = wip_jobs(db)
    book = retainer_book(db)
    monthly_by = book.get("monthly_by_client") or {}

    open_counts: Dict[int, int] = {}
    for j in jobs:
        if _client_is_retainer(j.client) and j.client_id:
            open_counts[j.client_id] = open_counts.get(j.client_id, 0) + 1

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
    clients_with_jobs = set()

    for j in jobs:
        is_ret = _client_is_retainer(j.client)
        if is_ret:
            retainer_job_count += 1
            if j.client_id:
                clients_with_jobs.add(j.client_id)
            amt = _retainer_share_for_job(j, open_counts, monthly_by)
        else:
            amt = float(j.fee or 0)
            jobs_value += amt
        total += amt
        hkey = job_horizon_key(j, today) or "later"
        label = horizon_labels.get(hkey, "Everything else")
        buckets[label].count += 1
        buckets[label].amount += amt

    # Retainer clients with no open jobs — still count full annual in WIP
    for cid, monthly in monthly_by.items():
        if cid in clients_with_jobs:
            continue
        annual = float(monthly) * 12.0
        if annual <= 0:
            continue
        total += annual
        buckets["Everything else"].count += 1
        buckets["Everything else"].amount += annual

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
    """List status: Overdue | Imminent | Planning | Pre Planning | Later."""
    today = today or date.today()
    if getattr(job, "is_closed", lambda: False)():
        return job.status or "—"
    if getattr(job, "is_on_hold", lambda: False)():
        return "On hold"
    due = _job_due_for_horizon(job)
    if due and due < today:
        return "Overdue"
    key = job_horizon_key_for_due(due, today)
    return HORIZON_STATUS.get(key, "Later")


def compute_wip_type_horizons(
    db: Session, today: Optional[date] = None
) -> List[WipTypeHorizon]:
    """
    Rows for WIP page: Accounts, Confirmation Statements (and optionally tasks separately).
    Buckets: Overdue and Imminent · Planning · Pre Planning · Everything else.
    """
    today = today or date.today()
    jobs = wip_jobs(db)
    book = retainer_book(db)
    monthly_by = book.get("monthly_by_client") or {}
    open_counts: Dict[int, int] = {}
    for j in jobs:
        if _client_is_retainer(j.client) and j.client_id:
            open_counts[j.client_id] = open_counts.get(j.client_id, 0) + 1

    rows_spec = [
        ("Accounts", "Accounts"),
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
            amt = wip_amount_for_job(
                j, open_counts=open_counts, monthly_by_client=monthly_by
            )
            total_c += 1
            total_a += amt
            key = job_horizon_key(j, today) or "later"
            if key not in by_key:
                key = "later"
            by_key[key].count += 1
            by_key[key].amount += amt
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
