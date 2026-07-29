"""
Selective “Push email → Task” — create a PracticeTask from an Outlook/email payload.

Chatter stays in Outlook; only emails deliberately pushed become tasks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.practice_task import PracticeTask
from app.services.client_matching import match_client, normalize_client_name
from app.services.practice_tasks import create_task
from app.services.task_import import DATE_WINDOW_DAYS, _subject_similar

IMPORT_SOURCE = "outlook_push"
PREVIEW_MAX = 500
BODY_MAX = 12000


@dataclass
class EmailTaskPayload:
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    to: str = ""
    received_at: Optional[datetime] = None
    body_preview: str = ""
    body: str = ""
    message_id: str = ""
    conversation_id: str = ""
    web_link: str = ""
    priority: str = "Medium"
    # Optional overrides from the Outlook add-in
    client_id: Optional[int] = None
    job_id: Optional[int] = None


@dataclass
class EmailTaskResult:
    ok: bool
    created: bool = False
    duplicate: bool = False
    task_id: Optional[int] = None
    client_id: Optional[int] = None
    client_match: str = "none"
    client_name: str = ""
    message: str = ""
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "created": self.created,
            "duplicate": self.duplicate,
            "task_id": self.task_id,
            "client_id": self.client_id,
            "client_match": self.client_match,
            "client_name": self.client_name,
            "message": self.message,
            "errors": self.errors,
            "task_url": f"/tasks/{self.task_id}/edit" if self.task_id else None,
        }


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, datetime.min.time())
    s = str(value).strip()
    if not s:
        return None
    # ISO with Z
    s2 = s.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s2[:32])
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def _extract_email_address(raw: str) -> str:
    """'Name <a@b.com>' or bare address → a@b.com"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"<([^>]+@[^>]+)>", s)
    if m:
        return m.group(1).strip().lower()
    if "@" in s:
        # last token that looks like email
        for part in re.split(r"[\s,;]+", s):
            if "@" in part:
                return part.strip("<>\"' ").lower()
    return ""


