"""Prospecting Ledger UI."""

from __future__ import annotations

from datetime import date, datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.prospecting import (
    CAMPAIGN_CHANNELS,
    CAMPAIGN_STATUSES,
    FEE_PROFILE_HELP,
    FEE_PROFILE_LABELS,
    FEE_PROFILES,
    NEXT_ACTIVITY_MAIL_TYPES,
    NEXT_ACTIVITY_OPTIONS,
    PIPELINE_LABELS,
    PIPELINE_STATUSES,
    SERVICE_LINES,
    SOURCE_CHANNEL_LABELS,
    SOURCE_CHANNELS,
    CampaignMember,
    Prospect,
    ProspectActivity,
    ProspectCampaign,
)
from app.models.sales import Service
from app.services.companies_house import (
    download_document_content,
    fetch_document_metadata,
    fetch_filing_history,
    has_api_key,
    search_companies,
)
from app.services.prospecting import (
    add_to_campaign,
    convert_prospect_to_client,
    create_campaign,
    create_prospect,
    enrich_prospect_from_ch,
    export_campaign_email_csv,
    export_campaign_letter_csv,
    hub_stats,
    import_emails_csv,
    import_incorporations,
    list_prospects,
    log_activity,
    prospect_company_opportunities,
    prospect_job_buckets,
    prospect_source_label,
    set_pipeline_status,
    update_prospect_details,
    update_prospect_fees,
    update_prospect_next_step,
)
from app.templating import render

router = APIRouter(prefix="/prospecting", tags=["prospecting"])


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


@router.get("", response_class=HTMLResponse)
async def prospecting_hub(request: Request, db: Session = Depends(get_db)):
    from app.services.prospecting import campaign_list_with_values

    stats = hub_stats(db)
    # Individual open leads with a pipeline value only (campaign members excluded)
    recent = list_prospects(
        db,
        open_only=True,
        individual_only=True,
        with_value_only=True,
        limit=8,
    )
    campaign_rows = campaign_list_with_values(db, limit=8)
    return render(
        request,
        "prospecting/hub.html",
        {
            "stats": stats,
            "campaigns": [r["campaign"] for r in campaign_rows],
            "campaign_rows": campaign_rows,
            "recent": recent,
            "pipeline_labels": PIPELINE_LABELS,
            "ch_key": has_api_key(),
        },
    )


@router.get("/prospects", response_class=HTMLResponse)
async def prospects_list(
    request: Request,
    q: str = Query(""),
    status: str = Query(""),
    source: str = Query(""),
    sic: str = Query(""),
    postcode: str = Query(""),
    campaign_id: str = Query(""),
    min_score: str = Query(""),
    individual: str = Query(""),
    db: Session = Depends(get_db),
):
    cid = int(campaign_id) if (campaign_id or "").isdigit() else None
    ms = int(min_score) if (min_score or "").isdigit() else None
    individual_only = (individual or "").strip().lower() in ("1", "true", "yes", "on")
    rows = list_prospects(
        db,
        q=q,
        status=status,
        source=source,
        sic=sic,
        postcode=postcode,
        campaign_id=cid,
        min_score=ms,
        open_only=not status,
        individual_only=individual_only,
        limit=250,
    )
    campaigns = db.query(ProspectCampaign).order_by(ProspectCampaign.name).all()
    return render(
        request,
        "prospecting/prospects_list.html",
        {
            "rows": rows,
            "q": q,
            "status": status,
            "source": source,
            "sic": sic,
            "postcode": postcode,
            "campaign_id": cid,
            "min_score": min_score,
            "individual": individual_only,
            "campaigns": campaigns,
            "pipeline_statuses": PIPELINE_STATUSES,
            "pipeline_labels": PIPELINE_LABELS,
        },
    )


@router.get("/prospects/new", response_class=HTMLResponse)
async def prospect_new_form(request: Request):
    return render(
        request,
        "prospecting/prospect_form.html",
        {"prospect": None, "error": None},
    )


@router.post("/prospects/new", response_class=HTMLResponse)
async def prospect_create(
    request: Request,
    company_name: str = Form(...),
    company_number: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    sic_codes: str = Form(""),
    notes: str = Form(""),
    estimated_value: str = Form("0"),
    db: Session = Depends(get_db),
):
    try:
        est = float((estimated_value or "0").replace("£", "").replace(",", "").strip() or 0)
    except ValueError:
        est = 0.0
    p = create_prospect(
        db,
        company_name=company_name,
        company_number=company_number,
        contact_name=contact_name,
        email=email,
        phone=phone,
        address_line1=address_line1,
        town=town,
        postcode=postcode,
        sic_codes=sic_codes,
        notes=notes,
        source="manual",
        estimated_value=est,
    )
    return RedirectResponse(f"/prospecting/prospects/{p.id}", status_code=303)


