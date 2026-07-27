"""
Export key lists to CSV and reimport with update-or-create semantics.

Matching order for reimport:
  1. Row ``id`` if present and found
  2. Business key (e.g. company_number for clients)
  3. Else create new (where allowed)

Only columns present in the CSV (non-empty header) are applied on update,
except empty cells clear optional text fields when ``clear_empty`` is True.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session, joinedload

from app.models import Client, Job, Person
from app.models.practice_task import PracticeTask
from app.services.company_numbers import normalize_company_number
from app.services.dates import calculate_dates
from app.services.working_capital import wip_jobs


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Excel serial? skip
    return None


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip().replace("£", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _fmt_date(d) -> str:
    if not d:
        return ""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m-%d")
    return str(d)


def rows_to_csv(headers: Sequence[str], rows: Iterable[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    # utf-8 BOM helps Excel open UTF-8 correctly
    buf.write("\ufeff")
    w = csv.DictWriter(buf, fieldnames=list(headers), extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for row in rows:
        out = {}
        for h in headers:
            v = row.get(h, "")
            if v is None:
                v = ""
            out[h] = v
        w.writerow(out)
    return buf.getvalue()


def parse_csv_text(text: str) -> Tuple[List[str], List[Dict[str, str]], List[str]]:
    """Return (headers_normalized, rows as raw_header->value, warnings)."""
    warnings: List[str] = []
    if not text or not str(text).strip():
        return [], [], ["No data provided"]
    text = str(text).lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    sample = text[:4096]
    delim = ","
    if "\t" in sample and sample.count("\t") >= sample.count(","):
        delim = "\t"
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    raw = [r for r in reader if any((c or "").strip() for c in r)]
    if not raw:
        return [], [], ["No data rows found"]
    headers_raw = [(c or "").strip() for c in raw[0]]
    headers_norm = [_norm_header(h) for h in headers_raw]
    rows: List[Dict[str, str]] = []
    for line in raw[1:]:
        d: Dict[str, str] = {}
        for i, h in enumerate(headers_norm):
            if not h:
                continue
            cell = line[i].strip() if i < len(line) else ""
            d[h] = cell
        rows.append(d)
    return headers_norm, rows, warnings


@dataclass
class ExchangeResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Updated {self.updated}, created {self.created}, "
            f"skipped {self.skipped}, errors {len(self.errors)}."
        )


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

CLIENT_EXPORT_HEADERS = [
    "id",
    "company_name",
    "company_number",
    "overall_status",
    "client_type",
    "billing_model",
    "retainer_amount",
    "retainer_frequency",
    "retainer_notes",
    "contact_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "town",
    "postcode",
    "vat_number",
    "utr",
    "engagement_date",
    "disengagement_date",
    "ch_authentication_code",
    "ch_personal_code",
    "notes",
    "source",
]

CLIENT_UPDATABLE = {
    "company_name",
    "company_number",
    "overall_status",
    "client_type",
    "billing_model",
    "retainer_amount",
    "retainer_frequency",
    "retainer_notes",
    "contact_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "town",
    "postcode",
    "vat_number",
    "utr",
    "engagement_date",
    "disengagement_date",
    "ch_authentication_code",
    "ch_personal_code",
    "notes",
}


def export_clients_csv(db: Session, *, status: str = "") -> str:
    q = db.query(Client).order_by(Client.company_name.asc(), Client.id.asc())
    if status:
        q = q.filter(Client.overall_status == status)
    rows = []
    for c in q.all():
        rows.append(
            {
                "id": c.id,
                "company_name": c.company_name or "",
                "company_number": c.company_number or "",
                "overall_status": c.overall_status or "",
                "client_type": c.client_type or "",
                "billing_model": c.billing_model or "Per job",
                "retainer_amount": (
                    f"{float(c.retainer_amount):.2f}"
                    if c.retainer_amount is not None
                    else ""
                ),
                "retainer_frequency": c.retainer_frequency or "",
                "retainer_notes": (c.retainer_notes or "").replace("\n", " "),
                "contact_name": c.contact_name or "",
                "email": c.email or "",
                "phone": c.phone or "",
                "address_line1": c.address_line1 or "",
                "address_line2": c.address_line2 or "",
                "town": c.town or "",
                "postcode": c.postcode or "",
                "vat_number": c.vat_number or "",
                "utr": c.utr or "",
                "engagement_date": _fmt_date(c.engagement_date),
                "disengagement_date": _fmt_date(c.disengagement_date),
                "ch_authentication_code": c.ch_authentication_code or "",
                "ch_personal_code": c.ch_personal_code or "",
                "notes": (c.notes or "").replace("\r\n", " ").replace("\n", " "),
                "source": c.source or "",
            }
        )
    return rows_to_csv(CLIENT_EXPORT_HEADERS, rows)


def reimport_clients(db: Session, text: str) -> ExchangeResult:
    result = ExchangeResult()
    headers, rows, warnings = parse_csv_text(text)
    result.messages.extend(warnings)
    if not rows:
        result.errors.append("No rows to import.")
        return result

    date_fields = {"engagement_date", "disengagement_date"}
    float_fields = {"retainer_amount"}
    for idx, row in enumerate(rows, start=2):
        cid = _parse_int(row.get("id"))
        cn_raw = row.get("company_number") or ""
        cn = normalize_company_number(cn_raw) if cn_raw.strip() else None
        name = (row.get("company_name") or "").strip() or None

        client: Optional[Client] = None
        if cid:
            client = db.query(Client).filter(Client.id == cid).first()
        if not client and cn:
            client = db.query(Client).filter(Client.company_number == cn).first()

        if client:
            changed = False
            for key in CLIENT_UPDATABLE:
                if key not in row:
                    continue
                raw = row.get(key)
                if key == "company_number":
                    val = normalize_company_number(raw) if (raw or "").strip() else client.company_number
                elif key in date_fields:
                    if raw is None or str(raw).strip() == "":
                        continue  # leave existing date if blank
                    val = _parse_date(raw)
                elif key in float_fields:
                    if raw is None or str(raw).strip() == "":
                        continue
                    val = _parse_float(raw)
                else:
                    val = (raw or "").strip() or None
                if getattr(client, key) != val:
                    setattr(client, key, val)
                    changed = True
            if changed:
                client.updated_at = datetime.utcnow()
                result.updated += 1
            else:
                result.skipped += 1
            continue

        # Create new
        if not cn and not name:
            result.skipped += 1
            result.errors.append(f"Row {idx}: need id, company_number, or company_name to create")
            continue
        if cn:
            clash = db.query(Client).filter(Client.company_number == cn).first()
            if clash:
                result.skipped += 1
                result.errors.append(f"Row {idx}: company_number {cn} already exists as #{clash.id}")
                continue
        client = Client(
            company_name=name or cn or "New client",
            company_number=cn,
            contact_name=(row.get("contact_name") or "").strip() or None,
            email=(row.get("email") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None,
            address_line1=(row.get("address_line1") or "").strip() or None,
            address_line2=(row.get("address_line2") or "").strip() or None,
            town=(row.get("town") or "").strip() or None,
            postcode=(row.get("postcode") or "").strip() or None,
            client_type=(row.get("client_type") or "").strip() or None,
            overall_status=(row.get("overall_status") or "").strip() or "Active",
            billing_model=(row.get("billing_model") or "Per job").strip() or "Per job",
            retainer_amount=_parse_float(row.get("retainer_amount")),
            retainer_frequency=(row.get("retainer_frequency") or "").strip() or None,
            retainer_notes=(row.get("retainer_notes") or "").strip() or None,
            vat_number=(row.get("vat_number") or "").strip() or None,
            utr=(row.get("utr") or "").strip() or None,
            engagement_date=_parse_date(row.get("engagement_date")),
            disengagement_date=_parse_date(row.get("disengagement_date")),
            ch_authentication_code=(row.get("ch_authentication_code") or "").strip() or None,
            ch_personal_code=(row.get("ch_personal_code") or "").strip() or None,
            notes=(row.get("notes") or "").strip() or None,
            source="csv_reimport",
        )
        db.add(client)
        result.created += 1

    db.commit()
    result.messages.append(result.summary())
    return result


# ---------------------------------------------------------------------------
# Jobs / WIP
# ---------------------------------------------------------------------------

JOB_EXPORT_HEADERS = [
    "id",
    "client_id",
    "client_name",
    "company_number",
    "type",
    "title",
    "status",
    "period_end",
    "statutory_due_date",
    "target_start",
    "target_completion",
    "actual_start",
    "actual_completion",
    "fee",
    "invoice_reference",
    "billing_status",
    "gross_amount",
    "vat_amount",
    "is_recurring",
    "notes",
]

JOB_UPDATABLE = {
    "type",
    "title",
    "status",
    "period_end",
    "statutory_due_date",
    "target_start",
    "target_completion",
    "actual_start",
    "actual_completion",
    "fee",
    "invoice_reference",
    "billing_status",
    "gross_amount",
    "vat_amount",
    "is_recurring",
    "notes",
    "client_id",
}

JOB_DATE_FIELDS = {
    "period_end",
    "statutory_due_date",
    "target_start",
    "target_completion",
    "actual_start",
    "actual_completion",
}
JOB_FLOAT_FIELDS = {"fee", "gross_amount", "vat_amount"}


def _job_row(j: Job) -> Dict[str, Any]:
    c = j.client
    return {
        "id": j.id,
        "client_id": j.client_id or "",
        "client_name": c.display_name() if c else "",
        "company_number": (c.company_number if c else "") or "",
        "type": j.type or "",
        "title": j.title or "",
        "status": j.status or "",
        "period_end": _fmt_date(j.period_end),
        "statutory_due_date": _fmt_date(j.statutory_due_date),
        "target_start": _fmt_date(j.target_start),
        "target_completion": _fmt_date(j.target_completion),
        "actual_start": _fmt_date(j.actual_start),
        "actual_completion": _fmt_date(j.actual_completion),
        "fee": f"{float(j.fee or 0):.2f}",
        "invoice_reference": j.invoice_reference or "",
        "billing_status": j.billing_status or "",
        "gross_amount": "" if j.gross_amount is None else f"{float(j.gross_amount):.2f}",
        "vat_amount": "" if j.vat_amount is None else f"{float(j.vat_amount):.2f}",
        "is_recurring": j.is_recurring or "",
        "notes": (j.notes or "").replace("\r\n", " ").replace("\n", " "),
    }


def export_wip_csv(db: Session) -> str:
    jobs = wip_jobs(db)
    jobs.sort(
        key=lambda j: (
            j.statutory_due_date or j.target_completion or date.max,
            j.id or 0,
        )
    )
    return rows_to_csv(JOB_EXPORT_HEADERS, [_job_row(j) for j in jobs])


def export_jobs_csv(
    db: Session,
    *,
    status: str = "",
    job_type: str = "",
    open_only: bool = False,
) -> str:
    q = db.query(Job).options(joinedload(Job.client)).order_by(Job.id.asc())
    if status:
        q = q.filter(Job.status == status)
    if job_type:
        q = q.filter(Job.type == job_type)
    jobs = q.all()
    if open_only:
        jobs = [j for j in jobs if (j.status or "") not in ("Completed", "Cancelled")]
    return rows_to_csv(JOB_EXPORT_HEADERS, [_job_row(j) for j in jobs])


def _apply_job_fields(job: Job, row: Dict[str, str]) -> bool:
    changed = False
    for key in JOB_UPDATABLE:
        if key not in row:
            continue
        raw = row.get(key)
        if key == "client_id":
            if raw is None or str(raw).strip() == "":
                continue
            val = _parse_int(raw)
        elif key in JOB_DATE_FIELDS:
            if raw is None or str(raw).strip() == "":
                continue
            val = _parse_date(raw)
        elif key in JOB_FLOAT_FIELDS:
            if raw is None or str(raw).strip() == "":
                continue
            val = _parse_float(raw)
            if val is None:
                continue
        else:
            val = (raw or "").strip() or None
        if getattr(job, key) != val:
            setattr(job, key, val)
            changed = True
    return changed


def reimport_jobs(db: Session, text: str, *, allow_create: bool = True) -> ExchangeResult:
    result = ExchangeResult()
    headers, rows, warnings = parse_csv_text(text)
    result.messages.extend(warnings)
    if not rows:
        result.errors.append("No rows to import.")
        return result

    for idx, row in enumerate(rows, start=2):
        jid = _parse_int(row.get("id"))
        job: Optional[Job] = None
        if jid:
            job = db.query(Job).filter(Job.id == jid).first()

        if job:
            if _apply_job_fields(job, row):
                job.updated_at = datetime.utcnow()
                result.updated += 1
            else:
                result.skipped += 1
            continue

        if not allow_create:
            result.skipped += 1
            result.errors.append(f"Row {idx}: job id {jid or '—'} not found (create disabled)")
            continue

        # Resolve client
        client_id = _parse_int(row.get("client_id"))
        client = None
        if client_id:
            client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            cn = row.get("company_number") or ""
            if cn.strip():
                cn_n = normalize_company_number(cn)
                client = db.query(Client).filter(Client.company_number == cn_n).first()
        if not client:
            result.skipped += 1
            result.errors.append(
                f"Row {idx}: cannot create job — client not found "
                f"(client_id / company_number)"
            )
            continue

        jtype = (row.get("type") or "Other").strip() or "Other"
        pe = _parse_date(row.get("period_end"))
        statutory, ts, tc = calculate_dates(jtype, pe)
        fee = _parse_float(row.get("fee")) or 0.0
        job = Job(
            client_id=client.id,
            type=jtype,
            title=(row.get("title") or "").strip() or f"{jtype}",
            status=(row.get("status") or "Planned").strip() or "Planned",
            period_end=pe,
            statutory_due_date=_parse_date(row.get("statutory_due_date")) or statutory,
            target_start=_parse_date(row.get("target_start")) or ts,
            target_completion=_parse_date(row.get("target_completion")) or tc,
            actual_start=_parse_date(row.get("actual_start")),
            actual_completion=_parse_date(row.get("actual_completion")),
            fee=fee,
            invoice_reference=(row.get("invoice_reference") or "").strip() or None,
            billing_status=(row.get("billing_status") or "").strip() or None,
            gross_amount=_parse_float(row.get("gross_amount")),
            vat_amount=_parse_float(row.get("vat_amount")),
            is_recurring=(row.get("is_recurring") or "Yes").strip() or "Yes",
            notes=(row.get("notes") or "").strip() or None,
            source="csv_reimport",
        )
        db.add(job)
        result.created += 1

    db.commit()
    result.messages.append(result.summary())
    return result


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

TASK_EXPORT_HEADERS = [
    "id",
    "title",
    "status",
    "priority",
    "fee",
    "due_on",
    "period_end",
    "source_email_date",
    "client_id",
    "client_name",
    "job_id",
    "job_type",
    "description",
    "notes",
]

TASK_UPDATABLE = {
    "title",
    "status",
    "priority",
    "fee",
    "due_on",
    "period_end",
    "source_email_date",
    "client_id",
    "job_id",
    "description",
    "notes",
}


def export_tasks_csv(db: Session, *, status: str = "", include_closed: bool = True) -> str:
    q = (
        db.query(PracticeTask)
        .options(joinedload(PracticeTask.client), joinedload(PracticeTask.job))
        .order_by(PracticeTask.id.asc())
    )
    if status:
        q = q.filter(PracticeTask.status == status)
    elif not include_closed:
        q = q.filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
    rows = []
    for t in q.all():
        rows.append(
            {
                "id": t.id,
                "title": t.title or "",
                "status": t.status or "",
                "priority": t.priority or "Medium",
                "fee": f"{float(t.fee or 0):.2f}",
                "due_on": _fmt_date(t.due_on),
                "period_end": _fmt_date(t.period_end),
                "source_email_date": _fmt_date(getattr(t, "source_email_date", None)),
                "client_id": t.client_id or "",
                "client_name": t.client.display_name() if t.client else "",
                "job_id": t.job_id or "",
                "job_type": (t.job.type if t.job else "") or "",
                "description": (t.description or "").replace("\n", " "),
                "notes": (t.notes or "").replace("\n", " "),
            }
        )
    return rows_to_csv(TASK_EXPORT_HEADERS, rows)


def reimport_tasks(db: Session, text: str, *, allow_create: bool = True) -> ExchangeResult:
    result = ExchangeResult()
    headers, rows, warnings = parse_csv_text(text)
    result.messages.extend(warnings)
    if not rows:
        result.errors.append("No rows to import.")
        return result

    for idx, row in enumerate(rows, start=2):
        tid = _parse_int(row.get("id"))
        task: Optional[PracticeTask] = None
        if tid:
            task = db.query(PracticeTask).filter(PracticeTask.id == tid).first()

        if task:
            changed = False
            for key in TASK_UPDATABLE:
                if key not in row:
                    continue
                raw = row.get(key)
                if key in ("due_on", "period_end", "source_email_date"):
                    if raw is None or str(raw).strip() == "":
                        continue
                    val = _parse_date(raw)
                elif key == "fee":
                    if raw is None or str(raw).strip() == "":
                        continue
                    val = _parse_float(raw)
                elif key in ("client_id", "job_id"):
                    if raw is None or str(raw).strip() == "":
                        continue
                    val = _parse_int(raw)
                elif key == "title":
                    val = (raw or "").strip() or task.title
                else:
                    val = (raw or "").strip() or None
                if getattr(task, key, None) != val:
                    setattr(task, key, val)
                    changed = True
            if changed:
                task.updated_at = datetime.utcnow()
                result.updated += 1
            else:
                result.skipped += 1
            continue

        if not allow_create:
            result.skipped += 1
            continue

        title = (row.get("title") or "").strip()
        if not title:
            result.skipped += 1
            result.errors.append(f"Row {idx}: title required to create task")
            continue
        pri = (row.get("priority") or "Medium").strip() or "Medium"
        task = PracticeTask(
            title=title,
            status=(row.get("status") or "Planned").strip() or "Planned",
            priority=pri,
            fee=_parse_float(row.get("fee")) or 0.0,
            due_on=_parse_date(row.get("due_on")),
            period_end=_parse_date(row.get("period_end")),
            source_email_date=_parse_date(row.get("source_email_date")),
            client_id=_parse_int(row.get("client_id")),
            job_id=_parse_int(row.get("job_id")),
            description=(row.get("description") or "").strip() or None,
            notes=(row.get("notes") or "").strip() or None,
        )
        db.add(task)
        result.created += 1

    db.commit()
    result.messages.append(result.summary())
    return result


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

PEOPLE_EXPORT_HEADERS = [
    "id",
    "full_name",
    "role",
    "email",
    "phone",
    "person_status",
    "is_primary",
    "is_individual_client",
    "utr",
    "ni_number",
    "ch_code",
    "company_numbers",
    "company_names",
    "notes",
]

PEOPLE_UPDATABLE = {
    "full_name",
    "role",
    "email",
    "phone",
    "person_status",
    "is_primary",
    "is_individual_client",
    "utr",
    "ni_number",
    "ch_code",
    "notes",
}


def export_people_csv(db: Session) -> str:
    people = (
        db.query(Person)
        .options(joinedload(Person.clients))
        .order_by(Person.full_name.asc(), Person.id.asc())
        .all()
    )
    rows = []
    for p in people:
        cos = p.clients or []
        rows.append(
            {
                "id": p.id,
                "full_name": p.full_name or "",
                "role": p.role or "",
                "email": p.email or "",
                "phone": p.phone or "",
                "person_status": p.person_status or "",
                "is_primary": "yes" if p.is_primary else "",
                "is_individual_client": "yes" if p.is_individual_client else "",
                "utr": p.utr or "",
                "ni_number": p.ni_number or "",
                "ch_code": p.ch_code or "",
                "company_numbers": ";".join(
                    (c.company_number or "") for c in cos if c.company_number
                ),
                "company_names": ";".join(
                    (c.company_name or "") for c in cos if c.company_name
                ),
                "notes": (p.notes or "").replace("\n", " "),
            }
        )
    return rows_to_csv(PEOPLE_EXPORT_HEADERS, rows)


def reimport_people(db: Session, text: str, *, allow_create: bool = True) -> ExchangeResult:
    result = ExchangeResult()
    headers, rows, warnings = parse_csv_text(text)
    result.messages.extend(warnings)
    if not rows:
        result.errors.append("No rows to import.")
        return result

    for idx, row in enumerate(rows, start=2):
        pid = _parse_int(row.get("id"))
        person: Optional[Person] = None
        if pid:
            person = (
                db.query(Person)
                .options(joinedload(Person.clients))
                .filter(Person.id == pid)
                .first()
            )

        def _boolish(v: Optional[str]) -> bool:
            return (v or "").strip().lower() in ("1", "yes", "y", "true", "t")

        if person:
            changed = False
            for key in PEOPLE_UPDATABLE:
                if key not in row:
                    continue
                raw = row.get(key)
                if key in ("is_primary", "is_individual_client"):
                    if raw is None or str(raw).strip() == "":
                        continue
                    val = _boolish(raw)
                elif key == "full_name":
                    val = (raw or "").strip() or person.full_name
                else:
                    val = (raw or "").strip() or None
                if getattr(person, key) != val:
                    setattr(person, key, val)
                    changed = True
            # Optional link by company numbers (additive)
            cns = (row.get("company_numbers") or "").strip()
            if cns:
                existing_ids = {c.id for c in (person.clients or [])}
                for part in re.split(r"[;|,]", cns):
                    part = part.strip()
                    if not part:
                        continue
                    cn = normalize_company_number(part)
                    c = db.query(Client).filter(Client.company_number == cn).first()
                    if c and c.id not in existing_ids:
                        person.clients.append(c)
                        existing_ids.add(c.id)
                        changed = True
            if changed:
                result.updated += 1
            else:
                result.skipped += 1
            continue

        if not allow_create:
            result.skipped += 1
            continue
        name = (row.get("full_name") or "").strip()
        if not name:
            result.skipped += 1
            result.errors.append(f"Row {idx}: full_name required to create person")
            continue
        person = Person(
            full_name=name,
            role=(row.get("role") or "").strip() or None,
            email=(row.get("email") or "").strip() or None,
            phone=(row.get("phone") or "").strip() or None,
            person_status=(row.get("person_status") or "").strip() or "Contact",
            is_primary=_boolish(row.get("is_primary")),
            is_individual_client=_boolish(row.get("is_individual_client")),
            utr=(row.get("utr") or "").strip() or None,
            ni_number=(row.get("ni_number") or "").strip() or None,
            ch_code=(row.get("ch_code") or "").strip() or None,
            notes=(row.get("notes") or "").strip() or None,
        )
        cns = (row.get("company_numbers") or "").strip()
        if cns:
            linked = []
            for part in re.split(r"[;|,]", cns):
                part = part.strip()
                if not part:
                    continue
                cn = normalize_company_number(part)
                c = db.query(Client).filter(Client.company_number == cn).first()
                if c:
                    linked.append(c)
            person.clients = linked
        db.add(person)
        result.created += 1

    db.commit()
    result.messages.append(result.summary())
    return result


# ---------------------------------------------------------------------------
# Registry for router
# ---------------------------------------------------------------------------

DATASETS = {
    "clients": {
        "label": "Clients",
        "export": lambda db, **kw: export_clients_csv(db, status=kw.get("status") or ""),
        "reimport": lambda db, text, **kw: reimport_clients(db, text),
        "filename": "clients.csv",
        "help": "Match by id or company_number. Updates contact/status/fees fields; creates new clients when no match.",
    },
    "wip": {
        "label": "WIP (open jobs)",
        "export": lambda db, **kw: export_wip_csv(db),
        "reimport": lambda db, text, **kw: reimport_jobs(db, text, allow_create=True),
        "filename": "wip_jobs.csv",
        "help": "Open WIP jobs only on export. Reimport updates by job id (e.g. fee, status, due dates) or creates if client found.",
    },
    "jobs": {
        "label": "Jobs",
        "export": lambda db, **kw: export_jobs_csv(
            db,
            status=kw.get("status") or "",
            job_type=kw.get("type") or "",
            open_only=kw.get("open_only") == "1",
        ),
        "reimport": lambda db, text, **kw: reimport_jobs(db, text, allow_create=True),
        "filename": "jobs.csv",
        "help": "All jobs (or filtered). Update by id; create with client_id or company_number.",
    },
    "tasks": {
        "label": "Tasks",
        "export": lambda db, **kw: export_tasks_csv(
            db, status=kw.get("status") or "", include_closed=kw.get("include_closed") != "0"
        ),
        "reimport": lambda db, text, **kw: reimport_tasks(db, text, allow_create=True),
        "filename": "tasks.csv",
        "help": "Match by task id. Update fee/status/due; create with title.",
    },
    "people": {
        "label": "People",
        "export": lambda db, **kw: export_people_csv(db),
        "reimport": lambda db, text, **kw: reimport_people(db, text, allow_create=True),
        "filename": "people.csv",
        "help": "Match by person id. Update contacts/CH PIC; link companies via company_numbers (semicolon-separated).",
    },
}
