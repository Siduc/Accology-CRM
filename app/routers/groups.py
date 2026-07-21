"""Relationship groups — list and drill-down."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.grouping import (
    DEFAULT_GROUP_STATUSES,
    get_group_detail,
    list_group_summaries,
)
from app.templating import render

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_class=HTMLResponse)
async def groups_list(
    request: Request,
    db: Session = Depends(get_db),
    q: str = Query(""),
):
    """List of groups — name = highest-fee client in each group."""
    summaries = list_group_summaries(
        db,
        statuses=DEFAULT_GROUP_STATUSES,
        q=q or None,
    )
    return render(
        request,
        "groups/list.html",
        {
            "groups": summaries,
            "q": q or "",
            "count": len(summaries),
        },
    )


@router.get("/{group_id:int}", response_class=HTMLResponse)
async def groups_detail(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Drill into one group: constituent companies and people."""
    detail = get_group_detail(db, group_id)
    if not detail:
        return RedirectResponse("/groups", status_code=303)
    return render(
        request,
        "groups/detail.html",
        {
            "group": detail,
            "client_fees": detail.client_fees,
        },
    )
