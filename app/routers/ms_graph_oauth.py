"""Microsoft Graph OAuth routes (OneDrive)."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import (
    MS_GRAPH_CLIENT_ID,
    MS_GRAPH_CLIENT_SECRET,
    MS_GRAPH_REDIRECT_URI,
    ms_graph_configured,
)
from app.database import get_db
from app.services.ms_graph_oauth import (
    build_authorise_url,
    connection_status,
    default_redirect_uri,
    exchange_code,
    fetch_drive,
    fetch_me,
    get_valid_access_token,
    mask_client_id,
    oauth_is_ready,
    parse_state,
    probe_connection,
    revoke_local,
    save_token,
    sign_state,
)
from app.templating import render

router = APIRouter(tags=["microsoft-graph-oauth"])


@router.get("/oauth/microsoft/start")
async def oauth_start(
    request: Request,
    return_to: str = "",
    db: Session = Depends(get_db),
):
    if not oauth_is_ready():
        return RedirectResponse(
            "/settings?oauth_error="
            + url_quote(
                "Set MS_GRAPH_CLIENT_ID and MS_GRAPH_CLIENT_SECRET in .env and restart."
            ),
            status_code=303,
        )
    ret = (return_to or "").strip() or "/settings/microsoft-graph"
    state = sign_state(return_to=ret)
    if hasattr(request, "session"):
        request.session["ms_oauth_state"] = state
    try:
        url = build_authorise_url(state=state)
    except RuntimeError as exc:
        return RedirectResponse(
            f"/settings?oauth_error={url_quote(str(exc))}", status_code=303
        )
    return RedirectResponse(url, status_code=303)


@router.get("/oauth/microsoft/callback")
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
            f"/settings?oauth_error={url_quote(msg[:300])}", status_code=303
        )
    ok, payload, serr = parse_state(state or "")
    ret = "/settings/microsoft-graph"
    if ok:
        ret = (payload.get("ret") or ret) or ret
    if not ok:
        return RedirectResponse(
            f"/settings?oauth_error={url_quote(serr or 'Invalid state')}",
            status_code=303,
        )
    if not (code or "").strip():
        return RedirectResponse(
            f"/settings?oauth_error={url_quote('No authorisation code returned.')}",
            status_code=303,
        )

    result = exchange_code(code.strip())
    if not result.ok:
        return RedirectResponse(
            f"/settings?oauth_error={url_quote(result.error[:300])}",
            status_code=303,
        )

    email, uid, drive_id = "", "", ""
    ok_me, email, uid, _ = fetch_me(result.access_token)
    if ok_me:
        ok_d, drive_id, _ = fetch_drive(result.access_token)
        _ = ok_d

    save_token(
        db,
        result,
        ms_user_email=email,
        ms_user_id=uid,
        drive_id=drive_id,
    )
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=connected", status_code=303)


@router.post("/oauth/microsoft/disconnect")
async def oauth_disconnect(
    token_id: str = Form(""),
    return_to: str = Form("/settings/microsoft-graph"),
    db: Session = Depends(get_db),
):
    tid = int(token_id) if (token_id or "").isdigit() else None
    revoke_local(db, tid)
    ret = (return_to or "").strip() or "/settings/microsoft-graph"
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}oauth_msg=disconnected", status_code=303)


@router.get("/settings/microsoft-graph", response_class=HTMLResponse)
async def settings_ms_graph(request: Request, db: Session = Depends(get_db)):
    status = connection_status(db)
    probe = None
    if request.query_params.get("probe") == "1" and status.get("connected"):
        probe = probe_connection(db)
        status = connection_status(db)
    return render(
        request,
        "settings_ms_graph.html",
        {
            "status": status,
            "probe": probe,
            "configured": ms_graph_configured(),
            "client_mask": mask_client_id(MS_GRAPH_CLIENT_ID),
            "secret_set": bool((MS_GRAPH_CLIENT_SECRET or "").strip()),
            "redirect_uri": MS_GRAPH_REDIRECT_URI or default_redirect_uri(),
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
        },
    )


@router.post("/settings/microsoft-graph/probe")
async def settings_ms_probe(db: Session = Depends(get_db)):
    token, err = get_valid_access_token(db)
    if not token:
        return RedirectResponse(
            f"/settings/microsoft-graph?oauth_error={url_quote(err or 'Not connected')}",
            status_code=303,
        )
    result = probe_connection(db)
    if not result.get("ok"):
        return RedirectResponse(
            f"/settings/microsoft-graph?oauth_error={url_quote(result.get('error') or 'Probe failed')}",
            status_code=303,
        )
    return RedirectResponse(
        "/settings/microsoft-graph?oauth_msg=connected&probe=1", status_code=303
    )
