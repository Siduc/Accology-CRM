"""Sales Ledger business logic: services, invoices, payments, ageing, chase, backfill."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import Client, Job
from app.models.sales import (
    DebtChaseAction,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentAllocation,
    Quote,
    QuoteLine,
    Service,
    ServicePrice,
)
from app.services.working_capital import AgeBucket

PAID_JOB_STATUSES = {
    "paid",
    "written off",
    "written-off",
    "waived",
    "cancelled",
}

CHASE_TYPES = [
    ("polite", "Polite email (7+ days)"),
    ("firm", "Firm email (14+ days)"),
    ("final", "Final notice (30+ days)"),
    ("legal", "Legal / formal (60+ days)"),
    ("call", "Phone call"),
    ("hold", "On hold"),
    ("note", "Internal note"),
]

DEFAULT_SERVICES_SEED = [
    {
        "code": "ACCOUNTS",
        "name": "Accounts",
        "description": "Year-end accounts preparation and filing support.",
        "default_fee": 2000.0,
        "category": "compliance",
        "unit": "job",
    },
    {
        "code": "CS",
        "name": "Confirmation Statement",
        "description": "Companies House confirmation statement.",
        "default_fee": 50.0,
        "category": "compliance",
        "unit": "job",
    },
    {
        "code": "CT",
        "name": "Corporation Tax",
        "description": "Corporation tax computation and filing.",
        "default_fee": 0.0,
        "category": "compliance",
        "unit": "job",
    },
    {
        "code": "SA",
        "name": "Self Assessment",
        "description": "Personal tax return.",
        "default_fee": 0.0,
        "category": "compliance",
        "unit": "job",
    },
    {
        "code": "DEBT_CHASE",
        "name": "Credit control / debt chasing",
        "description": (
            "Structured debtor reminders, letters and escalation. "
            "Offered as an internal process and as a sellable client service."
        ),
        "default_fee": 150.0,
        "category": "credit_control",
        "unit": "fixed",
        "is_sellable_to_clients": True,
    },
]


def seed_services(db: Session) -> int:
    added = 0
    for row in DEFAULT_SERVICES_SEED:
        exists = db.query(Service).filter(Service.code == row["code"]).first()
        if exists:
            continue
        db.add(
            Service(
                code=row["code"],
                name=row["name"],
                description=row.get("description"),
                default_fee=row.get("default_fee", 0.0),
                default_vat_rate=row.get("default_vat_rate", 0.0),
                unit=row.get("unit", "job"),
                category=row.get("category", "compliance"),
                is_active=True,
                is_sellable_to_clients=row.get("is_sellable_to_clients", True),
            )
        )
        added += 1
    if added:
        db.commit()
    return added


def service_for_job_type(db: Session, job_type: str) -> Optional[Service]:
    t = (job_type or "").strip().lower()
    code = None
    if "confirmation" in t:
        code = "CS"
    elif "accounts" in t:
        code = "ACCOUNTS"
    elif "corporation" in t or t == "ct":
        code = "CT"
    elif "self assessment" in t or t == "sa":
        code = "SA"
    if code:
        return db.query(Service).filter(Service.code == code).first()
    # fuzzy name match
    return (
        db.query(Service)
        .filter(func.lower(Service.name).contains(t[:20] if t else "___"))
        .first()
    )


def next_document_number(db: Session, prefix: str, model, field_name: str = "number") -> str:
    year = date.today().year
    like = f"{prefix}-{year}-%"
    col = getattr(model, field_name)
    last = (
        db.query(model)
        .filter(col.like(like))
        .order_by(col.desc())
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(getattr(last, field_name)).rsplit("-", 1)[-1]) + 1
        except ValueError:
            seq = 1
    return f"{prefix}-{year}-{seq:04d}"


def line_amounts(qty: float, unit_price: float, vat_rate: float) -> Tuple[float, float, float]:
    net = round(float(qty) * float(unit_price), 2)
    vat = round(net * float(vat_rate or 0), 2)
    return net, vat, round(net + vat, 2)


def recompute_invoice_totals(db: Session, invoice: Invoice) -> None:
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).all()
    subtotal = 0.0
    vat_total = 0.0
    for ln in lines:
        net = round(float(ln.qty or 0) * float(ln.unit_price or 0), 2)
        vat = round(net * float(ln.vat_rate or 0), 2)
        ln.line_total = round(net + vat, 2)
        subtotal += net
        vat_total += vat
    paid = (
        db.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .scalar()
    )
    paid_f = float(paid or 0)
    total = round(subtotal + vat_total, 2)
    invoice.subtotal = round(subtotal, 2)
    invoice.vat_total = round(vat_total, 2)
    invoice.total = total
    invoice.amount_paid = round(paid_f, 2)
    invoice.balance = round(max(0.0, total - paid_f), 2)
    if invoice.status in ("void", "written_off"):
        return
    if paid_f <= 0:
        invoice.status = "sent" if invoice.status != "draft" else invoice.status
        if invoice.status == "draft" and total > 0:
            pass
        elif invoice.status not in ("draft",):
            invoice.status = "sent"
    elif paid_f + 0.001 >= total:
        invoice.status = "paid"
        invoice.balance = 0.0
    else:
        invoice.status = "part_paid"


def recompute_quote_totals(db: Session, quote: Quote) -> None:
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote.id).all()
    subtotal = 0.0
    vat_total = 0.0
    for ln in lines:
        net = round(float(ln.qty or 0) * float(ln.unit_price or 0), 2)
        vat = round(net * float(ln.vat_rate or 0), 2)
        ln.line_total = round(net + vat, 2)
        subtotal += net
        vat_total += vat
    quote.subtotal = round(subtotal, 2)
    quote.vat_total = round(vat_total, 2)
    quote.total = round(subtotal + vat_total, 2)


def create_invoice(
    db: Session,
    *,
    client_id: int,
    lines: Sequence[dict],
    issue_date: Optional[date] = None,
    due_date: Optional[date] = None,
    job_id: Optional[int] = None,
    quote_id: Optional[int] = None,
    notes: Optional[str] = None,
    source: str = "manual",
    status: str = "sent",
    number: Optional[str] = None,
    import_key: Optional[str] = None,
) -> Invoice:
    inv = Invoice(
        number=number or next_document_number(db, "INV", Invoice),
        client_id=client_id,
        job_id=job_id,
        quote_id=quote_id,
        issue_date=issue_date or date.today(),
        due_date=due_date or (date.today() + timedelta(days=30)),
        status=status,
        notes=notes,
        source=source,
        import_key=import_key,
    )
    db.add(inv)
    db.flush()
    for row in lines:
        qty = float(row.get("qty") or 1)
        price = float(row.get("unit_price") or 0)
        vat_rate = float(row.get("vat_rate") or 0)
        net, vat, gross = line_amounts(qty, price, vat_rate)
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                service_id=row.get("service_id"),
                description=row.get("description") or "Service",
                qty=qty,
                unit_price=price,
                vat_rate=vat_rate,
                line_total=gross,
            )
        )
    db.flush()
    recompute_invoice_totals(db, inv)
    db.commit()
    db.refresh(inv)
    return inv


def allocate_payment(
    db: Session,
    payment: Payment,
    allocations: Sequence[Tuple[int, float]],
) -> None:
    """allocations: list of (invoice_id, amount)."""
    for inv_id, amt in allocations:
        if amt <= 0:
            continue
        db.add(
            PaymentAllocation(
                payment_id=payment.id, invoice_id=inv_id, amount=round(float(amt), 2)
            )
        )
        inv = db.query(Invoice).filter(Invoice.id == inv_id).first()
        if inv:
            recompute_invoice_totals(db, inv)
    db.commit()


def record_payment(
    db: Session,
    *,
    client_id: int,
    amount: float,
    payment_date: Optional[date] = None,
    method: str = "bank",
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    invoice_allocations: Optional[Sequence[Tuple[int, float]]] = None,
    post_to_bank: bool = False,
) -> Payment:
    pay = Payment(
        client_id=client_id,
        payment_date=payment_date or date.today(),
        amount=round(float(amount), 2),
        method=method,
        reference=reference,
        notes=notes,
    )
    db.add(pay)
    db.flush()

    if post_to_bank and amount > 0:
        from app.models.finance import BankTransaction
        from app.services.bank_ledger import ensure_default_bank_account

        acc = ensure_default_bank_account(db)
        from app.models import Client

        client = db.query(Client).filter(Client.id == client_id).first()
        counterparty = client.display_name() if client else f"Client #{client_id}"
        txn = BankTransaction(
            account_id=acc.id,
            txn_date=pay.payment_date,
            description=reference or f"Receipt — {counterparty}",
            amount=float(amount),
            reference=reference,
            counterparty=counterparty,
            category="client_receipt",
            source="sales",
            matched_type="payment",
            matched_id=None,  # set after flush of payment
        )
        db.add(txn)
        db.flush()
        pay.bank_transaction_id = txn.id
        txn.matched_id = pay.id

    if invoice_allocations:
        for inv_id, amt in invoice_allocations:
            if amt <= 0:
                continue
            db.add(
                PaymentAllocation(
                    payment_id=pay.id, invoice_id=inv_id, amount=round(float(amt), 2)
                )
            )
            inv = db.query(Invoice).filter(Invoice.id == inv_id).first()
            if inv:
                recompute_invoice_totals(db, inv)
    db.commit()
    db.refresh(pay)
    return pay


def outstanding_invoices(db: Session) -> List[Invoice]:
    return (
        db.query(Invoice)
        .filter(Invoice.balance > 0.001)
        .filter(Invoice.status.notin_(["void", "written_off", "draft"]))
        .order_by(Invoice.issue_date.asc())
        .all()
    )


def invoice_age_days(inv: Invoice, today: Optional[date] = None) -> int:
    """Days since issue (AR age)."""
    today = today or date.today()
    base = inv.issue_date or today
    return max(0, (today - base).days)


def invoice_overdue_days(inv: Invoice, today: Optional[date] = None) -> int:
    """Days past due date (fallback issue_date)."""
    today = today or date.today()
    base = inv.due_date or inv.issue_date or today
    return max(0, (today - base).days)


def ageing_report(db: Session, today: Optional[date] = None) -> List[AgeBucket]:
    today = today or date.today()
    buckets = {
        "0–30": AgeBucket("0–30"),
        "31–60": AgeBucket("31–60"),
        "61–90": AgeBucket("61–90"),
        "90+": AgeBucket("90+"),
    }
    for inv in outstanding_invoices(db):
        days = invoice_age_days(inv, today)
        if days <= 30:
            lab = "0–30"
        elif days <= 60:
            lab = "31–60"
        elif days <= 90:
            lab = "61–90"
        else:
            lab = "90+"
        buckets[lab].count += 1
        buckets[lab].amount += float(inv.balance or 0)
    for b in buckets.values():
        b.amount = round(b.amount, 2)
    return list(buckets.values())


def debtors_total(db: Session) -> Tuple[float, int]:
    rows = outstanding_invoices(db)
    total = round(sum(float(i.balance or 0) for i in rows), 2)
    return total, len(rows)


def suggested_chase_action(days_overdue: int) -> str:
    """Map overdue days to stage: polite / firm / final / legal."""
    from app.services.chase_emails import stage_for_days

    return stage_for_days(days_overdue) or "polite"


def highest_stage_logged(db: Session, invoice_id: int) -> Optional[str]:
    from app.services.chase_emails import STAGE_ORDER, stage_rank

    rows = (
        db.query(DebtChaseAction.stage)
        .filter(DebtChaseAction.invoice_id == invoice_id)
        .filter(DebtChaseAction.stage.isnot(None))
        .all()
    )
    best = None
    best_r = -1
    for (st,) in rows:
        r = stage_rank(st)
        if r > best_r:
            best_r = r
            best = st
    return best


def is_on_hold(db: Session, invoice_id: int, today: Optional[date] = None) -> bool:
    today = today or date.today()
    last = (
        db.query(DebtChaseAction)
        .filter(DebtChaseAction.invoice_id == invoice_id)
        .order_by(DebtChaseAction.id.desc())
        .first()
    )
    if not last:
        return False
    if (last.action_type or "") == "hold" or (last.stage or "") == "hold":
        if last.next_action_date and last.next_action_date > today:
            return True
        if not last.next_action_date:
            return True
    return False


def chase_pipeline_rows(db: Session, today: Optional[date] = None) -> List[dict]:
    """Invoices eligible for chase (overdue ≥ 7 days), with stage suggestion."""
    from app.services.chase_emails import stage_for_days, stage_rank

    today = today or date.today()
    rows = []
    for inv in outstanding_invoices(db):
        overdue = invoice_overdue_days(inv, today)
        if overdue < 7:
            continue
        if is_on_hold(db, inv.id, today):
            stage = "hold"
            suggest = "hold"
        else:
            suggest = stage_for_days(overdue) or "polite"
            highest = highest_stage_logged(db, inv.id)
            # If already logged this stage or higher, still show but mark
            stage = suggest
        rows.append(
            {
                "inv": inv,
                "overdue": overdue,
                "age": invoice_age_days(inv, today),
                "suggest": suggest,
                "highest_logged": highest_stage_logged(db, inv.id),
                "on_hold": is_on_hold(db, inv.id, today),
            }
        )
    rows.sort(key=lambda r: -r["overdue"])
    return rows


def chase_status_summary(db: Session, today: Optional[date] = None) -> dict:
    """Counts/£ for dashboard and sales home."""
    today = today or date.today()
    rows = chase_pipeline_rows(db, today)
    by_stage = {"polite": 0, "firm": 0, "final": 0, "legal": 0, "hold": 0}
    by_stage_amt = {k: 0.0 for k in by_stage}
    for r in rows:
        st = "hold" if r["on_hold"] else (r["suggest"] or "polite")
        if st not in by_stage:
            st = "polite"
        by_stage[st] += 1
        by_stage_amt[st] += float(r["inv"].balance or 0)
    week_ago = today - timedelta(days=7)
    actions_week = (
        db.query(func.count(DebtChaseAction.id))
        .filter(DebtChaseAction.action_date >= week_ago)
        .scalar()
    )
    try:
        from app.config import CHASE_LIVE_MODE as _live
    except Exception:  # noqa: BLE001
        _live = False
    return {
        "pipeline_count": len(rows),
        "pipeline_amount": round(sum(float(r["inv"].balance or 0) for r in rows), 2),
        "by_stage": by_stage,
        "by_stage_amount": {k: round(v, 2) for k, v in by_stage_amt.items()},
        "actions_this_week": int(actions_week or 0),
        "live_mode": bool(_live),
    }


def build_legal_export_zip(
    db: Session,
    *,
    min_days: int = 60,
    client_id: Optional[int] = None,
    solicitor_name: str = "Thomas Higgins",
) -> bytes:
    """ZIP pack for solicitor handover (no client email)."""
    import csv
    import io
    import zipfile
    from app.config import PRACTICE_EMAIL, PRACTICE_NAME, PRACTICE_PHONE

    today = date.today()
    invs = outstanding_invoices(db)
    selected = []
    for inv in invs:
        if invoice_overdue_days(inv, today) < min_days:
            continue
        if client_id and inv.client_id != client_id:
            continue
        selected.append(inv)

    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({i.client_id for i in selected} or {-1}))
        .all()
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # summary
        s = io.StringIO()
        w = csv.writer(s)
        w.writerow(
            [
                "client",
                "email",
                "invoice",
                "issue_date",
                "due_date",
                "total",
                "paid",
                "balance",
                "overdue_days",
                "status",
            ]
        )
        for inv in selected:
            c = clients.get(inv.client_id)
            w.writerow(
                [
                    c.display_name() if c else inv.client_id,
                    c.email if c else "",
                    inv.number,
                    inv.issue_date,
                    inv.due_date,
                    inv.total,
                    inv.amount_paid,
                    inv.balance,
                    invoice_overdue_days(inv, today),
                    inv.status,
                ]
            )
        zf.writestr("summary.csv", s.getvalue())

        # lines
        s2 = io.StringIO()
        w2 = csv.writer(s2)
        w2.writerow(
            ["invoice", "description", "qty", "unit_price", "vat_rate", "line_total"]
        )
        for inv in selected:
            lines = (
                db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id).all()
            )
            for ln in lines:
                w2.writerow(
                    [
                        inv.number,
                        ln.description,
                        ln.qty,
                        ln.unit_price,
                        ln.vat_rate,
                        ln.line_total,
                    ]
                )
        zf.writestr("invoice_lines.csv", s2.getvalue())

        # chase log
        s3 = io.StringIO()
        w3 = csv.writer(s3)
        w3.writerow(
            [
                "invoice",
                "action_date",
                "stage",
                "channel",
                "action_type",
                "send_status",
                "notes",
            ]
        )
        for inv in selected:
            acts = (
                db.query(DebtChaseAction)
                .filter(DebtChaseAction.invoice_id == inv.id)
                .order_by(DebtChaseAction.action_date)
                .all()
            )
            for a in acts:
                w3.writerow(
                    [
                        inv.number,
                        a.action_date,
                        a.stage,
                        a.channel,
                        a.action_type,
                        a.send_status,
                        a.notes,
                    ]
                )
        zf.writestr("chase_log.csv", s3.getvalue())

        total_bal = sum(float(i.balance or 0) for i in selected)
        cover = f"""{PRACTICE_NAME}
