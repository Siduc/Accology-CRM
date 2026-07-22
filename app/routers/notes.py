"""Scrapbook / Post-it notes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.scrap_notes import (
    NOTE_COLORS,
    create_note,
    delete_note,
    list_notes,
    update_note,
)
from app.templating import render

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("", response_class=HTMLResponse)
async def notes_page(request: Request, db: Session = Depends(get_db)):
    notes = list_notes(db)
    return render(
        request,
        "notes/scrapbook.html",
        {
            "notes": notes,
            "colors": NOTE_COLORS,
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/new", response_class=HTMLResponse)
async def notes_create(
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    color: str = Form("yellow"),
    pin_live: str = Form(""),
    db: Session = Depends(get_db),
):
    create_note(
        db,
        title=title,
        body=body,
        color=color,
        pin_live=pin_live == "yes",
    )
    return RedirectResponse("/notes?msg=created", status_code=303)


@router.post("/{note_id:int}/edit", response_class=HTMLResponse)
async def notes_edit(
    note_id: int,
    request: Request,
    title: str = Form(""),
    body: str = Form(""),
    color: str = Form("yellow"),
    pin_live: str = Form(""),
    db: Session = Depends(get_db),
):
    update_note(
        db,
        note_id,
        title=title,
        body=body,
        color=color,
        pin_live=pin_live == "yes",
    )
    return RedirectResponse("/notes?msg=saved", status_code=303)


@router.post("/{note_id:int}/delete", response_class=HTMLResponse)
async def notes_delete(
    note_id: int, request: Request, db: Session = Depends(get_db)
):
    delete_note(db, note_id)
    return RedirectResponse("/notes?msg=deleted", status_code=303)
