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
    DEFAULT_ACCOUNTS_FEE,
    DEFAULT_SA_FEE_PER_DIRECTOR,
    FEE_PROFILE_ANNUAL,
    FEE_PROFILE_BILLS_ON_ACCOUNT,
    FEE_PROFILE_LABELS,
    FEE_PROFILE_MONTHLY,
    FEE_PROFILE_ONE_OFF,
    FEE_PROFILES,
    MEMBER_STATUSES,
    NEXT_ACTIVITY_OPTIONS,
    OPEN_PIPELINE,
    PIPELINE_LABELS,
    PIPELINE_STATUSES,
    SERVICE_LINES,
    SOURCE_CHANNEL_LABELS,
    SOURCE_CHANNELS,
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
        confidence_pct=50,
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


def _active_campaign_prospect_ids_subq(db: Session):
    """Prospect IDs currently on any campaign (not removed)."""
    return (
        db.query(CampaignMember.prospect_id)
        .filter(CampaignMember.status != "removed")
        .distinct()
        .subquery()
    )


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
    individual_only: bool = False,
    with_value_only: bool = False,
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
    if with_value_only:
        query = query.filter(
            Prospect.estimated_value.isnot(None),
            Prospect.estimated_value > 0,
        )
    if individual_only:
        camp_sq = _active_campaign_prospect_ids_subq(db)
        query = query.filter(~Prospect.id.in_(db.query(camp_sq.c.prospect_id)))
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
        query.order_by(
            Prospect.estimated_value.desc().nullslast(),
            Prospect.updated_at.desc(),
            Prospect.created_at.desc(),
        )
        .limit(min(limit, 500))
        .all()
    )


def _normalise_weighted_values(db: Session) -> int:
    """
    Ensure estimated_value is confidence-weighted pipeline £ (not gross).

    When gross is missing but estimated_value and confidence are set, treat the
    stored figure as gross and recompute estimated = gross × confidence%.
    Example: £10,000 at 50% → estimated_value £5,000.
    """
    fixed = 0
    rows = (
        db.query(Prospect)
        .filter(
            Prospect.estimated_value.isnot(None),
            Prospect.estimated_value > 0,
            Prospect.gross_value.is_(None),
        )
        .all()
    )
    for p in rows:
        conf = p.confidence_pct if p.confidence_pct is not None else 50
        conf = max(0, min(100, int(conf)))
        gross = float(p.estimated_value or 0)
        weighted = round(gross * (conf / 100.0), 2)
        # Only rewrite when confidence would change the figure
        if conf < 100 and weighted != gross:
            p.gross_value = round(gross, 2)
            p.estimated_value = weighted
            fixed += 1
    if fixed:
        db.commit()
    return fixed