Legal handover pack — debt recovery
Date: {today.isoformat()}

To: {solicitor_name}

Please find enclosed particulars of outstanding client debts we wish you to consider for recovery.

Number of invoices: {len(selected)}
Total balance: £{total_bal:,.2f}
Minimum overdue days included: {min_days}

Contents:
  - summary.csv
  - invoice_lines.csv
  - chase_log.csv
  - README.txt

Contact: {PRACTICE_EMAIL or '—'} | {PRACTICE_PHONE or '—'}

Yours faithfully,
{PRACTICE_NAME}
"""
        zf.writestr("cover_letter.txt", cover)
        zf.writestr(
            "README.txt",
            "Accologise legal handover pack.\n"
            "Import CSVs into your case system. Verify balances before action.\n"
            "Generated in practice/export mode (not a court filing).\n",
        )

    return buf.getvalue()


def backfill_invoices_from_jobs(db: Session) -> dict:
    """Create invoices from historical job billing fields (idempotent)."""
    seed_services(db)
    created = 0
    skipped = 0
    jobs = (
        db.query(Job)
        .filter(Job.client_id.isnot(None))
        .filter((Job.fee > 0) | (Job.gross_amount > 0) | (Job.invoice_reference.isnot(None)))
        .all()
    )
    for job in jobs:
        key = f"job-{job.id}"
        if db.query(Invoice).filter(Invoice.import_key == key).first():
            skipped += 1
            continue
        if job.invoice_reference:
            if db.query(Invoice).filter(Invoice.number == job.invoice_reference).first():
                skipped += 1
                continue
        amount = float(job.gross_amount) if job.gross_amount else float(job.fee or 0)
        if amount <= 0:
            skipped += 1
            continue
        svc = service_for_job_type(db, job.type or "")
        billing = (job.billing_status or "").strip().lower()
        is_paid = billing in PAID_JOB_STATUSES
        issue = job.period_end or (
            job.created_at.date() if job.created_at else date.today()
        )
        inv = create_invoice(
            db,
            client_id=int(job.client_id),
            job_id=job.id,
            issue_date=issue,
            due_date=issue + timedelta(days=30) if issue else date.today() + timedelta(days=30),
            source="import",
            status="sent",
            number=job.invoice_reference or None,
            import_key=key,
            notes=f"Backfilled from job #{job.id}",
            lines=[
                {
                    "service_id": svc.id if svc else None,
                    "description": job.title or job.type or "Professional services",
                    "qty": 1,
                    "unit_price": amount,
                    "vat_rate": 0.0,
                }
            ],
        )
        if is_paid:
            inv.amount_paid = float(inv.total or 0)
            inv.balance = 0.0
            inv.status = "paid"
            db.commit()
        created += 1
    return {"created": created, "skipped": skipped}


def create_quote(
    db: Session,
    *,
    client_id: int,
    lines: Sequence[dict],
    issue_date: Optional[date] = None,
    valid_until: Optional[date] = None,
    notes: Optional[str] = None,
    status: str = "draft",
) -> Quote:
    q = Quote(
        number=next_document_number(db, "QTE", Quote),
        client_id=client_id,
        issue_date=issue_date or date.today(),
        valid_until=valid_until,
        status=status,
        notes=notes,
    )
    db.add(q)
    db.flush()
    for row in lines:
        qty = float(row.get("qty") or 1)
        price = float(row.get("unit_price") or 0)
        vat_rate = float(row.get("vat_rate") or 0)
        net, vat, gross = line_amounts(qty, price, vat_rate)
        db.add(
            QuoteLine(
                quote_id=q.id,
                service_id=row.get("service_id"),
                description=row.get("description") or "Service",
                qty=qty,
                unit_price=price,
                vat_rate=vat_rate,
                line_total=gross,
            )
        )
    db.flush()
    recompute_quote_totals(db, q)
    db.commit()
    db.refresh(q)
    return q


def invoice_from_quote(db: Session, quote: Quote) -> Invoice:
    lines = [
        {
            "service_id": ln.service_id,
            "description": ln.description,
            "qty": ln.qty,
            "unit_price": ln.unit_price,
            "vat_rate": ln.vat_rate,
        }
        for ln in quote.lines
    ]
    inv = create_invoice(
        db,
        client_id=quote.client_id,
        quote_id=quote.id,
        lines=lines,
        source="quote",
        status="sent",
        notes=f"From quote {quote.number}",
    )
    quote.status = "accepted"
    db.commit()
    return inv