@router.get("/{prospect_id:int}", response_class=HTMLResponse)
async def prospect_detail_legacy_redirect(prospect_id: int):
    """
    Legacy alert links used /prospecting/123 — real page is /prospecting/prospects/123.
    Keep this redirect so old notifications still open.
    """
    return RedirectResponse(f"/prospecting/prospects/{prospect_id}", status_code=303)


@router.get("/prospects/{prospect_id:int}", response_class=HTMLResponse)
async def prospect_detail(
    request: Request,
    prospect_id: int,
    msg: str = Query(""),
    bucket: str = Query(""),
    db: Session = Depends(get_db),
):
    from app.services.ms_graph_oauth import connection_status

    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    activities = (
        db.query(ProspectActivity)
        .filter(ProspectActivity.prospect_id == p.id)
        .order_by(ProspectActivity.activity_at.desc())
        .limit(40)
        .all()
    )
    campaigns = (
        db.query(ProspectCampaign)
        .filter(ProspectCampaign.status.in_(("draft", "active", "paused")))
        .order_by(ProspectCampaign.name)
        .all()
    )
    memberships = (
        db.query(CampaignMember)
        .filter(CampaignMember.prospect_id == p.id, CampaignMember.status != "removed")
        .all()
    )
    buckets = prospect_job_buckets(p)
    # Top tiles ordered by £ value (highest first). Tie-break: Accounts (core service), then job count.
    _tile_ui = {
        "other": {"wc_box": "wc-box-wip", "live_tile": "tile-wip"},
        "accounts": {"wc_box": "wc-box-debtors", "live_tile": "tile-debtors"},
        "sa": {"wc_box": "wc-box-cash", "live_tile": "tile-cash"},
    }
    raw_tiles = [
        buckets["other"],
        buckets["accounts"],
        buckets["self_assessment"],
    ]
    ordered_tiles = sorted(
        raw_tiles,
        key=lambda b: (
            float(b.get("value") or 0),
            1 if b.get("key") == "accounts" else 0,
            int(b.get("count") or 0),
        ),
        reverse=True,
    )
    for t in ordered_tiles:
        ui = _tile_ui.get(t.get("key") or "", {})
        t["wc_box"] = ui.get("wc_box", "wc-box-wip")
        t["live_tile"] = ui.get("live_tile", "tile-wip")

    bkey = (bucket or "").strip().lower()
    if bkey in ("sa", "self_assessment", "self-assessment"):
        bkey = "sa"
    elif bkey not in ("other", "accounts", "sa"):
        # Default to highest-value bucket (Accounts when it leads; Other for CF mandates)
        bkey = (ordered_tiles[0].get("key") if ordered_tiles else "accounts") or "accounts"
    bucket_map = {
        "other": buckets["other"],
        "accounts": buckets["accounts"],
        "sa": buckets["self_assessment"],
    }
    active_bucket = bucket_map.get(bkey) or ordered_tiles[0]
    graph = {}
    try:
        graph = connection_status(db) or {}
    except Exception:
        graph = {}
    graph_connected = bool(graph.get("connected") and graph.get("fresh"))

    # Prefill email draft
    contact = (p.contact_name or "").strip() or "Sir/Madam"
    email_subject = f"{p.display_name()} — next steps"
    email_body = (
        f"Dear {contact},\n\n"
        f"I hope you are well.\n\n"
        f"I am writing regarding {p.display_name()}"
        + (f" ({p.company_number})" if p.company_number else "")
        + ".\n\n"
        f"Kind regards\n"
    )

    return render(
        request,
        "prospecting/prospect_detail.html",
        {
            "p": p,
            "activities": activities,
            "campaigns": campaigns,
            "memberships": memberships,
            "buckets": buckets,
            "ordered_tiles": ordered_tiles,
            "bucket_key": bkey,
            "active_bucket": active_bucket,
            "pipeline_statuses": PIPELINE_STATUSES,
            "pipeline_labels": PIPELINE_LABELS,
            "next_activity_options": NEXT_ACTIVITY_OPTIONS,
            "next_activity_mail_types": list(NEXT_ACTIVITY_MAIL_TYPES),
            "fee_profiles": FEE_PROFILES,
            "fee_profile_labels": FEE_PROFILE_LABELS,
            "fee_profile_help": FEE_PROFILE_HELP,
            "service_lines": SERVICE_LINES,
            "source_channels": SOURCE_CHANNELS,
            "source_channel_labels": SOURCE_CHANNEL_LABELS,
            "source_label": prospect_source_label(p),
            "msg": msg,
            "ch_key": has_api_key(),
            "graph_connected": graph_connected,
            "email_subject": email_subject,
            "email_body": email_body,
        },
    )


