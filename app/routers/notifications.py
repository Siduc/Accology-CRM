"""Staff notification centre routes."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import notifications as notify_svc
from app.templating import render

router = APIRouter(tags=["notifications"])


def _safe_return(path: str) -> str:
    p = (path or "").strip() or "/dashboard"
    if not p.startswith("/"):
        return "/dashboard"
    # Block protocol-relative / external
    if p.startswith("//") or "://" in p:
        return "/dashboard"
    return p


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_list(
    request: Request,
    unread: str = "",
    db: Session = Depends(get_db),
):
    unread_only = unread in ("1", "true", "yes", "on")
    rows = notify_svc.list_recent(db, limit=50, unread_only=unread_only)
    return render(
        request,
        "notifications/list.html",
        {
            "notifications": rows,
            "unread_only": unread_only,
            "unread_count": notify_svc.unread_count(db),
        },
    )


@router.post("/notifications/{notification_id:int}/read")
async def notification_mark_read(
    request: Request,
    notification_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    n = notify_svc.mark_read(db, notification_id)
    dest = _safe_return(return_to)
    if n and n.link and not return_to:
        dest = n.link
    return RedirectResponse(dest, status_code=303)


@router.post("/notifications/read-all")
async def notifications_mark_all_read(
    request: Request,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    notify_svc.mark_all_read(db)
    return RedirectResponse(_safe_return(return_to) or "/notifications", status_code=303)


@router.get("/notifications/{notification_id:int}/open")
async def notification_open(
    request: Request,
    notification_id: int,
    db: Session = Depends(get_db),
):
    """Mark read and go to the linked prospect / page."""
    n = notify_svc.mark_read(db, notification_id)
    if n and n.link:
        return RedirectResponse(n.link, status_code=303)
    return RedirectResponse("/notifications", status_code=303)
