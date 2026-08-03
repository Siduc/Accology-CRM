"""Prospecting Ledger business logic."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import or_, func
from sqlalchemy.orm import Session

from app.models.client import Client
from app.models.prospecting import (
    ACTIVITY_TYPES,
    CAMPAIGN_CHANNELS,
    CAMPAIGN_STATUSES,
    MEMBER_STATUSES,
    OPEN_PIPELINE,
    PIPELINE_LABELS,
    PIPELINE_STATUSES,
    CampaignMember,
    ChSyncRun,
    Prospect,
    ProspectActivity,
    ProspectCampaign,
)
from app.models.sales import Service
from app.services.company_numbers import normalize_company_number

# Local postcode prefixes that score higher (practice catchment — editable later)
LOCAL_POSTCODE_PREFIXES = (
    "B",
    "CV",
    "DY",
    "WS",
    "WV",
    "WR",
    "HR",
    "ST",
    "TF",
    "SY",
)


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    s = str(value)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def score_prospect(p: Prospect, *, target_sics: Sequence[str] = ()) -> int:
    """Simple 0–100 rule score."""
    score = 0
    today = date.today()
    if p.incorporation_date and (today - p.incorporation_date).days <= 90:
        score += 25
    if (p.email or "").strip():
        score += 15
    st = (p.company_status or "").lower()
    if st in ("active", "open", ""):
        score += 10
    sic = (p.sic_codes or "").lower()
    for t in target_sics:
        if t and t.lower() in sic:
            score += 20
            break
    pc = (p.postcode or "").upper().replace(" ", "")
    for pref in LOCAL_POSTCODE_PREFIXES:
        if pc.startswith(pref):
            score += 15
            break
    if p.accounts_next_due and 0 <= (p.accounts_next_due - today).days <= 183:
        score += 10
    if p.memberships:
        open_m = [m for m in p.memberships if (m.status or "") != "removed"]
        if open_m:
            score += 5
    return min(100, score)


def rescore(db: Session, p: Prospect) -> Prospect:
    p.score = score_prospect(p)
    p.updated_at = datetime.utcnow()
    return p


def log_activity(
    db: Session,
    prospect_id: int,
    *,
    activity_type: str = "note",
    subject: str = "",
    body: str = "",
    direction: str = "outbound",
    outcome: str = "",
    campaign_id: Optional[int] = None,
    activity_at: Optional[datetime] = None,
    commit: bool = True,
) -> ProspectActivity:
    at = (activity_type or "note").strip().lower()
    if at not in ACTIVITY_TYPES:
        at = "note"
    act = ProspectActivity(
        prospect_id=prospect_id,
        campaign_id=campaign_id,
        activity_type=at,
        subject=(subject or "")[:240] or None,
        body=body or None,
        direction=direction or "outbound",
        outcome=(outcome or "")[:120] or None,
        activity_at=activity_at or datetime.utcnow(),
    )
    db.add(act)
    if commit:
        db.commit()
        db.refresh(act)
    return act


def create_prospect(
    db: Session,
    *,
    company_name: str,
    company_number: str = "",
    contact_name: str = "",
    email: str = "",
    phone: str = "",
    address_line1: str = "",
    town: str = "",
    postcode: str = "",
    sic_codes: str = "",
    notes: str = "",
    source: str = "manual",
    incorporation_date: Optional[date] = None,
    pipeline_status: str = "new",
    estimated_value: float = 0.0,
) -> Prospect:
    cn = normalize_company_number(company_number) if company_number else None
    if cn:
        existing = db.query(Prospect).filter(Prospect.company_number == cn).first()
        if existing:
            return existing
    p = Prospect(
        company_name=(company_name or "").strip() or (cn or "Unnamed"),
        company_number=cn,
        contact_name=(contact_name or "").strip() or None,
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        address_line1=(address_line1 or "").strip() or None,
        town=(town or "").strip() or None,
        postcode=(postcode or "").strip() or None,
        sic_codes=(sic_codes or "").strip() or None,
        notes=(notes or "").strip() or None,
        source=source or "manual",
        incorporation_date=incorporation_date,
        pipeline_status=pipeline_status if pipeline_status in PIPELINE_STATUSES else "new",
        estimated_value=round(float(estimated_value or 0), 2),
    )
    rescore(db, p)
    db.add(p)
    db.flush()
    log_activity(
        db,
        p.id,
        activity_type="note",
        subject="Prospect created",
        body=f"Source: {p.source}",
        direction="internal",
        commit=False,
    )
    db.commit()
    db.refresh(p)
    return p


def list_prospects(
    db: Session,
    *,
    q: str = "",
    status: str = "",
    source: str = "",
    campaign_id: Optional[int] = None,
    sic: str = "",
    postcode: str = "",
    min_score: Optional[int] = None,
    open_only: bool = False,
    limit: int = 200,
) -> List[Prospect]:
    query = db.query(Prospect)
    if open_only:
        query = query.filter(Prospect.pipeline_status.in_(OPEN_PIPELINE))
    if status and status in PIPELINE_STATUSES:
        query = query.filter(Prospect.pipeline_status == status)
    if source:
        query = query.filter(Prospect.source == source)
    if sic:
        query = query.filter(Prospect.sic_codes.ilike(f"%{sic.strip()}%"))
    if postcode:
        query = query.filter(Prospect.postcode.ilike(f"%{postcode.strip()}%"))
    if min_score is not None:
        query = query.filter(Prospect.score >= int(min_score))
    if campaign_id:
        query = query.join(CampaignMember).filter(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.status != "removed",
        )
    qq = (q or "").strip()
    if qq:
        like = f"%{qq}%"
        query = query.filter(
            or_(
                Prospect.company_name.ilike(like),
                Prospect.company_number.ilike(like),
                Prospect.email.ilike(like),
                Prospect.contact_name.ilike(like),
                Prospect.town.ilike(like),
            )
        )
    return (
        query.order_by(Prospect.score.desc(), Prospect.created_at.desc())
        .limit(min(limit, 500))
        .all()
    )


def hub_stats(db: Session) -> dict:
    open_count = (
        db.query(func.count(Prospect.id))
        .filter(Prospect.pipeline_status.in_(OPEN_PIPELINE))
        .scalar()
        or 0
    )
    open_value = (
        db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
        .filter(Prospect.pipeline_status.in_(OPEN_PIPELINE))
        .scalar()
        or 0
    )
    by_status = {}
    for st in PIPELINE_STATUSES:
        by_status[st] = (
            db.query(func.count(Prospect.id))
            .filter(Prospect.pipeline_status == st)
            .scalar()
            or 0
        )
    campaigns_active = (
        db.query(func.count(ProspectCampaign.id))
        .filter(ProspectCampaign.status == "active")
        .scalar()
        or 0
    )
    week_ago = datetime.utcnow() - timedelta(days=7)
    activities_week = (
        db.query(func.count(ProspectActivity.id))
        .filter(ProspectActivity.activity_at >= week_ago)
        .scalar()
        or 0
    )
    won_month = (
        db.query(func.count(Prospect.id))
        .filter(
            Prospect.pipeline_status == "won",
            Prospect.converted_at >= datetime.utcnow().replace(day=1, hour=0, minute=0),
        )
        .scalar()
        or 0
    )
    last_sync = (
        db.query(ChSyncRun).order_by(ChSyncRun.started_at.desc()).first()
    )
    return {
        "open_count": int(open_count),
        "open_value": round(float(open_value), 2),
        "by_status": by_status,
        "campaigns_active": int(campaigns_active),
        "activities_week": int(activities_week),
        "won_month": int(won_month),
        "last_sync": last_sync,
    }


def set_pipeline_status(
    db: Session, prospect_id: int, status: str, *, lost_reason: str = ""
) -> Optional[Prospect]:
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p or status not in PIPELINE_STATUSES:
        return None
    old = p.pipeline_status
    p.pipeline_status = status
    if status == "lost":
        p.lost_reason = (lost_reason or "").strip() or p.lost_reason
    p.updated_at = datetime.utcnow()
    log_activity(
        db,
        p.id,
        activity_type="status_change",
        subject=f"{PIPELINE_LABELS.get(old, old)} → {PIPELINE_LABELS.get(status, status)}",
        body=lost_reason or "",
        direction="internal",
        commit=False,
    )
    rescore(db, p)
    db.commit()
    db.refresh(p)
    return p


def _parse_fee(value: Any) -> float:
    try:
        return max(0.0, round(float(value or 0), 2))
    except (TypeError, ValueError):
        return 0.0


def apply_campaign_value_to_prospect(
    db: Session, campaign: ProspectCampaign, prospect: Prospect, *, commit: bool = False
) -> float:
    """
    Set prospect pipeline value from campaign fees:
    initial + ongoing (annualised if monthly). Renewal is stored on campaign only.
    """
    val = campaign.pipeline_value_per_prospect()
    # Don't wipe a higher manually-set value unless campaign value is positive
    if val > 0:
        prospect.estimated_value = val
        prospect.updated_at = datetime.utcnow()
        rescore(db, prospect)
        if commit:
            db.commit()
            db.refresh(prospect)
    return float(prospect.estimated_value or 0)


def create_campaign(
    db: Session,
    *,
    name: str,
    description: str = "",
    service_id: Optional[int] = None,
    service_label: str = "",
    channel: str = "mixed",
    status: str = "draft",
    sequence_json: str = "",
    fee_initial: float = 0.0,
    fee_ongoing: float = 0.0,
    fee_ongoing_frequency: str = "annual",
    fee_renewal: float = 0.0,
    email_subject: str = "",
    email_body: str = "",
) -> ProspectCampaign:
    if service_id and not service_label:
        svc = db.query(Service).filter(Service.id == service_id).first()
        if svc:
            service_label = svc.name or svc.code or ""
    ch = channel if channel in CAMPAIGN_CHANNELS else "mixed"
    st = status if status in CAMPAIGN_STATUSES else "draft"
    freq = (fee_ongoing_frequency or "annual").strip().lower()
    if freq not in ("monthly", "annual"):
        freq = "annual"
    c = ProspectCampaign(
        name=(name or "").strip() or "Untitled campaign",
        description=(description or "").strip() or None,
        service_id=service_id,
        service_label=(service_label or "").strip() or None,
        channel=ch,
        status=st,
        start_date=date.today(),
        sequence_json=(sequence_json or "").strip() or None,
        fee_initial=_parse_fee(fee_initial),
        fee_ongoing=_parse_fee(fee_ongoing),
        fee_ongoing_frequency=freq,
        fee_renewal=_parse_fee(fee_renewal),
        email_subject=(email_subject or "").strip() or None,
        email_body=(email_body or "").strip() or None,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def update_campaign_fees(
    db: Session,
    campaign: ProspectCampaign,
    *,
    fee_initial: float = 0.0,
    fee_ongoing: float = 0.0,
    fee_ongoing_frequency: str = "annual",
    fee_renewal: float = 0.0,
    apply_to_members: bool = True,
) -> ProspectCampaign:
    freq = (fee_ongoing_frequency or "annual").strip().lower()
    if freq not in ("monthly", "annual"):
        freq = "annual"
    campaign.fee_initial = _parse_fee(fee_initial)
    campaign.fee_ongoing = _parse_fee(fee_ongoing)
    campaign.fee_ongoing_frequency = freq
    campaign.fee_renewal = _parse_fee(fee_renewal)
    campaign.updated_at = datetime.utcnow()
    if apply_to_members:
        members = (
            db.query(CampaignMember)
            .filter(
                CampaignMember.campaign_id == campaign.id,
                CampaignMember.status != "removed",
            )
            .all()
        )
        for m in members:
            if m.prospect:
                apply_campaign_value_to_prospect(db, campaign, m.prospect, commit=False)
    db.commit()
    db.refresh(campaign)
    return campaign


def ensure_prospect_for_client(db: Session, client: Client) -> Prospect:
    """
    Find or create a Prospect row for an existing Client so they can sit on a
    campaign target list without converting them again.
    """
    # Prefer existing link
    if client.id:
        linked = (
            db.query(Prospect)
            .filter(Prospect.client_id == client.id)
            .order_by(Prospect.id.desc())
            .first()
        )
        if linked:
            return linked

    cn = normalize_company_number(client.company_number) if client.company_number else None
    if cn:
        by_num = (
            db.query(Prospect)
            .filter(Prospect.company_number == cn)
            .order_by(Prospect.id.desc())
            .first()
        )
        if by_num:
            if not by_num.client_id:
                by_num.client_id = client.id
                by_num.updated_at = datetime.utcnow()
                db.flush()
            return by_num

    p = Prospect(
        company_name=client.company_name or client.display_name() or (cn or "Unnamed"),
        company_number=cn,
        contact_name=client.contact_name,
        email=client.email,
        phone=client.phone,
        address_line1=client.address_line1,
        address_line2=getattr(client, "address_line2", None),
        town=client.town,
        postcode=client.postcode,
        source="client_book",
        pipeline_status="new",
        client_id=client.id,
        notes=f"Seeded from client #{client.id} for campaign targeting",
    )
    rescore(db, p)
    db.add(p)
    db.flush()
    return p


def add_clients_to_campaign(
    db: Session, campaign_id: int, client_ids: Sequence[int]
) -> dict:
    """Add existing CRM clients as campaign members (via prospect mirror)."""
    added = 0
    skipped = 0
    errors: List[str] = []
    for cid in client_ids:
        client = db.query(Client).filter(Client.id == int(cid)).first()
        if not client:
            errors.append(f"Client #{cid} not found")
            continue
        # Skip pure individual shells for company campaigns
        cn = (client.company_number or "").upper()
        if cn.startswith("IND-") or (client.client_type or "").lower() == "individual":
            skipped += 1
            continue
        try:
            p = ensure_prospect_for_client(db, client)
            before = (
                db.query(CampaignMember)
                .filter(
                    CampaignMember.campaign_id == campaign_id,
                    CampaignMember.prospect_id == p.id,
                    CampaignMember.status != "removed",
                )
                .first()
            )
            m = add_to_campaign(
                db, campaign_id, p.id, notes=f"From client book #{client.id}"
            )
            if m and not before:
                added += 1
            else:
                skipped += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{client.display_name()}: {exc}")
    return {"added": added, "skipped": skipped, "errors": errors}


def add_to_campaign(
    db: Session, campaign_id: int, prospect_id: int, *, notes: str = ""
) -> Optional[CampaignMember]:
    camp = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not camp or not p:
        return None
    existing = (
        db.query(CampaignMember)
        .filter(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.prospect_id == prospect_id,
        )
        .first()
    )
    if existing:
        if existing.status == "removed":
            existing.status = "queued"
            existing.added_at = datetime.utcnow()
            existing.notes = notes or existing.notes
            db.commit()
            db.refresh(existing)
        return existing
    m = CampaignMember(
        campaign_id=campaign_id,
        prospect_id=prospect_id,
        status="queued",
        notes=(notes or "").strip() or None,
    )
    db.add(m)
    # Pipeline value: initial + ongoing (not renewal)
    apply_campaign_value_to_prospect(db, camp, p, commit=False)
    log_activity(
        db,
        prospect_id,
        activity_type="note",
        subject=f"Added to campaign: {camp.name}",
        campaign_id=campaign_id,
        direction="internal",
        commit=False,
    )
    rescore(db, p)
    db.commit()
    db.refresh(m)
    return m


def convert_prospect_to_client(
    db: Session, prospect_id: int
) -> Tuple[Optional[Client], Optional[Prospect], str]:
    """
    Convert prospect → Active Client. Returns (client, prospect, message).
    """
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return None, None, "Prospect not found"
    if p.client_id:
        cl = db.query(Client).filter(Client.id == p.client_id).first()
        return cl, p, "Already converted"

    cn = normalize_company_number(p.company_number) if p.company_number else None
    client = None
    if cn:
        client = db.query(Client).filter(Client.company_number == cn).first()

    if client:
        client.overall_status = "Active"
        if not client.engagement_date:
            client.engagement_date = date.today()
        if p.email and not client.email:
            client.email = p.email
        if p.phone and not client.phone:
            client.phone = p.phone
        if p.contact_name and not client.contact_name:
            client.contact_name = p.contact_name
        msg = f"Linked to existing client #{client.id}"
    else:
        client = Client(
            company_name=p.company_name,
            company_number=cn,
            contact_name=p.contact_name,
            email=p.email,
            phone=p.phone,
            address_line1=p.address_line1,
            address_line2=p.address_line2,
            town=p.town,
            postcode=p.postcode,
            overall_status="Active",
            engagement_date=date.today(),
            source="prospecting",
            notes=p.notes,
        )
        db.add(client)
        db.flush()
        msg = f"Created client #{client.id}"

    p.client_id = client.id
    p.pipeline_status = "won"
    p.converted_at = datetime.utcnow()
    p.updated_at = datetime.utcnow()
    log_activity(
        db,
        p.id,
        activity_type="convert",
        subject="Converted to client",
        body=msg,
        direction="internal",
        commit=False,
    )
    for m in p.memberships:
        if m.status not in ("removed", "converted"):
            m.status = "converted"
            m.last_touch_at = datetime.utcnow()
    db.commit()
    db.refresh(p)
    db.refresh(client)
    return client, p, msg


def export_campaign_letter_csv(db: Session, campaign_id: int) -> str:
    camp = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not camp:
        return ""
    members = (
        db.query(CampaignMember)
        .filter(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.status != "removed",
        )
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "campaign_ref",
            "prospect_id",
            "company_name",
            "contact_name",
            "address_line1",
            "town",
            "postcode",
            "service_name",
            "letter_code",
            "company_number",
        ]
    )
    svc = camp.service_label or ""
    for m in members:
        p = m.prospect
        if not p:
            continue
        w.writerow(
            [
                f"CAMP-{camp.id}",
                p.id,
                p.company_name or "",
                p.contact_name or "",
                p.address_line1 or "",
                p.town or "",
                p.postcode or "",
                svc,
                camp.channel or "letter",
                p.company_number or "",
            ]
        )
    return buf.getvalue()


def export_campaign_email_csv(db: Session, campaign_id: int) -> str:
    camp = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not camp:
        return ""
    members = (
        db.query(CampaignMember)
        .filter(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.status != "removed",
        )
        .all()
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "email",
            "company_name",
            "contact_name",
            "company_number",
            "prospect_id",
            "campaign",
            "service",
        ]
    )
    for m in members:
        p = m.prospect
        if not p:
            continue
        w.writerow(
            [
                p.email or "",
                p.company_name or "",
                p.contact_name or "",
                p.company_number or "",
                p.id,
                camp.name,
                camp.service_label or "",
            ]
        )
    return buf.getvalue()


def import_emails_csv(db: Session, text: str) -> dict:
    """Match rows by company_number or company_name; set email."""
    reader = csv.DictReader(io.StringIO(text))
    updated = 0
    skipped = 0
    for row in reader:
        email = (row.get("email") or row.get("Email") or "").strip()
        if not email or "@" not in email:
            skipped += 1
            continue
        cn = normalize_company_number(
            row.get("company_number") or row.get("Company Number") or ""
        )
        p = None
        if cn:
            p = db.query(Prospect).filter(Prospect.company_number == cn).first()
        if not p:
            name = (row.get("company_name") or row.get("Company Name") or "").strip()
            if name:
                p = (
                    db.query(Prospect)
                    .filter(Prospect.company_name.ilike(name))
                    .first()
                )
        if not p:
            skipped += 1
            continue
        p.email = email
        rescore(db, p)
        updated += 1
    db.commit()
    return {"updated": updated, "skipped": skipped}


def enrich_prospect_from_ch(db: Session, prospect_id: int) -> Tuple[Optional[Prospect], str]:
    from app.services.companies_house import (
        fetch_company_officers,
        fetch_company_profile,
        summarize_profile_dates,
    )

    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return None, "Not found"
    cn = normalize_company_number(p.company_number or "")
    if not cn:
        return p, "No company number"
    prof = fetch_company_profile(cn)
    if not prof.ok:
        return p, prof.error or "CH profile failed"
    data = prof.profile or {}
    summary = summarize_profile_dates(data)
    p.company_name = summary.get("company_name") or p.company_name
    p.company_status = summary.get("company_status") or data.get("company_status")
    p.accounts_next_due = _parse_date(summary.get("accounts_due"))
    p.cs_next_due = _parse_date(
        (data.get("confirmation_statement") or {}).get("next_due")
    )
    p.incorporation_date = _parse_date(data.get("date_of_creation"))
    sic = data.get("sic_codes") or []
    if isinstance(sic, list):
        p.sic_codes = ", ".join(str(x) for x in sic)
    addr = data.get("registered_office_address") or {}
    p.address_line1 = addr.get("address_line_1") or p.address_line1
    p.address_line2 = addr.get("address_line_2") or p.address_line2
    p.town = addr.get("locality") or p.town
    p.postcode = addr.get("postal_code") or p.postcode
    p.country = addr.get("country") or p.country
    p.ch_profile_json = json.dumps(data)[:200000]
    officers = fetch_company_officers(cn)
    if officers.ok:
        p.ch_officers_json = json.dumps(officers.profile)[:200000]
    p.ch_fetched_at = datetime.utcnow()
    rescore(db, p)
    log_activity(
        db,
        p.id,
        activity_type="ch_refresh",
        subject="Companies House profile refreshed",
        body=f"Status {p.company_status or '—'}; SIC {p.sic_codes or '—'}",
        direction="internal",
        commit=False,
    )
    db.commit()
    db.refresh(p)
    return p, "Enriched from Companies House"


def import_incorporations(
    db: Session,
    *,
    from_date: date,
    to_date: Optional[date] = None,
    sic_codes: str = "",
    location: str = "",
    limit: int = 50,
) -> ChSyncRun:
    from app.services.companies_house import advanced_search_companies

    to_date = to_date or date.today()
    run = ChSyncRun(
        kind="incorporations",
        status="running",
        params_json=json.dumps(
            {
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "sic": sic_codes,
                "location": location,
                "limit": limit,
            }
        ),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    created = updated = errors = 0
    start = 0
    remaining = min(max(int(limit), 1), 200)
    messages: List[str] = []

    while remaining > 0:
        page = min(remaining, 50)
        result = advanced_search_companies(
            incorporated_from=from_date.isoformat(),
            incorporated_to=to_date.isoformat(),
            company_status="active",
            sic_codes=(sic_codes or "").strip() or None,
            location=(location or "").strip() or None,
            size=page,
            start_index=start,
        )
        if not result.ok:
            errors += 1
            messages.append(result.error or "Search failed")
            break
        items = (result.profile or {}).get("items") or []
        if not items:
            break
        for item in items:
            try:
                cn = normalize_company_number(
                    item.get("company_number") or item.get("company_number")
                )
                name = item.get("company_name") or item.get("title") or cn
                if not cn:
                    continue
                existing = (
                    db.query(Prospect).filter(Prospect.company_number == cn).first()
                )
                inc = _parse_date(
                    item.get("date_of_creation")
                    or item.get("incorporation_date")
                )
                addr = item.get("registered_office_address") or {}
                if isinstance(addr, str):
                    addr = {}
                if existing:
                    existing.company_name = name or existing.company_name
                    if inc:
                        existing.incorporation_date = inc
                    existing.company_status = item.get("company_status") or existing.company_status
                    rescore(db, existing)
                    updated += 1
                else:
                    create_prospect(
                        db,
                        company_name=name or cn,
                        company_number=cn,
                        address_line1=addr.get("address_line_1") or "",
                        town=addr.get("locality") or "",
                        postcode=addr.get("postal_code") or "",
                        source="ch_incorporation",
                        incorporation_date=inc,
                        sic_codes=", ".join(
                            str(x) for x in (item.get("sic_codes") or [])
                        )
                        if isinstance(item.get("sic_codes"), list)
                        else (item.get("sic_codes") or ""),
                    )
                    created += 1
            except Exception as exc:  # noqa: BLE001
                errors += 1
                messages.append(str(exc)[:120])
        start += len(items)
        remaining -= len(items)
        if len(items) < page:
            break

    run.created_count = created
    run.updated_count = updated
    run.error_count = errors
    run.finished_at = datetime.utcnow()
    run.status = "ok" if errors == 0 or created + updated > 0 else "failed"
    run.message = (
        f"Created {created}, updated {updated}, errors {errors}. "
        + (" ".join(messages[:3]) if messages else "")
    )[:500]
    db.commit()
    db.refresh(run)
    return run