@router.get("/prospects/{prospect_id:int}/edit", response_class=HTMLResponse)
async def prospect_edit_form(
    request: Request, prospect_id: int, db: Session = Depends(get_db)
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    return render(
        request,
        "prospecting/prospect_edit.html",
        {
            "p": p,
            "pipeline_labels": PIPELINE_LABELS,
            "fee_profiles": FEE_PROFILES,
            "fee_profile_labels": FEE_PROFILE_LABELS,
            "service_lines": SERVICE_LINES,
            "source_channels": SOURCE_CHANNELS,
            "source_channel_labels": SOURCE_CHANNEL_LABELS,
            "ch_key": has_api_key(),
        },
    )


@router.post("/prospects/{prospect_id:int}/edit")
async def prospect_edit_save(
    prospect_id: int,
    company_name: str = Form(""),
    company_number: str = Form(""),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    country: str = Form(""),
    sic_codes: str = Form(""),
    notes: str = Form(""),
    next_step: str = Form(""),
    next_step_due: str = Form(""),
    service_line: str = Form(""),
    fee_profile: str = Form(""),
    referred_by: str = Form(""),
    source_channel: str = Form(""),
    source_notes: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    _, msg = update_prospect_details(
        db,
        p,
        company_name=company_name,
        company_number=company_number,
        contact_name=contact_name,
        email=email,
        phone=phone,
        address_line1=address_line1,
        address_line2=address_line2,
        town=town,
        postcode=postcode,
        country=country,
        sic_codes=sic_codes,
        notes=notes,
        next_step=next_step,
        next_step_due=next_step_due,
        service_line=service_line,
        fee_profile=fee_profile,
        referred_by=referred_by,
        source_channel=source_channel,
        source_notes=source_notes,
    )
    if msg != "saved":
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}/edit?msg={quote(msg)}",
            status_code=303,
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=saved", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/next-step")
async def prospect_next_step_save(
    prospect_id: int,
    next_activity: str = Form(""),
    next_step: str = Form(""),
    next_step_due: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    # Picker wins; free-text "Other" uses next_step field
    chosen = (next_activity or "").strip()
    if chosen and chosen != "Other":
        step_text = chosen
    else:
        step_text = (next_step or "").strip() or chosen
    update_prospect_next_step(
        db, p, next_step=step_text, next_step_due=next_step_due, notes=notes
    )
    if step_text:
        log_activity(
            db,
            prospect_id,
            activity_type="note",
            subject=f"Next activity: {step_text[:180]}",
            body=(
                f"Due: {next_step_due or '—'}"
                + (f"\n{notes}" if (notes or "").strip() else "")
            ),
            direction="internal",
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=next_step", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/email")
async def prospect_email_outlook(
    prospect_id: int,
    to_email: str = Form(""),
    contact_name: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    mode: str = Form("draft"),
    db: Session = Depends(get_db),
):
    """Create Outlook draft or send mail via Microsoft Graph for this prospect."""
    from app.services.ms_graph_mail import create_outlook_draft, send_mail
    from app.services.ms_graph_oauth import get_valid_access_token

    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)

    to = (to_email or p.email or "").strip()
    # Persist contact/email if provided
    if (contact_name or "").strip():
        p.contact_name = contact_name.strip()
    if to and "@" in to:
        p.email = to
    p.updated_at = datetime.utcnow()
    db.commit()

    token, err = get_valid_access_token(db)
    if not token:
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}?msg="
            + quote(err or "Connect Microsoft to send via Outlook"),
            status_code=303,
        )

    subj = (subject or "").strip() or f"{p.display_name()} — next steps"
    body_text = (body or "").strip()
    mode_l = (mode or "draft").strip().lower()

    if mode_l == "send":
        ok, serr = send_mail(token, to=to, subject=subj, body=body_text)
        if ok:
            log_activity(
                db,
                p.id,
                activity_type="email",
                subject=f"Sent via Outlook: {subj[:180]}",
                body=f"To: {to}\n\n{body_text[:2000]}",
                direction="outbound",
            )
            return RedirectResponse(
                f"/prospecting/prospects/{prospect_id}?msg="
                + quote("Email sent via Outlook"),
                status_code=303,
            )
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}?msg="
            + quote(serr or "Send failed"),
            status_code=303,
        )

    draft, derr = create_outlook_draft(token, to=to, subject=subj, body=body_text)
    if draft:
        link = draft.get("webLink") or ""
        log_activity(
            db,
            p.id,
            activity_type="email",
            subject=f"Outlook draft: {subj[:180]}",
            body=f"Draft created for {to}. Review & send in Outlook.\n{link}",
            direction="outbound",
        )
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}?msg="
            + quote("Outlook draft created — open Outlook to review & send"),
            status_code=303,
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=" + quote(derr or "Draft failed"),
        status_code=303,
    )


