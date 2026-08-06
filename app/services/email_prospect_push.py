"""
Push an Outlook email (sent proposal / outbound) into prospecting.

Creates:
  - Prospect activity (type=email, outbound)
  - Optional estimated_value update
  - Follow-up practice task
  - OneDrive upload of attachments under Accologise / Prospects / … / Proposals
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.prospecting import Prospect
from app.services import documents as docs_svc
from app.services.email_task_push import (
    _extract_email_address,
    _extract_display_name,
    _parse_datetime,
)
from app.services.practice_tasks import create_task
from app.services.prospecting import log_activity, rescore

IMPORT_SOURCE = "outlook_prospect_push"
PREVIEW_MAX = 800
BODY_MAX = 12000
DEFAULT_FOLLOW_UP_DAYS = 7


@dataclass
class AttachmentIn:
    filename: str
    content: bytes
    content_type: str = ""


@dataclass
class ProspectEmailPayload:
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    to: str = ""
    to_emails: List[str] = field(default_factory=list)
    received_at: Optional[datetime] = None
    body_preview: str = ""
    body: str = ""
    message_id: str = ""
    conversation_id: str = ""
    web_link: str = ""
    prospect_id: Optional[int] = None
    company_name: str = ""
    estimated_value: Optional[float] = None
    follow_up: bool = True
    follow_up_days: int = DEFAULT_FOLLOW_UP_DAYS
    direction: str = "outbound"
    attachments: List[AttachmentIn] = field(default_factory=list)


@dataclass
class ProspectEmailResult:
    ok: bool
    prospect_id: Optional[int] = None
    activity_id: Optional[int] = None
    task_id: Optional[int] = None
    document_ids: List[int] = field(default_factory=list)
    estimated_value: Optional[float] = None
    message: str = ""
    errors: List[str] = field(default_factory=list)
    upload_errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "prospect_id": self.prospect_id,
            "prospect_url": (
                f"/prospecting/prospects/{self.prospect_id}" if self.prospect_id else None
            ),
            "activity_id": self.activity_id,
            "task_id": self.task_id,
            "task_url": f"/tasks/{self.task_id}/edit" if self.task_id else None,
            "document_ids": self.document_ids,
            "estimated_value": self.estimated_value,
            "message": self.message,
            "errors": self.errors,
            "upload_errors": self.upload_errors,
        }


def _parse_money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("£", "").replace("$", "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _decode_attachment(raw: Any) -> Optional[AttachmentIn]:
    if not isinstance(raw, dict):
        return None
    name = (
        str(raw.get("filename") or raw.get("name") or raw.get("Name") or "").strip()
    )
    if not name:
        return None
    b64 = raw.get("content_base64") or raw.get("contentBase64") or raw.get("content") or ""
    if isinstance(b64, str) and b64.startswith("data:") and "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception:
        return None
    if not data:
        return None
    ct = str(raw.get("content_type") or raw.get("contentType") or "").strip()
    return AttachmentIn(filename=name[:200], content=data, content_type=ct)


def parse_payload(data: Dict[str, Any]) -> Tuple[Optional[ProspectEmailPayload], List[str]]:
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

    from_raw = g("from", "from_email", "sender", "From")
    from_name = g("from_name") or _extract_display_name(from_raw)
    from_email = g("from_address") or _extract_email_address(from_raw)

    to_raw = g("to", "To", "email_to", "recipients")
    to_emails: List[str] = []
    if to_raw:
        for part in re.split(r"[;,]", to_raw):
            em = _extract_email_address(part)
            if em:
                to_emails.append(em)
    # also accept to_emails array
    raw_list = data.get("to_emails") or data.get("recipients_list")
    if isinstance(raw_list, list):
        for item in raw_list:
            em = _extract_email_address(str(item))
            if em and em not in to_emails:
                to_emails.append(em)

    prospect_id = None
    raw_pid = data.get("prospect_id")
    if raw_pid is not None and str(raw_pid).strip().isdigit():
        prospect_id = int(raw_pid)

    company_name = g("company_name", "company", "Company")
    estimated = _parse_money(
        data.get("estimated_value")
        or data.get("value")
        or data.get("fee")
        or data.get("amount")
    )
    # also try subject/body for £10,000 style
    if estimated is None:
        estimated = _parse_money(subject) or _parse_money(g("body_preview", "body"))

    follow_raw = data.get("follow_up", data.get("create_task", True))
    if isinstance(follow_raw, str):
        follow_up = follow_raw.strip().lower() not in ("0", "false", "no", "off")
    else:
        follow_up = bool(follow_raw)

    try:
        follow_days = int(data.get("follow_up_days") or DEFAULT_FOLLOW_UP_DAYS)
    except (TypeError, ValueError):
        follow_days = DEFAULT_FOLLOW_UP_DAYS
    follow_days = max(1, min(follow_days, 90))

    direction = g("direction") or "outbound"
    if direction.lower() not in ("outbound", "inbound", "internal"):
        direction = "outbound"

    received = _parse_datetime(
        data.get("received_at")
        or data.get("sent_at")
        or data.get("sentDateTime")
        or data.get("date")
    )
    preview = g("body_preview", "preview", "snippet")
    body = g("body", "body_text", "Body")
    if not preview and body:
        preview = re.sub(r"\s+", " ", body).strip()[:PREVIEW_MAX]

    attachments: List[AttachmentIn] = []
    raw_atts = data.get("attachments") or data.get("files") or []
    if isinstance(raw_atts, list):
        for a in raw_atts:
            att = _decode_attachment(a)
            if att:
                attachments.append(att)

    if errors:
        return None, errors

    return (
        ProspectEmailPayload(
            subject=subject,
            from_name=from_name,
            from_email=from_email,
            to=to_raw[:500] if to_raw else "",
            to_emails=to_emails,
            received_at=received,
            body_preview=preview[:PREVIEW_MAX] if preview else "",
            body=body[:BODY_MAX] if body else "",
            message_id=g("message_id", "messageId", "id"),
            conversation_id=g("conversation_id", "conversationId"),
            web_link=g("web_link", "webLink", "link"),
            prospect_id=prospect_id,
            company_name=company_name,
            estimated_value=estimated,
            follow_up=follow_up,
            follow_up_days=follow_days,
            direction=direction.lower(),
            attachments=attachments,
        ),
        [],
    )


def match_prospect(db: Session, payload: ProspectEmailPayload) -> Optional[Prospect]:
    if payload.prospect_id:
        p = db.query(Prospect).filter(Prospect.id == payload.prospect_id).first()
        if p:
            return p

    # Prefer recipient emails (outbound proposal → prospect is the To:)
    candidates = list(payload.to_emails)
    if payload.direction == "inbound" and payload.from_email:
        candidates = [payload.from_email] + candidates

    for em in candidates:
        if not em:
            continue
        hit = (
            db.query(Prospect)
            .filter(Prospect.email.ilike(em))
            .order_by(Prospect.id.desc())
            .first()
        )
        if hit:
            return hit

    if payload.company_name:
        like = f"%{payload.company_name.strip()}%"
        hit = (
            db.query(Prospect)
            .filter(Prospect.company_name.ilike(like))
            .order_by(Prospect.id.desc())
            .first()
        )
        if hit:
            return hit

    # Fallback: from_email if not already tried
    if payload.from_email and payload.from_email not in candidates:
        hit = (
            db.query(Prospect)
            .filter(Prospect.email.ilike(payload.from_email))
            .order_by(Prospect.id.desc())
            .first()
        )
        if hit:
            return hit
    return None


def process_prospect_email(
    db: Session,
    payload: ProspectEmailPayload,
    *,
    uploaded_by: str = "outlook",
) -> ProspectEmailResult:
    prospect = match_prospect(db, payload)
    if not prospect:
        return ProspectEmailResult(
            ok=False,
            errors=[
                "No matching prospect. Set prospect_id, or ensure the To: email "
                "matches Prospect.email (or pass company_name)."
            ],
            message="prospect_not_found",
        )

    # Pipeline value from proposal
    if payload.estimated_value is not None and payload.estimated_value > 0:
        prospect.estimated_value = round(float(payload.estimated_value), 2)
        rescore(db, prospect)

    # Activity
    body_bits = [
        payload.body_preview or payload.body or "",
        f"To: {payload.to}" if payload.to else "",
        f"From: {payload.from_email or payload.from_name}" if (payload.from_email or payload.from_name) else "",
        f"Outlook: {payload.web_link}" if payload.web_link else "",
        f"Message-Id: {payload.message_id}" if payload.message_id else "",
    ]
    if payload.estimated_value:
        body_bits.insert(0, f"Proposal value: £{payload.estimated_value:,.2f}")

    act = log_activity(
        db,
        prospect.id,
        activity_type="email",
        subject=payload.subject[:240],
        body="\n".join(b for b in body_bits if b).strip() or None,
        direction=payload.direction,
        outcome="proposal" if payload.attachments or payload.estimated_value else "",
        activity_at=payload.received_at or datetime.utcnow(),
        commit=False,
    )
    db.flush()

    # Follow-up task
    task = None
    if payload.follow_up:
        due = date.today() + timedelta(days=payload.follow_up_days)
        value_bit = (
            f" · £{payload.estimated_value:,.0f}"
            if payload.estimated_value
            else ""
        )
        title = f"Follow up proposal{value_bit}: {prospect.display_name()}"
        desc_parts = [
            f"Prospect: {prospect.display_name()} (#{prospect.id})",
            f"Email subject: {payload.subject}",
            f"Contact: {prospect.contact_name or '—'} · {prospect.email or '—'}",
            payload.body_preview or "",
            f"Open prospect: /prospecting/prospects/{prospect.id}",
            f"Outlook: {payload.web_link}" if payload.web_link else "",
        ]
        task = create_task(
            db,
            title=title[:240],
            description="\n".join(p for p in desc_parts if p).strip()[:4000],
            prospect_id=prospect.id,
            client_id=prospect.client_id,
            fee=float(payload.estimated_value or 0),
            due_on=due,
            priority="High" if (payload.estimated_value or 0) >= 5000 else "Medium",
            source_email_date=(payload.received_at.date() if payload.received_at else date.today()),
            import_source=IMPORT_SOURCE,
            import_hash=hashlib.sha1(
                f"{IMPORT_SOURCE}|{payload.message_id or payload.subject}|{prospect.id}".encode()
            ).hexdigest(),
            email_from=payload.from_email or None,
            email_to=payload.to or None,
            email_preview=(payload.body_preview or "")[:500] or None,
            outlook_message_id=payload.message_id or None,
            outlook_conversation_id=payload.conversation_id or None,
            outlook_web_link=payload.web_link or None,
            commit=False,
        )
        db.flush()

    # Attachments → OneDrive Proposals
    doc_ids: List[int] = []
    upload_errors: List[str] = []
    for att in payload.attachments:
        doc, err = docs_svc.create_document(
            db,
            filename=att.filename,
            content=att.content,
            content_type=att.content_type,
            title=att.filename,
            description=f"From email: {payload.subject}"[:500],
            tags="proposal,email,prospect",
            category="Proposals",
            prospect_id=prospect.id,
            is_key=True,
            uploaded_by=uploaded_by,
        )
        if doc:
            doc_ids.append(doc.id)
        else:
            upload_errors.append(f"{att.filename}: {err or 'upload failed'}")

    db.commit()
    db.refresh(prospect)

    msg_parts = [f"Logged email on prospect #{prospect.id}"]
    if task:
        msg_parts.append(f"follow-up task #{task.id} due {task.due_on}")
    if doc_ids:
        msg_parts.append(f"{len(doc_ids)} file(s) in OneDrive Proposals")
    if upload_errors:
        msg_parts.append(f"{len(upload_errors)} upload error(s)")

    return ProspectEmailResult(
        ok=True,
        prospect_id=prospect.id,
        activity_id=act.id if act else None,
        task_id=task.id if task else None,
        document_ids=doc_ids,
        estimated_value=prospect.estimated_value,
        message="; ".join(msg_parts),
        upload_errors=upload_errors,
    )
