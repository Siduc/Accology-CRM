"""Sales Ledger business logic: services, invoices, payments, ageing, chase, backfill."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from datetime import date, datetime, timedelta
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

# Standard UK VAT for practice sales / Xero sync (0.20 = 20%)
DEFAULT_SALES_VAT_RATE = 0.20

# Patterns that must never appear on the client-facing invoice notes
_INTERNAL_NOTE_MARKERS = (
    "finalised in crm",
    "issued via xero",
    "no accology email",
    "mirrored from xero",
    "dual-run",
    "xero mirror",
    "raised from job",
    "held as draft",
    "backfilled from job",
    "from quote ",
)


def format_uk_date(value) -> str:
    """Always DD/MM/YYYY for invoice text and notes."""
    if value is None or value == "":
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10]).strftime("%d/%m/%Y")
        except Exception:
            pass
    return s


def is_internal_note_line(line: str) -> bool:
    low = (line or "").strip().lower()
    if not low:
        return False
    return any(m in low for m in _INTERNAL_NOTE_MARKERS)


def split_invoice_notes(raw: Optional[str]) -> Tuple[str, str]:
    """Split mixed notes into (client_facing, internal)."""
    client_lines: List[str] = []
    internal_lines: List[str] = []
    for ln in (raw or "").splitlines():
        if is_internal_note_line(ln):
            internal_lines.append(ln.strip())
        elif ln.strip():
            client_lines.append(ln.strip())
    return "\n".join(client_lines).strip(), "\n".join(internal_lines).strip()


def append_internal_note(invoice: Invoice, text: str) -> None:
    """Append staff/system note — never shown on the printed invoice."""
    bit = (text or "").strip()
    if not bit:
        return
    cur = (invoice.internal_notes or "").strip()
    if bit in cur:
        return
    invoice.internal_notes = (cur + "\n" + bit).strip() if cur else bit


def scrub_invoice_client_notes(db: Session) -> int:
    """
    Move system/Xero dual-run text out of client-facing notes into internal_notes.
    Returns number of invoices updated.
    """
    n = 0
    for inv in db.query(Invoice).all():
        client, internal = split_invoice_notes(inv.notes)
        if not internal and client == (inv.notes or "").strip():
            continue
        # Also move pure-internal existing notes field content
        if internal or client != (inv.notes or "").strip():
            if internal:
                append_internal_note(inv, internal)
            inv.notes = client or None
            n += 1
    if n:
        db.commit()
    return n

PAID_JOB_STATUSES = {
    "paid",
    "written off",
    "written-off",
    "waived",
    "cancelled",
}


def coerce_sales_vat_rate(raw, *, default: float = DEFAULT_SALES_VAT_RATE) -> float:
    """
    Parse a VAT rate for sales lines.
    Missing / blank → practice default (20%). Explicit 0 stays 0 (zero-rated).
    """
    if raw is None:
        return float(default)
    if isinstance(raw, str) and not raw.strip():
        return float(default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)

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
        "recurrence": "annually",
    },
    {
        "code": "CS",
        "name": "Confirmation Statement",
        "description": "Companies House confirmation statement.",
        "default_fee": 50.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "annually",
    },
    {
        "code": "CT",
        "name": "Corporation Tax",
        "description": "Corporation tax computation and filing.",
        "default_fee": 0.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "annually",
    },
    {
        "code": "SA",
        "name": "Self Assessment",
        "description": "Personal tax return.",
        "default_fee": 250.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "annually",
    },
    {
        "code": "VAT",
        "name": "VAT Return",
        "description": (
            "VAT return preparation and submission. "
            "Usually quarterly on one of four HMRC-style period patterns "
            "(or monthly / annual accounting)."
        ),
        "default_fee": 75.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "quarterly",
        "quarterly_pattern": "stagger_1",
    },
    {
        "code": "PAYROLL",
        "name": "Payroll",
        "description": "Payroll processing and RTI submissions.",
        "default_fee": 0.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "monthly",
    },
    {
        "code": "BOOKKEEPING",
        "name": "Bookkeeping",
        "description": "Bookkeeping and management accounts support.",
        "default_fee": 0.0,
        "category": "compliance",
        "unit": "job",
        "recurrence": "monthly",
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
        "recurrence": "none",
    },
]


def normalise_service_recurrence(raw: Optional[str]) -> str:
    v = (raw or "none").strip().lower().replace("-", "_")
    if v in ("annual", "yearly", "year"):
        return "annually"
    if v in ("quarter", "qtr"):
        return "quarterly"
    if v in ("month", "mth"):
        return "monthly"
    if v in ("none", "one_off", "oneoff", "ad_hoc", "adhoc", ""):
        return "none"
    if v in ("monthly", "quarterly", "annually"):
        return v
    return "none"


def normalise_quarterly_pattern(raw: Optional[str], *, recurrence: str = "") -> Optional[str]:
    """Keep pattern only when recurrence is quarterly or annually."""
    rec = normalise_service_recurrence(recurrence)
    if rec not in ("quarterly", "annually"):
        return None
    code = (raw or "").strip().lower()
    if not code:
        return None
    from app.models.sales import SERVICE_QUARTERLY_PATTERNS

    valid = {p[0] for p in SERVICE_QUARTERLY_PATTERNS}
    if code in valid:
        return code
    # Friendly aliases
    aliases = {
        "1": "stagger_1",
        "hmrc_1": "stagger_1",
        "mar": "stagger_1",
        "2": "stagger_2",
        "hmrc_2": "stagger_2",
        "apr": "stagger_2",
        "3": "stagger_3",
        "hmrc_3": "stagger_3",
        "may": "stagger_3",
        "4": "stagger_4",
        "hmrc_4": "stagger_4",
        "jan": "stagger_4",
    }
    return aliases.get(code)


def quarterly_period_end_months(pattern: Optional[str]) -> Tuple[int, int, int, int]:
    """Return the four period-end months for a quarterly pattern (defaults stagger 1)."""
    from app.models.sales import SERVICE_QUARTERLY_PATTERNS

    code = (pattern or "stagger_1").strip().lower()
    for key, _label, months in SERVICE_QUARTERLY_PATTERNS:
        if key == code:
            return months  # type: ignore[return-value]
    return (3, 6, 9, 12)


def seed_services(db: Session) -> int:
    """Insert missing catalogue rows and backfill recurrence on known codes."""
    added = 0
    updated = 0
    for row in DEFAULT_SERVICES_SEED:
        exists = db.query(Service).filter(Service.code == row["code"]).first()
        rec = normalise_service_recurrence(row.get("recurrence"))
        pat = normalise_quarterly_pattern(
            row.get("quarterly_pattern"), recurrence=rec
        )
        if exists:
            # One-time backfill after column add: only when recurrence is unset (NULL/blank)
            if exists.recurrence is None or str(exists.recurrence).strip() == "":
                exists.recurrence = rec
                if pat:
                    exists.quarterly_pattern = pat
                updated += 1
            elif (
                normalise_service_recurrence(exists.recurrence)
                in ("quarterly", "annually")
                and pat
                and not (exists.quarterly_pattern or "").strip()
                and row["code"] == "VAT"
            ):
                # Seed default stagger on VAT only if pattern still blank
                exists.quarterly_pattern = pat
                updated += 1
            continue
        db.add(
            Service(
                code=row["code"],
                name=row["name"],
                description=row.get("description"),
                default_fee=row.get("default_fee", 0.0),
                default_vat_rate=row.get(
                    "default_vat_rate", DEFAULT_SALES_VAT_RATE
                ),
                unit=row.get("unit", "job"),
                category=row.get("category", "compliance"),
                recurrence=rec,
                quarterly_pattern=pat,
                is_active=True,
                is_sellable_to_clients=row.get("is_sellable_to_clients", True),
            )
        )
        added += 1
    # Ensure existing sellable services default to 20% when still 0
    for svc in db.query(Service).filter(Service.is_active.is_(True)).all():
        if float(svc.default_vat_rate or 0) <= 1e-9:
            svc.default_vat_rate = DEFAULT_SALES_VAT_RATE
            updated += 1
    # Normalise NULL recurrence on any catalogue row (column add / legacy)
    for s in db.query(Service).filter(Service.recurrence.is_(None)).all():
        s.recurrence = "none"
        updated += 1
    if added or updated:
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
    elif "vat" in t:
        code = "VAT"
    elif "payroll" in t:
        code = "PAYROLL"
    elif "bookkeep" in t:
        code = "BOOKKEEPING"
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


def invoice_number_seq(number: Optional[str]) -> int:
    """Extract trailing integer sequence from an invoice number (Xero-style).

    INV-0793 → 793, 793 → 793, INV-2026-0001 → 1.
    """
    s = (number or "").strip().replace(" ", "")
    if not s:
        return 0
    m = re.search(r"(\d+)$", s)
    if not m:
        return 0
    try:
        return int(m.group(1))
    except ValueError:
        return 0


def format_invoice_number(seq: int) -> str:
    """Xero-style display: INV-0793."""
    n = max(0, int(seq or 0))
    return f"INV-{n:04d}"


def next_invoice_number(db: Session) -> str:
    """
    Next invoice number: fill the lowest free sequence first so deleted
    invoice numbers are reused (helps Xero sequence alignment), otherwise max+1.
    """
    used = {
        invoice_number_seq(row[0])
        for row in db.query(Invoice.number).all()
        if str(row[0] or "").upper().startswith("INV") and invoice_number_seq(row[0]) > 0
    }
    if not used:
        return format_invoice_number(1)
    max_n = max(used)
    for i in range(1, max_n + 2):
        if i not in used:
            return format_invoice_number(i)
    return format_invoice_number(max_n + 1)


def next_pays_invoice_number(db: Session) -> str:
    """Accology Pays Limited sequence: AP-0001, AP-0002, …"""
    used = {
        invoice_number_seq(row[0])
        for row in db.query(Invoice.number).all()
        if str(row[0] or "").upper().startswith("AP-") and invoice_number_seq(row[0]) > 0
    }
    n = 1
    while n in used:
        n += 1
    return f"AP-{n:04d}"


def normalise_invoice_number(raw: str) -> str:
    """Accept '793', 'INV-793', 'INV-0793' → canonical INV-0793."""
    s = (raw or "").strip().upper()
    if not s:
        return ""
    seq = invoice_number_seq(s)
    if seq <= 0 and s.isdigit():
        seq = int(s)
    if seq <= 0:
        return s  # leave exotic values as typed
    return format_invoice_number(seq)


def line_amounts(qty: float, unit_price: float, vat_rate: float) -> Tuple[float, float, float]:
    net = round(float(qty) * float(unit_price), 2)
    vat = round(net * float(vat_rate or 0), 2)
    return net, vat, round(net + vat, 2)


def recompute_invoice_totals(db: Session, invoice: Invoice) -> None:
    """Refresh invoice totals from lines + payment allocations.

    Flushes pending allocations first so SUM(amount) sees rows just added in
    the same transaction (otherwise paid stays 0 and balance never drops).
    """
    db.flush()
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
    # Opening-balance / imported invoices may store total without relying on
    # line math if lines are missing; prefer computed lines when present.
    if not lines and invoice.total is not None:
        total = round(float(invoice.total or 0), 2)
    invoice.subtotal = round(subtotal, 2) if lines else float(invoice.subtotal or 0)
    invoice.vat_total = round(vat_total, 2) if lines else float(invoice.vat_total or 0)
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
    issuer: str = "accology",
) -> Invoice:
    inv_number = number
    issuer_key = (issuer or "accology").strip().lower() or "accology"
    if inv_number:
        inv_number = inv_number.strip()
        if issuer_key != "accology_pays":
            inv_number = normalise_invoice_number(inv_number)
    elif issuer_key == "accology_pays":
        inv_number = next_pays_invoice_number(db)
    else:
        inv_number = next_invoice_number(db)
    client_notes, internal_from_notes = split_invoice_notes(notes)
    inv = Invoice(
        number=inv_number,
        client_id=client_id,
        job_id=job_id,
        quote_id=quote_id,
        issue_date=issue_date or date.today(),
        # Credit terms: 0 days (due on issue date)
        due_date=due_date if due_date is not None else (issue_date or date.today()),
        status=status,
        notes=client_notes or None,
        internal_notes=internal_from_notes or None,
        source=source,
        import_key=import_key,
        issuer=issuer_key,
    )
    db.add(inv)
    db.flush()
    for row in lines:
        qty = float(row.get("qty") or 1)
        price = float(row.get("unit_price") or 0)
        if "vat_rate" in row:
            vat_rate = coerce_sales_vat_rate(row.get("vat_rate"))
        else:
            vat_rate = DEFAULT_SALES_VAT_RATE
        net, vat, gross = line_amounts(qty, price, vat_rate)
        pe = row.get("period_end")
        if isinstance(pe, str) and pe.strip():
            try:
                pe = date.fromisoformat(pe.strip()[:10])
            except Exception:
                pe = None
        elif pe is not None and not hasattr(pe, "strftime"):
            pe = None
        db.add(
            InvoiceLine(
                invoice_id=inv.id,
                service_id=row.get("service_id"),
                description=row.get("description") or "Service",
                period_end=pe,
                qty=qty,
                unit_price=price,
                vat_rate=vat_rate,
                line_total=gross,
            )
        )
    db.flush()
    recompute_invoice_totals(db, inv)

    # Keep job completion / invoicing control in sync
    if job_id:
        from app.models.job import Job

        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.invoice_reference = inv.number
            # Draft = held for review (not yet in debtors as sent)
            if (status or "").strip().lower() == "draft":
                job.billing_status = "draft"
            else:
                job.billing_status = "invoiced"
            if inv.subtotal is not None:
                job.fee = float(inv.subtotal)
            if inv.total is not None:
                job.gross_amount = float(inv.total)
            if inv.vat_total is not None:
                job.vat_amount = float(inv.vat_total)

    db.commit()
    db.refresh(inv)
    return inv


def update_invoice(
    db: Session,
    invoice: Invoice,
    *,
    client_id: Optional[int] = None,
    number: Optional[str] = None,
    issue_date: Optional[date] = None,
    due_date: Optional[date] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    lines: Optional[Sequence[dict]] = None,
) -> Invoice:
    """Update invoice header and optionally replace all lines."""
    if client_id is not None:
        invoice.client_id = client_id
    if number is not None:
        num = normalise_invoice_number(number)
        if num:
            clash = (
                db.query(Invoice)
                .filter(Invoice.number == num, Invoice.id != invoice.id)
                .first()
            )
            if clash:
                raise ValueError(f"Invoice number {num} is already used")
            invoice.number = num
    if issue_date is not None:
        invoice.issue_date = issue_date
    if due_date is not None:
        invoice.due_date = due_date
    if notes is not None:
        client_n, internal_n = split_invoice_notes(notes)
        invoice.notes = client_n or None
        if internal_n:
            append_internal_note(invoice, internal_n)
    if status is not None and status in (
        "draft",
        "sent",
        "part_paid",
        "paid",
        "void",
        "written_off",
    ):
        invoice.status = status
        if status in ("void", "written_off"):
            invoice.balance = 0.0

    if lines is not None:
        db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).delete()
        db.flush()
        for row in lines:
            qty = float(row.get("qty") or 1)
            price = float(row.get("unit_price") or 0)
            if "vat_rate" in row:
                vat_rate = coerce_sales_vat_rate(row.get("vat_rate"))
            else:
                vat_rate = DEFAULT_SALES_VAT_RATE
            _net, _vat, gross = line_amounts(qty, price, vat_rate)
            pe = row.get("period_end")
            if isinstance(pe, str) and pe.strip():
                try:
                    pe = date.fromisoformat(pe.strip()[:10])
                except Exception:
                    pe = None
            elif pe is not None and not hasattr(pe, "strftime"):
                pe = None
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    service_id=row.get("service_id"),
                    description=row.get("description") or "Service",
                    period_end=pe,
                    qty=qty,
                    unit_price=price,
                    vat_rate=vat_rate,
                    line_total=gross,
                )
            )
        db.flush()

    invoice.updated_at = datetime.utcnow()
    recompute_invoice_totals(db, invoice)

    if invoice.job_id:
        job = db.query(Job).filter(Job.id == invoice.job_id).first()
        if job:
            job.invoice_reference = invoice.number
            if invoice.status not in ("void", "written_off"):
                job.billing_status = "invoiced"
            if invoice.subtotal is not None:
                job.fee = float(invoice.subtotal)
            if invoice.total is not None:
                job.gross_amount = float(invoice.total)
            if invoice.vat_total is not None:
                job.vat_amount = float(invoice.vat_total)

    db.commit()
    db.refresh(invoice)
    return invoice


def delete_invoice(
    db: Session,
    invoice: Invoice,
    *,
    allow_with_payments: bool = False,
) -> Tuple[bool, str]:
    """
    Permanently delete an invoice so its number can be reused.

    Removes lines, chase history, and payment allocations on this invoice.
    Blocks if payments are allocated unless allow_with_payments=True
    (allocations are removed; parent Payment rows are left for the cashbook).
    """
    if not invoice:
        return False, "Invoice not found"

    number = invoice.number or f"#{invoice.id}"
    paid = float(invoice.amount_paid or 0)
    alloc_n = (
        db.query(PaymentAllocation)
        .filter(PaymentAllocation.invoice_id == invoice.id)
        .count()
    )
    if (paid > 0 or alloc_n > 0) and not allow_with_payments:
        return (
            False,
            f"Invoice {number} has payment(s) allocated — remove payments first, "
            "or confirm delete with payments.",
        )

    job_id = invoice.job_id
    # Free allocations so number reuse is clean
    db.query(PaymentAllocation).filter(
        PaymentAllocation.invoice_id == invoice.id
    ).delete(synchronize_session=False)
    db.query(DebtChaseAction).filter(
        DebtChaseAction.invoice_id == invoice.id
    ).delete(synchronize_session=False)
    db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).delete(
        synchronize_session=False
    )

    if job_id:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            if (job.invoice_reference or "") == (invoice.number or ""):
                job.invoice_reference = None
            if (job.billing_status or "").lower() in ("invoiced", "draft"):
                job.billing_status = None

    db.delete(invoice)
    db.commit()
    return True, f"Deleted {number} — that number can be reused on the next invoice"


def apply_default_vat_to_sales(
    db: Session,
    *,
    rate: float = DEFAULT_SALES_VAT_RATE,
    only_zero_lines: bool = True,
    include_void: bool = False,
) -> Dict[str, int]:
    """
    Set sales line VAT to the standard rate (default 20%) and recompute totals.
    By default only touches lines currently at 0% (so intentional zero-rated
    can be re-set after if needed — use only_zero_lines=False to force all).
    """
    rate = float(rate)
    q = db.query(Invoice)
    if not include_void:
        q = q.filter(Invoice.status.notin_(["void", "written_off"]))
    invoices = q.options(joinedload(Invoice.lines)).all()
    lines_updated = 0
    invoices_touched = 0
    for inv in invoices:
        changed = False
        for ln in inv.lines or []:
            cur = float(ln.vat_rate or 0)
            if only_zero_lines and abs(cur) > 1e-9:
                continue
            if abs(cur - rate) < 1e-9:
                continue
            ln.vat_rate = rate
            lines_updated += 1
            changed = True
        if changed:
            recompute_invoice_totals(db, inv)
            invoices_touched += 1
            if inv.job_id:
                job = db.query(Job).filter(Job.id == inv.job_id).first()
                if job and inv.vat_total is not None:
                    job.vat_amount = float(inv.vat_total)
                    if inv.total is not None:
                        job.gross_amount = float(inv.total)

    # Service catalogue defaults
    svc_n = 0
    for svc in db.query(Service).all():
        if only_zero_lines and float(svc.default_vat_rate or 0) > 1e-9:
            continue
        if abs(float(svc.default_vat_rate or 0) - rate) < 1e-9:
            continue
        svc.default_vat_rate = rate
        svc_n += 1

    db.commit()
    return {
        "lines_updated": lines_updated,
        "invoices_touched": invoices_touched,
        "services_updated": svc_n,
    }


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


def ageing_bucket_key(days: int) -> str:
    """Bucket key for debtor age in days since invoice date."""
    if days <= 30:
        return "d30"
    if days <= 60:
        return "d60"
    if days <= 90:
        return "d90"
    return "older"


DEBTOR_BUCKET_META = [
    ("d30", "30 days", "0–30 days"),
    ("d60", "60 days", "31–60 days"),
    ("d90", "90 days", "61–90 days"),
    ("older", "Older", "Over 90 days"),
]


def ageing_report(db: Session, today: Optional[date] = None) -> List[AgeBucket]:
    """Legacy-style buckets for dashboard bars (labels 0–30 … 90+)."""
    today = today or date.today()
    buckets = {
        "0–30": AgeBucket("0–30"),
        "31–60": AgeBucket("31–60"),
        "61–90": AgeBucket("61–90"),
        "90+": AgeBucket("90+"),
    }
    key_to_lab = {
        "d30": "0–30",
        "d60": "31–60",
        "d90": "61–90",
        "older": "90+",
    }
    for inv in outstanding_invoices(db):
        days = invoice_age_days(inv, today)
        lab = key_to_lab[ageing_bucket_key(days)]
        buckets[lab].count += 1
        buckets[lab].amount += float(inv.balance or 0)
    for b in buckets.values():
        b.amount = round(b.amount, 2)
    return list(buckets.values())


def debtor_age_tiles(db: Session, today: Optional[date] = None) -> List[dict]:
    """
    Tiles for debtors drill-down: 30 / 60 / 90 days, Older (no Total tile).
    Each: key, label, detail, count, amount.
    """
    today = today or date.today()
    totals = {k: {"count": 0, "amount": 0.0} for k, _, _ in DEBTOR_BUCKET_META}
    for inv in outstanding_invoices(db):
        days = invoice_age_days(inv, today)
        k = ageing_bucket_key(days)
        totals[k]["count"] += 1
        totals[k]["amount"] += float(inv.balance or 0)
    tiles = []
    for key, label, detail in DEBTOR_BUCKET_META:
        tiles.append(
            {
                "key": key,
                "label": label,
                "detail": detail or "",
                "count": totals[key]["count"],
                "amount": round(totals[key]["amount"], 2),
            }
        )
    return tiles


def debtors_total(db: Session) -> Tuple[float, int]:
    rows = outstanding_invoices(db)
    total = round(sum(float(i.balance or 0) for i in rows), 2)
    return total, len(rows)


def sales_day_book(
    db: Session, *, year: Optional[int] = None, today: Optional[date] = None
) -> dict:
    """
    Sales day book / debtors control roll-forward:

      Debtors b/Fwd + Invoices − Receipts = Debtors c/Fwd

    c/Fwd = current outstanding debtors.
    Invoices / Receipts = totals for the year (or all time if year is None).
    b/Fwd is derived so the equation always holds
    (absorbs write-offs / opening residual).
    """
    today = today or date.today()
    c_fwd, c_count = debtors_total(db)

    inv_q = db.query(Invoice).filter(
        Invoice.status.notin_(["void", "written_off", "draft"])
    )
    pay_q = db.query(Payment)
    if year:
        y0 = date(int(year), 1, 1)
        y1 = date(int(year), 12, 31)
        inv_q = inv_q.filter(Invoice.issue_date >= y0, Invoice.issue_date <= y1)
        pay_q = pay_q.filter(Payment.payment_date >= y0, Payment.payment_date <= y1)

    inv_rows = inv_q.all()
    pay_rows = pay_q.all()
    invoices = round(sum(float(i.total or 0) for i in inv_rows), 2)
    receipts = round(sum(float(p.amount or 0) for p in pay_rows), 2)
    b_fwd = round(float(c_fwd) - invoices + receipts, 2)

    return {
        "year": year,
        "label": str(year) if year else "Overall",
        "b_fwd": b_fwd,
        "invoices": invoices,
        "invoice_count": len(inv_rows),
        "receipts": receipts,
        "receipt_count": len(pay_rows),
        "c_fwd": float(c_fwd),
        "c_count": int(c_count),
        "formula_check": round(b_fwd + invoices - receipts, 2),
        "today": today,
        "tiles": [
            {
                "key": "b_fwd",
                "label": "Debtors b/Fwd",
                "amount": b_fwd,
                "count": None,
                "unit": "",
                "href": "/sales/ageing",
                "tone": "",
            },
            {
                "key": "invoices",
                "label": "Invoices",
                "amount": invoices,
                "count": len(inv_rows),
                "unit": "raised",
                "href": "/sales/invoices",
                "tone": "new",
            },
            {
                "key": "receipts",
                "label": "Receipts",
                "amount": receipts,
                "count": len(pay_rows),
                "unit": "payments",
                "href": "/sales",
                "tone": "ok",
            },
            {
                "key": "c_fwd",
                "label": "Debtors c/Fwd",
                "amount": float(c_fwd),
                "count": int(c_count),
                "unit": "open",
                "href": "/sales/ageing",
                "tone": "lost" if c_fwd > 0 else "ok",
            },
        ],
    }


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


def clear_debtors_ledger(db: Session) -> dict:
    """
    Wipe the sales debtors ledger (invoices, lines, payments, allocations, chase)
    so opening balances can be imported cleanly.

    Does not delete clients, jobs, or quotes.
    """
    deleted_chase = db.query(DebtChaseAction).delete(synchronize_session=False)
    deleted_alloc = db.query(PaymentAllocation).delete(synchronize_session=False)
    deleted_pay = db.query(Payment).delete(synchronize_session=False)
    deleted_lines = db.query(InvoiceLine).delete(synchronize_session=False)
    deleted_inv = db.query(Invoice).delete(synchronize_session=False)
    db.commit()
    total, count = debtors_total(db)
    return {
        "invoices_deleted": deleted_inv,
        "lines_deleted": deleted_lines,
        "allocations_deleted": deleted_alloc,
        "chase_deleted": deleted_chase,
        "payments_deleted": deleted_pay,
        "debtors_remaining": total,
        "debtors_count": count,
    }


def _norm_client_name(name: str) -> str:
    """Collapse whitespace / punctuation for fuzzy company matching."""
    s = (name or "").replace("\xa0", " ").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\bltd\.?\b", "limited", s)
    s = re.sub(r"\bplc\.?\b", "plc", s)
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _match_client_by_name(db: Session, name: str) -> Optional[Client]:
    raw = (name or "").strip()
    if not raw:
        return None
    want = _norm_client_name(raw)
    if not want:
        return None

    # Exact (case-insensitive) on stored name
    c = (
        db.query(Client)
        .filter(func.lower(Client.company_name) == raw.lower())
        .first()
    )
    if c:
        return c

    # SQL contains (fails on double-space mismatches — kept as first pass)
    c = (
        db.query(Client)
        .filter(Client.company_name.ilike(f"%{raw}%"))
        .order_by(Client.id.asc())
        .first()
    )
    if c:
        return c

    # Company number
    from app.services.company_numbers import normalize_company_number

    cn = normalize_company_number(raw)
    if cn:
        c = db.query(Client).filter(Client.company_number == cn).first()
        if c:
            return c

    # Fuzzy: seed candidates by first significant word, compare normalised names
    tokens = [t for t in want.split() if t not in {"the", "a", "an", "and", "&"}]
    seed = tokens[0] if tokens else want[:6]
    if len(seed) < 2:
        return None
    candidates = (
        db.query(Client)
        .filter(Client.company_name.ilike(f"%{seed}%"))
        .order_by(Client.id.asc())
        .limit(80)
        .all()
    )
    # Exact normalised
    for c in candidates:
        if _norm_client_name(c.company_name or "") == want:
            return c
    # Either name contains the other (normalised)
    for c in candidates:
        cn = _norm_client_name(c.company_name or "")
        if not cn:
            continue
        if want in cn or cn in want:
            return c
    # Token overlap (at least 2 shared significant tokens, or all of shorter name)
    want_set = set(tokens)
    best = None
    best_score = 0
    for c in candidates:
        cn_tokens = [
            t
            for t in _norm_client_name(c.company_name or "").split()
            if t not in {"the", "a", "an", "and", "&"}
        ]
        if not cn_tokens:
            continue
        overlap = len(want_set & set(cn_tokens))
        need = min(2, len(want_set), len(cn_tokens))
        if overlap >= need and overlap > best_score:
            best_score = overlap
            best = c
    return best


def _parse_money_cell(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace("£", "").replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date_cell(value) -> Optional[date]:
    """Parse CSV/Excel date cells (UK-first). Handles time suffixes and serials."""
    if value is None:
        return None
    # Excel / openpyxl may pass date/datetime already
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("\xa0", " ")
    if not s:
        return None
    # Strip time portion: "15/03/2026 00:00:00" or "2026-03-15T00:00:00"
    if "T" in s:
        s = s.split("T", 1)[0].strip()
    elif " " in s and re.search(r"\d{1,2}:\d{2}", s):
        s = s.split(" ", 1)[0].strip()

    # Excel serial number (days since 1899-12-30)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        try:
            serial = float(s)
            if 20000 < serial < 80000:  # ~1954–2119
                return (datetime(1899, 12, 30) + timedelta(days=int(serial))).date()
        except ValueError:
            pass

    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
        "%m/%d/%Y",
        "%d %b %Y",
        "%d-%b-%Y",
        "%d %b %y",
        "%d-%b-%y",  # e.g. 08-Jan-26 from Excel export
        "%d %B %Y",
        "%d-%B-%Y",
        "%d %B %y",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # Flexible UK d/m/y with optional leading zeros
    m = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            return date(y, mo, d)
        except ValueError:
            # try US m/d/y if day > 12 was actually month
            try:
                return date(y, d, mo)
            except ValueError:
                pass

    if len(s) >= 10 and s[4] == "-":
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def import_opening_balances(
    db: Session,
    text: str,
    *,
    as_at: Optional[date] = None,
) -> dict:
    """
    Import debtors opening balances from CSV.

    Expected headers (flexible):
      Client, Invoice Date, Invoice Number, Gross, VAT, Net

    Creates outstanding invoices (source=opening_balance). Uses Invoice Date for
    issue/due (ageing). Notes mark import as-at date (default today).
    """
    import csv
    import io

    as_at = as_at or date.today()
    text = (text or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": ["No data"],
            "total_gross": 0.0,
        }

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": ["No header row"],
            "total_gross": 0.0,
        }

    def norm(h: str) -> str:
        h = (h or "").replace("\xa0", " ").replace("\ufeff", "").strip().lower()
        return re.sub(r"[^a-z0-9]+", "_", h).strip("_")

    field_map = {norm(h): h for h in reader.fieldnames if h is not None}

    def col(*aliases: str) -> Optional[str]:
        for a in aliases:
            if a in field_map:
                return field_map[a]
        return None

    c_client = col("client", "client_name", "company", "company_name", "name")
    c_date = col(
        "invoice_date",
        "date",
        "issue_date",
        "inv_date",
        "invoice_dt",
        "dated",
        "inv_dated",
    )
    c_num = col(
        "invoice_number",
        "invoice_no",
        "number",
        "inv_no",
        "invoice",
        "inv_number",
        "invoice_ref",
    )
    c_gross = col("gross", "total", "amount", "gross_amount")
    c_vat = col("vat", "vat_amount", "vat_total")
    c_net = col("net", "net_amount", "subtotal")

    if not c_client:
        return {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [
                "Missing Client column. Found headers: "
                + ", ".join(reader.fieldnames or [])
            ],
            "total_gross": 0.0,
        }

    created = 0
    updated = 0
    skipped = 0
    errors: List[str] = []
    total_gross = 0.0
    date_fallbacks = 0

    for idx, row in enumerate(reader, start=2):
        client_name = (row.get(c_client) or "").strip() if c_client else ""
        if not client_name:
            skipped += 1
            continue

        client = _match_client_by_name(db, client_name)
        if not client:
            skipped += 1
            errors.append(f"Row {idx}: client not found — {client_name}")
            continue

        inv_num = (row.get(c_num) or "").strip() if c_num else ""
        if not inv_num:
            inv_num = f"OB-{as_at.strftime('%Y%m%d')}-{client.id}-{idx}"

        raw_date = row.get(c_date) if c_date else None
        issue = _parse_date_cell(raw_date)
        if issue is None:
            issue = as_at
            date_fallbacks += 1
            if raw_date is not None and str(raw_date).strip():
                errors.append(
                    f"Row {idx}: could not parse Invoice Date "
                    f"{raw_date!r} — used {as_at.isoformat()}"
                )
            elif not c_date and idx == 2:
                errors.append(
                    "No Invoice Date column found — using import date for all rows. "
                    "Headers: " + ", ".join(reader.fieldnames or [])
                )

        net = _parse_money_cell(row.get(c_net) if c_net else None)
        vat = _parse_money_cell(row.get(c_vat) if c_vat else None)
        gross = _parse_money_cell(row.get(c_gross) if c_gross else None)
        if gross <= 0 and net > 0:
            gross = round(net + vat, 2)
        if net <= 0 and gross > 0:
            net = round(gross - vat, 2) if vat else gross
        if gross <= 0:
            skipped += 1
            errors.append(f"Row {idx}: no Gross/Net amount for {client_name}")
            continue

        vat_rate = 0.0
        if net > 0 and vat > 0:
            vat_rate = round(vat / net, 4)

        existing = db.query(Invoice).filter(Invoice.number == inv_num).first()
        if existing:
            # Update opening-balance rows (e.g. reimport after date fix)
            existing.client_id = client.id
            existing.issue_date = issue
            existing.due_date = issue
            existing.subtotal = round(net, 2)
            existing.vat_total = round(vat, 2)
            existing.total = round(gross, 2)
            existing.amount_paid = 0.0
            existing.balance = round(gross, 2)
            existing.status = "sent"
            existing.source = "opening_balance"
            existing.notes = (
                f"Opening balance import as at {as_at.isoformat()} · "
                f"invoice date {issue.isoformat()}"
            )
            if existing.lines:
                existing.lines[0].description = f"Opening balance · {inv_num}"
                existing.lines[0].unit_price = net
                existing.lines[0].vat_rate = vat_rate
                existing.lines[0].line_total = gross
            else:
                db.add(
                    InvoiceLine(
                        invoice_id=existing.id,
                        description=f"Opening balance · {inv_num}",
                        qty=1,
                        unit_price=net,
                        vat_rate=vat_rate,
                        line_total=gross,
                    )
                )
            db.commit()
            updated += 1
            total_gross += gross
            continue

        create_invoice(
            db,
            client_id=client.id,
            issue_date=issue,
            due_date=issue,
            source="opening_balance",
            status="sent",
            number=inv_num,
            import_key=f"ob-{inv_num}",
            notes=(
                f"Opening balance import as at {as_at.isoformat()} · "
                f"invoice date {issue.isoformat()}"
            ),
            lines=[
                {
                    "description": f"Opening balance · {inv_num}",
                    "qty": 1,
                    "unit_price": net,
                    "vat_rate": vat_rate,
                }
            ],
        )
        # Force amounts + dates (create_invoice commits; re-load and pin dates)
        inv = db.query(Invoice).filter(Invoice.number == inv_num).first()
        if inv:
            inv.issue_date = issue
            inv.due_date = issue
            inv.subtotal = round(net, 2)
            inv.vat_total = round(vat, 2)
            inv.total = round(gross, 2)
            inv.amount_paid = 0.0
            inv.balance = round(gross, 2)
            inv.status = "sent"
            if inv.lines:
                inv.lines[0].unit_price = net
                inv.lines[0].vat_rate = vat_rate
                inv.lines[0].line_total = gross
            db.commit()
        created += 1
        total_gross += gross

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:60],
        "total_gross": round(total_gross, 2),
        "as_at": as_at.isoformat(),
        "date_fallbacks": date_fallbacks,
        "date_column": c_date or "(not found)",
    }


def find_invoice_for_job(db: Session, job: Job) -> Optional[Invoice]:
    """Return existing non-void sales invoice linked to this job, if any."""
    if not job or not job.id:
        return None
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.job_id == job.id,
            Invoice.status.notin_(["void", "written_off"]),
        )
        .order_by(Invoice.id.desc())
        .first()
    )
    if inv:
        return inv
    key = f"job-{job.id}"
    inv = (
        db.query(Invoice)
        .filter(
            Invoice.import_key == key,
            Invoice.status.notin_(["void", "written_off"]),
        )
        .first()
    )
    if inv:
        return inv
    ref = (job.invoice_reference or "").strip()
    if ref:
        inv = (
            db.query(Invoice)
            .filter(
                Invoice.number == ref,
                Invoice.status.notin_(["void", "written_off"]),
            )
            .first()
        )
        if inv:
            return inv
    return None


def discard_invoice_do_not_bill(
    db: Session,
    invoice: Invoice,
    *,
    reason: str = "Not billed",
) -> Invoice:
    """
    Void a draft/sent invoice and mark the linked job as not billable.

    Used after Done → draft when the work should not be invoiced (retainer, etc.).
    """
    invoice.status = "void"
    invoice.balance = 0.0
    note = (invoice.notes or "").strip()
    tag = f"Discarded — {reason}."
    invoice.notes = f"{note} {tag}".strip() if note else tag

    if invoice.job_id:
        from app.models.job import Job

        job = db.query(Job).filter(Job.id == invoice.job_id).first()
        if job:
            job.invoice_reference = None
            # Prefer retainer label when client is on retainer
            is_ret = False
            if job.client_id:
                from app.models import Client

                client = db.query(Client).filter(Client.id == job.client_id).first()
                if client is not None:
                    try:
                        is_ret = bool(client.is_retainer())
                    except Exception:
                        is_ret = False
            job.billing_status = "retainer" if is_ret else "not_billable"
            jnote = (job.notes or "").strip()
            jtag = f"Invoice {invoice.number} discarded — not billed."
            job.notes = f"{jnote} {jtag}".strip() if jnote else jtag

    db.commit()
    db.refresh(invoice)
    return invoice


def invoice_from_job(
    db: Session,
    job: Job,
    *,
    status: str = "sent",
    source: str = "job",
) -> Invoice:
    """
    Create (or return existing) invoice for a completed job.
    Line description from job title/type; unit price from fee / gross.

    status: 'draft' holds the invoice for review; 'sent' is ready for debtors.
    """
    seed_services(db)
    existing = find_invoice_for_job(db, job)
    if existing:
        return existing

    if not job.client_id:
        raise ValueError("Job has no client — cannot raise invoice")

    inv_status = (status or "sent").strip().lower()
    if inv_status not in ("draft", "sent", "part_paid", "paid"):
        inv_status = "sent"

    amount = float(job.gross_amount) if job.gross_amount else float(job.fee or 0)
    # Prefer net fee when gross not set (VAT 0 default for practice fees)
    if job.fee and not job.gross_amount:
        amount = float(job.fee or 0)

    svc = service_for_job_type(db, job.type or "")
    # Practice sales default to 20% VAT (Xero-aligned) unless service overrides
    if svc and svc.default_vat_rate is not None and float(svc.default_vat_rate or 0) > 0:
        vat_rate = float(svc.default_vat_rate)
    else:
        vat_rate = DEFAULT_SALES_VAT_RATE

    pe = job.period_end
    # Description stays clean — Service + Period end are separate invoice columns
    desc = (job.title or job.type or "Professional services").strip()
    # Strip accidental "period end YYYY-MM-DD" from titles for display cleanliness
    desc = re.sub(
        r"\s*[—\-–]\s*period end\s+\d{4}-\d{2}-\d{2}\s*$",
        "",
        desc,
        flags=re.I,
    ).strip() or (job.type or "Professional services")

    issue = date.today()
    # System context → internal notes only (markers route off the printed invoice)
    internal = f"Raised from job #{job.id}"
    if inv_status == "draft":
        internal += " (held as draft)"
    inv = create_invoice(
        db,
        client_id=int(job.client_id),
        job_id=job.id,
        issue_date=issue,
        due_date=issue,  # 0 days credit
        source=source,
        status=inv_status,
        import_key=f"job-{job.id}",
        notes=internal,
        lines=[
            {
                "service_id": svc.id if svc else None,
                "description": desc,
                "period_end": pe,
                "qty": 1,
                "unit_price": amount,
                "vat_rate": vat_rate,
            }
        ],
    )
    return inv


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
            due_date=issue or date.today(),  # 0 days credit
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
        if "vat_rate" in row:
            vat_rate = coerce_sales_vat_rate(row.get("vat_rate"))
        else:
            vat_rate = DEFAULT_SALES_VAT_RATE
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