@router.get("/prospects/{prospect_id:int}/company", response_class=HTMLResponse)
async def prospect_company_screen(
    request: Request,
    prospect_id: int,
    msg: str = Query(""),
    db: Session = Depends(get_db),
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    opportunities = prospect_company_opportunities(p)
    officers = []
    if p.ch_officers_json:
        try:
            import json as _json

            raw = _json.loads(p.ch_officers_json)
            items = raw.get("items") if isinstance(raw, dict) else []
            if isinstance(items, list):
                officers = items[:12]
        except Exception:
            officers = []
    return render(
        request,
        "prospecting/prospect_company.html",
        {
            "p": p,
            "opportunities": opportunities,
            "officers": officers,
            "msg": msg,
            "ch_key": has_api_key(),
            "pipeline_labels": PIPELINE_LABELS,
            "service_lines": SERVICE_LINES,
            "fee_profile_labels": FEE_PROFILE_LABELS,
        },
    )


@router.post("/prospects/{prospect_id:int}/company")
async def prospect_company_save(
    prospect_id: int,
    company_name: str = Form(""),
    company_number: str = Form(""),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    town: str = Form(""),
    postcode: str = Form(""),
    country: str = Form(""),
    sic_codes: str = Form(""),
    service_line: str = Form(""),
    pull_ch: str = Form(""),
    db: Session = Depends(get_db),
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    _, msg = update_prospect_details(
        db,
        p,
        company_name=company_name or (p.company_name or ""),
        company_number=company_number,
        contact_name=p.contact_name or "",
        email=p.email or "",
        phone=p.phone or "",
        address_line1=address_line1,
        address_line2=address_line2,
        town=town,
        postcode=postcode,
        country=country,
        sic_codes=sic_codes,
        notes=p.notes or "",
        next_step=p.next_step or "",
        next_step_due=p.next_step_due,
        service_line=service_line,
        fee_profile=p.fee_profile or "",
    )
    if msg != "saved":
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}/company?msg={quote(msg)}",
            status_code=303,
        )
    if (pull_ch or "").strip().lower() in ("1", "yes", "true", "on"):
        _, ch_msg = enrich_prospect_from_ch(db, prospect_id)
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}/company?msg={quote(ch_msg[:100])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}/company?msg=saved", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/status")
async def prospect_status(
    prospect_id: int,
    status: str = Form(...),
    lost_reason: str = Form(""),
    db: Session = Depends(get_db),
):
    set_pipeline_status(db, prospect_id, status, lost_reason=lost_reason)
    return RedirectResponse(f"/prospecting/prospects/{prospect_id}?msg=status", status_code=303)


@router.post("/prospects/{prospect_id:int}/activity")
async def prospect_activity(
    prospect_id: int,
    activity_type: str = Form("note"),
    subject: str = Form(""),
    body: str = Form(""),
    direction: str = Form("outbound"),
    db: Session = Depends(get_db),
):
    if db.query(Prospect).filter(Prospect.id == prospect_id).first():
        log_activity(
            db,
            prospect_id,
            activity_type=activity_type,
            subject=subject,
            body=body,
            direction=direction,
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=activity", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/convert")
async def prospect_convert(prospect_id: int, db: Session = Depends(get_db)):
    client, p, msg = convert_prospect_to_client(db, prospect_id)
    if client:
        return RedirectResponse(f"/clients/{client.id}?msg=converted", status_code=303)
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg={quote(msg)}", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/fees")
async def prospect_update_fees(
    prospect_id: int,
    fee_initial: str = Form("0"),
    fee_ongoing: str = Form("0"),
    fee_ongoing_frequency: str = Form("annual"),
    fee_renewal: str = Form("0"),
    confidence_pct: str = Form("50"),
    fee_profile: str = Form("annual"),
    service_line: str = Form(""),
    db: Session = Depends(get_db),
):
    """Adjust this prospect's fees only — does not change the campaign or peers."""
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    try:
        conf = int(float(confidence_pct or 50))
    except (TypeError, ValueError):
        conf = 50
    update_prospect_fees(
        db,
        p,
        fee_initial=fee_initial,
        fee_ongoing=fee_ongoing,
        fee_ongoing_frequency=fee_ongoing_frequency,
        fee_renewal=fee_renewal,
        confidence_pct=conf,
        fee_profile=fee_profile,
        service_line=service_line,
    )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=fees", status_code=303
    )


