"""Import outstanding tasks from Grok/Outlook (paste or CSV) with review."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.practice_task import PracticeTask
from app.services.client_matching import match_client, normalize_client_name
from app.services.practice_tasks import TASK_PRIORITIES, create_task

IMPORT_SOURCE = "outlook_grok"
SUBJECT_SIM_THRESHOLD = 0.88
DATE_WINDOW_DAYS = 3

# Header aliases → logical field
_HEADER_MAP = {
    "subject": "subject",
    "title": "subject",
    "task": "subject",
    "item": "subject",
    "summary": "subject",
    "task name": "subject",
    "description": "description",
    "body": "description",
    "details": "description",
    "notes body": "description",
    "client": "client",
    "company": "client",
    "company name": "client",
    "client name": "client",
    "organisation": "client",
    "organization": "client",
    "email": "email",
    "from": "email",
    "from email": "email",
    "sender": "email",
    "due": "due_on",
    "due date": "due_on",
    "due_on": "due_on",
    "deadline": "due_on",
    "email date": "source_email_date",
    "received": "source_email_date",
    "source date": "source_email_date",
    "message date": "source_email_date",
    "date": "source_email_date",
    "notes": "notes",
    "comment": "notes",
    "remark": "notes",
    "priority": "priority",
    "importance": "priority",
    "urgency": "priority",
    "fee": "fee",
    "amount": "fee",
    "outlook": "web_link",
    "outlook link": "web_link",
    "web link": "web_link",
    "weblink": "web_link",
    "outlook_web_link": "web_link",
    "message id": "message_id",
    "message_id": "message_id",
    "outlook_message_id": "message_id",
}


@dataclass
class TaskImportRow:
    row_num: int = 0
    subject: str = ""
    description: str = ""
    client_raw: str = ""
    email_raw: str = ""
    due_on: Optional[str] = None  # ISO date string for JSON
    source_email_date: Optional[str] = None
    notes: str = ""
    priority: str = "Medium"
    fee: float = 0.0
    client_id: Optional[int] = None
    client_match_status: str = "none"
    client_candidates: List[dict] = field(default_factory=list)
    client_match_name: str = ""
    is_duplicate: bool = False
    duplicate_of_id: Optional[int] = None
    duplicate_reason: str = ""
    include: bool = True
    error: str = ""

    def due_date(self) -> Optional[date]:
        return _parse_iso(self.due_on)

    def source_date(self) -> Optional[date]:
        return _parse_iso(self.source_email_date)


def new_batch_id() -> str:
    return secrets.token_hex(8)


def _norm_header(h: str) -> str:
    s = (h or "").replace("\ufeff", "").strip().lower()
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_date_flexible(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().replace("\xa0", " ")
    if not s:
        return None
    if "T" in s:
        s = s.split("T", 1)[0].strip()
    elif " " in s and re.search(r"\d{1,2}:\d{2}", s):
        s = s.split(" ", 1)[0].strip()
    # Excel serial
    try:
        if re.fullmatch(r"\d+(\.\d+)?", s):
            n = float(s)
            if 30000 < n < 60000:
                # Excel epoch 1899-12-30
                base = date(1899, 12, 30)
                return base + timedelta(days=int(n))
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d/%m/%y",
        "%d-%m-%y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%d %b %Y",
        "%d %B %Y",
    ):
        try:
            return datetime.strptime(s[:20].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_money(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace("£", "").replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _norm_priority(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in ("high", "h", "1", "urgent", "important"):
        return "High"
    if s in ("low", "l", "3"):
        return "Low"
    if s in ("medium", "med", "m", "2", "normal", ""):
        return "Medium"
    # title case match
    for p in TASK_PRIORITIES:
        if s == p.lower():
            return p
    return "Medium"


def _detect_delimiter(sample: str) -> str:
    first = (sample or "").splitlines()[0] if sample else ""
    if first.count("\t") >= first.count(",") and first.count("\t") > 0:
        return "\t"
    if first.count(";") > first.count(","):
        return ";"
    return ","


def _map_headers(fieldnames: List[str]) -> Dict[str, str]:
    """Map logical field → actual header name."""
    out: Dict[str, str] = {}
    for h in fieldnames or []:
        key = _HEADER_MAP.get(_norm_header(h))
        if key and key not in out:
            out[key] = h
    return out


def parse_task_csv(text: str) -> List[TaskImportRow]:
    text = (text or "").replace("\ufeff", "").strip()
    if not text:
        return []
    delim = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        return []
    mapping = _map_headers(list(reader.fieldnames))
    # If no subject column, treat first column as subject
    if "subject" not in mapping and reader.fieldnames:
        mapping["subject"] = reader.fieldnames[0]

    rows: List[TaskImportRow] = []
    for idx, raw in enumerate(reader, start=2):
        def g(logical: str) -> str:
            h = mapping.get(logical)
            if not h:
                return ""
            return (raw.get(h) or "").strip()

        subject = g("subject")
        if not subject:
            # skip empty
            continue
        due = parse_date_flexible(g("due_on"))
        src = parse_date_flexible(g("source_email_date"))
        rows.append(
            TaskImportRow(
                row_num=idx,
                subject=subject,
                description=g("description"),
                client_raw=g("client"),
                email_raw=g("email"),
                due_on=due.isoformat() if due else None,
                source_email_date=src.isoformat() if src else None,
                notes=g("notes"),
                priority=_norm_priority(g("priority")),
                fee=_parse_money(g("fee")),
            )
        )
    return rows


def parse_task_paste(text: str) -> List[TaskImportRow]:
    """
    Support:
    - Labelled blocks (Subject: / Client: / Due:) separated by blank lines
    - Bullet / numbered lines
    - Pipe-separated: Subject | Client | Due
    - Plain one-task-per-line
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    # Prefer labelled blocks only when the first content line is a Label:
    # (pure Grok/Outlook structured paste). Mixed bullet lists use line mode.
    nonempty = [ln for ln in text.split("\n") if ln.strip()]
    first = nonempty[0] if nonempty else ""
    if re.match(
        r"^\s*(subject|title|task|client|company|due|priority|notes|description)\s*:",
        first,
        re.I,
    ):
        return _parse_labelled_blocks(text)

    # Line mode; also stitch inline Label: blocks that appear mid-paste
    raw_lines = text.split("\n")
    rows: List[TaskImportRow] = []
    i = 0
    row_num = 0
    while i < len(raw_lines):
        ln = raw_lines[i].strip()
        i += 1
        if not ln:
            continue
        # Start of a labelled mini-block
        if re.match(
            r"^\s*(subject|title|task)\s*:",
            ln,
            re.I,
        ):
            block_lines = [ln]
            while i < len(raw_lines):
                nxt = raw_lines[i].strip()
                if not nxt:
                    i += 1
                    break
                if re.match(r"^\s*[A-Za-z][A-Za-z /_]+\s*:", nxt):
                    block_lines.append(nxt)
                    i += 1
                    continue
                break
            block_rows = _parse_labelled_blocks("\n".join(block_lines))
            for br in block_rows:
                row_num += 1
                br.row_num = row_num
                rows.append(br)
            continue

        ln = re.sub(r"^[-*•]\s+", "", ln)
        ln = re.sub(r"^\d+[.)]\s+", "", ln)
        # Skip orphan label lines (Client:/Due: without Subject:)
        if re.match(
            r"^\s*(client|company|due|priority|notes|description|email|from)\s*:",
            ln,
            re.I,
        ):
            continue
        row_num += 1
        if "|" in ln:
            parts = [p.strip() for p in ln.split("|")]
            subject = parts[0] if parts else ln
            client = parts[1] if len(parts) > 1 else ""
            due = parse_date_flexible(parts[2]) if len(parts) > 2 else None
            rows.append(
                TaskImportRow(
                    row_num=row_num,
                    subject=subject,
                    client_raw=client,
                    due_on=due.isoformat() if due else None,
                    priority="Medium",
                )
            )
        else:
            due = None
            subject = ln
            m = re.search(
                r"(?:[\(—\-\u00b7·\|]|due:?)\s*"
                r"(\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}|\d{4}-\d{2}-\d{2})"
                r"\s*\)?\s*$",
                ln,
                re.I,
            )
            if m:
                due = parse_date_flexible(m.group(1))
                subject = ln[: m.start()].strip(" —-\t·|")
            rows.append(
                TaskImportRow(
                    row_num=row_num,
                    subject=subject,
                    due_on=due.isoformat() if due else None,
                    priority="Medium",
                )
            )
    return rows


