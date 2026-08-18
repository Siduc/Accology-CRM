"""PAYE registration onboarding form + apply answers onto the client record."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.person import Person
from app.models.prospecting import CampaignMember, Prospect

PAYROLL_MAILBOX = "payroll@accology.co"
MELISSA_MAILBOX = "melissa@accology.co"


def _secret() -> str:
    import app.config as cfg

    return (getattr(cfg, "SESSION_SECRET", None) or "accologise-dev") + ":paye-onboard"


def make_token(client_id: int, prospect_id: int = 0) -> str:
    payload = f"{int(client_id)}:{int(prospect_id or 0)}"
    sig = hmac.new(_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:20]
    return f"{payload.replace(':', '-')}-{sig}"


def parse_token(token: str) -> Tuple[Optional[int], Optional[int], str]:
    raw = (token or "").strip()
    parts = raw.split("-")
    if len(parts) < 3:
        return None, None, "Invalid link."
    try:
        client_id = int(parts[0])
        prospect_id = int(parts[1])
    except ValueError:
        return None, None, "Invalid link."
    sig = parts[2]
    expect = make_token(client_id, prospect_id).rsplit("-", 1)[-1]
    if not hmac.compare_digest(sig, expect):
        return None, None, "This link is not valid."
    return client_id, prospect_id, ""


def form_url(client_id: int, prospect_id: int = 0, *, base: str = "") -> str:
    token = make_token(client_id, prospect_id)
    path = f"/payroll/paye/{quote(token)}"
    root = (base or "").rstrip("/")
    return f"{root}{path}" if root else path


def apply_onboarding(
    db: Session,
    client: Client,
    data: Dict[str, Any],
    *,
    prospect: Optional[Prospect] = None,
) -> None:
    """Write form / parsed-reply fields onto the client and director."""
    contact = (data.get("contact_name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    if contact:
        client.contact_name = contact
    if email and "@" in email:
        client.email = email
    if phone:
        client.phone = phone
    paye_ref = (data.get("existing_paye_ref") or "").strip()
    ao_ref = (data.get("existing_ao_ref") or "").strip()
    if paye_ref:
        client.paye_reference = paye_ref
    if ao_ref:
        client.accounts_office_reference = ao_ref
    gg = (data.get("gg_username") or "").strip()
    if gg:
        client.gov_gateway_username = gg

    payload = dict(data)
    payload["saved_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    client.payroll_onboarding_json = json.dumps(payload, indent=2)
    extra = (
        f"PAYE onboarding {payload['saved_at']}: "
        f"first payday {data.get('first_pay_date') or '—'}; "
        f"staff {data.get('staff_count') or '—'}; "
        f"already PAYE {data.get('already_paye') or 'no'}."
    )
    notes = (client.notes or "").strip()
    if extra not in notes:
        client.notes = f"{notes}\n{extra}".strip() if notes else extra
    client.updated_at = datetime.utcnow()

    director_name = (data.get("director_name") or contact or "").strip()
    person = None
    for p in client.people or []:
        if director_name and (p.full_name or "").lower() == director_name.lower():
            person = p
            break
    if person is None and client.people:
        person = client.people[0]
    if person:
        if director_name:
            person.full_name = director_name
        if email and "@" in email:
            person.email = email
        if phone:
            person.phone = phone
        ni = (data.get("ni_number") or "").strip().upper().replace(" ", "")
        if ni:
            person.ni_number = ni
        bits = []
        if data.get("date_of_birth"):
            bits.append(f"DOB: {data.get('date_of_birth')}")
        if data.get("home_address"):
            bits.append(f"Home: {data.get('home_address')}")
        if bits:
            pn = (person.notes or "").strip()
            add = "; ".join(bits)
            if add not in pn:
                person.notes = f"{pn}\n{add}".strip() if pn else add

    if prospect:
        if email and "@" in email:
            prospect.email = email
        if contact:
            prospect.contact_name = contact
        if phone:
            prospect.phone = phone
        prospect.pipeline_status = "won"
        prospect.next_step = "PAYE registration — Accology Pays, fee £100"
        prospect.updated_at = datetime.utcnow()
        member = (
            db.query(CampaignMember)
            .filter(CampaignMember.prospect_id == prospect.id, CampaignMember.campaign_id == 2)
            .first()
        )
        if member:
            member.status = "converted"
            member.last_touch_at = datetime.utcnow()


_FIELD_ALIASES = {
    "phone": ("phone", "mobile", "tel"),
    "email": ("email", "e-mail"),
    "contact_name": ("contact name", "name"),
    "director_name": ("director name", "director"),
    "ni_number": ("ni number", "nino", "national insurance"),
    "date_of_birth": ("date of birth", "dob", "born"),
    "home_address": ("home address", "home"),
    "trading_name": ("trading name",),
    "trading_address": ("trading address",),
    "nature_of_business": ("nature of business", "trade", "sic"),
    "first_pay_date": ("first payday", "first pay date", "start date"),
    "staff_count": ("how many people", "staff", "employees"),
    "existing_paye_ref": ("paye reference", "paye ref"),
    "existing_ao_ref": ("accounts office", "ao ref"),
    "gg_username": ("gateway", "gg id", "user id"),
}


def write_pays_proforma_pdf(path, *, issuer: Client, client: Client, number: str, issue, amount: float, description: str) -> None:
    """Accology Pays pro forma using the Imagine ACCOLOGY lockup + cyan/purple bar."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    from app.services.branding import ensure_pays_letterhead

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cnv = canvas.Canvas(str(path), pagesize=A4)
    page_w, page_h = A4
    left = 18 * mm
    right = page_w - 18 * mm
    navy = HexColor("#0F172A")
    teal = HexColor("#1E4064")
    cyan = HexColor("#00E5FF")
    muted = HexColor("#64748B")

    header = ensure_pays_letterhead()
    y = page_h - 14 * mm
    if header and header.is_file():
        # Preserve Imagine wordmark + gradient bar; scale to page width
        img_w = right - left
        # Natural header is wide and short
        img_h = 32 * mm
        cnv.drawImage(
            str(header),
            left,
            y - img_h,
            width=img_w,
            height=img_h,
            preserveAspectRatio=True,
            mask="auto",
            anchor="sw",
        )
        y = y - img_h - 10 * mm
    else:
        y -= 4 * mm
        cnv.setStrokeColor(cyan)
        cnv.setLineWidth(1.6)
        cnv.line(left, y, right, y)
        y -= 12 * mm

    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 16)
    cnv.drawString(left, y, "PRO FORMA INVOICE")
    cnv.setFont("Helvetica-Bold", 12)
    cnv.drawRightString(right, y, number)
    y -= 6 * mm
    cnv.setFillColor(muted)
    cnv.setFont("Helvetica", 9)
    issued = issue.strftime("%d/%m/%Y") if hasattr(issue, "strftime") else str(issue)
    cnv.drawString(left, y, issuer.company_name or "Accology Pays Limited")
    cnv.drawRightString(right, y, f"Date  {issued}")
    y -= 4 * mm
    cnv.drawString(left, y, f"Company {issuer.company_number or '16011017'}")
    y -= 10 * mm

    cnv.setFillColor(teal)
    cnv.setFont("Helvetica-Bold", 8)
    cnv.drawString(left, y, "BILL TO")
    y -= 5 * mm
    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 11)
    cnv.drawString(left, y, client.display_name())
    y -= 5 * mm
    cnv.setFont("Helvetica", 9)
    cnv.setFillColor(navy)
    if client.company_number:
        cnv.drawString(left, y, f"Company {client.company_number}")
        y -= 4.5 * mm
    for line in (
        client.address_line1,
        client.address_line2,
        f"{client.town or ''} {client.postcode or ''}".strip(),
        client.email,
    ):
        if line:
            cnv.drawString(left, y, str(line))
            y -= 4.5 * mm
    y -= 8 * mm

    # Line table
    row_h = 8 * mm
    cnv.setFillColor(cyan)
    cnv.rect(left, y - 2 * mm, right - left, row_h, fill=1, stroke=0)
    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 8)
    cnv.drawString(left + 3 * mm, y + 1.2 * mm, "DESCRIPTION")
    cnv.drawRightString(right - 3 * mm, y + 1.2 * mm, "AMOUNT")
    y -= row_h + 2 * mm
    cnv.setFont("Helvetica", 10)
    cnv.drawString(left + 3 * mm, y, description)
    cnv.drawRightString(right - 3 * mm, y, f"£{amount:,.2f}")
    y -= 8 * mm
    cnv.setStrokeColor(HexColor("#E2E8F0"))
    cnv.setLineWidth(0.6)
    cnv.line(left, y + 4 * mm, right, y + 4 * mm)
    cnv.setFillColor(muted)
    cnv.setFont("Helvetica", 9)
    cnv.drawString(left + 3 * mm, y, "VAT")
    cnv.drawRightString(right - 3 * mm, y, "£0.00")
    y -= 8 * mm
    cnv.setFillColor(navy)
    cnv.setFont("Helvetica-Bold", 11)
    cnv.drawString(left + 3 * mm, y, "Total due")
    cnv.drawRightString(right - 3 * mm, y, f"£{amount:,.2f}")
    y -= 3 * mm
    cnv.setStrokeColor(cyan)
    cnv.setLineWidth(1.4)
    cnv.line(left, y, right, y)

    y -= 14 * mm
    cnv.setFillColor(muted)
    cnv.setFont("Helvetica", 8)
    for line in (
        "This is a pro forma invoice from Accology Pays Limited. It is not a VAT invoice.",
        "Accology Pays Limited is not VAT registered. Bank details will follow.",
        "Please reply to payroll@accology.co.",
    ):
        cnv.drawString(left, y, line)
        y -= 4.2 * mm
    cnv.save()


def parse_reply_body(text: str) -> Dict[str, str]:
    """Pull labelled answers from a plain-text reply."""
    found: Dict[str, str] = {}
    blob = text or ""
    for key, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            m = re.search(
                rf"(?im)^\s*{re.escape(alias)}\s*[:\-]\s*(.+)$",
                blob,
            )
            if m:
                val = m.group(1).strip()
                if val:
                    found[key] = val
                    break
    return found
