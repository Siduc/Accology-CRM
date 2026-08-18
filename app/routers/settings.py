"""User settings (client-side preferences for now)."""

from datetime import datetime
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import (
    ASANA_ACCESS_TOKEN,
    ASANA_ENABLED,
    ASANA_PROJECT_GID,
    ASANA_WORKSPACE_GID,
    CH_OAUTH_CLIENT_ID,
    CH_OAUTH_CLIENT_SECRET,
    CH_OAUTH_REDIRECT_URI,
    CH_XML_GATEWAY_TEST,
    CH_XML_GATEWAY_URL,
    CH_XML_PACKAGE_REFERENCE,
    CH_XML_PRESENTER_AUTH,
    CH_XML_SUBMIT_LIVE,
    CHASE_LIVE_MODE,
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    PRACTICE_PHONE,
    SMTP_FROM,
    SMTP_HOST,
    ch_oauth_configured,
    ch_xml_gateway_configured,
    ms_graph_configured,
    refresh_ms_graph_settings,
)
import app.config as app_config
from app.database import get_db
from app.models.dev_backlog import DevBacklogItem
from app.services.chase_emails import smtp_configured
from app.services.ch_oauth import (
    diagnose_stu_from_events,
    is_loopback_redirect,
    latest_oauth_summary,
    list_active_tokens,
    mask_client_id,
    redirect_uri_warning,
)
from app.services.ch_xml_gateway import gateway_url as ch_xml_gateway_url_fn
from app.services.ch_xml_gateway import presenter_id_masked as ch_xml_presenter_mask_fn
from app.services.dev_backlog import list_backlog, seed_system_backlog
from app.services.ms_graph_oauth import (
    connection_status as ms_connection_status,
    mask_client_id as ms_mask_client_id,
    resolve_redirect_uri as ms_resolve_redirect_uri,
)
from app.templating import render