def _parse_labelled_blocks(text: str) -> List[TaskImportRow]:
    blocks = re.split(r"\n\s*\n", text.strip())
    rows: List[TaskImportRow] = []
    for i, block in enumerate(blocks, start=1):
        data: Dict[str, str] = {}
        for ln in block.split("\n"):
            m = re.match(r"^\s*([A-Za-z][A-Za-z /_]+)\s*:\s*(.*)$", ln)
            if m:
                key = _HEADER_MAP.get(_norm_header(m.group(1)))
                if key:
                    data[key] = (m.group(2) or "").strip()
            elif "subject" not in data and ln.strip():
                data["subject"] = ln.strip()
        subject = data.get("subject") or ""
        if not subject:
            continue
        due = parse_date_flexible(data.get("due_on"))
        src = parse_date_flexible(data.get("source_email_date"))
        rows.append(
            TaskImportRow(
                row_num=i,
                subject=subject,
                description=data.get("description") or "",
                client_raw=data.get("client") or "",
                email_raw=data.get("email") or "",
                due_on=due.isoformat() if due else None,
                source_email_date=src.isoformat() if src else None,
                notes=data.get("notes") or "",
                priority=_norm_priority(data.get("priority") or ""),
                fee=_parse_money(data.get("fee")),
            )
        )
    return rows


