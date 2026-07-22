"""User settings (client-side preferences for now)."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
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
    PRACTICE_EMAIL,
    PRACTICE_NAME,
    PRACTICE_PHONE,
    SMTP_FROM,
    SMTP_HOST,
    ch_oauth_configured,
)
from app.database import get_db
from app.services.chase_emails import smtp_configured
from app.services.ch_oauth import (
    diagnose_stu_from_events,
    is_loopback_redirect,
    latest_oauth_summary,
    list_active_tokens,
    mask_client_id,
    redirect_uri_warning,
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
    oauth_last = latest_oauth_summary()
    oauth_stu = diagnose_stu_from_events()
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
            "oauth_error": request.query_params.get("oauth_error", ""),
            "oauth_msg": request.query_params.get("oauth_msg", ""),
        },
    )