@router.post("/prospects/{prospect_id:int}/enrich")
async def prospect_enrich(
    prospect_id: int,
    return_to: str = Form("detail"),
    db: Session = Depends(get_db),
):
    """Pull Companies House profile; optional return to company screen."""
    _, msg = enrich_prospect_from_ch(db, prospect_id)
    if (return_to or "").strip() == "company":
        return RedirectResponse(
            f"/prospecting/prospects/{prospect_id}/company?msg={quote(msg[:80])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg={quote(msg[:80])}",
        status_code=303,
    )


@router.post("/prospects/{prospect_id:int}/convert-job")
async def prospect_convert_job(
    prospect_id: int,
    job_type: str = Form(""),
    job_title: str = Form(""),
    job_fee: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Convert prospect → Active client (if needed) and create a job.
    Job fee defaults to this prospect's initial fee (not the campaign default).
    """
    from app.services.prospecting import convert_prospect_to_job

    fee = None
    raw = (job_fee or "").strip().replace(",", "").replace("£", "")
    if raw:
        try:
            fee = float(raw)
        except ValueError:
            fee = None
    client, job, p, msg = convert_prospect_to_job(
        db,
        prospect_id,
        job_type=job_type,
        job_title=job_title,
        job_fee=fee,
        notes=notes,
    )
    if job and client:
        return RedirectResponse(
            f"/jobs/{job.id}?msg={quote('Created from prospect')}",
            status_code=303,
        )
    if client:
        return RedirectResponse(f"/clients/{client.id}?msg={quote(msg)}", status_code=303)
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg={quote(msg or 'Conversion failed')}",
        status_code=303,
    )


@router.post("/prospects/{prospect_id:int}/add-campaign")
async def prospect_add_campaign(
    prospect_id: int,
    campaign_id: int = Form(...),
    db: Session = Depends(get_db),
):
    add_to_campaign(db, campaign_id, prospect_id)
    return RedirectResponse(
        f"/prospecting/prospects/{prospect_id}?msg=campaign", status_code=303
    )


@router.post("/prospects/bulk-add-campaign")
async def bulk_add_campaign(
    campaign_id: int = Form(...),
    prospect_ids: str = Form(""),
    db: Session = Depends(get_db),
):
    for part in (prospect_ids or "").split(","):
        part = part.strip()
        if part.isdigit():
            add_to_campaign(db, campaign_id, int(part))
    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}?msg=added", status_code=303
    )


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_list(request: Request, db: Session = Depends(get_db)):
    rows = db.query(ProspectCampaign).order_by(ProspectCampaign.updated_at.desc()).all()
    counts = {}
    for c in rows:
        n = (
            db.query(CampaignMember)
            .filter(
                CampaignMember.campaign_id == c.id,
                CampaignMember.status != "removed",
            )
            .count()
        )
        counts[c.id] = n
    return render(
        request,
        "prospecting/campaigns.html",
        {"rows": rows, "counts": counts},
    )


def _company_client_choices(db: Session):
    """Active book companies for campaign targeting (exclude individual shells)."""
    from app.models import Client

    rows = (
        db.query(Client)
        .order_by(Client.company_name)
        .limit(800)
        .all()
    )
    out = []
    for c in rows:
        cn = (c.company_number or "").upper()
        if cn.startswith("IND-"):
            continue
        if (c.client_type or "").lower() == "individual":
            continue
        if (c.overall_status or "") == "Inactive":
            continue
        out.append(c)
    return out


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaign_new_form(request: Request, db: Session = Depends(get_db)):
    services = (
        db.query(Service)
        .filter(Service.is_active == True)  # noqa: E712
        .order_by(Service.name)
        .all()
    )
    return render(
        request,
        "prospecting/campaign_form.html",
        {
            "campaign": None,
            "services": services,
            "channels": CAMPAIGN_CHANNELS,
            "statuses": CAMPAIGN_STATUSES,
            "clients": _company_client_choices(db),
            "selected_client_ids": [],
        },
    )


@router.post("/campaigns/new")
async def campaign_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    service_id: str = Form(""),
    channel: str = Form("mixed"),
    status: str = Form("draft"),
    sequence_json: str = Form(""),
    fee_initial: str = Form("0"),
    fee_ongoing: str = Form("0"),
    fee_ongoing_frequency: str = Form("annual"),
    fee_renewal: str = Form("0"),
    email_subject: str = Form(""),
    email_body: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.services.prospecting import add_clients_to_campaign

    sid = int(service_id) if (service_id or "").isdigit() else None
    c = create_campaign(
        db,
        name=name,
        description=description,
        service_id=sid,
        channel=channel,
        status=status,
        sequence_json=sequence_json,
        fee_initial=fee_initial,
        fee_ongoing=fee_ongoing,
        fee_ongoing_frequency=fee_ongoing_frequency,
        fee_renewal=fee_renewal,
        email_subject=email_subject,
        email_body=email_body,
    )
    form = await request.form()
    raw_ids = form.getlist("client_ids")
    client_ids = []
    for v in raw_ids:
        if str(v).strip().isdigit():
            client_ids.append(int(v))
    summary = ""
    if client_ids:
        result = add_clients_to_campaign(db, c.id, client_ids)
        summary = (
            f"?msg=targets&added={result.get('added', 0)}"
            f"&skipped={result.get('skipped', 0)}"
        )
    return RedirectResponse(f"/prospecting/campaigns/{c.id}{summary}", status_code=303)


@router.get("/campaigns/{campaign_id:int}", response_class=HTMLResponse)
async def campaign_detail(
    request: Request,
    campaign_id: int,
    msg: str = Query(""),
    added: str = Query(""),
    skipped: str = Query(""),
    db: Session = Depends(get_db),
):
    from sqlalchemy.orm import joinedload

    c = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not c:
        return RedirectResponse("/prospecting/campaigns", status_code=303)
    members = (
        db.query(CampaignMember)
        .options(joinedload(CampaignMember.prospect))
        .filter(
            CampaignMember.campaign_id == c.id,
            CampaignMember.status != "removed",
        )
        .all()
    )
    # Clients already represented (via prospect.client_id or company number)
    member_client_ids = set()
    for m in members:
        p = m.prospect
        if p and p.client_id:
            member_client_ids.add(p.client_id)

    banner = msg
    if msg == "targets" or (added or skipped):
        a = int(added) if str(added).isdigit() else 0
        s = int(skipped) if str(skipped).isdigit() else 0
        banner = f"Target list updated: {a} added, {s} already on list / skipped."
    elif msg == "added":
        banner = "Member(s) added to campaign."
    elif msg == "fees":
        banner = "Fees and email draft saved. Member pipeline values updated where applicable."
    elif msg == "drafts":
        banner = "Outlook drafts created where email addresses were available."

    graph_connected = False
    try:
        from app.services.ms_graph_oauth import connection_status

        graph = connection_status(db) or {}
        graph_connected = bool(graph.get("connected") and graph.get("fresh"))
    except Exception:
        graph_connected = False

    try:
        pipeline_value = float(c.pipeline_value_per_prospect() or 0)
    except Exception:
        pipeline_value = 0.0

    return render(
        request,
        "prospecting/campaign_detail.html",
        {
            "c": c,
            "members": members,
            "msg": banner,
            "pipeline_labels": PIPELINE_LABELS,
            "clients": _company_client_choices(db),
            "member_client_ids": member_client_ids,
            "pipeline_value": pipeline_value,
            "graph_connected": graph_connected,
        },
    )


@router.post("/campaigns/{campaign_id:int}/fees")
async def campaign_update_fees(
    campaign_id: int,
    fee_initial: str = Form("0"),
    fee_ongoing: str = Form("0"),
    fee_ongoing_frequency: str = Form("annual"),
    fee_renewal: str = Form("0"),
    email_subject: str = Form(""),
    email_body: str = Form(""),
    apply_to_members: str = Form("yes"),
    db: Session = Depends(get_db),
):
    from app.services.prospecting import update_campaign_fees

    c = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not c:
        return RedirectResponse("/prospecting/campaigns", status_code=303)
    update_campaign_fees(
        db,
        c,
        fee_initial=fee_initial,
        fee_ongoing=fee_ongoing,
        fee_ongoing_frequency=fee_ongoing_frequency,
        fee_renewal=fee_renewal,
        apply_to_members=(apply_to_members or "").lower() in ("1", "yes", "on", "true"),
    )
    c.email_subject = (email_subject or "").strip() or None
    c.email_body = (email_body or "").strip() or None
    c.updated_at = __import__("datetime").datetime.utcnow()
    db.commit()
    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}?msg=fees", status_code=303
    )


@router.post("/campaigns/{campaign_id:int}/email-drafts")
async def campaign_push_email_drafts(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Create Outlook drafts for campaign members (review & send in Outlook).
    Does not send immediately — drafts land in the signed-in mailbox.
    """
    from app.services.ms_graph_mail import create_outlook_draft
    from app.services.ms_graph_oauth import get_valid_access_token
    from app.services.prospecting import log_activity

    c = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not c:
        return RedirectResponse("/prospecting/campaigns", status_code=303)

    form = await request.form()
    only_ids = set()
    for v in form.getlist("member_prospect_ids"):
        if str(v).strip().isdigit():
            only_ids.add(int(v))

    token, err = get_valid_access_token(db)
    if not token:
        return RedirectResponse(
            f"/prospecting/campaigns/{campaign_id}?msg="
            + __import__("urllib.parse").quote(
                err or "Connect Microsoft Graph with Mail.Send to create Outlook drafts"
            ),
            status_code=303,
        )

    subject_tmpl = (c.email_subject or "").strip() or f"{c.name} — proposal"
    body_tmpl = (c.email_body or "").strip() or (
        "Dear {{contact}},\n\n"
        "Please find details of our {{campaign}} offering.\n\n"
        "Initial fee: £{{initial_fee}}\n"
        "Ongoing: £{{ongoing_fee}} ({{ongoing_frequency}})\n\n"
        "Kind regards\n"
    )

    def render_tpl(tmpl: str, p) -> str:
        initial = f"{float(c.fee_initial or 0):,.2f}"
        ongoing = f"{float(c.fee_ongoing or 0):,.2f}"
        renewal = f"{float(c.fee_renewal or 0):,.2f}"
        freq = (c.fee_ongoing_frequency or "annual").lower()
        mapping = {
            "{{company}}": p.display_name() if p else "",
            "{{contact}}": (p.contact_name if p else None) or "Sir/Madam",
            "{{initial_fee}}": initial,
            "{{ongoing_fee}}": ongoing,
            "{{ongoing_frequency}}": freq,
            "{{renewal_fee}}": renewal,
            "{{campaign}}": c.name or "",
            "{{email}}": (p.email if p else None) or "",
        }
        out = tmpl
        for k, v in mapping.items():
            out = out.replace(k, str(v))
        return out

    members = (
        db.query(CampaignMember)
        .filter(
            CampaignMember.campaign_id == c.id,
            CampaignMember.status != "removed",
        )
        .all()
    )
    created = 0
    skipped = 0
    failed = 0
    for m in members:
        p = m.prospect
        if not p:
            skipped += 1
            continue
        if only_ids and p.id not in only_ids:
            continue
        to = (p.email or "").strip()
        if not to or "@" not in to:
            skipped += 1
            continue
        subj = render_tpl(subject_tmpl, p)
        body = render_tpl(body_tmpl, p)
        draft, derr = create_outlook_draft(token, to=to, subject=subj, body=body)
        if draft:
            created += 1
            link = draft.get("webLink") or ""
            log_activity(
                db,
                p.id,
                activity_type="email",
                subject=f"Outlook draft: {subj[:180]}",
                body=f"Draft created in Outlook for review/send.\n{link}",
                direction="outbound",
                campaign_id=c.id,
                commit=False,
            )
            m.last_touch_at = __import__("datetime").datetime.utcnow()
        else:
            failed += 1
            log_activity(
                db,
                p.id,
                activity_type="email",
                subject="Outlook draft failed",
                body=derr or "unknown error",
                direction="outbound",
                campaign_id=c.id,
                commit=False,
            )
    db.commit()
    from urllib.parse import quote as uq

    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}?msg="
        + uq(f"Outlook drafts: {created} created, {skipped} skipped (no email), {failed} failed"),
        status_code=303,
    )


