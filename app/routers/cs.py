"""Confirmation Statement review packs (CH download + practice workflow)."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.services.ch_filing import prepare_cs_filing, prep_dict, readiness_for_pack
from app.services.ch_oauth import latest_token_for_client, oauth_is_ready, token_is_fresh
from app.services.cs_automation import (
    apply_ch_address_to_client,
    build_cs_comparison,
    company_public_url,
    create_contact_from_officer,
    create_or_refresh_pack,
    export_pack_text,
    fix_cs_job_title,
    form_dict,
    get_pack,
    latest_pack_for_client,
    mark_filed,
    mark_ready,
    save_review,
    sync_accounts_job_from_ch,
    unlink_person_from_client,
    webfiling_url,
)
from app.templating import render

router = APIRouter(tags=["confirmation-statement"])


def _user(request: Request) -> str:
    u = request.session.get("user") if hasattr(request, "session") else None
    return str(u) if u else ""


@router.post("/clients/{client_id:int}/cs/download", response_class=HTMLResponse)
async def cs_download(
    client_id: int, request: Request, db: Session = Depends(get_db)
):
    result = create_or_refresh_pack(
        db, client_id, prepared_by=_user(request), force_new=False
    )
    if not result.ok or not result.pack:
        return RedirectResponse(
            f"/clients/{client_id}?cs_error={url_quote(result.error or 'Download failed')}",
            status_code=303,
        )
    return RedirectResponse(f"/cs/{result.pack.id}", status_code=303)


@router.get("/cs/{pack_id:int}", response_class=HTMLResponse)
async def cs_review(
    pack_id: int, request: Request, db: Session = Depends(get_db)
):
    pack = get_pack(db, pack_id)
    if not pack:
        return RedirectResponse("/clients", status_code=303)
    client = db.query(Client).filter(Client.id == pack.client_id).first()
    job = db.query(Job).filter(Job.id == pack.job_id).first() if pack.job_id else None
    accounts_job = None
    if client:
        accounts_job = (
            db.query(Job)
            .filter(Job.client_id == client.id)
            .filter(Job.type == "Accounts")
            .filter(Job.status.notin_(["Completed", "Cancelled"]))
            .order_by(Job.id.desc())
            .first()
        )
    form = form_dict(pack)
    people = list(client.people) if client and client.people is not None else []
    compare = build_cs_comparison(
        pack, client, people=people, job=job, accounts_job=accounts_job
    )
    oauth_token = (
        latest_token_for_client(db, pack.client_id) if pack.client_id else None
    )
    readiness = readiness_for_pack(db, pack, client)
    return render(
        request,
        "cs/review.html",
        {
            "pack": pack,
            "client": client,
            "job": job,
            "accounts_job": accounts_job,
            "form": form,
            "compare": compare,
            "webfiling_url": webfiling_url(pack.company_number or ""),
            "company_public_url": company_public_url(pack.company_number or ""),
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_configured": oauth_is_ready(),
            "oauth_token": oauth_token,
            "oauth_token_fresh": token_is_fresh(oauth_token) if oauth_token else False,
            "readiness": readiness,
            "filing_prep": prep_dict(pack),
        },
    )


@router.post("/cs/{pack_id:int}/fix/{action}", response_class=HTMLResponse)
async def cs_fix(
    pack_id: int,
    action: str,
    request: Request,
    person_id: str = Form(""),
    officer_name: str = Form(""),
    officer_role: str = Form(""),
    db: Session = Depends(get_db),
):
    """Apply a fix from the CS compare screen (address, contacts, accounts dates)."""
    pack = get_pack(db, pack_id)
    if not pack:
        return RedirectResponse("/clients", status_code=303)
    client = db.query(Client).filter(Client.id == pack.client_id).first()
    if not client:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote('Client not found')}", status_code=303
        )
    job = db.query(Job).filter(Job.id == pack.job_id).first() if pack.job_id else None
    msg = "fixed"
    err = ""

    try:
        if action == "address":
            if apply_ch_address_to_client(client, pack):
                db.commit()
                msg = "address_fixed"
            else:
                err = "No CH registered office in pack — refresh from CH first."
        elif action == "unlink":
            pid = int(person_id) if (person_id or "").isdigit() else 0
            if unlink_person_from_client(db, client, pid):
                db.commit()
                msg = "unlinked"
            else:
                err = "Could not unlink that contact."
        elif action == "create-contact":
            person = create_contact_from_officer(
                db,
                client,
                officer_name=officer_name,
                officer_role=officer_role,
            )
            if person:
                db.commit()
                msg = "contact_created"
            else:
                err = "Officer name missing."
        elif action == "accounts-dates":
            aj = sync_accounts_job_from_ch(db, client, pack)
            if aj:
                db.commit()
                msg = "accounts_synced"
            else:
                err = "No CH accounts period in pack — refresh from CH."
        elif action == "cs-job-title":
            if fix_cs_job_title(db, client, pack, job):
                db.commit()
                msg = "job_title_fixed"
            else:
                err = "No CS job linked to this pack."
        else:
            err = f"Unknown fix action: {action}"
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        err = str(exc)[:200]

    if err:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(err)}", status_code=303
        )
    return RedirectResponse(f"/cs/{pack_id}?msg={msg}", status_code=303)


@router.post("/cs/{pack_id:int}/save", response_class=HTMLResponse)
async def cs_save(
    pack_id: int,
    request: Request,
    review_notes: str = Form(""),
    confirmed_no_changes: str = Form(""),
    checklist_notes: str = Form(""),
    confirmed_accurate: str = Form(""),
    changes_needed: str = Form(""),
    db: Session = Depends(get_db),
):
    result = save_review(
        db,
        pack_id,
        review_notes=review_notes,
        confirmed_no_changes=confirmed_no_changes,
        checklist_notes=checklist_notes,
        confirmed_accurate=confirmed_accurate == "yes",
        changes_needed=changes_needed == "yes",
    )
    if not result.ok:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(result.error)}", status_code=303
        )
    return RedirectResponse(f"/cs/{pack_id}?msg=saved", status_code=303)


@router.post("/cs/{pack_id:int}/ready", response_class=HTMLResponse)
async def cs_ready(
    pack_id: int, request: Request, db: Session = Depends(get_db)
):
    result = mark_ready(db, pack_id)
    if not result.ok:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(result.error)}", status_code=303
        )
    return RedirectResponse(f"/cs/{pack_id}?msg=ready", status_code=303)


@router.post("/cs/{pack_id:int}/filed", response_class=HTMLResponse)
async def cs_filed(
    pack_id: int, request: Request, db: Session = Depends(get_db)
):
    result = mark_filed(db, pack_id, complete_job=True)
    if not result.ok:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(result.error)}", status_code=303
        )
    return RedirectResponse(f"/cs/{pack_id}?msg=filed", status_code=303)


@router.post("/cs/{pack_id:int}/refresh", response_class=HTMLResponse)
async def cs_refresh(
    pack_id: int, request: Request, db: Session = Depends(get_db)
):
    pack = get_pack(db, pack_id)
    if not pack:
        return RedirectResponse("/clients", status_code=303)
    result = create_or_refresh_pack(
        db,
        pack.client_id,
        job_id=pack.job_id,
        prepared_by=_user(request),
        force_new=False,
    )
    if not result.ok or not result.pack:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(result.error or 'Refresh failed')}",
            status_code=303,
        )
    return RedirectResponse(f"/cs/{result.pack.id}?msg=refreshed", status_code=303)


@router.get("/cs/{pack_id:int}/export")
async def cs_export(pack_id: int, db: Session = Depends(get_db)):
    pack = get_pack(db, pack_id)
    if not pack:
        return RedirectResponse("/clients", status_code=303)
    client = db.query(Client).filter(Client.id == pack.client_id).first()
    text = export_pack_text(pack, client)
    fname = f"cs-pack-{pack.company_number or pack.id}.txt"
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/cs/{pack_id:int}/prepare-filing", response_class=HTMLResponse)
async def cs_prepare_filing(
    pack_id: int,
    request: Request,
    create_transaction: str = Form("yes"),
    db: Session = Depends(get_db),
):
    pack = get_pack(db, pack_id)
    if not pack:
        return RedirectResponse("/clients", status_code=303)
    client = db.query(Client).filter(Client.id == pack.client_id).first()
    try:
        prep = prepare_cs_filing(
            db,
            pack,
            client,
            create_tx=(create_transaction or "yes") == "yes",
        )
    except Exception as exc:
        return RedirectResponse(
            f"/cs/{pack_id}?error={url_quote(str(exc)[:200])}", status_code=303
        )
    msg = "prepared"
    if prep.get("transaction", {}).get("ok"):
        msg = "prepared_tx"
    elif prep.get("transaction", {}).get("attempted") and not prep.get(
        "transaction", {}
    ).get("ok"):
        msg = "prepared_tx_fail"
    return RedirectResponse(f"/cs/{pack_id}?msg={msg}", status_code=303)
