"""Import PriorJobAnalysis.csv — historical fees/jobs; missing clients → Inactive."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.models import Client, Job
from app.services.company_numbers import normalize_company_number

DEFAULT_CSV = BASE_DIR / "PriorJobAnalysis.csv"
SOURCE = "prior_job_analysis"


def normalize_client_name(name: Optional[str]) -> str:
    n = re.sub(r"\s+", " ", (name or "").upper().strip())
    for suffix in (
        " LIMITED",
        " LTD.",
        " LTD",
        " LLP",
        " PLC",
        " LLC",
    ):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
    return n


def parse_uk_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    value = value.strip()
    if not value or value.startswith("00/"):
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            d = datetime.strptime(value[:10], fmt).date()
            if d.year < 1990:
                return None
            return d
        except ValueError:
            continue
    return None


def parse_money(value: Optional[str]) -> Optional[float]:
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip().replace("£", "").replace(",", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return None


def make_import_key(
    norm_name: str,
    job_type: str,
    period_end: Optional[date],
    invoice_ref: str,
    fee: Optional[float],
) -> str:
    pe = period_end.isoformat() if period_end else ""
    fee_s = f"{fee:.2f}" if fee is not None else ""
    raw = f"{norm_name}|{job_type}|{pe}|{(invoice_ref or '').strip().upper()}|{fee_s}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


@dataclass
class PriorImportResult:
    rows_read: int = 0
    jobs_added: int = 0
    jobs_skipped: int = 0
    clients_created: int = 0
    clients_matched: int = 0
    errors: List[str] = field(default_factory=list)
    created_client_names: List[str] = field(default_factory=list)


def _build_client_index(db: Session) -> Tuple[Dict[str, Client], Dict[str, Client]]:
    by_norm: Dict[str, Client] = {}
    by_exact: Dict[str, Client] = {}
    for c in db.query(Client).all():
        if c.company_name:
            exact = c.company_name.strip().upper()
            by_exact[exact] = c
            n = normalize_client_name(c.company_name)
            # Prefer first; don't overwrite with HIST shells if real exists
            if n not in by_norm or not (c.company_number or "").startswith("HIST-"):
                by_norm[n] = c
    return by_norm, by_exact


def _resolve_or_create_client(
    db: Session,
    company_name: str,
    company_number: str,
    by_norm: Dict[str, Client],
    by_exact: Dict[str, Client],
    result: PriorImportResult,
) -> Optional[Client]:
    exact = company_name.strip().upper()
    norm = normalize_client_name(company_name)
    if not norm:
        return None

    client = by_exact.get(exact) or by_norm.get(norm)
    if client:
        result.clients_matched += 1
        return client

    # Create lost/historical client
    result.clients_created += 1
    seq = result.clients_created
    cn = normalize_company_number(company_number) if company_number else None
    if not cn:
        cn = f"HIST-{seq:05d}"
    # Ensure unique company_number
    base_cn = cn
    i = 0
    while db.query(Client).filter(Client.company_number == cn).first():
        i += 1
        cn = f"{base_cn}-{i}"

    name_u = company_name.strip()
    client_type = "Limited Company"
    if not re.search(r"\b(LIMITED|LTD|LLP|PLC)\b", name_u, re.I):
        # Person-like or other
        if " " in name_u and len(name_u) < 40:
            client_type = "Individual"
        else:
            client_type = "Other"

    client = Client(
        company_name=name_u,
        company_number=cn,
        overall_status="Inactive",
        client_type=client_type,
        source=SOURCE,
        notes="Created from PriorJobAnalysis.csv (not in live clients list).",
    )
    db.add(client)
    db.flush()
    by_norm[norm] = client
    by_exact[exact] = client
    result.created_client_names.append(name_u)
    return client


def import_prior_job_analysis(
    db: Session,
    text: Optional[str] = None,
    path: Optional[Path] = None,
) -> PriorImportResult:
    result = PriorImportResult()
    if text is None:
        p = path or DEFAULT_CSV
        if not p.exists():
            result.errors.append(f"File not found: {p}")
            return result
        text = p.read_text(encoding="utf-8-sig")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        result.errors.append("CSV has no header row")
        return result

    by_norm, by_exact = _build_client_index(db)
    existing_keys = {
        k[0]
        for k in db.query(Job.import_key).filter(Job.import_key.isnot(None)).all()
    }

    for idx, row in enumerate(reader, start=2):
        result.rows_read += 1
        company_name = (row.get("Company Name") or "").strip()
        if not company_name:
            result.jobs_skipped += 1
            result.errors.append(f"Row {idx}: missing company name")
            continue

        job_type = (row.get("Job Type") or "Other").strip() or "Other"
        period_end = parse_uk_date(row.get("Period End Date"))
        fee = parse_money(row.get("Fee (£)"))
        invoice_ref = (row.get("Invoice Reference") or "").strip()
        norm = normalize_client_name(company_name)
        import_key = make_import_key(norm, job_type, period_end, invoice_ref, fee)

        if import_key in existing_keys:
            result.jobs_skipped += 1
            continue

        client = _resolve_or_create_client(
            db,
            company_name,
            row.get("Company Number") or "",
            by_norm,
            by_exact,
            result,
        )
        if not client:
            result.jobs_skipped += 1
            result.errors.append(f"Row {idx}: could not resolve client")
            continue

        target_start = parse_uk_date(row.get("Target Start Date"))
        target_completion = parse_uk_date(row.get("Target Completion Date"))
        statutory = parse_uk_date(row.get("Statutory Due Date"))
        actual_start = parse_uk_date(row.get("Actual Start Date"))
        actual_completion = parse_uk_date(row.get("Actual Completion Date"))
        late = (row.get("Late?") or "").strip()
        billing = (row.get("Billing Status") or "").strip() or None
        gross = parse_money(row.get("Gross (£)"))
        vat = parse_money(row.get("VAT (£)"))
        desc = (row.get("Source Description") or "").strip()
        notes_parts = [f"Imported from PriorJobAnalysis.csv (row {idx})."]
        if desc:
            notes_parts.append(desc)
        if row.get("Billing Notes"):
            notes_parts.append(str(row.get("Billing Notes")))

        pe_label = period_end.isoformat() if period_end else "TBC"
        title = f"{job_type} — {pe_label} (historical)"

        job = Job(
            title=title,
            type=job_type,
            client_id=client.id,
            period_end=period_end,
            statutory_due_date=statutory,
            target_start=target_start,
            target_completion=target_completion,
            actual_start=actual_start,
            actual_completion=actual_completion,
            fee=fee if fee is not None else 0.0,
            status="Completed",
            is_recurring="No",
            notes=" ".join(notes_parts),
            source=SOURCE,
            invoice_reference=invoice_ref or None,
            billing_status=billing,
            gross_amount=gross,
            vat_amount=vat,
            was_late=late or None,
            import_key=import_key,
        )
        db.add(job)
        existing_keys.add(import_key)
        result.jobs_added += 1

    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        result.errors.append(f"Commit failed: {exc}")

    return result


def client_fee_history(db: Session, client_id: int) -> dict:
    """Build fee history stats for client detail."""
    jobs = (
        db.query(Job)
        .filter(Job.client_id == client_id)
        .order_by(Job.period_end.desc(), Job.id.desc())
        .all()
    )
    by_year: Dict[int, float] = defaultdict(float)
    by_year_type: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    rows = []
    for j in jobs:
        year = j.period_end.year if j.period_end else None
        fee = j.fee or 0.0
        jtype = j.type or "Other"
        if year:
            by_year[year] += fee
            by_year_type[year][jtype] += fee
        rows.append(
            {
                "id": j.id,
                "year": year,
                "type": j.type,
                "fee": fee,
                "period_end": j.period_end,
                "statutory_due_date": j.statutory_due_date,
                "billing_status": j.billing_status,
                "invoice_reference": j.invoice_reference,
                "status": j.status,
                "source": j.source,
                "was_late": j.was_late,
            }
        )

    year_totals = sorted(by_year.items(), key=lambda x: x[0])
    totals_only = [t for _, t in year_totals]
    avg = sum(totals_only) / len(totals_only) if totals_only else 0.0
    current_year = date.today().year
    current = by_year.get(current_year, 0.0)
    prior_totals = [t for y, t in year_totals if y < current_year]
    hist_avg = (
        sum(prior_totals) / len(prior_totals) if prior_totals else avg
    )
    variance = current - hist_avg if prior_totals or current else 0.0
    variance_pct = (variance / hist_avg * 100.0) if hist_avg else None

    # Chart.js payload: stacked bars by service type per year + average line
    chart_years = [y for y, _ in year_totals]
    type_set = sorted(
        {jt for ymap in by_year_type.values() for jt in ymap.keys()}
    )
    chart_datasets = []
    palette = [
        "#1d4ed8",
        "#0d9488",
        "#b45309",
        "#7c3aed",
        "#be123c",
        "#475569",
        "#15803d",
        "#c2410c",
    ]
    for i, jt in enumerate(type_set):
        chart_datasets.append(
            {
                "label": jt,
                "data": [
                    round(by_year_type[y].get(jt, 0.0), 2) for y in chart_years
                ],
                "backgroundColor": palette[i % len(palette)],
                "stack": "fees",
            }
        )

    return {
        "rows": rows,
        "year_totals": year_totals,
        "average_per_year": round(avg, 2),
        "historical_average": round(hist_avg, 2),
        "current_year": current_year,
        "current_year_fee": round(current, 2),
        "variance": round(variance, 2),
        "variance_pct": round(variance_pct, 1) if variance_pct is not None else None,
        "job_count": len(jobs),
        "chart_years": chart_years,
        "chart_datasets": chart_datasets,
        "chart_average": round(hist_avg if prior_totals else avg, 2),
    }
