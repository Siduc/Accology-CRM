"""Debt chase email templates and gated SMTP send (live mode required)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Optional, Tuple

from app.config import (
    CHASE_LIVE_MODE,
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    PRACTICE_PHONE,
    SMTP_FROM,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_TLS,
    SMTP_USER,
)

logger = logging.getLogger("accountant_crm.chase_emails")

# Stage ladder: min days overdue → stage code
STAGE_THRESHOLDS = (
    (60, "legal"),
    (30, "final"),
    (14, "firm"),
    (7, "polite"),
)

STAGE_ORDER = ("polite", "firm", "final", "legal")

STAGE_LABELS = {
    "polite": "Polite reminder (7+ days)",
    "firm": "Firm follow-up (14+ days)",
    "final": "Final notice (30+ days)",
    "legal": "Legal / formal notice (60+ days)",
}

TONE = {
    "polite": "polite",
    "firm": "firm",
    "final": "final",
    "legal": "formal",
}


def stage_for_days(days_overdue: int) -> Optional[str]:
    if days_overdue < 7:
        return None
    for threshold, stage in STAGE_THRESHOLDS:
        if days_overdue >= threshold:
            return stage
    return "polite"


def stage_rank(stage: Optional[str]) -> int:
    if not stage or stage not in STAGE_ORDER:
        return -1
    return STAGE_ORDER.index(stage)


def smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def build_chase_email(
    *,
    stage: str,
    client_name: str,
    client_email: str,
    invoice_number: str,
    balance: float,
    issue_date: str,
    due_date: str,
    age_days: int,
) -> Tuple[str, str, str]:
    """Return (to, subject, body)."""
    practice = PRACTICE_NAME
    phone = PRACTICE_PHONE or "our office"
    email_from = PRACTICE_EMAIL or SMTP_FROM or "accounts@practice.local"
    bal = f"£{balance:,.2f}"
    to = (client_email or "").strip()

    if stage == "polite":
        subject = f"Friendly reminder — invoice {invoice_number}"
        body = f"""Dear {client_name},

I hope you are well.

This is a polite reminder that invoice {invoice_number} for {bal} appears to remain outstanding.

  Invoice: {invoice_number}
  Issued:  {issue_date}
  Due:     {due_date or 'see invoice'}
  Balance: {bal}
  Age:     {age_days} days

If payment has already been made, please ignore this note or send the remittance advice so we can update our records.

If you have any queries, reply to this email or call {phone}.

Kind regards,
{practice}
{email_from}
"""
    elif stage == "firm":
        subject = f"Payment overdue — invoice {invoice_number}"
        body = f"""Dear {client_name},

We still show an outstanding balance on invoice {invoice_number}.

  Invoice: {invoice_number}
  Issued:  {issue_date}
  Due:     {due_date or 'see invoice'}
  Balance: {bal}
  Overdue: {age_days} days

Please arrange payment within 7 days, or contact us immediately if there is a query so we can resolve it.

If you have already paid, please send proof of payment so we can allocate it.

Yours sincerely,
{practice}
{email_from}
{phone}
"""
    elif stage == "final":
        subject = f"FINAL NOTICE — invoice {invoice_number}"
        body = f"""Dear {client_name},

FINAL NOTICE

Despite previous reminders, invoice {invoice_number} for {bal} remains unpaid.

  Invoice: {invoice_number}
  Issued:  {issue_date}
  Due:     {due_date or 'see invoice'}
  Balance: {bal}
  Overdue: {age_days} days

Please settle this account in full within 7 days of this notice.

If payment is not received, we may refer the matter for formal recovery action without further notice.

If you believe this is incorrect, contact us immediately on {phone} or {email_from}.

Yours faithfully,
{practice}
"""
    else:  # legal
        subject = f"Formal demand — invoice {invoice_number} — notice before action"
        body = f"""Dear {client_name},

FORMAL NOTICE BEFORE FURTHER ACTION

This letter concerns invoice {invoice_number} with an outstanding balance of {bal}, now {age_days} days overdue.

  Invoice: {invoice_number}
  Issued:  {issue_date}
  Due:     {due_date or 'see invoice'}
  Balance: {bal}

Unless payment in full is received within 7 days, we reserve the right to pass this matter to our solicitors for recovery, and you may become liable for additional costs.

Please treat this as a formal demand for payment.

Yours faithfully,
{practice}
{email_from}
{phone}
"""
    return to, subject, body.strip() + "\n"


def send_email(to: str, subject: str, body: str) -> Tuple[bool, str]:
    """
    Attempt SMTP send. Returns (ok, message).
    Caller must enforce CHASE_LIVE_MODE before calling.
    """
    if not CHASE_LIVE_MODE:
        return False, "blocked_not_live"
    if not to:
        return False, "no_recipient_email"
    if not smtp_configured():
        return False, "skipped_no_smtp"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM}>" if SMTP_FROM_NAME else SMTP_FROM
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
        return True, "sent"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Chase email failed")
        return False, f"failed:{exc}"
