"""Practice groups board — membership, rename, group connections."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.client_connections import CONNECTION_PROVIDERS
from app.services.practice_groups import (
    apply_group_connection,
    create_group,
    delete_group,
    get_group_detail,
    list_board,
    move_client,
    rename_group,
)
from app.templating import render

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_class=HTMLResponse)
async def groups_board(
    request: Request,
    db: Session = Depends(get_db),
    msg: str = Query(""),
    q: str = Query(""),
):
    board, ungrouped = list_board(db, q=q or None)
    return render(
        request,
        "groups/list.html",
        {
            "board": board,
            "ungrouped": ungrouped,
            "count": len(board),
            "msg": msg,
            "q": q or "",
            "providers": CONNECTION_PROVIDERS,
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def groups_new(
    request: Request,
    name: str = Form("New group"),
    db: Session = Depends(get_db),
):
    g = create_group(db, name=name)
    return RedirectResponse(f"/groups/{g.id}", status_code=303)


@router.post("/move")
async def groups_move(
    request: Request,
    client_id: int = Form(...),
    group_id: str = Form(""),
    db: Session = Depends(get_db),
):
    gid = int(group_id) if (group_id or "").strip().isdigit() else None
    ok = move_client(db, client_id, gid)
    # JSON for DnD fetch; also accept form redirect
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept or request.headers.get("x-requested-with") == "fetch":
        return JSONResponse({"ok": ok})
    return RedirectResponse("/groups", status_code=303)


@router.post("/{group_id:int}/rename", response_class=HTMLResponse)
async def groups_rename(
    group_id: int,
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    g = rename_group(db, group_id, name)
    if not g:
        return RedirectResponse("/groups", status_code=303)
    next_url = request.query_params.get("next") or f"/groups/{group_id}"
    return RedirectResponse(next_url, status_code=303)


@router.post("/{group_id:int}/delete", response_class=HTMLResponse)
async def groups_delete(
    group_id: int, request: Request, db: Session = Depends(get_db)
):
    delete_group(db, group_id)
    return RedirectResponse("/groups?msg=deleted", status_code=303)


@router.post("/{group_id:int}/connections", response_class=HTMLResponse)
async def groups_connections(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    form = await request.form()
    for code, _l, _d in CONNECTION_PROVIDERS:
        enabled = form.get(f"conn_{code}") == "yes"
        apply_group_connection(db, group_id, code, enabled)
    return RedirectResponse(
        f"/groups/{group_id}?msg=connections", status_code=303
    )


@router.get("/{group_id:int}", response_class=HTMLResponse)
async def groups_detail(
    group_id: int,
    request: Request,
    msg: str = Query(""),
    db: Session = Depends(get_db),
):
    detail = get_group_detail(db, group_id)
    if not detail:
        return RedirectResponse("/groups", status_code=303)
    board, ungrouped = list_board(db)
    return render(
        request,
        "groups/detail.html",
        {
            "bg": detail,
            "group": detail.group,
            "members": detail.members,
            "conn_summary": detail.conn_summary,
            "providers": CONNECTION_PROVIDERS,
            "all_groups": board,
            "ungrouped": ungrouped,
            "msg": msg,
        },
    )