@router.post("/campaigns/{campaign_id:int}/add")
async def campaign_add_member(
    campaign_id: int,
    prospect_id: int = Form(...),
    db: Session = Depends(get_db),
):
    add_to_campaign(db, campaign_id, prospect_id)
    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}?msg=added", status_code=303
    )


@router.post("/campaigns/{campaign_id:int}/add-clients")
async def campaign_add_clients(
    campaign_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Add existing CRM companies to the campaign target list."""
    from app.services.prospecting import add_clients_to_campaign

    c = db.query(ProspectCampaign).filter(ProspectCampaign.id == campaign_id).first()
    if not c:
        return RedirectResponse("/prospecting/campaigns", status_code=303)
    form = await request.form()
    client_ids = []
    for v in form.getlist("client_ids"):
        if str(v).strip().isdigit():
            client_ids.append(int(v))
    result = add_clients_to_campaign(db, campaign_id, client_ids)
    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}"
        f"?msg=targets&added={result.get('added', 0)}&skipped={result.get('skipped', 0)}",
        status_code=303,
    )


@router.get("/campaigns/{campaign_id:int}/export/letter")
async def campaign_export_letter(campaign_id: int, db: Session = Depends(get_db)):
    data = export_campaign_letter_csv(db, campaign_id)
    return Response(
        content=data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="campaign-{campaign_id}-letter.csv"'
        },
    )


@router.get("/campaigns/{campaign_id:int}/export/email")
async def campaign_export_email(campaign_id: int, db: Session = Depends(get_db)):
    data = export_campaign_email_csv(db, campaign_id)
    return Response(
        content=data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="campaign-{campaign_id}-email.csv"'
        },
    )


@router.post("/campaigns/{campaign_id:int}/import-emails")
async def campaign_import_emails(
    campaign_id: int,
    csv_data: str = Form(""),
    csv_file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    text = csv_data or ""
    if csv_file and csv_file.filename:
        text = (await csv_file.read()).decode("utf-8", errors="replace")
    result = import_emails_csv(db, text)
    msg = f"updated-{result['updated']}"
    return RedirectResponse(
        f"/prospecting/campaigns/{campaign_id}?msg={msg}", status_code=303
    )


@router.get("/ch/import", response_class=HTMLResponse)
async def ch_import_form(request: Request, msg: str = Query(""), db: Session = Depends(get_db)):
    return render(
        request,
        "prospecting/ch_import.html",
        {
            "msg": msg,
            "ch_key": has_api_key(),
            "today": date.today(),
            "default_from": (date.today().replace(day=1)).isoformat(),
        },
    )


@router.post("/ch/import")
async def ch_import_run(
    from_date: str = Form(...),
    to_date: str = Form(""),
    sic_codes: str = Form(""),
    location: str = Form(""),
    limit: str = Form("50"),
    db: Session = Depends(get_db),
):
    fd = _parse_date(from_date) or date.today()
    td = _parse_date(to_date) or date.today()
    lim = int(limit) if (limit or "").isdigit() else 50
    run = import_incorporations(
        db,
        from_date=fd,
        to_date=td,
        sic_codes=sic_codes,
        location=location,
        limit=lim,
    )
    return RedirectResponse(
        f"/prospecting/ch/import?msg={quote(run.message or 'done')}",
        status_code=303,
    )


@router.get("/ch/search", response_class=HTMLResponse)
async def ch_search(
    request: Request,
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    items = []
    error = ""
    if q.strip():
        res = search_companies(q.strip())
        if res.ok:
            items = (res.profile or {}).get("items") or []
        else:
            error = res.error or "Search failed"
    return render(
        request,
        "prospecting/ch_search.html",
        {"q": q, "items": items, "error": error, "ch_key": has_api_key()},
    )


@router.post("/ch/search/add")
async def ch_search_add(
    company_number: str = Form(...),
    company_name: str = Form(""),
    db: Session = Depends(get_db),
):
    p = create_prospect(
        db,
        company_name=company_name or company_number,
        company_number=company_number,
        source="ch_search",
    )
    enrich_prospect_from_ch(db, p.id)
    return RedirectResponse(f"/prospecting/prospects/{p.id}", status_code=303)


@router.get("/prospects/{prospect_id:int}/filings", response_class=HTMLResponse)
async def prospect_filings(
    request: Request, prospect_id: int, db: Session = Depends(get_db)
):
    p = db.query(Prospect).filter(Prospect.id == prospect_id).first()
    if not p:
        return RedirectResponse("/prospecting/prospects", status_code=303)
    items = []
    error = ""
    if p.company_number:
        res = fetch_filing_history(p.company_number, items_per_page=40)
        if res.ok:
            items = (res.profile or {}).get("items") or []
        else:
            error = res.error or "Could not load filings"
    return render(
        request,
        "prospecting/filings.html",
        {"p": p, "items": items, "error": error},
    )


@router.get("/ch/document")
async def ch_document(
    company_number: str = Query(...),
    transaction_id: str = Query(...),
    db: Session = Depends(get_db),
):
    meta = fetch_document_metadata(company_number, transaction_id)
    if not meta.ok:
        return Response(meta.error or "Document metadata failed", status_code=400)
    links = (meta.profile or {}).get("links") or {}
    doc_link = links.get("document_metadata") or links.get("self") or ""
    document_id = (meta.profile or {}).get("id") or ""
    if not document_id and "/document/" in str(doc_link):
        document_id = str(doc_link).split("/document/")[-1].strip("/")
    ok, body, ctype, err = download_document_content(document_id)
    if not ok:
        return Response(err or "Download failed", status_code=400)
    return Response(
        content=body,
        media_type=ctype or "application/pdf",
        headers={"Content-Disposition": "inline; filename=filing.pdf"},
    )