def _extract_display_name(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.match(r"^([^<]+)<", s)
    if m:
        return m.group(1).strip().strip("\"'")
    if "@" in s and " " not in s:
        return s.split("@", 1)[0]
    return s


def parse_payload(data: Dict[str, Any]) -> Tuple[Optional[EmailTaskPayload], List[str]]:
    """Normalise JSON / form-like dict into EmailTaskPayload."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return None, ["Body must be a JSON object"]

    def g(*keys: str, default: str = "") -> str:
        for k in keys:
            if k in data and data[k] is not None:
                return str(data[k]).strip()
        return default

    subject = g("subject", "title", "Subject")
    if not subject:
        errors.append("subject is required")

    from_raw = g("from", "from_email", "sender", "From", "email_from")
    from_name = g("from_name", "sender_name", "FromName") or _extract_display_name(from_raw)
    from_email = g("from_address", "sender_email") or _extract_email_address(from_raw)
    if not from_email and "@" in from_raw:
        from_email = _extract_email_address(from_raw)

    to_raw = g("to", "To", "email_to", "recipients")
    received = _parse_datetime(
        data.get("received_at")
        or data.get("received")
        or data.get("date")
        or data.get("receivedDateTime")
        or data.get("source_email_date")
    )
    preview = g("body_preview", "preview", "snippet", "bodyPreview")
    body = g("body", "body_text", "Body", "content")
    if not preview and body:
        preview = re.sub(r"\s+", " ", body).strip()[:PREVIEW_MAX]
    message_id = g(
        "message_id",
        "messageId",
        "outlook_message_id",
        "id",
        "internetMessageId",
        "internet_message_id",
    )
    conversation_id = g(
        "conversation_id",
        "conversationId",
        "outlook_conversation_id",
    )
    web_link = g(
        "web_link",
        "webLink",
        "outlook_web_link",
        "link",
        "url",
    )
    priority = g("priority", "importance") or "Medium"
    if priority.lower() in ("high", "2", "urgent"):
        priority = "High"
    elif priority.lower() in ("low", "0"):
        priority = "Low"
    else:
        priority = "Medium"

    client_id = None
    raw_cid = data.get("client_id")
    if raw_cid is not None and str(raw_cid).strip().isdigit():
        client_id = int(raw_cid)
    job_id = None
    raw_jid = data.get("job_id")
    if raw_jid is not None and str(raw_jid).strip().isdigit():
        job_id = int(raw_jid)

    if errors:
        return None, errors

    return (
        EmailTaskPayload(
            subject=subject,
            from_name=from_name,
            from_email=from_email,
            to=to_raw[:500] if to_raw else "",
            received_at=received,
            body_preview=preview[:PREVIEW_MAX] if preview else "",
            body=body[:BODY_MAX] if body else "",
            message_id=message_id,
            conversation_id=conversation_id,
            web_link=web_link,
            priority=priority,
            client_id=client_id,
            job_id=job_id,
        ),
        [],
    )


def push_hash(payload: EmailTaskPayload) -> str:
    """Stable dedupe key: prefer Graph message id, else subject+from+date."""
    if (payload.message_id or "").strip():
        key = f"mid:{payload.message_id.strip()}"
    else:
        d = (
            payload.received_at.date().isoformat()
            if payload.received_at
            else ""
        )
        key = (
            f"sf:{normalize_client_name(payload.subject)}|"
            f"{(payload.from_email or payload.from_name or '').lower()}|{d}"
        )
    return hashlib.sha1(f"{IMPORT_SOURCE}|{key}".encode("utf-8")).hexdigest()


def find_duplicate(db: Session, payload: EmailTaskPayload) -> Optional[PracticeTask]:
    """Return existing open/recent task if this email was already pushed."""
    mid = (payload.message_id or "").strip()
    if mid:
        hit = (
            db.query(PracticeTask)
            .filter(PracticeTask.outlook_message_id == mid)
            .order_by(PracticeTask.id.desc())
            .first()
        )
        if hit:
            return hit

    h = push_hash(payload)
    hit = (
        db.query(PracticeTask)
        .filter(PracticeTask.import_hash == h)
        .order_by(PracticeTask.id.desc())
        .first()
    )
    if hit:
        return hit

    # Soft: same subject + sender + similar date among open tasks
    open_tasks = (
        db.query(PracticeTask)
        .filter(PracticeTask.status.notin_(["Completed", "Cancelled"]))
        .order_by(PracticeTask.id.desc())
        .limit(400)
        .all()
    )
    recv = payload.received_at.date() if payload.received_at else None
    from_key = (payload.from_email or payload.from_name or "").strip().lower()
    for t in open_tasks:
        ok_subj, _ = _subject_similar(payload.subject, t.title or "")
        if not ok_subj:
            continue
        t_from = (t.email_from or "").strip().lower()
        if from_key and t_from and from_key not in t_from and t_from not in from_key:
            continue
        t_date = t.source_email_date
        if recv and t_date and abs((recv - t_date).days) > DATE_WINDOW_DAYS:
            continue
        if ok_subj and (from_key or not t_from):
            # If both lack from, still require date window
            if not from_key and not t_from:
                if recv and t_date and abs((recv - t_date).days) <= DATE_WINDOW_DAYS:
                    return t
                continue
            return t
    return None


def resolve_client(
    db: Session, payload: EmailTaskPayload
) -> Tuple[Optional[int], str, str]:
    """
    Returns (client_id, match_status, client_name).
    Uses explicit client_id if provided, else email domain / from name.
    """
    if payload.client_id:
        from app.models import Client

        c = db.query(Client).filter(Client.id == payload.client_id).first()
        if c:
            return c.id, "explicit", c.display_name()
        return None, "none", ""

    name_hint = payload.from_name
    # Sometimes org name is in the display name after a dash
    m = match_client(db, name=name_hint, email=payload.from_email)
    if m.client and m.status not in ("none", "ambiguous"):
        return m.client.id, m.status, m.client.display_name()
    if m.status == "ambiguous":
        return None, "ambiguous", ""
    return None, "none", ""


def _build_description(payload: EmailTaskPayload) -> str:
    parts: List[str] = []
    if payload.from_name or payload.from_email:
        who = payload.from_name or ""
        if payload.from_email:
            who = f"{who} <{payload.from_email}>".strip()
        parts.append(f"From: {who}")
    if payload.to:
        parts.append(f"To: {payload.to}")
    if payload.received_at:
        parts.append(f"Received: {payload.received_at.strftime('%Y-%m-%d %H:%M')}")
    body = (payload.body or payload.body_preview or "").strip()
    if body:
        parts.append("")
        parts.append(body[:BODY_MAX])
    return "\n".join(parts).strip()


def create_task_from_email(db: Session, payload: EmailTaskPayload) -> EmailTaskResult:
    """Create PracticeTask from pushed email, or return existing if duplicate."""
    if not (payload.subject or "").strip():
        return EmailTaskResult(ok=False, errors=["subject is required"])

    dup = find_duplicate(db, payload)
    if dup:
        return EmailTaskResult(
            ok=True,
            created=False,
            duplicate=True,
            task_id=dup.id,
            client_id=dup.client_id,
            client_match="existing",
            client_name=dup.client.display_name() if dup.client else "",
            message=f"Already a task (#{dup.id}) — not created again",
        )

    client_id, match_status, client_name = resolve_client(db, payload)
    recv_date = payload.received_at.date() if payload.received_at else date.today()
    h = push_hash(payload)
    description = _build_description(payload)
    preview = (payload.body_preview or "").strip() or (
        re.sub(r"\s+", " ", payload.body or "").strip()[:PREVIEW_MAX]
    )

    task = create_task(
        db,
        title=payload.subject.strip()[:240],
        description=description or None,
        client_id=client_id,
        job_id=payload.job_id,
        status="Planned",
        priority=payload.priority or "Medium",
        source_email_date=recv_date,
        import_source=IMPORT_SOURCE,
        import_hash=h,
        email_from=(
            f"{payload.from_name} <{payload.from_email}>".strip()
            if payload.from_email
            else (payload.from_name or None)
        ),
        email_to=payload.to or None,
        email_preview=preview or None,
        outlook_message_id=payload.message_id or None,
        outlook_conversation_id=payload.conversation_id or None,
        outlook_web_link=payload.web_link or None,
        notes=None,
        commit=True,
    )

    msg = "Task created from email"
    if client_id:
        msg += f" · linked to {client_name} ({match_status})"
    elif match_status == "ambiguous":
        msg += " · client ambiguous — link manually"
    else:
        msg += " · no client match — link manually"

    return EmailTaskResult(
        ok=True,
        created=True,
        duplicate=False,
        task_id=task.id,
        client_id=client_id,
        client_match=match_status,
        client_name=client_name,
        message=msg,
    )
