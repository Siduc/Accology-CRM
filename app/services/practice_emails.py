"""Structured practice emails: templates, send, history log."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session, joinedload

from app.config import (
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    PRACTICE_PHONE,
    practice_email_live,
)
from app.models import Client, Job, Person
from app.models.email_message import EmailMessage, EmailTemplate
from app.models.person import person_clients
from app.services.chase_emails import send_email as smtp_send_email, smtp_configured
from app.services.ms_graph_oauth import get_valid_access_token, latest_active_token

DEFAULT_TEMPLATES = [
    {
        "code": "engagement_welcome",
        "name": "Welcome / engagement follow-up",
        "category": "Engagement",
        "sort_order": 10,
        "subject_template": "Welcome to {{practice_name}} — {{client_name}}",
        "body_template": """Dear {{contact_name}},

Thank you for instructing {{practice_name}}. We are pleased to act for {{client_name}}.

We will be in touch shortly regarding next steps and any information we need from you.

If you have any questions, reply to this email or call {{practice_phone}}.

Kind regards,
{{practice_name}}
{{practice_email}}
""",
    },
    {
        "code": "info_request_accounts",
        "name": "Information request — accounts",
        "category": "Accounts",
        "sort_order": 20,
        "subject_template": "Information needed — {{client_name}} accounts{{job_period}}",
        "body_template": """Dear {{contact_name}},

To progress the accounts for {{client_name}}{{job_period}}, please could you send the following:

• Bank statements for the period
• Sales and purchase invoices (if not already provided)
• Any other relevant paperwork

Please reply to this email with the documents attached, or let us know if you have any queries.

Kind regards,
{{practice_name}}
{{practice_email}}
{{practice_phone}}
""",
    },
    {
        "code": "cs_reminder",
        "name": "Confirmation statement reminder",
        "category": "Admin",
        "sort_order": 30,
        "subject_template": "Confirmation statement — {{client_name}}",
        "body_template": """Dear {{contact_name}},

This is a reminder that the confirmation statement for {{client_name}} is due shortly.

Please confirm any changes to officers, PSC, or registered office, or reply to confirm there are no changes.

Kind regards,
{{practice_name}}
{{practice_email}}
""",
    },
    {
        "code": "job_query",
        "name": "Job progress / query",
        "category": "Other",
        "sort_order": 40,
        "subject_template": "{{job_type}} — {{client_name}}{{job_period}}",
        "body_template": """Dear {{contact_name}},

I am writing regarding the {{job_type}} for {{client_name}}{{job_period}}.

{{query_placeholder}}

Please reply to this email at your earliest convenience.

Kind regards,
{{practice_name}}
{{practice_email}}
""",
    },
    {
        "code": "general",
        "name": "General (practice footer)",
        "category": "Other",
        "sort_order": 90,
        "subject_template": "{{client_name}}",
        "body_template": """Dear {{contact_name}},



