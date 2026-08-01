"""Public Accology single-page site + contact → Prospect."""

from __future__ import annotations

import re
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.prospecting import create_prospect
from app.templating import render

router = APIRouter(tags=["website"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _home_ctx(
    request: Request | None = None,
    *,
    error: str = "",
    sent: bool = False,
    form_name: str = "",
    form_email: str = "",
    form_company: str = "",
    form_message: str = "",
    login_error: str = "",
) -> dict:
    from app import config

    demo_available = bool((getattr(config, "DEMO_AUTH_PASSWORD", None) or "").strip())
    return {
        "error": error,
        "sent": sent,
        "form_name": form_name,
        "form_email": form_email,
        "form_company": form_company,
        "form_message": form_message,
        "login_error": login_error,
        "demo_available": demo_available,
    }


@router.get("/", response_class=HTMLResponse)
async def public_home(request: Request):
    """Accology landing (Imagine design) with embedded CRM log-in."""
    if request.session.get("user"):
        return RedirectResponse("/dashboard", status_code=303)
    sent = request.query_params.get("sent") in ("1", "true", "yes")
    return render(
        request,
        "website/home.html",
        _home_ctx(
            request,
            sent=sent,
            error=request.query_params.get("error", ""),
            form_name=request.query_params.get("name", ""),
            form_email=request.query_params.get("email", ""),
            form_company=request.query_params.get("company", ""),
            form_message=request.query_params.get("message", ""),
            login_error=request.query_params.get("login_error", ""),
        ),
    )


@router.get("/contact", response_class=HTMLResponse)
async def contact_anchor(request: Request):
    return RedirectResponse("/#contact", status_code=303)


@router.get("/contact/thanks", response_class=HTMLResponse)
async def contact_thanks(request: Request):
    return RedirectResponse("/?sent=1#contact", status_code=303)


@router.post("/contact", response_class=HTMLResponse)
async def contact_post(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    company: str = Form(""),
    message: str = Form(""),
    company_url: str = Form(""),  # honeypot
    db: Session = Depends(get_db),
):
    if (company_url or "").strip():
        return RedirectResponse("/?sent=1#contact", status_code=303)

    name_s = (name or "").strip()
    email_s = (email or "").strip()
    company_s = (company or "").strip()
    message_s = (message or "").strip()

    def fail(msg: str) -> RedirectResponse:
        q = (
            f"error={url_quote(msg)}"
            f"&name={url_quote(name_s[:120])}"
            f"&email={url_quote(email_s[:120])}"
            f"&company={url_quote(company_s[:120])}"
            f"&message={url_quote(message_s[:500])}"
        )
        return RedirectResponse(f"/?{q}#contact", status_code=303)

    if not name_s:
        return fail("Name is required.")
    if not email_s or not _EMAIL_RE.match(email_s):
        return fail("A valid email is required.")
    if not message_s:
        return fail("A message is required.")
    if len(message_s) > 4000:
        return fail("Message is too long.")

    company_name = f"Website · {company_s}" if company_s else f"Website · {name_s}"
    notes = message_s
    if company_s:
        notes = f"Company: {company_s}\n\n{notes}"

    try:
        create_prospect(
            db,
            company_name=company_name[:200],
            contact_name=name_s,
            email=email_s,
            phone="",
            notes=notes,
            source="Website",
            pipeline_status="new",
        )
    except Exception:
        return fail("The message could not be sent. Email contact@accology.co.")

    return RedirectResponse("/?sent=1#contact", status_code=303)
