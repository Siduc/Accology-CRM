"""Restore clients, people, and jobs from a backup.json payload."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.models import Client, Job, Person, person_clients
from app.services.company_numbers import normalize_company_number

# Scalar columns we restore (never id / relationships)
CLIENT_FIELDS = (
    "company_name",
    "company_number",
    "contact_name",
    "email",
    "phone",
    "address_line1",
    "address_line2",
    "town",
    "postcode",
    "client_type",
    "overall_status",
    "vat_number",
    "utr",
    "paye_reference",
    "accounts_office_reference",
    "gov_gateway_username",
    "gov_gateway_password",
    "accounts_software_id",
    "accounts_software_password",
    "xero_username",
    "xero_password",
    "ch_authentication_code",
    "ch_personal_code",
    "notes",
    "source",
)

PERSON_FIELDS = (
    "full_name",
    "email",
    "phone",
    "role",
    "utr",
    "ni_number",
    "ch_code",
    "person_status",
    "is_primary",
    "is_individual_client",
    "notes",
)

JOB_FIELDS = (
    "title",
    "type",
    "period_end",
    "statutory_due_date",
    "target_start",
    "target_completion",
    "actual_start",
    "actual_completion",
    "fee",
    "status",
    "is_recurring",
    "notes",
    "source",
    "invoice_reference",
    "billing_status",
    "gross_amount",
    "vat_amount",
    "was_late",
    "lost_reason",
    "import_key",
)

CLIENT_DATE_FIELDS: Set[str] = set()
PERSON_DATE_FIELDS: Set[str] = set()
JOB_DATE_FIELDS = {
    "period_end",
    "statutory_due_date",
    "target_start",
    "target_completion",
    "actual_start",
    "actual_completion",
}


@dataclass
class RestoreResult:
    clients_created: int = 0
    clients_updated: int = 0
    clients_skipped: int = 0
    people_created: int = 0
    people_updated: int = 0
    people_skipped: int = 0
    jobs_created: int = 0
    jobs_updated: int = 0
    jobs_skipped: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "clients_created": self.clients_created,
            "clients_updated": self.clients_updated,
            "clients_skipped": self.clients_skipped,
            "people_created": self.people_created,
            "people_updated": self.people_updated,
            "people_skipped": self.people_skipped,
            "jobs_created": self.jobs_created,
            "jobs_updated": self.jobs_updated,
            "jobs_skipped": self.jobs_skipped,
            "errors": self.errors,
        }


def parse_backup_json(raw: bytes | str) -> dict:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig")
    else:
        text = raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Backup JSON must be an object with clients/people/jobs arrays.")
    return data


def restore_from_backup(db: Session, data: dict) -> RestoreResult:
    result = RestoreResult()
    clients_data = data.get("clients") or []
    people_data = data.get("people") or []
    jobs_data = data.get("jobs") or []

    if not isinstance(clients_data, list):
        raise ValueError("'clients' must be a list")
    if not isinstance(people_data, list):
        raise ValueError("'people' must be a list")
    if not isinstance(jobs_data, list):
        raise ValueError("'jobs' must be a list")

    client_id_map: Dict[int, int] = {}  # backup id -> db id

    _restore_clients(db, clients_data, client_id_map, result)
    _restore_people(db, people_data, client_id_map, result)
    _restore_jobs(db, jobs_data, client_id_map, result)

    db.commit()
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_junk_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.startswith("<app.models."):
        return True
    if isinstance(value, (list, dict)):
        return True
    return False


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _norm_compare(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s if s else None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, float):
        return round(value, 4)
    return value


def _parse_date(value: Any) -> Optional[date]:
    if _empty(value) or _is_junk_value(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # "2025-03-31" or "2025-03-31 00:00:00"
    s = s.replace("T", " ").split(".")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _clean_row(raw: dict, fields: Iterable[str], date_fields: Set[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in fields:
        if key not in raw:
            continue
        val = raw.get(key)
        if _is_junk_value(val):
            continue
        if key in date_fields:
            out[key] = _parse_date(val)
        elif key in ("is_primary", "is_individual_client"):
            out[key] = _parse_bool(val)
        elif key in ("fee", "gross_amount", "vat_amount"):
            if _empty(val):
                out[key] = None if key != "fee" else 0.0
            else:
                try:
                    out[key] = float(val)
                except (TypeError, ValueError):
                    out[key] = None if key != "fee" else 0.0
        else:
            if isinstance(val, str):
                out[key] = val.strip() if val.strip() else None
            else:
                out[key] = val
    return out


_BOOL_FIELDS = frozenset({"is_primary", "is_individual_client"})


def _fields_equal(obj: Any, payload: Dict[str, Any], fields: Iterable[str]) -> bool:
    for key in fields:
        if key not in payload:
            continue
        current = getattr(obj, key, None)
        new = payload[key]
        # SQLite often stores booleans as 0/1
        if key in _BOOL_FIELDS:
            if bool(current) != bool(new):
                return False
            continue
        if _norm_compare(current) != _norm_compare(new):
            return False
    return True


def _apply_fields(obj: Any, payload: Dict[str, Any], fields: Iterable[str]) -> None:
    for key in fields:
        if key in payload:
            setattr(obj, key, payload[key])


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def _restore_clients(
    db: Session,
    rows: List[dict],
    client_id_map: Dict[int, int],
    result: RestoreResult,
) -> None:
    existing = db.query(Client).all()
    by_number: Dict[str, Client] = {}
    for c in existing:
        cn = normalize_company_number(c.company_number)
        if cn:
            by_number[cn] = c

    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            result.errors.append(f"Client row {i + 1}: not an object")
            result.clients_skipped += 1
            continue

        payload = _clean_row(raw, CLIENT_FIELDS, CLIENT_DATE_FIELDS)
        cn = normalize_company_number(payload.get("company_number") or raw.get("company_number"))
        if not cn:
            result.errors.append(
                f"Client row {i + 1} ({payload.get('company_name') or '?'}): "
                "missing company_number — skipped"
            )
            result.clients_skipped += 1
            continue

        payload["company_number"] = cn
        if not payload.get("source"):
            payload["source"] = "backup"

        backup_id = raw.get("id")
        found = by_number.get(cn)

        if found:
            if _fields_equal(found, payload, CLIENT_FIELDS):
                result.clients_skipped += 1
            else:
                _apply_fields(found, payload, CLIENT_FIELDS)
                found.updated_at = datetime.utcnow()
                result.clients_updated += 1
            if isinstance(backup_id, int):
                client_id_map[backup_id] = found.id
        else:
            client = Client(**{k: payload.get(k) for k in CLIENT_FIELDS if k in payload})
            if not client.overall_status:
                client.overall_status = "Active"
            db.add(client)
            db.flush()
            by_number[cn] = client
            result.clients_created += 1
            if isinstance(backup_id, int):
                client_id_map[backup_id] = client.id


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


def _person_match_key(payload: Dict[str, Any]) -> Optional[Tuple]:
    """Stable identity for people. Prefer email+name so shared mailboxes do not merge."""
    email = (payload.get("email") or "").strip().lower() if payload.get("email") else ""
    name = (payload.get("full_name") or "").strip().lower() if payload.get("full_name") else ""
    ni = (payload.get("ni_number") or "").strip().upper() if payload.get("ni_number") else ""
    utr = (payload.get("utr") or "").strip() if payload.get("utr") else ""
    if email and name:
        return ("email_name", email, name)
    if name and ni:
        return ("name_ni", name, ni)
    if name and utr:
        return ("name_utr", name, utr)
    if email:
        return ("email", email)
    if name:
        return ("name", name)
    return None


def _restore_people(
    db: Session,
    rows: List[dict],
    client_id_map: Dict[int, int],
    result: RestoreResult,
) -> None:
    existing = db.query(Person).all()
    index: Dict[Tuple, Person] = {}
    for p in existing:
        key = _person_match_key(
            {
                "email": p.email,
                "full_name": p.full_name,
                "ni_number": p.ni_number,
                "utr": p.utr,
            }
        )
        if key and key not in index:
            index[key] = p

    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            result.errors.append(f"Person row {i + 1}: not an object")
            result.people_skipped += 1
            continue

        payload = _clean_row(raw, PERSON_FIELDS, PERSON_DATE_FIELDS)
        if not payload.get("full_name"):
            result.errors.append(f"Person row {i + 1}: missing full_name — skipped")
            result.people_skipped += 1
            continue

        key = _person_match_key(payload)
        found = index.get(key) if key else None

        # Map legacy client_id
        mapped_client_id: Optional[int] = None
        raw_cid = raw.get("client_id")
        if isinstance(raw_cid, int) and raw_cid in client_id_map:
            mapped_client_id = client_id_map[raw_cid]

        if found:
            if _fields_equal(found, payload, PERSON_FIELDS):
                result.people_skipped += 1
            else:
                _apply_fields(found, payload, PERSON_FIELDS)
                result.people_updated += 1
            person = found
        else:
            person = Person(**{k: payload.get(k) for k in PERSON_FIELDS if k in payload})
            if mapped_client_id:
                person.client_id = mapped_client_id
            db.add(person)
            db.flush()
            if key:
                index[key] = person
            result.people_created += 1

        if mapped_client_id:
            person.client_id = mapped_client_id
            _ensure_person_client_link(db, person.id, mapped_client_id, person.role, person.is_primary)


def _ensure_person_client_link(
    db: Session,
    person_id: int,
    client_id: int,
    role: Optional[str],
    is_primary: bool,
) -> None:
    exists = db.execute(
        person_clients.select().where(
            (person_clients.c.person_id == person_id)
            & (person_clients.c.client_id == client_id)
        )
    ).first()
    if exists:
        return
    db.execute(
        person_clients.insert().values(
            person_id=person_id,
            client_id=client_id,
            role=role,
            is_primary=bool(is_primary),
        )
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _restore_jobs(
    db: Session,
    rows: List[dict],
    client_id_map: Dict[int, int],
    result: RestoreResult,
) -> None:
    existing = db.query(Job).all()
    by_import_key: Dict[str, Job] = {}
    by_natural: Dict[Tuple, Job] = {}
    for j in existing:
        if j.import_key:
            by_import_key[j.import_key] = j
        nat = (
            j.client_id,
            (j.type or "").strip().lower(),
            j.period_end,
            (j.title or "").strip().lower(),
        )
        by_natural[nat] = j

    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            result.errors.append(f"Job row {i + 1}: not an object")
            result.jobs_skipped += 1
            continue

        payload = _clean_row(raw, JOB_FIELDS, JOB_DATE_FIELDS)

        raw_cid = raw.get("client_id")
        if not isinstance(raw_cid, int) or raw_cid not in client_id_map:
            # Try resolve by company_number on the job row if present
            cn = normalize_company_number(raw.get("company_number"))
            if cn:
                client = db.query(Client).filter(Client.company_number == cn).first()
                if client:
                    db_client_id = client.id
                else:
                    result.errors.append(
                        f"Job row {i + 1} ({payload.get('title') or '?'}): "
                        "unknown client — skipped"
                    )
                    result.jobs_skipped += 1
                    continue
            else:
                result.errors.append(
                    f"Job row {i + 1} ({payload.get('title') or '?'}): "
                    "unmapped client_id — skipped"
                )
                result.jobs_skipped += 1
                continue
        else:
            db_client_id = client_id_map[raw_cid]

        ik = payload.get("import_key")
        found: Optional[Job] = None
        if ik and ik in by_import_key:
            found = by_import_key[ik]
        else:
            nat = (
                db_client_id,
                (payload.get("type") or "").strip().lower(),
                payload.get("period_end"),
                (payload.get("title") or "").strip().lower(),
            )
            found = by_natural.get(nat)

        if found:
            compare_payload = dict(payload)
            # client_id is not in JOB_FIELDS compare for equality of job data
            if _fields_equal(found, compare_payload, JOB_FIELDS) and found.client_id == db_client_id:
                result.jobs_skipped += 1
            else:
                _apply_fields(found, payload, JOB_FIELDS)
                found.client_id = db_client_id
                found.updated_at = datetime.utcnow()
                result.jobs_updated += 1
        else:
            job = Job(**{k: payload.get(k) for k in JOB_FIELDS if k in payload})
            job.client_id = db_client_id
            if job.fee is None:
                job.fee = 0.0
            if not job.status:
                job.status = "Planned"
            db.add(job)
            db.flush()
            if job.import_key:
                by_import_key[job.import_key] = job
            nat = (
                db_client_id,
                (job.type or "").strip().lower(),
                job.period_end,
                (job.title or "").strip().lower(),
            )
            by_natural[nat] = job
            result.jobs_created += 1
