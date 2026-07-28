"""Structured practice emails — template compose, send, history."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.services import practice_emails as mail
from app.templating import render

router = APIRouter(prefix="/emails", tags=["emails"])


def _user(request: Request) -> str:
    return (request.session.get("user") or "").strip() or "user"


@router.get("/compose", response_class=HTMLResponse)
async def email_compose_get(
    request: Request,
    client_id: str = "",
    job_id: str = "",
    template_id: str = "",
    return_to: str = "",
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    tid = int(template_id) if (template_id or "").isdigit() else None

    if not cid and jid:
        job = db.query(Job).filter(Job.id == jid).first()
        if job and job.client_id:
            cid = job.client_id

    if not cid:
        return RedirectResponse(
            f"/clients?error={url_quote('Select a client to send email')}",
            status_code=303,
        )

    client = db.query(Client).filter(Client.id == cid).first()
    if not client:
        return RedirectResponse("/clients", status_code=303)

    job = db.query(Job).filter(Job.id == jid).first() if jid else None
    if job and job.client_id and job.client_id != cid:
        job = None
        jid = None

    templates = mail.list_templates(db)
    tmpl = mail.get_template(db, template_id=tid) if tid else None
    if not tmpl and templates:
        tmpl = templates[0]
        tid = tmpl.id

    ctx = mail.build_context(client, job)
    subject, body = ("", "")
    if tmpl:
        subject, body = mail.render_template(tmpl, ctx)

    recipients = mail.recipients_for_client(db, client)
    default_to = recipients[0]["email"] if recipients else (client.email or "")
    cap = mail.send_capability(db)
    ret = (return_to or "").strip() or (
        f"/jobs/{jid}" if jid else f"/clients/{cid}?tab=email"
    )

    return render(
        request,
        "emails/compose.html",
        {
            "client": client,
            "job": job,
            "templates": templates,
            "template_id": tid,
            "subject": subject,
            "body": body,
            "recipients": recipients,
            "to_address": default_to,
            "return_to": ret,
            "cap": cap,
            "error": request.query_params.get("error", ""),
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/compose")
async def email_compose_post(
    request: Request,
    client_id: str = Form(...),
    job_id: str = Form(""),
    template_id: str = Form(""),
    to_address: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    return_to: str = Form(""),
    log_only: str = Form(""),
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else 0
    jid = int(job_id) if (job_id or "").isdigit() else None
    tid = int(template_id) if (template_id or "").isdigit() else None
    if not cid:
        return RedirectResponse("/clients", status_code=303)

    ret = (return_to or "").strip() or (
        f"/jobs/{jid}" if jid else f"/clients/{cid}?tab=email"
    )
    _row, flash = mail.send_practice_email(
        db,
        client_id=cid,
        job_id=jid,
        to_address=to_address,
        subject=subject,
        body=body,
        template_id=tid,
        sent_by=_user(request),
        force_log_only=log_only in ("1", "true", "on", "yes"),
    )
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(
        f"{ret}{sep}msg={url_quote(flash)}", status_code=303
    )
