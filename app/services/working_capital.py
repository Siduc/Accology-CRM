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
    value: float
    count: int
    ageing: List[AgeBucket] = field(default_factory=list)
    jobs: List[Job] = field(default_factory=list)


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
    return (job.status or "") not in ("Completed", "Cancelled")


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
        .filter(Job.status.notin_(["Completed", "Cancelled"]))
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


def compute_wip(db: Session, today: Optional[date] = None) -> WipSnapshot:
    today = today or date.today()
    jobs = wip_jobs(db)
    buckets = _empty_buckets(["Current", "1–30", "31–60", "61+"])
    total = 0.0
    for j in jobs:
        amt = float(j.fee or 0)
        total += amt
        due = _as_date(j.target_completion) or _as_date(j.statutory_due_date)
        if due and due < today:
            days = _days_overdue(due, today)
            label = _age_bucket_overdue(days)
        else:
            label = "Current"
        buckets[label].count += 1
        buckets[label].amount += amt
    return WipSnapshot(
        value=round(total, 2),
        count=len(jobs),
        ageing=list(buckets.values()),
        jobs=jobs,
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
    eom = _end_of_month(today)
    eom_next = _end_of_month(_add_months(today, 1))
    eom_plus4 = _end_of_month(_add_months(today, 4))
    eom_plus7 = _end_of_month(_add_months(today, 7))
    return {
        "today": today,
        "eom": eom,
        "eom_next": eom_next,
        "eom_plus4": eom_plus4,
        "eom_plus7": eom_plus7,
    }


def _empty_horizon_buckets(today: date) -> List[WipHorizonBucket]:
    from datetime import timedelta

    b = wip_horizon_boundaries(today)

    def day_after(d: date) -> date:
        return d + timedelta(days=1)

    return [
        WipHorizonBucket(
            key="overdue",
            label="Overdue",
            to_date=b["today"] - timedelta(days=1),
        ),
        WipHorizonBucket(
            key="eom",
            label="End of month",
            from_date=b["today"],
            to_date=b["eom"],
        ),
        WipHorizonBucket(
            key="next_eom",
            label="End of next month",
            from_date=day_after(b["eom"]),
            to_date=b["eom_next"],
        ),
        WipHorizonBucket(
            key="plus3",
            label="Following 3 months",
            from_date=day_after(b["eom_next"]),
            to_date=b["eom_plus4"],
        ),
        WipHorizonBucket(
            key="plus3b",
            label="Next 3 months",
            from_date=day_after(b["eom_plus4"]),
            to_date=b["eom_plus7"],
        ),
        # Remainder so type-row tiles sum to full open WIP for that service
        WipHorizonBucket(
            key="later",
            label="Later / undated",
            from_date=day_after(b["eom_plus7"]),
            to_date=None,
        ),
    ]


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
    Keys: overdue | eom | next_eom | plus3 | plus3b | later
    Undated and due after the +7 month window → later (so tiles cover full WIP).
    """
    today = today or date.today()
    due = _job_due_for_horizon(job)
    if due is None:
        return "later"
    buckets = _empty_horizon_buckets(today)
    by_key = {b.key: b for b in buckets}
    if due < today:
        return "overdue"
    for key in ("eom", "next_eom", "plus3", "plus3b"):
        b = by_key[key]
        if b.from_date and b.to_date and b.from_date <= due <= b.to_date:
            return key
    # After last dated window (or any other residual)
    return "later"


def compute_wip_type_horizons(
    db: Session, today: Optional[date] = None
) -> List[WipTypeHorizon]:
    """
    Two rows for WIP page: Accounts and Confirmation Statements.
    Buckets: overdue · end of month · end of next month · following 3 months ·
    next 3 months · later/undated (so tile fees sum to full open WIP for the type).
    """
    today = today or date.today()
    jobs = wip_jobs(db)
    rows_spec = [
        ("Accounts", "Accounts"),
        ("Confirmation Statement", "Confirmation statements"),
    ]
    titles = {
        "overdue": "Overdue",
        "eom": "End of month",
        "next_eom": "End of next month",
        "plus3": "Following 3 months",
        "plus3b": "Next 3 months",
        "later": "Later / undated",
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
            amt = float(j.fee or 0)
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
