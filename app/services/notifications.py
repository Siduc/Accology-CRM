"""Staff notification centre — CRM inbox + optional email."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.config import (
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    SMTP_FROM,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USER,
)
from app.models.notification import Notification

logger = logging.getLogger("accountant_crm.notifications")


def _notify_email_enabled() -> bool:
    from app import config

    return bool(getattr(config, "NOTIFY_WEBSITE_PROSPECT_EMAIL", False))


def _alert_recipient() -> str:
    from app import config

    return (
        (getattr(config, "NOTIFY_ALERT_EMAIL", None) or "").strip()
        or (PRACTICE_EMAIL or "").strip()
        or (SMTP_FROM or "").strip()
        or (SMTP_USER or "").strip()
    )


def smtp_ready() -> bool:
    return bool(SMTP_HOST and (SMTP_FROM or SMTP_USER))


def create_notification(
    db: Session,
    *,
    type: str,
    title: str,
    body: str = "",
    link: str = "",
    entity_type: str | None = None,
    entity_id: int | None = None,
    commit: bool = True,
    send_email: bool = False,
) -> Notification:
    n = Notification(
        type=(type or "system").strip()[:64] or "system",
        title=(title or "Notification").strip()[:240],
        body=(body or "").strip() or None,
        link=(link or "").strip() or None,
        entity_type=(entity_type or "").strip() or None,
        entity_id=entity_id,
        created_at=datetime.utcnow(),
    )
    db.add(n)
    if commit:
        db.commit()
        db.refresh(n)
    else:
        db.flush()

    if send_email:
        try:
            send_staff_alert_email(n)
        except Exception:  # noqa: BLE001
            logger.exception("Staff alert email failed for notification %s", n.id)

    return n


def notify_website_prospect(
    db: Session,
    *,
    prospect_id: int,
    contact_name: str,
    email: str,
    company: str = "",
    message_preview: str = "",
) -> Notification:
    """Create CRM alert (and optional email) for a website contact form lead."""
    name = (contact_name or "").strip() or "Unknown"
    em = (email or "").strip()
    co = (company or "").strip()
    preview = (message_preview or "").strip()
    if len(preview) > 280:
        preview = preview[:277] + "…"

    title = f"New website prospect: {name}"
    lines = [
        f"Contact: {name}",
        f"Email: {em}" if em else None,
        f"Company: {co}" if co else None,
        f"Message: {preview}" if preview else None,
    ]
    body = "\n".join(x for x in lines if x)
    link = f"/prospecting/{prospect_id}"

    email_on = _notify_email_enabled()
    return create_notification(
        db,
        type="website_prospect",
        title=title,
        body=body,
        link=link,
        entity_type="prospect",
        entity_id=prospect_id,
        commit=True,
        send_email=email_on,
    )


def unread_count(db: Session) -> int:
    return (
        db.query(Notification)
        .filter(Notification.read_at.is_(None))
        .count()
    )


def list_unread(db: Session, *, limit: int = 20) -> List[Notification]:
    return (
        db.query(Notification)
        .filter(Notification.read_at.is_(None))
        .order_by(Notification.created_at.desc())
        .limit(max(1, min(limit, 100)))
        .all()
    )


def list_recent(db: Session, *, limit: int = 40, unread_only: bool = False) -> List[Notification]:
    q = db.query(Notification)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    return q.order_by(Notification.created_at.desc()).limit(max(1, min(limit, 100))).all()


def mark_read(db: Session, notification_id: int) -> Optional[Notification]:
    n = db.query(Notification).filter(Notification.id == notification_id).first()
    if not n:
        return None
    if n.read_at is None:
        n.read_at = datetime.utcnow()
        db.commit()
        db.refresh(n)
    return n


def mark_all_read(db: Session) -> int:
    now = datetime.utcnow()
    rows = (
        db.query(Notification)
        .filter(Notification.read_at.is_(None))
        .all()
    )
    for n in rows:
        n.read_at = now
    db.commit()
    return len(rows)


def send_staff_alert_email(n: Notification) -> Tuple[bool, str]:
    """
    Internal staff alert — not gated by CHASE_LIVE_MODE.
    Requires SMTP + NOTIFY_ALERT_EMAIL / PRACTICE_EMAIL.
    """
    to = _alert_recipient()
    if not to:
        return False, "no_alert_recipient"
    if not smtp_ready():
        return False, "skipped_no_smtp"

    subject = f"[Accology] {n.title}"
    link = n.link or ""
    # Absolute-ish note; CRM host is environment-specific
    body_lines = [
        n.title,
        "",
        n.body or "",
        "",
        f"Open in CRM: {link}" if link else "",
        "",
        f"— {PRACTICE_NAME} notifications",
    ]
    body = "\n".join(line for line in body_lines if line is not None).strip() + "\n"

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        from_addr = SMTP_FROM or SMTP_USER or to
        msg["From"] = f"{SMTP_FROM_NAME} <{from_addr}>" if SMTP_FROM_NAME else from_addr
        msg["To"] = to
        msg.set_content(body)
        if SMTP_USE_TLS:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.starttls()
                if SMTP_USER and SMTP_PASSWORD:
                    smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                if SMTP_USER and SMTP_PASSWORD:
                    smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(msg)
        logger.info("Staff alert email sent to %s for notification %s", to, n.id)
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Staff alert email failed")
        return False, f"failed:{exc}"
