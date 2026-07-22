"""Asana integration UI — push jobs, pull my tasks, basic status sync."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import (
    ASANA_ACCESS_TOKEN,
    ASANA_ENABLED,
    ASANA_PROJECT_GID,
    ASANA_WORKSPACE_GID,
)
from app.database import get_db
from app.models import Job
from app.services import asana_client
from app.services.asana_jobs import (
    fetch_my_tasks_views,
    jobs_for_asana_push,
    pull_linked_batch,
    pull_status_for_job,
    push_job,
    push_overdue_batch,
)
from app.templating import render

router = APIRouter(prefix="/asana", tags=["asana"])


def _status_ctx():
    return {
        "asana_enabled": ASANA_ENABLED,
        "token_set": bool((ASANA_ACCESS_TOKEN or "").strip()),
        "workspace_set": bool((ASANA_WORKSPACE_GID or "").strip()),
        "project_set": bool((ASANA_PROJECT_GID or "").strip()),
        "workspace_gid": ASANA_WORKSPACE_GID or "",
        "project_gid": ASANA_PROJECT_GID or "",
    }


@router.get("", response_class=HTMLResponse)
async def asana_hub(request: Request, db: Session = Depends(get_db)):
    msg = request.query_params.get("msg", "")
    error = request.query_params.get("error", "")
    me_name = ""
    my_tasks = []
    pull_error = ""

    if asana_client.is_configured():
        me = asana_client.test_connection()
        if me.ok:
            me_name = (me.data or {}).get("name") or (me.data or {}).get("email") or "Connected"
        if asana_client.workspace_configured():
            res, views = fetch_my_tasks_views(40)
            if res.ok:
                my_tasks = views
            else:
                pull_error = res.error

    overdue = jobs_for_asana_push(db, only_overdue=True, limit=40)
    linked = (
        db.query(Job)
        .filter(Job.asana_task_gid.isnot(None))
        .order_by(Job.id.desc())
        .limit(30)
        .all()
    )

    return render(
        request,
        "asana/hub.html",
        {
            **_status_ctx(),
            "me_name": me_name,
            "my_tasks": my_tasks,
            "pull_error": pull_error,
            "overdue_jobs": overdue,
            "linked_jobs": linked,
            "msg": msg,
            "error": error,
            "today": date.today(),
        },
    )


@router.post("/test", response_class=HTMLResponse)
async def asana_test(request: Request):
    if not asana_client.is_configured():
        return RedirectResponse(
            "/asana?error=" + url_quote("Set ASANA_ACCESS_TOKEN in .env and restart."),
            status_code=303,
        )
    res = asana_client.test_connection()
    if res.ok:
        name = (res.data or {}).get("name") or "OK"
        return RedirectResponse(
            f"/asana?msg={url_quote('Connected as ' + str(name))}",
            status_code=303,
        )
    return RedirectResponse(
        f"/asana?error={url_quote(res.error)}",
        status_code=303,
    )


@router.post("/jobs/{job_id:int}/push", response_class=HTMLResponse)
async def asana_push_job(
    job_id: int, request: Request, db: Session = Depends(get_db)
):
    r = push_job(db, job_id)
    next_url = request.query_params.get("next") or f"/jobs/{job_id}"
    if r.ok:
        return RedirectResponse(
            f"{next_url}?asana_msg={url_quote(r.message)}",
            status_code=303,
        )
    return RedirectResponse(
        f"{next_url}?asana_error={url_quote(r.message)}",
        status_code=303,
    )


@router.post("/jobs/{job_id:int}/pull-status", response_class=HTMLResponse)
async def asana_pull_job(
    job_id: int, request: Request, db: Session = Depends(get_db)
):
    r = pull_status_for_job(db, job_id)
    next_url = request.query_params.get("next") or f"/jobs/{job_id}"
    key = "asana_msg" if r.ok else "asana_error"
    return RedirectResponse(
        f"{next_url}?{key}={url_quote(r.message)}",
        status_code=303,
    )


@router.post("/push-overdue", response_class=HTMLResponse)
async def asana_push_overdue(request: Request, db: Session = Depends(get_db)):
    ok_n, fail_n, msgs = push_overdue_batch(db, limit=25)
    msg = f"Pushed {ok_n} overdue Accounts/CS task(s)"
    if fail_n:
        msg += f"; {fail_n} failed"
        if msgs:
            msg += " — " + "; ".join(msgs[:3])
    return RedirectResponse(f"/asana?msg={url_quote(msg)}", status_code=303)


@router.post("/pull-linked-status", response_class=HTMLResponse)
async def asana_pull_linked(request: Request, db: Session = Depends(get_db)):
    ok_n, fail_n = pull_linked_batch(db, limit=50)
    msg = f"Checked {ok_n} linked job(s)"
    if fail_n:
        msg += f"; {fail_n} failed"
    return RedirectResponse(f"/asana?msg={url_quote(msg)}", status_code=303)