def _subject_similar(a: str, b: str) -> Tuple[bool, float]:
    na = normalize_client_name(a)  # same collapse works for subjects
    nb = normalize_client_name(b)
    if not na or not nb:
        return False, 0.0
    if na == nb:
        return True, 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return ratio >= SUBJECT_SIM_THRESHOLD, ratio


def _dates_similar(a: Optional[date], b: Optional[date]) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return True  # treat missing as compatible
    return abs((a - b).days) <= DATE_WINDOW_DAYS


def import_hash_for(
    *, subject: str, client_id: Optional[int], due_on: Optional[date]
) -> str:
    key = f"{client_id or 0}|{normalize_client_name(subject)}|{due_on or ''}|{IMPORT_SOURCE}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def enrich_rows(db: Session, rows: List[TaskImportRow]) -> List[TaskImportRow]:
    open_tasks = (
        db.query(PracticeTask)
        .filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
        .all()
    )
    hash_index = {
        t.import_hash: t
        for t in open_tasks
        if t.import_hash
    }

    for row in rows:
        if not (row.subject or "").strip():
            row.error = "Missing subject"
            row.include = False
            continue

        # Client match
        m = match_client(
            db,
            name=row.client_raw,
            email=row.email_raw,
        )
        row.client_match_status = m.status
        row.client_candidates = m.candidates or []
        if m.client and m.status not in ("ambiguous", "none"):
            row.client_id = m.client.id
            row.client_match_name = m.client.display_name()
        elif m.status == "ambiguous":
            row.client_id = None
            row.client_match_name = ""
        else:
            row.client_id = None

        due = row.due_date()
        h = import_hash_for(
            subject=row.subject, client_id=row.client_id, due_on=due
        )
        # Duplicate by hash
        if h in hash_index:
            existing = hash_index[h]
            row.is_duplicate = True
            row.duplicate_of_id = existing.id
            row.duplicate_reason = f"Same import key as task #{existing.id}"
            row.include = False
            continue

        # Content similarity against open tasks
        for t in open_tasks:
            same_client = (t.client_id or None) == (row.client_id or None)
            if not same_client and row.client_id and t.client_id:
                continue
            if not same_client and (row.client_id or t.client_id):
                # one has client one doesn't — still compare if subjects very close
                pass
            ok, ratio = _subject_similar(row.subject, t.title or "")
            if not ok:
                continue
            if not _dates_similar(due, t.due_on):
                continue
            # Prefer same client
            if row.client_id and t.client_id and row.client_id != t.client_id:
                continue
            row.is_duplicate = True
            row.duplicate_of_id = t.id
            row.duplicate_reason = (
                f"Similar to open task #{t.id} ({t.title[:60]})"
                f" · similarity {ratio:.0%}"
            )
            row.include = False
            break

        if not row.is_duplicate:
            row.include = True

    return rows


def serialize_review(rows: List[TaskImportRow]) -> str:
    return json.dumps([asdict(r) for r in rows], ensure_ascii=False)


