"""Public signed approval page + staff mint link."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Job
from app.services.client_approval import apply_decision, mark_sent, mint_token, read_token
from app.templating import render

router = APIRouter(tags=["client-approval"])


def _job(db: Session, job_id: int) -> Job | None:
    return (
        db.query(Job)
        .options(joinedload(Job.client))
        .filter(Job.id == job_id)
        .first()
    )


@router.get("/jobs/{job_id:int}/approval-link", response_class=HTMLResponse)
async def staff_approval_link(job_id: int, request: Request, db: Session = Depends(get_db)):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    job = _job(db, job_id)
    if not job:
        return RedirectResponse("/jobs", status_code=303)
    token = mint_token(job.id)
    mark_sent(db, job)
    url = str(request.base_url).rstrip("/") + "/approve/" + token
    return render(
        request,
        "approval/staff_link.html",
        {"job": job, "approval_url": url},
    )


@router.get("/approve/{token}", response_class=HTMLResponse)
async def approval_page(token: str, request: Request, db: Session = Depends(get_db)):
    job_id = read_token(token)
    if not job_id:
        return render(request, "approval/public.html", {"error": "expired", "job": None}, status_code=400)
    job = _job(db, job_id)
    if not job:
        return render(request, "approval/public.html", {"error": "missing", "job": None}, status_code=404)
    return render(
        request,
        "approval/public.html",
        {"job": job, "token": token, "error": None},
    )


@router.post("/approve/{token}")
async def approval_submit(
    token: str,
    request: Request,
    decision: str = Form(...),
    actor: str = Form(""),
    db: Session = Depends(get_db),
):
    job_id = read_token(token)
    if not job_id:
        return render(request, "approval/public.html", {"error": "expired", "job": None}, status_code=400)
    job = _job(db, job_id)
    if not job:
        return render(request, "approval/public.html", {"error": "missing", "job": None}, status_code=404)
    try:
        apply_decision(db, job, decision=decision, actor=actor)
    except ValueError:
        return render(
            request,
            "approval/public.html",
            {"job": job, "token": token, "error": "bad_decision"},
            status_code=400,
        )
    return render(
        request,
        "approval/public.html",
        {"job": job, "token": token, "error": None, "done": True},
    )