def hub_stats(db: Session) -> dict:
    """Desk tiles: overall open pipeline (incl. campaigns) + stage / campaign splits."""
    # One-shot data repair so £10k @ 50% shows as £5k pipeline value
    try:
        _normalise_weighted_values(db)
    except Exception:
        db.rollback()

    camp_sq = _active_campaign_prospect_ids_subq(db)
    camp_ids_q = db.query(camp_sq.c.prospect_id)

    all_open_filter = (Prospect.pipeline_status.in_(OPEN_PIPELINE),)
    # Overall open pipeline = individual + campaign members
    open_count = (
        db.query(func.count(Prospect.id)).filter(*all_open_filter).scalar() or 0
    )
    open_value = (
        db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
        .filter(*all_open_filter)
        .scalar()
        or 0
    )

    # Individual open leads (not on any campaign) — stage tiles
    ind_open_filter = (
        Prospect.pipeline_status.in_(OPEN_PIPELINE),
        ~Prospect.id.in_(camp_ids_q),
    )
    individual_count = (
        db.query(func.count(Prospect.id)).filter(*ind_open_filter).scalar() or 0
    )
    individual_value = round(
        float(
            db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
            .filter(*ind_open_filter)
            .scalar()
            or 0
        ),
        2,
    )

    by_status: Dict[str, int] = {}
    by_status_value: Dict[str, float] = {}
    for st in PIPELINE_STATUSES:
        # Stage tiles on the hub are individual-only; list filters still use full counts.
        if st in OPEN_PIPELINE:
            st_filter = (
                Prospect.pipeline_status == st,
                ~Prospect.id.in_(camp_ids_q),
            )
        else:
            st_filter = (Prospect.pipeline_status == st,)
        by_status[st] = (
            db.query(func.count(Prospect.id)).filter(*st_filter).scalar() or 0
        )
        by_status_value[st] = round(
            float(
                db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
                .filter(*st_filter)
                .scalar()
                or 0
            ),
            2,
        )

    # Campaign pipeline: open prospects that sit on a campaign
    camp_member_filter = (
        Prospect.pipeline_status.in_(OPEN_PIPELINE),
        Prospect.id.in_(camp_ids_q),
    )
    campaign_lead_count = (
        db.query(func.count(Prospect.id)).filter(*camp_member_filter).scalar() or 0
    )
    campaign_pipeline_value = round(
        float(
            db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
            .filter(*camp_member_filter)
            .scalar()
            or 0
        ),
        2,
    )
    campaigns_active = (
        db.query(func.count(ProspectCampaign.id))
        .filter(ProspectCampaign.status.in_(("active", "draft", "paused")))
        .scalar()
        or 0
    )
    campaigns_open = (
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
    last_sync = db.query(ChSyncRun).order_by(ChSyncRun.started_at.desc()).first()
    return {
        "open_count": int(open_count),
        "open_value": round(float(open_value), 2),
        "individual_count": int(individual_count),
        "individual_value": individual_value,
        "by_status": by_status,
        "by_status_value": by_status_value,
        "campaigns_active": int(campaigns_active),
        "campaigns_open": int(campaigns_open),
        "campaign_lead_count": int(campaign_lead_count),
        "campaign_pipeline_value": campaign_pipeline_value,
        "activities_week": int(activities_week),
        "won_month": int(won_month),
        "last_sync": last_sync,
    }


def campaign_list_with_values(
    db: Session, *, limit: int = 8
) -> List[Dict[str, Any]]:
    """Recent campaigns with open-member count and pipeline £."""
    campaigns = (
        db.query(ProspectCampaign)
        .order_by(ProspectCampaign.updated_at.desc())
        .limit(limit)
        .all()
    )
    out: List[Dict[str, Any]] = []
    for c in campaigns:
        members = (
            db.query(CampaignMember)
            .filter(
                CampaignMember.campaign_id == c.id,
                CampaignMember.status != "removed",
            )
            .all()
        )
        pids = [m.prospect_id for m in members if m.prospect_id]
        lead_count = 0
        value = 0.0
        if pids:
            lead_count = (
                db.query(func.count(Prospect.id))
                .filter(
                    Prospect.id.in_(pids),
                    Prospect.pipeline_status.in_(OPEN_PIPELINE),
                )
                .scalar()
                or 0
            )
            value = float(
                db.query(func.coalesce(func.sum(Prospect.estimated_value), 0.0))
                .filter(
                    Prospect.id.in_(pids),
                    Prospect.pipeline_status.in_(OPEN_PIPELINE),
                )
                .scalar()
                or 0
            )
        out.append(
            {
                "campaign": c,
                "lead_count": int(lead_count),
                "pipeline_value": round(value, 2),
            }
        )
    return out


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


def recalculate_prospect_value(db: Session, prospect: Prospect, *, commit: bool = False) -> float:
    """Recompute gross_value and estimated_value from prospect fee fields + confidence."""
    if prospect.confidence_pct is None:
        prospect.confidence_pct = 50
    prospect.gross_value = prospect.compute_gross_value()
    prospect.estimated_value = prospect.compute_weighted_value()
    prospect.updated_at = datetime.utcnow()
    rescore(db, prospect)
    if commit:
        db.commit()
        db.refresh(prospect)
    return float(prospect.estimated_value or 0)


def apply_campaign_value_to_prospect(
    db: Session,
    campaign: ProspectCampaign,
    prospect: Prospect,
    *,
    commit: bool = False,
    force: bool = False,
) -> float:
    """
    Copy campaign fee structure onto the prospect (unless already customised)
    and set internal valuation = (setup + 1yr ongoing) × 50% confidence.

    force=True overwrites individual fees (e.g. when campaign fees re-applied).
    """
    if force or prospect.fee_initial is None:
        prospect.fee_initial = float(campaign.fee_initial or 0)
    if force or prospect.fee_ongoing is None:
        prospect.fee_ongoing = float(campaign.fee_ongoing or 0)
    if force or not (prospect.fee_ongoing_frequency or "").strip():
        prospect.fee_ongoing_frequency = (
            (campaign.fee_ongoing_frequency or "annual").strip().lower() or "annual"
        )
    if force or prospect.fee_renewal is None:
        prospect.fee_renewal = float(campaign.fee_renewal or 0)
    if prospect.confidence_pct is None or force:
        prospect.confidence_pct = 50
    return recalculate_prospect_value(db, prospect, commit=commit)


def update_prospect_fees(
    db: Session,
    prospect: Prospect,
    *,
    fee_initial: float = 0.0,
    fee_ongoing: float = 0.0,
    fee_ongoing_frequency: str = "annual",
    fee_renewal: float = 0.0,
    confidence_pct: int = 50,
    fee_profile: str = "",
    service_line: str = "",
) -> Prospect:
    """Adjust one prospect's fees without changing the campaign or other members."""
    profile = (fee_profile or prospect.fee_profile or FEE_PROFILE_ANNUAL).strip().lower()
    if profile not in FEE_PROFILES:
        profile = FEE_PROFILE_ANNUAL
    if profile == FEE_PROFILE_MONTHLY:
        freq = "monthly"
    elif profile == FEE_PROFILE_ANNUAL:
        freq = "annual"
    else:
        # one_off / bills_on_account — ongoing not used in valuation
        freq = (fee_ongoing_frequency or prospect.fee_ongoing_frequency or "annual").strip().lower()
        if freq not in ("monthly", "annual"):
            freq = "annual"
    try:
        conf = int(confidence_pct)
    except (TypeError, ValueError):
        conf = 50
    conf = max(0, min(100, conf))
    prospect.fee_profile = profile
    if service_line is not None and str(service_line).strip():
        prospect.service_line = str(service_line).strip()[:120]
    prospect.fee_initial = _parse_fee(fee_initial)
    if profile in (FEE_PROFILE_ONE_OFF, FEE_PROFILE_BILLS_ON_ACCOUNT):
        # Store engagement total on initial; clear ongoing for clean valuation
        prospect.fee_ongoing = 0.0
        prospect.fee_ongoing_frequency = "annual"
    else:
        prospect.fee_ongoing = _parse_fee(fee_ongoing)
        prospect.fee_ongoing_frequency = freq
    prospect.fee_renewal = _parse_fee(fee_renewal)
    prospect.confidence_pct = conf
    recalculate_prospect_value(db, prospect, commit=True)
    log_activity(
        db,
        prospect.id,
        activity_type="note",
        subject="Fees adjusted (individual)",
        body=(
            f"Profile {FEE_PROFILE_LABELS.get(profile, profile)}; "
            f"initial £{prospect.fee_initial or 0:,.2f}; ongoing £{prospect.fee_ongoing or 0:,.2f} "
            f"({prospect.fee_ongoing_frequency}); renewal £{prospect.fee_renewal or 0:,.2f}; "
            f"confidence {conf}% → gross £{prospect.gross_value or 0:,.2f}; "
            f"pipeline £{prospect.estimated_value or 0:,.2f}"
        ),
        direction="internal",
        commit=True,
    )
    db.refresh(prospect)
    return prospect


def update_prospect_details(
    db: Session,
    prospect: Prospect,
    *,
    company_name: str = "",
    company_number: str = "",
    contact_name: str = "",
    email: str = "",
    phone: str = "",
    address_line1: str = "",
    address_line2: str = "",
    town: str = "",
    postcode: str = "",
    country: str = "",
    sic_codes: str = "",
    notes: str = "",
    next_step: str = "",
    next_step_due: Any = None,
    service_line: str = "",
    fee_profile: str = "",
    referred_by: str = "",
    source_channel: str = "",
    source_notes: str = "",
) -> Tuple[Prospect, str]:
    """Edit core prospect / company fields (best-practice CRM record update)."""
    name = (company_name or "").strip()
    if name:
        prospect.company_name = name
    cn_raw = (company_number or "").strip()
    if cn_raw:
        cn = normalize_company_number(cn_raw)
        if cn and cn != (prospect.company_number or ""):
            clash = (
                db.query(Prospect)
                .filter(Prospect.company_number == cn, Prospect.id != prospect.id)
                .first()
            )
            if clash:
                return prospect, f"Company number already on prospect #{clash.id}"
            prospect.company_number = cn
    elif company_number is not None and not cn_raw:
        # Explicit clear only when empty string posted from form with intent —
        # leave existing if the field was omitted differently
        pass

    prospect.contact_name = (contact_name or "").strip() or None
    prospect.email = (email or "").strip() or None
    prospect.phone = (phone or "").strip() or None
    prospect.address_line1 = (address_line1 or "").strip() or None
    prospect.address_line2 = (address_line2 or "").strip() or None
    prospect.town = (town or "").strip() or None
    prospect.postcode = (postcode or "").strip() or None
    if (country or "").strip():
        prospect.country = country.strip()
    prospect.sic_codes = (sic_codes or "").strip() or None
    if notes is not None:
        prospect.notes = (notes or "").strip() or None
    if next_step is not None:
        prospect.next_step = (next_step or "").strip() or None
    due = _parse_date(next_step_due) if next_step_due not in (None, "") else None
    if next_step_due is not None:
        prospect.next_step_due = due
    if (service_line or "").strip():
        prospect.service_line = service_line.strip()[:120]
    if (fee_profile or "").strip().lower() in FEE_PROFILES:
        prospect.fee_profile = fee_profile.strip().lower()
        if prospect.fee_profile == FEE_PROFILE_MONTHLY:
            prospect.fee_ongoing_frequency = "monthly"
        elif prospect.fee_profile == FEE_PROFILE_ANNUAL:
            prospect.fee_ongoing_frequency = "annual"

    # Source tracker — who introduced the lead
    if referred_by is not None:
        prospect.referred_by = (referred_by or "").strip()[:160] or None
    ch = (source_channel or "").strip().lower()
    if ch in SOURCE_CHANNELS:
        prospect.source_channel = ch
    elif source_channel is not None and not ch:
        prospect.source_channel = None
    if source_notes is not None:
        prospect.source_notes = (source_notes or "").strip() or None

    prospect.updated_at = datetime.utcnow()
    rescore(db, prospect)
    db.commit()
    db.refresh(prospect)
    return prospect, "saved"


def prospect_source_label(prospect: Prospect) -> str:
    """Human-readable source line for lists and the jobs box."""
    channel = SOURCE_CHANNEL_LABELS.get(
        (prospect.source_channel or "").strip().lower(),
        "",
    )
    who = (prospect.referred_by or "").strip()
    if who and channel:
        return f"{who} · {channel}"
    if who:
        return who
    if channel:
        return channel
    sys_src = (prospect.source or "").strip()
    return sys_src or "—"


def update_prospect_next_step(
    db: Session,
    prospect: Prospect,
    *,
    next_step: str = "",
    next_step_due: Any = None,
    notes: str = "",
) -> Prospect:
    """Quick-save next step / notes from the prospect home screen."""
    prospect.next_step = (next_step or "").strip() or None
    if next_step_due is not None:
        prospect.next_step_due = _parse_date(next_step_due) if next_step_due else None
    if notes is not None:
        prospect.notes = (notes or "").strip() or None
    prospect.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(prospect)
    return prospect


def _active_directors(prospect: Prospect) -> List[Dict[str, Any]]:
    """Active directors from CH officers JSON (excludes resigned / secretaries)."""
    out: List[Dict[str, Any]] = []
    if not prospect.ch_officers_json:
        return out
    try:
        raw = json.loads(prospect.ch_officers_json)
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return out
        for o in items:
            if o.get("resigned_on"):
                continue
            role = (o.get("officer_role") or "").strip().lower()
            if "director" not in role and role not in ("llp-member", "member"):
                continue
            name = (o.get("name") or "").strip()
            # CH often returns "GREENE, Mark" / "GREENE, MARK"
            if "," in name:
                last, first = [x.strip() for x in name.split(",", 1)]
                name = f"{first.title()} {last.title()}"
            elif name:
                name = name.title()
            out.append(
                {
                    "name": name or "Director",
                    "role": o.get("officer_role") or "director",
                    "appointed_on": o.get("appointed_on"),
                    "fee": DEFAULT_SA_FEE_PER_DIRECTOR,
                }
            )
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return out


def prospect_job_buckets(prospect: Prospect) -> Dict[str, Any]:
    """
    Landing-screen opportunity buckets:

    - other: commercial jobs (e.g. corporate finance £10k)
    - accounts: CH-linked annual accounts guestimate (default £2,500)
    - self_assessment: directors × £250
    """
    conf = prospect.confidence_pct if prospect.confidence_pct is not None else 50
    conf = max(0, min(100, int(conf)))

    # --- Other (commercial / service-line jobs) ---
    other_jobs: List[Dict[str, Any]] = []
    gross_other = float(prospect.compute_gross_value() or 0)
    # Prefer explicit engagement fee when set
    fee_init = float(prospect.fee_initial if prospect.fee_initial is not None else 0)
    if fee_init > 0 and prospect.fee_profile_key() in (
        FEE_PROFILE_ONE_OFF,
        FEE_PROFILE_BILLS_ON_ACCOUNT,
    ):
        gross_other = fee_init
    elif fee_init > 0 and gross_other <= 0:
        gross_other = fee_init

    sl = (prospect.service_line or "").strip()
    if gross_other > 0 or sl:
        title = sl or "Engagement"
        if sl and "corporate finance" in sl.lower():
            title = "Corporate finance"
        other_jobs.append(
            {
                "id": "other-main",
                "title": title,
                "detail": (
                    f"{prospect.fee_profile_label()}"
                    + (
                        f" · confidence {conf}%"
                        if conf
                        else ""
                    )
                ),
                "fee": round(gross_other, 2),
                "pipeline": round(gross_other * (conf / 100.0), 2),
                "source": "Opportunity",
                "status": prospect.pipeline_label(),
            }
        )

    other_value = round(sum(j["fee"] for j in other_jobs), 2)
    other_pipeline = round(sum(j["pipeline"] for j in other_jobs), 2)

    # --- Accounts (guestimate) ---
    accounts_jobs: List[Dict[str, Any]] = []
    # Show for any registered company we know about (CH number or status)
    has_co = bool((prospect.company_number or "").strip() or prospect.ch_fetched_at)
    if has_co:
        due = prospect.accounts_next_due
        days = (due - date.today()).days if due else None
        accounts_jobs.append(
            {
                "id": "accounts-annual",
                "title": "Annual accounts",
                "detail": (
                    f"Accounts next due {due.isoformat()}"
                    if due
                    else "Guestimate — due date not on CH profile (e.g. overseas / incomplete data)"
                ),
                "fee": DEFAULT_ACCOUNTS_FEE,
                "pipeline": round(DEFAULT_ACCOUNTS_FEE * (conf / 100.0), 2),
                "source": "Companies House",
                "due": due,
                "days": days,
                "status": (
                    "Overdue"
                    if days is not None and days < 0
                    else ("Due soon" if days is not None and days <= 90 else "Planned")
                ),
            }
        )
    accounts_value = round(sum(j["fee"] for j in accounts_jobs), 2)
    accounts_pipeline = round(sum(j["pipeline"] for j in accounts_jobs), 2)

    # --- Self assessment (directors × £250) ---
    directors = _active_directors(prospect)
    sa_jobs: List[Dict[str, Any]] = []
    for i, d in enumerate(directors):
        sa_jobs.append(
            {
                "id": f"sa-{i}",
                "title": f"Self Assessment — {d['name']}",
                "detail": f"{d['role']}"
                + (f" · appointed {d['appointed_on']}" if d.get("appointed_on") else ""),
                "fee": DEFAULT_SA_FEE_PER_DIRECTOR,
                "pipeline": round(DEFAULT_SA_FEE_PER_DIRECTOR * (conf / 100.0), 2),
                "source": "Companies House officers",
                "status": "Planned",
                "director": d["name"],
            }
        )
    sa_value = round(sum(j["fee"] for j in sa_jobs), 2)
    sa_pipeline = round(sum(j["pipeline"] for j in sa_jobs), 2)

    total_jobs = len(other_jobs) + len(accounts_jobs) + len(sa_jobs)
    total_value = round(other_value + accounts_value + sa_value, 2)
    total_pipeline = round(other_pipeline + accounts_pipeline + sa_pipeline, 2)

    return {
        "confidence_pct": conf,
        "other": {
            "key": "other",
            "label": "Other",
            "jobs": other_jobs,
            "count": len(other_jobs),
            "value": other_value,
            "pipeline": other_pipeline,
        },
        "accounts": {
            "key": "accounts",
            "label": "Accounts",
            "jobs": accounts_jobs,
            "count": len(accounts_jobs),
            "value": accounts_value,
            "pipeline": accounts_pipeline,
            "default_fee": DEFAULT_ACCOUNTS_FEE,
        },
        "self_assessment": {
            "key": "sa",
            "label": "Self Assessment",
            "jobs": sa_jobs,
            "count": len(sa_jobs),
            "value": sa_value,
            "pipeline": sa_pipeline,
            "per_director": DEFAULT_SA_FEE_PER_DIRECTOR,
            "directors": directors,
        },
        "total_jobs": total_jobs,
        "total_value": total_value,
        "total_pipeline": total_pipeline,
    }


def prospect_company_opportunities(prospect: Prospect) -> List[Dict[str, Any]]:
    """
    Potential work items for the company screen.

    Combines Companies House compliance cues with opportunity-style extras.
    """
    today = date.today()
    items: List[Dict[str, Any]] = []
    buckets = prospect_job_buckets(prospect)

    for j in buckets["accounts"]["jobs"]:
        days = j.get("days")
        items.append(
            {
                "kind": "ch_accounts",
                "title": j["title"],
                "source": "Companies House",
                "due": j.get("due"),
                "days": days,
                "urgency": (
                    "overdue"
                    if days is not None and days < 0
                    else ("soon" if days is not None and days <= 90 else "planned")
                ),
                "detail": f"{j['detail']} · guestimate £{j['fee']:,.0f}",
            }
        )
    if prospect.cs_next_due:
        days = (prospect.cs_next_due - today).days
        items.append(
            {
                "kind": "ch_cs",
                "title": "Confirmation statement",
                "source": "Companies House",
                "due": prospect.cs_next_due,
                "days": days,
                "urgency": "overdue" if days < 0 else ("soon" if days <= 60 else "planned"),
                "detail": f"CS next due {prospect.cs_next_due.isoformat()}",
            }
        )
    for j in buckets["other"]["jobs"]:
        items.append(
            {
                "kind": "opportunity",
                "title": j["title"],
                "source": "Opportunity",
                "due": None,
                "days": None,
                "urgency": "planned",
                "detail": f"{j['detail']} · £{j['fee']:,.0f}",
            }
        )
    for j in buckets["self_assessment"]["jobs"]:
        items.append(
            {
                "kind": "sa",
                "title": j["title"],
                "source": "Companies House officers",
                "due": None,
                "days": None,
                "urgency": "planned",
                "detail": f"{j['detail']} · £{j['fee']:,.0f}",
            }
        )

    if not prospect.company_number:
        items.append(
            {
                "kind": "action",
                "title": "Link Companies House number",
                "source": "Setup",
                "due": None,
                "days": None,
                "urgency": "soon",
                "detail": "Add a company number then pull profile, officers, and due dates.",
            }
        )
    elif not prospect.ch_fetched_at:
        items.append(
            {
                "kind": "action",
                "title": "Pull Companies House profile",
                "source": "Setup",
                "due": None,
                "days": None,
                "urgency": "soon",
                "detail": "Enrich address, SIC, status, accounts and CS due dates.",
            }
        )

    return items


def convert_prospect_to_job(
    db: Session,
    prospect_id: int,
    *,
    job_type: str = "",
    job_title: str = "",
    job_fee: Optional[float] = None,
    notes: str = "",
) -> Tuple[Optional[Client], Optional[Job], Optional[Prospect], str]:
    """
    Convert campaign prospect → Active client (if needed) + create a job.

    Job fee defaults to the prospect's initial (setup) fee.
    Ongoing/renewal stay on the prospect notes for the job.
    """
    from app.models.job import Job
    from app.services.fees import get_suggested_fee

    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return None, None, None, "Prospect not found"

    client, p, conv_msg = convert_prospect_to_client(db, prospect_id)
    if not client:
        return None, None, p, conv_msg or "Could not create/link client"

    # Prefer campaign service label for type/title
    camp_label = ""
    for m in p.memberships or []:
        if m.status != "removed" and m.campaign and m.campaign.service_label:
            camp_label = m.campaign.service_label
            break
        if m.status != "removed" and m.campaign:
            camp_label = m.campaign.name or ""
            break

    jtype = (job_type or "").strip() or (camp_label or "Other")[:80]
    title = (job_title or "").strip() or f"{jtype} — {p.display_name()}"
    initial = float(p.fee_initial if p.fee_initial is not None else 0)
    fee = float(job_fee) if job_fee is not None else initial
    if fee <= 0:
        suggested = get_suggested_fee(db, jtype, None, client_id=client.id)
        if suggested is not None:
            fee = float(suggested)

    ongoing = float(p.fee_ongoing if p.fee_ongoing is not None else 0)
    freq = (p.fee_ongoing_frequency or "annual").lower()
    renewal = float(p.fee_renewal if p.fee_renewal is not None else 0)
    fee_note = (
        f"From prospecting: setup £{initial:,.2f}; ongoing £{ongoing:,.2f}/{freq}; "
        f"renewal £{renewal:,.2f}; confidence {p.confidence_pct or 50}%."
    )
    extra = (notes or "").strip()
    job_notes = f"{fee_note}\n{extra}".strip() if extra else fee_note

    job = Job(
        title=title[:200],
        type=jtype[:80],
        client_id=client.id,
        fee=round(fee, 2),
        status="Planned",
        notes=job_notes,
        source="prospecting",
    )
    db.add(job)
    if p.pipeline_status != "won":
        p.pipeline_status = "won"
        p.converted_at = p.converted_at or datetime.utcnow()
    for m in p.memberships or []:
        if m.status not in ("removed", "converted"):
            m.status = "converted"
            m.last_touch_at = datetime.utcnow()
    log_activity(
        db,
        p.id,
        activity_type="convert",
        subject=f"Converted to job: {title}",
        body=f"Client #{client.id} · Job fee £{fee:,.2f}\n{job_notes}",
        direction="internal",
        commit=False,
    )
    db.commit()
    db.refresh(job)
    db.refresh(client)
    db.refresh(p)
    return client, job, p, f"Client #{client.id}; job #{job.id} created (£{fee:,.2f})"


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
                # force=True: campaign fee re-save overwrites member fee structure
                apply_campaign_value_to_prospect(
                    db, campaign, m.prospect, commit=False, force=True
                )
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
