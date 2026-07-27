"""User settings (client-side preferences for now)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
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
    CHASE_LIVE_MODE,
    MS_GRAPH_CLIENT_ID,
    MS_GRAPH_CLIENT_SECRET,
    MS_GRAPH_REDIRECT_URI,
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    PRACTICE_PHONE,
    SMTP_FROM,
    SMTP_HOST,
    ch_oauth_configured,
    ms_graph_configured,
)
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
from app.services.dev_backlog import list_backlog, seed_system_backlog
from app.services.ms_graph_oauth import (
    connection_status as ms_connection_status,
    mask_client_id as ms_mask_client_id,
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
    try:
        ms_status = ms_connection_status(db)
    except Exception:
        ms_status = {
            "configured": ms_graph_configured(),
            "connected": False,
            "fresh": False,
            "email": "",
        }
    return render(
        request,
        "settings.html",
        {
            "chase_live": CHASE_LIVE_MODE,
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
            "ms_graph_configured": ms_graph_configured(),
            "ms_graph_client_mask": ms_mask_client_id(MS_GRAPH_CLIENT_ID),
            "ms_graph_secret_set": bool((MS_GRAPH_CLIENT_SECRET or "").strip()),
            "ms_graph_redirect": MS_GRAPH_REDIRECT_URI or "",
            "ms_status": ms_status,
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
            "backlog": backlog,
            "backlog_msg": request.query_params.get("backlog_msg", ""),
        },
    )


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