def deserialize_review(payload: str) -> List[TaskImportRow]:
    data = json.loads(payload or "[]")
    rows: List[TaskImportRow] = []
    for item in data:
        rows.append(TaskImportRow(**{k: item.get(k) for k in TaskImportRow.__dataclass_fields__}))
    return rows


def apply_review_overrides(
    rows: List[TaskImportRow],
    form: Dict[str, Any],
) -> List[TaskImportRow]:
    """Apply include / client_id / priority / due overrides from form fields."""
    for i, row in enumerate(rows):
        key_inc = f"include_{i}"
        # checkbox: present means include (unchecked fields omitted)
        row.include = key_inc in form or str(form.get(key_inc) or "") in (
            "1",
            "true",
            "on",
            "yes",
        )
        cid_raw = form.get(f"client_id_{i}")
        if cid_raw is not None:
            cid_s = str(cid_raw).strip()
            if cid_s.isdigit():
                row.client_id = int(cid_s)
                row.client_match_status = "manual"
            else:
                row.client_id = None
        pri = form.get(f"priority_{i}")
        if pri:
            row.priority = _norm_priority(str(pri))
        due = form.get(f"due_on_{i}")
        if due is not None:
            d = parse_date_flexible(due)
            row.due_on = d.isoformat() if d else None
    return rows


def commit_rows(
    db: Session,
    rows: List[TaskImportRow],
    *,
    uploaded_by: str = "",
) -> Dict[str, Any]:
    batch = new_batch_id()
    created = 0
    skipped_dupe = 0
    skipped = 0
    errors: List[str] = []
    created_ids: List[int] = []

    for row in rows:
        if row.error:
            skipped += 1
            errors.append(f"Row {row.row_num}: {row.error}")
            continue
        if not row.include:
            if row.is_duplicate:
                skipped_dupe += 1
            else:
                skipped += 1
            continue
        if not (row.subject or "").strip():
            skipped += 1
            continue

        due = row.due_date()
        h = import_hash_for(
            subject=row.subject, client_id=row.client_id, due_on=due
        )
        # Re-check hash
        exists = (
            db.query(PracticeTask)
            .filter(
                PracticeTask.import_hash == h,
                PracticeTask.status.notin_(["Cancelled"]),
            )
            .first()
        )
        if exists and not row.is_duplicate:
            # still create if user forced include on non-dupe path — allow
            pass
        if exists and row.is_duplicate:
            # user forced include on dupe — still create with new hash suffix
            h = hashlib.sha1(
                f"{h}|force|{batch}|{row.row_num}".encode("utf-8")
            ).hexdigest()

        notes_parts = []
        if row.notes:
            notes_parts.append(row.notes)
        if row.source_email_date:
            notes_parts.append(f"Source email: {row.source_email_date}")
        if uploaded_by:
            notes_parts.append(f"Imported by {uploaded_by}")
        notes = " · ".join(notes_parts) if notes_parts else None

        try:
            task = create_task(
                db,
                title=row.subject.strip(),
                description=row.description or None,
                client_id=row.client_id,
                job_id=None,
                fee=float(row.fee or 0),
                status="Planned",
                due_on=due,
                notes=notes,
                priority=row.priority or "Medium",
                source_email_date=row.source_date(),
                import_source=IMPORT_SOURCE,
                import_hash=h,
                import_batch_id=batch,
            )
            created += 1
            created_ids.append(task.id)
        except Exception as exc:
            errors.append(f"Row {row.row_num}: {exc}")
            skipped += 1

    return {
        "created": created,
        "skipped_dupe": skipped_dupe,
        "skipped": skipped,
        "errors": errors,
        "batch_id": batch,
        "created_ids": created_ids,
    }


def summary_counts(rows: List[TaskImportRow]) -> Dict[str, int]:
    will = sum(1 for r in rows if r.include and not r.error)
    dupes = sum(1 for r in rows if r.is_duplicate)
    unmatched = sum(
        1
        for r in rows
        if not r.client_id and r.client_match_status in ("none", "ambiguous")
    )
    errs = sum(1 for r in rows if r.error)
    return {
        "total": len(rows),
        "will_create": will,
        "duplicates": dupes,
        "unmatched": unmatched,
        "errors": errs,
    }
