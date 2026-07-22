"""Companies House OAuth 2.0 routes (API Filing)."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import (
    CH_OAUTH_CLIENT_ID,
    CH_OAUTH_CLIENT_SECRET,
    CH_OAUTH_REDIRECT_URI,
    ch_oauth_configured,
)
from app.database import get_db
from app.models import Client
from app.services.ch_oauth import (
    build_authorise_url,
    build_scopes,
    default_redirect_uri,
    diagnose_stu_from_events,
    exchange_code,
    get_recent_events,
    is_loopback_redirect,
    list_active_tokens,
    latest_practice_token,
    log_event,
    mask_client_id,
    mask_secret,
    new_attempt_id,
    oauth_is_ready,
    parse_state,
    probe_oauth_configuration,
    redirect_uri_warning,
    revoke_local,
    sanitize_authorise_url,
    save_token,
    sign_state,
)
from app.services.company_numbers import normalize_company_number
from app.templating import render

router = APIRouter(tags=["companies-house-oauth"])


@router.get("/oauth/companies-house/start")
async def oauth_start(
    request: Request,
    client_id: str = "",
    pack_id: str = "",
    return_to: str = "",
    force: str = "",
    db: Session = Depends(get_db),
):
    attempt_id = new_attempt_id()
    if not oauth_is_ready():
        log_event(
            "start_blocked",
            attempt_id=attempt_id,
            reason="not_configured",
        )
        return RedirectResponse(
            "/settings?oauth_error="
            + url_quote(
                "Set CH_OAUTH_CLIENT_ID and CH_OAUTH_CLIENT_SECRET in .env and restart."
            ),
            status_code=303,
        )

    crm_client_id = int(client_id) if (client_id or "").isdigit() else None
    pid = int(pack_id) if (pack_id or "").isdigit() else None
    company_number = ""
    if crm_client_id:
        client = db.query(Client).filter(Client.id == crm_client_id).first()
        if client:
            company_number = normalize_company_number(client.company_number or "") or ""

    ret = (return_to or "").strip()
    if not ret:
        if pid:
            ret = f"/cs/{pid}"
        elif crm_client_id:
            ret = f"/clients/{crm_client_id}"
        else:
            ret = "/settings"

    log_event(
        "start",
        attempt_id=attempt_id,
        client_id=mask_client_id(),
        redirect_uri=default_redirect_uri(),
        loopback=is_loopback_redirect(),
        crm_client_id=crm_client_id,
        pack_id=pid,
        company_number=company_number or "",
        return_to=ret,
        scopes=build_scopes(company_number or None)[:200],
    )

    # CH edge returns bare 403 for localhost / 127.0.0.1 redirect_uri — warn first
    if is_loopback_redirect() and force != "1":
        log_event("start_loopback_warning", attempt_id=attempt_id)
        return render(
            request,
            "ch_oauth_loopback_warning.html",
            {
                "redirect_uri": default_redirect_uri(),
                "warning": redirect_uri_warning(),
                "continue_url": (
                    f"/oauth/companies-house/start?force=1"
                    f"&client_id={crm_client_id or ''}"
                    f"&pack_id={pid or ''}"
                    f"&return_to={url_quote(ret)}"
                ),
                "return_to": ret,
            },
        )

    state = sign_state(
        **{
            "crm_client_id": crm_client_id,
            "pack_id": pid,
            "return_to": ret,
            "company_number": company_number,
            "attempt_id": attempt_id,
        }
    )
    if hasattr(request, "session"):
        request.session["ch_oauth_state"] = state
        request.session["ch_oauth_attempt_id"] = attempt_id

    try:
        url = build_authorise_url(
            state=state, company_number=company_number or None
        )
    except RuntimeError as exc:
        log_event(
            "start_error",
            attempt_id=attempt_id,
            error=str(exc),
        )
        return RedirectResponse(
            f"/settings?oauth_error={url_quote(str(exc))}", status_code=303
        )

    safe = sanitize_authorise_url(url)
    log_event(
        "redirect_to_ch",
        attempt_id=attempt_id,
        authorise_url_base=safe.get("authorise_url_base"),
        redirect_uri=safe.get("authorise_redirect_uri") or default_redirect_uri(),
        authorise_scope=safe.get("authorise_scope"),
        authorise_client_id=safe.get("authorise_client_id"),
        authorise_state_len=safe.get("authorise_state_len"),
        authorise_url_debug=safe.get("authorise_url_debug"),
        note=(
            "Browser leaves Accologise for CH. If you see “service temporarily unavailable” "
            "and no callback_* event appears next, failure is on CH / One Login or the tunnel."
        ),
    )
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/companies-house/callback")
async def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
):
    fail_to = "/settings"
    # Prefer attempt id from signed state once parsed; provisional from session
    attempt_id = ""
    if hasattr(request, "session"):
        attempt_id = str(request.session.get("ch_oauth_attempt_id") or "")

    # Parse state early so attempt_id correlates with start logs
    ok, payload, err = parse_state(state or "")
    if ok and payload.get("aid"):
        attempt_id = str(payload.get("aid") or attempt_id)

    # Safe callback URL for logs (mask code/state values)
    qparts = []
    for k in sorted(request.query_params.keys()):
        v = request.query_params.get(k) or ""
        if k in ("code", "state") and v:
            qparts.append(f"{k}={mask_secret(v) if k == 'code' else f'[redacted len={len(v)}]'}")
        else:
            qparts.append(f"{k}={v}")
    callback_url_debug = str(request.url.path) + (
        ("?" + "&".join(qparts)) if qparts else ""
    )

    log_event(
        "callback_received",
        attempt_id=attempt_id,
        has_code=bool((code or "").strip()),
        code_masked=mask_secret(code) if code else "",
        has_state=bool((state or "").strip()),
        state_len=len(state or ""),
        error=error or "",
        error_description=error_description or "",
        query_keys=",".join(sorted(request.query_params.keys())),
        callback_url_debug=callback_url_debug,
        path=str(request.url.path),
        host=request.headers.get("host") or "",
        x_forwarded_host=request.headers.get("x-forwarded-host") or "",
        state_parse_ok=ok,
        state_parse_error="" if ok else (err or ""),
    )

    if error:
        msg = error_description or error or "Authorisation denied"
        log_event(
            "callback_ch_error",
            attempt_id=attempt_id,
            error=error,
            error_description=error_description,
            callback_url_debug=callback_url_debug,
        )
        return RedirectResponse(
            f"{fail_to}?oauth_error={url_quote(msg)}", status_code=303
        )

    if not ok:
        sess_state = ""
        if hasattr(request, "session"):
            sess_state = request.session.get("ch_oauth_state") or ""
        if not state or state != sess_state:
            log_event(
                "callback_state_error",
                attempt_id=attempt_id,
                error=err or "Invalid OAuth state",
                callback_url_debug=callback_url_debug,
            )
            return RedirectResponse(
                f"{fail_to}?oauth_error={url_quote(err or 'Invalid OAuth state')}",
                status_code=303,
            )
        payload = {}
        log_event(
            "callback_state_session_fallback",
            attempt_id=attempt_id,
        )

    ret = (payload.get("ret") or fail_to) if payload else fail_to
    crm_client_id = payload.get("cid") if payload else None
    pack_id = payload.get("pid") if payload else None
    company_number = (payload.get("cn") or "") if payload else ""

    if not (code or "").strip():
        log_event(
            "callback_missing_code",
            attempt_id=attempt_id,
        )
        return RedirectResponse(
            f"{ret}?oauth_error={url_quote('Missing authorisation code')}",
            status_code=303,
        )

    result = exchange_code(
        code, redirect_uri=default_redirect_uri(), attempt_id=attempt_id
    )
    if not result.ok:
        detail = result.error or "Token exchange failed"
        if result.response_body:
            detail = f"{detail} | CH body: {result.response_body[:400]}"
        log_event(
            "callback_token_fail",
            attempt_id=attempt_id,
            http_status=result.http_status,
            error=result.error,
            body=result.response_body[:800] if result.response_body else "",
        )
        return RedirectResponse(
            f"{ret}?oauth_error={url_quote(detail[:500])}", status_code=303
        )

    try:
        cid = int(crm_client_id) if crm_client_id is not None else None
    except (TypeError, ValueError):
        cid = None

    row = save_token(
        db,
        result,
        crm_client_id=cid,
        company_number=company_number or None,
        scope_requested=build_scopes(company_number or None),
    )

    if pack_id:
        try:
            from app.models.cs_pack import CsPack

            pack = db.query(CsPack).filter(CsPack.id == int(pack_id)).first()
            if pack:
                pack.oauth_token_id = row.id
                db.commit()
        except Exception as exc:  # noqa: BLE001
            log_event(
                "callback_pack_link_error",
                attempt_id=attempt_id,
                error=str(exc),
            )

    if hasattr(request, "session"):
        request.session.pop("ch_oauth_state", None)
        request.session.pop("ch_oauth_attempt_id", None)

    log_event(
        "callback_connected",
        attempt_id=attempt_id,
        token_row_id=row.id,
        crm_client_id=cid,
        company_number=company_number or "",
        expires_at=str(row.expires_at or ""),
        ch_user_email=row.ch_user_email or "",
    )

    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=connected", status_code=303)


@router.post("/oauth/companies-house/disconnect")
async def oauth_disconnect(
    request: Request,
    token_id: str = Form(""),
    client_id: str = Form(""),
    return_to: str = Form("/settings"),
    db: Session = Depends(get_db),
):
    tid = int(token_id) if (token_id or "").isdigit() else None
    cid = int(client_id) if (client_id or "").isdigit() else None
    n = 0
    if tid:
        n = revoke_local(db, tid)
    elif cid is not None:
        n = revoke_local(db, crm_client_id=cid)
    log_event(
        "disconnect",
        token_id=tid,
        crm_client_id=cid,
        revoked=n,
    )
    ret = (return_to or "/settings").strip() or "/settings"
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=disconnected", status_code=303)


@router.post("/settings/companies-house-oauth/probe")
async def oauth_probe(request: Request):
    probe = probe_oauth_configuration()
    # Store last probe summary in session for GET page
    if hasattr(request, "session"):
        rh = probe.get("redirect_health") or {}
        tp = probe.get("token_ping") or {}
        au = probe.get("authorise") or {}
        request.session["ch_oauth_last_probe"] = {
            "attempt_id": probe.get("attempt_id"),
            "authorise_status": au.get("http_status"),
            "health_status": rh.get("http_status"),
            "health_error": rh.get("error") or "",
            "token_status": tp.get("http_status"),
            "interpretation": probe.get("interpretation") or [],
            "authorise_body": au.get("body") or au.get("error") or "",
            "token_body": tp.get("body") or tp.get("error") or "",
        }
    return RedirectResponse(
        "/settings/companies-house-oauth?oauth_msg=probed", status_code=303
    )


@router.get("/settings/companies-house-oauth", response_class=HTMLResponse)
async def oauth_settings_page(request: Request, db: Session = Depends(get_db)):
    tokens = list_active_tokens(db, 30)
    practice = latest_practice_token(db)
    clients_by_id = {}
    ids = [t.client_id for t in tokens if t.client_id]
    if ids:
        for c in db.query(Client).filter(Client.id.in_(ids)).all():
            clients_by_id[c.id] = c
    events = get_recent_events(25)
    stu = diagnose_stu_from_events(events)
    last_probe = {}
    if hasattr(request, "session"):
        last_probe = request.session.get("ch_oauth_last_probe") or {}
    return render(
        request,
        "settings_ch_oauth.html",
        {
            "configured": ch_oauth_configured(),
            "client_id_mask": mask_client_id(CH_OAUTH_CLIENT_ID),
            "secret_set": bool((CH_OAUTH_CLIENT_SECRET or "").strip()),
            "redirect_uri": CH_OAUTH_REDIRECT_URI or "",
            "loopback_redirect": is_loopback_redirect(),
            "redirect_warning": redirect_uri_warning(),
            "tokens": tokens,
            "practice_token": practice,
            "clients_by_id": clients_by_id,
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "oauth_events": events,
            "stu_diag": stu,
            "last_probe": last_probe,
        },
    )