router = APIRouter(tags=["settings"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    oauth_tokens = []
    try:
        oauth_tokens = list_active_tokens(db, 8)
    except Exception:
        oauth_tokens = []
    try:
        seed_system_backlog(db)
        backlog = list_backlog(db)
    except Exception:
        backlog = []
    oauth_last = latest_oauth_summary()
    oauth_stu = diagnose_stu_from_events()
    # Always re-read MS_GRAPH_CLIENT_ID / SECRET / REDIRECT_URI from .env
    ms_snap = refresh_ms_graph_settings(force_dotenv=True)
    try:
        ms_status = ms_connection_status(db)
    except Exception:
        ms_status = {
            "configured": bool(ms_snap.get("configured")),
            "connected": False,
            "fresh": False,
            "email": "",
        }
    from app.config import (
        AI_ASSISTANT_ENABLED,
        AI_ASSISTANT_HEURISTIC,
        AI_MODEL,
        DEMO_AUTH_PASSWORD,
        DEMO_AUTH_USERNAME,
        TASK_PUSH_API_KEY,
        XAI_API_KEY,
    )
    from app.services.demo_mode import is_demo_locked, is_demo_request
    from app.services.branding import branding_status
    from app.services.client_playbook import practice_files_root as practice_files_root_fn
    from app.services.xero_oauth import connection_status as xero_connection_status
    from app.services.xero_oauth import mask_client_id as xero_mask_client_id
    from app.config import xero_configured as xero_is_configured, XERO_CLIENT_ID

    ms_ok = ms_graph_configured(refresh=True)
    branding = branding_status()
    try:
        practice_files_root = str(practice_files_root_fn())
    except Exception:
        practice_files_root = ""
    try:
        xero_status = xero_connection_status(db)
    except Exception:
        xero_status = {"configured": False, "connected": False, "fresh": False, "tenant_count": 0}

    return render(
        request,
        "settings.html",
        {
            "chase_live": CHASE_LIVE_MODE,
            "branding": branding,
            "branding_msg": request.query_params.get("branding_msg", ""),
            "branding_error": request.query_params.get("branding_error", ""),
            "task_push_key_set": bool((TASK_PUSH_API_KEY or "").strip()),
            "ai_key_set": bool((XAI_API_KEY or "").strip()),
            "ai_enabled": bool(AI_ASSISTANT_ENABLED),
            "ai_heuristic": bool(AI_ASSISTANT_HEURISTIC),
            "ai_model": AI_MODEL,
            "demo_locked": is_demo_locked(request),
            "demo_login_configured": bool((DEMO_AUTH_PASSWORD or "").strip()),
            "demo_login_username": (DEMO_AUTH_USERNAME or "demo"),
            "smtp_ok": smtp_configured(),
            "smtp_host": SMTP_HOST or "",
            "smtp_from": SMTP_FROM or "",
            "practice_name": PRACTICE_NAME,
            "practice_email": PRACTICE_EMAIL or "",
            "practice_phone": PRACTICE_PHONE or "",
            "asana_enabled": ASANA_ENABLED,
            "asana_token_set": bool((ASANA_ACCESS_TOKEN or "").strip()),
            "asana_workspace_set": bool((ASANA_WORKSPACE_GID or "").strip()),
            "asana_project_set": bool((ASANA_PROJECT_GID or "").strip()),
            "ch_oauth_configured": ch_oauth_configured(),
            "ch_oauth_client_mask": mask_client_id(CH_OAUTH_CLIENT_ID),
            "ch_oauth_secret_set": bool((CH_OAUTH_CLIENT_SECRET or "").strip()),
            "ch_oauth_redirect": CH_OAUTH_REDIRECT_URI or "",
            "ch_oauth_loopback": is_loopback_redirect(),
            "ch_oauth_redirect_warning": redirect_uri_warning(),
            "ch_oauth_tokens": oauth_tokens,
            "ch_oauth_last": oauth_last,
            "ch_oauth_stu": oauth_stu,
            "ch_xml_configured": ch_xml_gateway_configured(),
            "ch_xml_presenter_mask": ch_xml_presenter_mask_fn(),
            "ch_xml_auth_set": bool((CH_XML_PRESENTER_AUTH or "").strip()),
            "ch_xml_gateway_test": bool(CH_XML_GATEWAY_TEST),
            "ch_xml_gateway_url": ch_xml_gateway_url_fn() or (CH_XML_GATEWAY_URL or ""),
            "ch_xml_package": CH_XML_PACKAGE_REFERENCE or "0000",
            "ch_xml_submit_live": bool(CH_XML_SUBMIT_LIVE),
            "ms_graph_configured": ms_ok,
            "ms_graph_client_mask": ms_mask_client_id(app_config.MS_GRAPH_CLIENT_ID),
            "ms_graph_secret_set": bool(
                (app_config.MS_GRAPH_CLIENT_SECRET or "").strip()
            ),
            "ms_graph_redirect": (
                ms_resolve_redirect_uri(request)
                or app_config.MS_GRAPH_REDIRECT_URI
                or ""
            ),
            "ms_status": ms_status,
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "backlog": backlog,
            "backlog_msg": request.query_params.get("backlog_msg", ""),
            "demo_mode_on": is_demo_request(request),
            "demo_msg": request.query_params.get("demo_msg", ""),
            "practice_files_root": practice_files_root,
            "pack_msg": request.query_params.get("pack_msg", ""),
            "xero_configured": xero_is_configured(refresh=True),
            "xero_status": xero_status,
            "xero_client_mask": xero_mask_client_id(XERO_CLIENT_ID),
        },
    )


@router.post("/settings/ensure-client-packs")
async def settings_ensure_client_packs(db: Session = Depends(get_db)):
    """Create Current packs + AGENTS.md for every live client on this machine."""
    from urllib.parse import quote as url_quote

    from app.services.client_playbook import ensure_live_client_packs

    res = ensure_live_client_packs(db, move_prior_years=True)
    msg = (
        f"Packs: {res['ok']} ok, {res['failed']} failed, "
        f"{res['moved']} papers filed. Root {res['root']}"
    )
    if res.get("errors"):
        msg += " — " + "; ".join(res["errors"][:3])
    return RedirectResponse(
        f"/settings?pack_msg={url_quote(msg[:400])}#settings-playbooks",
        status_code=303,
    )


@router.post("/settings/demo-mode")
async def settings_demo_mode(
    request: Request,
    enabled: str = Form(""),
):
    """Toggle presentation demo mode (session only — database unchanged)."""
    from app.services.demo_mode import is_demo_locked, set_demo_mode

    if is_demo_locked(request):
        return RedirectResponse(
            "/settings?demo_msg=locked#settings-demo", status_code=303
        )
    on = (enabled or "").strip().lower() in ("1", "yes", "on", "true")
    set_demo_mode(request, on)
    msg = "on" if on else "off"
    return RedirectResponse(f"/settings?demo_msg={msg}#settings-demo", status_code=303)


@router.post("/settings/branding/upload")
async def settings_branding_upload(
    request: Request,
    kind: str = Form(...),
    file: UploadFile = File(...),
):
    """Upload practice logo or letterhead for invoices / letter templates."""
    from app.services.demo_mode import is_demo_locked
    from app.services.branding import save_upload

    if is_demo_locked(request):
        return RedirectResponse(
            "/settings?branding_error="
            + url_quote("Demo login cannot change branding")
            + "#settings-branding",
            status_code=303,
        )
    ok, msg = await save_upload(file, kind=kind)
    if ok:
        return RedirectResponse(
            f"/settings?branding_msg={url_quote(msg)}#settings-branding",
            status_code=303,
        )
    return RedirectResponse(
        f"/settings?branding_error={url_quote(msg)}#settings-branding",
        status_code=303,
    )


@router.post("/settings/branding/remove")
async def settings_branding_remove(
    request: Request,
    kind: str = Form(...),
):
    from app.services.demo_mode import is_demo_locked
    from app.services.branding import delete_asset

    if is_demo_locked(request):
        return RedirectResponse(
            "/settings?branding_error="
            + url_quote("Demo login cannot change branding")
            + "#settings-branding",
            status_code=303,
        )
    ok, msg = delete_asset(kind)
    key = "branding_msg" if ok else "branding_error"
    return RedirectResponse(
        f"/settings?{key}={url_quote(msg)}#settings-branding",
        status_code=303,
    )


@router.get("/demo/on")
async def demo_on(request: Request):
    from app.services.demo_mode import is_demo_locked, set_demo_mode

    if is_demo_locked(request):
        return RedirectResponse("/dashboard", status_code=303)
    set_demo_mode(request, True)
    return RedirectResponse("/dashboard", status_code=303)


@router.get("/demo/off")
async def demo_off(request: Request):
    from app.services.demo_mode import is_demo_locked, set_demo_mode

    # Demo-only visitors cannot switch to live data
    if is_demo_locked(request):
        return RedirectResponse("/dashboard?demo_msg=locked", status_code=303)
    set_demo_mode(request, False)
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/settings/backlog/add")
async def backlog_add(
    title: str = Form(...),
    detail: str = Form(""),
    area: str = Form(""),
    db: Session = Depends(get_db),
):
    t = (title or "").strip()
    if t:
        db.add(
            DevBacklogItem(
                title=t[:240],
                detail=(detail or "").strip() or None,
                area=(area or "").strip() or "User",
                status="planned",
                source="user",
                sort_order=200,
            )
        )
        db.commit()
    return RedirectResponse("/settings?backlog_msg=added#dev-backlog", status_code=303)


@router.post("/settings/backlog/{item_id:int}/status")
async def backlog_status(
    item_id: int,
    status: str = Form("planned"),
    db: Session = Depends(get_db),
):
    item = db.query(DevBacklogItem).filter(DevBacklogItem.id == item_id).first()
    if item and status in ("planned", "started", "paused", "done"):
        item.status = status
        item.updated_at = datetime.utcnow()
        if status == "done":
            item.is_archived = False
        db.commit()
    return RedirectResponse("/settings?backlog_msg=updated#dev-backlog", status_code=303)


@router.post("/settings/backlog/{item_id:int}/archive")
async def backlog_archive(item_id: int, db: Session = Depends(get_db)):
    item = db.query(DevBacklogItem).filter(DevBacklogItem.id == item_id).first()
    if item:
        item.is_archived = True
        item.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse("/settings?backlog_msg=archived#dev-backlog", status_code=303)
