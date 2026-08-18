"""Xero OAuth, books pull, and journal post-back."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

import app.config as app_config
from app.config import refresh_xero_settings, xero_configured
from app.database import get_db
from app.models import Client
from app.services.xero_books import (
    assign_tenant,
    list_journal_drafts,
    load_journal_draft,
    post_journal_draft,
    pull_client_books,
    save_uploaded_draft,
)
from app.services.xero_oauth import (
    build_authorise_url,
    connection_status,
    default_redirect_uri,
    exchange_code,
    fetch_connections,
    fetch_userinfo,
    mask_client_id,
    oauth_is_ready,
    parse_state,
    refresh_tenants,
    resolve_redirect_uri,
    revoke_local,
    save_token,
    sign_state,
)
from app.templating import render

router = APIRouter(tags=["xero"])


@router.get("/oauth/xero/start")
async def oauth_start(
    request: Request,
    return_to: str = "",
):
    refresh_xero_settings(force_dotenv=True)
    if not oauth_is_ready():
        return RedirectResponse(
            "/settings/xero?oauth_error="
            + url_quote(
                "Xero is not configured. Set XERO_CLIENT_ID and XERO_CLIENT_SECRET "
                "in .env, add the redirect URI in the Xero app, then restart."
            ),
            status_code=303,
        )
    ret = (return_to or "").strip() or "/settings/xero"
    if "#" in ret:
        ret = ret.split("#", 1)[0]
    if not ret.startswith("/"):
        ret = "/settings/xero"
    redir = resolve_redirect_uri(request)
    state = sign_state(return_to=ret, redirect_uri=redir)
    if hasattr(request, "session"):
        request.session["xero_oauth_state"] = state
        request.session["xero_oauth_redirect"] = redir
    try:
        url = build_authorise_url(state=state, redirect_uri=redir)
    except RuntimeError as exc:
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote(str(exc)[:300])}",
            status_code=303,
        )
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


@router.get("/oauth/xero/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    if error:
        msg = error_description or error
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote(msg[:300])}",
            status_code=303,
        )
    ok, payload, serr = parse_state(state or "")
    ret = "/settings/xero"
    redir = ""
    if ok:
        ret = (payload.get("ret") or ret) or ret
        redir = (payload.get("redir") or "").strip()
    if not ok:
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote(serr or 'Invalid state')}",
            status_code=303,
        )
    if not (code or "").strip():
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote('No authorisation code returned.')}",
            status_code=303,
        )
    if not redir:
        try:
            redir = (request.session.get("xero_oauth_redirect") or "").strip()
        except Exception:
            redir = ""
    if not redir:
        redir = resolve_redirect_uri(request)

    result = exchange_code(code.strip(), redirect_uri=redir)
    if not result.ok:
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote(result.error[:300])}",
            status_code=303,
        )
    email, uid = fetch_userinfo(result.access_token)
    tenants, _ = fetch_connections(result.access_token)
    save_token(db, result, xero_email=email, xero_user_id=uid, tenants=tenants)
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=connected", status_code=303)


@router.post("/oauth/xero/disconnect")
async def oauth_disconnect(
    token_id: str = Form(""),
    return_to: str = Form("/settings/xero"),
    db: Session = Depends(get_db),
):
    tid = int(token_id) if (token_id or "").isdigit() else None
    revoke_local(db, tid)
    ret = (return_to or "").strip() or "/settings/xero"
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=disconnected", status_code=303)


@router.post("/settings/xero/refresh-orgs")
async def settings_refresh_orgs(db: Session = Depends(get_db)):
    tenants, err = refresh_tenants(db)
    if err and not tenants:
        return RedirectResponse(
            f"/settings/xero?oauth_error={url_quote(err[:300])}",
            status_code=303,
        )
    return RedirectResponse("/settings/xero?oauth_msg=orgs", status_code=303)


@router.get("/settings/xero", response_class=HTMLResponse)
async def settings_xero(request: Request, db: Session = Depends(get_db)):
    refresh_xero_settings(force_dotenv=True)
    status = connection_status(db)
    configured = xero_configured(refresh=True)
    effective = resolve_redirect_uri(request)
    env_redir = (app_config.XERO_REDIRECT_URI or default_redirect_uri() or "").strip()
    return render(
        request,
        "settings_xero.html",
        {
            "status": status,
            "configured": configured,
            "client_mask": mask_client_id(app_config.XERO_CLIENT_ID),
            "secret_set": bool((app_config.XERO_CLIENT_SECRET or "").strip()),
            "redirect_uri": effective,
            "redirect_uri_env": env_redir,
            "scopes": (app_config.XERO_SCOPES or "").strip(),
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
        },
    )


def _client_or_redirect(db: Session, client_id: int):
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        return None, RedirectResponse("/clients", status_code=303)
    return client, None


@router.post("/clients/{client_id:int}/xero/assign")
async def assign_xero_org(
    client_id: int,
    tenant_id: str = Form(""),
    db: Session = Depends(get_db),
):
    client, resp = _client_or_redirect(db, client_id)
    if resp:
        return resp
    from app.services.xero_oauth import connection_status as xstatus

    name = ""
    for t in xstatus(db).get("tenants") or []:
        if str(t.get("tenantId") or "") == (tenant_id or "").strip():
            name = str(t.get("tenantName") or "")
            break
    assign_tenant(db, client, (tenant_id or "").strip(), name)
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=playbook",
        status_code=303,
    )


@router.post("/clients/{client_id:int}/xero/pull")
async def pull_xero_books(
    client_id: int,
    as_at: str = Form(""),
    db: Session = Depends(get_db),
):
    client, resp = _client_or_redirect(db, client_id)
    if resp:
        return resp
    when = None
    raw = (as_at or "").strip()
    if raw:
        try:
            when = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            when = None
    res = pull_client_books(db, client, as_at=when)
    if not res.get("ok"):
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
            + url_quote((res.get("error") or "Xero pull failed.")[:400]),
            status_code=303,
        )
    n = res.get("counts") or {}
    msg = (
        f"Pulled Xero to Current/Source as at {res.get('as_at')}. "
        f"TB {n.get('trial_balance', 0)} rows, "
        f"bank {n.get('bank_transactions', 0)}, "
        f"manuals {n.get('manual_journals', 0)}."
    )
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg={url_quote(msg[:400])}",
        status_code=303,
    )


@router.post("/clients/{client_id:int}/xero/journals/upload")
async def upload_journal_csv(
    client_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    client, resp = _client_or_redirect(db, client_id)
    if resp:
        return resp
    raw = await file.read()
    if not raw:
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
            + url_quote("Empty file."),
            status_code=303,
        )
    name, err = save_uploaded_draft(db, client, file.filename or "journal.csv", raw)
    if err:
        return RedirectResponse(
            f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
            + url_quote(err[:400]),
            status_code=303,
        )
    return RedirectResponse(
        f"/clients/{client_id}/xero/journals/{url_quote(name)}",
        status_code=303,
    )


@router.get("/clients/{client_id:int}/xero/journals/{filename}", response_class=HTMLResponse)
async def review_journal(
    client_id: int,
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
):
    client, resp = _client_or_redirect(db, client_id)
    if resp:
        return resp
    path, journals, err = load_journal_draft(db, client, filename)
    return render(
        request,
        "xero/journal_review.html",
        {
            "client": client,
            "filename": path.name or filename,
            "journals": journals,
            "error": err,
            "all_balanced": bool(journals) and all(j.get("Balanced") for j in journals),
        },
    )


@router.post("/clients/{client_id:int}/xero/journals/{filename}/post")
async def confirm_post_journal(
    client_id: int,
    filename: str,
    status: str = Form("DRAFT"),
    confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    client, resp = _client_or_redirect(db, client_id)
    if resp:
        return resp
    if (confirm or "").strip().lower() not in ("yes", "1", "on", "true"):
        return RedirectResponse(
            f"/clients/{client_id}/xero/journals/{url_quote(filename)}"
            f"?err={url_quote('Tick confirm before posting to Xero.')}",
            status_code=303,
        )
    res = post_journal_draft(db, client, filename, status=status)
    if not res.get("ok"):
        return RedirectResponse(
            f"/clients/{client_id}/xero/journals/{url_quote(filename)}"
            f"?err={url_quote((res.get('error') or 'Post failed')[:400])}",
            status_code=303,
        )
    n = len(res.get("posted") or [])
    want = (status or "DRAFT").upper()
    return RedirectResponse(
        f"/clients/{client_id}?tab=playbook&saved=pack&pack_msg="
        + url_quote(f"Posted {n} journal(s) to Xero as {want}."),
        status_code=303,
    )