Kind regards,
{{practice_name}}
{{practice_email}}
{{practice_phone}}
""",
    },
]


def seed_email_templates(db: Session) -> int:
    """Insert default templates if missing. Returns number created."""
    created = 0
    for t in DEFAULT_TEMPLATES:
        exists = (
            db.query(EmailTemplate).filter(EmailTemplate.code == t["code"]).first()
        )
        if exists:
            continue
        db.add(
            EmailTemplate(
                code=t["code"],
                name=t["name"],
                category=t["category"],
                subject_template=t["subject_template"],
                body_template=t["body_template"],
                is_active=True,
                sort_order=t["sort_order"],
            )
        )
        created += 1
    if created:
        db.commit()
    return created


def list_templates(db: Session, *, active_only: bool = True) -> List[EmailTemplate]:
    seed_email_templates(db)
    q = db.query(EmailTemplate)
    if active_only:
        q = q.filter(EmailTemplate.is_active.is_(True))
    return q.order_by(EmailTemplate.sort_order.asc(), EmailTemplate.name.asc()).all()


def get_template(db: Session, template_id: Optional[int] = None, code: str = "") -> Optional[EmailTemplate]:
    seed_email_templates(db)
    if template_id:
        return db.query(EmailTemplate).filter(EmailTemplate.id == template_id).first()
    if code:
        return db.query(EmailTemplate).filter(EmailTemplate.code == code).first()
    return None


def build_context(client: Client, job: Optional[Job] = None) -> Dict[str, str]:
    pe = ""
    job_type = ""
    if job:
        job_type = (job.type or job.title or "work").strip()
        if job.period_end:
            pe = f" (period end {job.period_end.strftime('%d/%m/%Y')})"
    contact = (client.contact_name or client.display_name() or "Sir/Madam").strip()
    return {
        "client_name": client.display_name() or "Client",
        "contact_name": contact,
        "company_number": (client.company_number or "").strip(),
        "client_email": (client.email or "").strip(),
        "job_type": job_type or "matter",
        "job_period": pe,
        "query_placeholder": "[Add your question or update here]",
        "practice_name": PRACTICE_NAME or "Accologise Practice",
        "practice_email": PRACTICE_EMAIL or "",
        "practice_phone": PRACTICE_PHONE or "",
    }


def render_placeholders(text: str, context: Dict[str, str]) -> str:
    out = text or ""
    for key, val in context.items():
        out = out.replace("{{" + key + "}}", str(val or ""))
        out = out.replace("{{ " + key + " }}", str(val or ""))
    # strip leftover unknown placeholders lightly
    out = re.sub(r"\{\{\s*[\w]+\s*\}\}", "", out)
    return out


def render_template(
    tmpl: EmailTemplate, context: Dict[str, str]
) -> Tuple[str, str]:
    subject = render_placeholders(tmpl.subject_template or "", context)
    body = render_placeholders(tmpl.body_template or "", context)
    return subject.strip(), body.strip() + ("\n" if body and not body.endswith("\n") else "")


def recipients_for_client(db: Session, client: Client) -> List[Dict[str, str]]:
    """Build To options: client.email + linked people emails."""
    out: List[Dict[str, str]] = []
    seen = set()
    ce = (client.email or "").strip()
    if ce:
        out.append(
            {
                "email": ce,
                "label": f"{client.display_name()} <{ce}>",
            }
        )
        seen.add(ce.lower())
    people = (
        db.query(Person)
        .join(person_clients, person_clients.c.person_id == Person.id)
        .filter(person_clients.c.client_id == client.id)
        .order_by(Person.full_name)
        .all()
    )
    for p in people:
        em = (p.email or "").strip()
        if not em or em.lower() in seen:
            continue
        seen.add(em.lower())
        name = p.full_name or "Contact"
        primary = " · primary" if getattr(p, "is_primary", False) else ""
        out.append({"email": em, "label": f"{name} <{em}>{primary}"})
    return out


def list_messages(
    db: Session,
    *,
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    limit: int = 50,
) -> List[EmailMessage]:
    q = db.query(EmailMessage).options(
        joinedload(EmailMessage.template),
        joinedload(EmailMessage.job),
    )
    if job_id:
        q = q.filter(EmailMessage.job_id == job_id)
    elif client_id:
        q = q.filter(EmailMessage.client_id == client_id)
    else:
        return []
    return q.order_by(EmailMessage.id.desc()).limit(limit).all()


def send_capability(db: Session) -> Dict[str, Any]:
    """What send path is available."""
    token, terr = get_valid_access_token(db)
    graph_ok = bool(token)
    live = practice_email_live()
    smtp_ok = smtp_configured()
    return {
        "graph_connected": graph_ok,
        "graph_error": terr or "",
        "smtp_configured": smtp_ok,
        "live": live,
        "can_send_graph": graph_ok,
        "can_send_smtp": smtp_ok and live,
        "can_send": graph_ok or (smtp_ok and live),
        "mode": (
            "graph"
            if graph_ok
            else ("smtp" if smtp_ok and live else "dry_run")
        ),
    }


def send_practice_email(
    db: Session,
    *,
    client_id: int,
    job_id: Optional[int] = None,
    to_address: str,
    subject: str,
    body: str,
    template_id: Optional[int] = None,
    sent_by: str = "",
    force_log_only: bool = False,
    attachments: Optional[list] = None,
) -> Tuple[EmailMessage, str]:
    """
    Send (or dry-run log) a practice email. Returns (message_row, flash_message).

    attachments: optional list of {name, content (bytes), content_type}.
    """
    to = (to_address or "").strip()
    subj = (subject or "").strip() or "(no subject)"
    body_text = body or ""
    atts = list(attachments or [])

    row = EmailMessage(
        client_id=client_id,
        job_id=job_id,
        template_id=template_id,
        direction="outbound",
        to_address=to,
        subject=subj,
        body=body_text,
        status="logged",
        sent_by=(sent_by or "").strip() or None,
        sent_at=datetime.utcnow(),
    )
    db.add(row)

    if force_log_only or not to:
        row.status = "logged" if to else "failed"
        if not to:
            row.error_detail = "no_recipient_email"
        db.commit()
        db.refresh(row)
        return row, "Email logged (not sent — no recipient)." if not to else "Email logged only."

    cap = send_capability(db)
    flash = ""

    if cap["can_send_graph"]:
        token, _ = get_valid_access_token(db)
        from app.services import ms_graph_mail as gmail

        ok, err = gmail.send_mail(
            token or "",
            to=to,
            subject=subj,
            body=body_text,
            attachments=atts,
        )
        row.provider = "graph"
        if ok:
            row.status = "sent"
            n_att = len(atts)
            flash = (
                f"Email sent via Microsoft Graph"
                + (f" with {n_att} attachment(s)." if n_att else ".")
            )
        else:
            row.status = "failed"
            row.error_detail = err
            flash = f"Graph send failed: {err}"
        db.commit()
        db.refresh(row)
        return row, flash

    if cap["can_send_smtp"]:
        # smtp path: attach when possible
        ok, status = smtp_send_email(to, subj, body_text, attachments=atts)
        row.provider = "smtp"
        if ok:
            row.status = "sent"
            flash = "Email sent via SMTP" + (
                f" with {len(atts)} attachment(s)." if atts else "."
            )
        else:
            row.status = status if status in (
                "blocked_not_live",
                "skipped_no_smtp",
                "no_recipient_email",
            ) else "failed"
            row.error_detail = status
            flash = f"SMTP: {status}"
        db.commit()
        db.refresh(row)
        return row, flash

    # Dry-run: log only
    row.status = "blocked_not_live" if not cap["live"] else "skipped_no_smtp"
    row.provider = None
    row.error_detail = (
        "Connect Microsoft (Mail.Send) or enable PRACTICE_EMAIL_LIVE / SMTP"
    )
    db.commit()
    db.refresh(row)
    return (
        row,
        "Email logged only (not sent). Connect Microsoft Graph with mail scopes, "
        "or set PRACTICE_EMAIL_LIVE=true with SMTP configured.",
    )


def archive_outlook_for_task(db: Session, task) -> Tuple[bool, str]:
    """
    Move linked Outlook message to archive folder when task completes.
    Returns (ok, message).
    """
    mid = (getattr(task, "outlook_message_id", None) or "").strip()
    if not mid:
        return True, ""  # nothing to do

    token, err = get_valid_access_token(db)
    if not token:
        task.outlook_archive_status = "failed"
        db.commit()
        return False, err or "Microsoft not connected"

    from app.services import ms_graph_mail as gmail

    task.outlook_archive_status = "pending"
    db.commit()

    ok, aerr, extra = gmail.archive_message(token, mid)
    if ok:
        # Graph gives the message a new id + webLink in the destination folder
        new_id = (extra.get("id") or "").strip()
        new_link = (extra.get("webLink") or "").strip()
        if new_id:
            task.outlook_message_id = new_id
        if new_link.startswith("http"):
            task.outlook_web_link = new_link
        elif new_id:
            task.outlook_web_link = gmail.outlook_deeplink_from_id(new_id)
        task.outlook_archive_status = "archived"
        task.outlook_archived_at = datetime.utcnow()
        db.commit()
        return True, "Outlook message moved to Archive — Open in Outlook still follows it."
    task.outlook_archive_status = "failed"
    db.commit()
    return False, aerr or "Archive failed"
