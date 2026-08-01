"""Login / logout — staff + demo-only visitor credentials from env."""

import os
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import render

router = APIRouter(tags=["auth"])

# First demo login lands on the guided checklist once per session
SESSION_TUTORIAL_DONE = "demo_tutorial_done"


def _expected_credentials() -> tuple[str, str]:
    """Staff login credentials from environment (populated by dotenv in app.config)."""
    from app import config  # noqa: F401

    username = (os.environ.get("AUTH_USERNAME") or "").strip()
    password = (os.environ.get("AUTH_PASSWORD") or "").strip()
    if not username:
        username = (config.AUTH_USERNAME or "").strip()
    if not password:
        password = (config.AUTH_PASSWORD or "").strip()
    return username, password


def _demo_credentials() -> tuple[str, str]:
    """Demo-only visitor login — always forced into locked demo mode."""
    from app import config  # noqa: F401

    username = (os.environ.get("DEMO_AUTH_USERNAME") or "").strip()
    password = (os.environ.get("DEMO_AUTH_PASSWORD") or "").strip()
    if not username:
        username = (getattr(config, "DEMO_AUTH_USERNAME", None) or "demo").strip()
    if not password:
        password = (getattr(config, "DEMO_AUTH_PASSWORD", None) or "").strip()
    return username, password


def _creds_match(user: str, password: str, expected_user: str, expected_pass: str) -> bool:
    if not expected_user or not expected_pass:
        return False
    u = (user or "").strip()
    # Length-safe compare for password
    if u != expected_user:
        return False
    if len(password) != len(expected_pass):
        return False
    return secrets.compare_digest(password, expected_pass)


def _login_context(request: Request, *, error: str | None = None) -> dict:
    from app import config
    from app.services.branding import practice_branding_context

    demo_available = bool((getattr(config, "DEMO_AUTH_PASSWORD", None) or "").strip())
    ctx = practice_branding_context()
    ctx.update(
        {
            "error": error,
            "demo_available": demo_available,
            "demo_username_hint": (getattr(config, "DEMO_AUTH_USERNAME", None) or "demo"),
        }
    )
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """CRM log in — public site is GET /."""
    if request.session.get("user"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", _login_context(request))


@router.get("/demo/tour", response_class=HTMLResponse)
async def demo_tour_public(request: Request):
    """
    Shareable pre-demo tutorial — no login required.
    Send this link before handing out demo credentials.
    """
    from app import config

    demo_available = bool((getattr(config, "DEMO_AUTH_PASSWORD", None) or "").strip())
    return render(
        request,
        "demo_tour.html",
        {
            "demo_available": demo_available,
            "demo_username_hint": (getattr(config, "DEMO_AUTH_USERNAME", None) or "demo"),
            "logged_in": bool(request.session.get("user")),
        },
    )


@router.get("/demo/tutorial", response_class=HTMLResponse)
async def demo_tutorial_in_app(request: Request):
    """In-app guided checklist (after login)."""
    if not request.session.get("user"):
        return RedirectResponse("/demo/tour", status_code=303)
    return render(request, "demo_tutorial.html", {})


@router.post("/demo/tutorial/done")
async def demo_tutorial_done(request: Request):
    """Mark tutorial complete for this session and go to the dashboard."""
    if not request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    request.session[SESSION_TUTORIAL_DONE] = True
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    from urllib.parse import quote as url_quote

    from app.services.demo_mode import enter_demo_locked

    u = (username or "").strip()
    p = password or ""

    # 1) Demo-only visitor (cannot access live data)
    demo_user, demo_pass = _demo_credentials()
    if _creds_match(u, p, demo_user, demo_pass):
        enter_demo_locked(request, username=demo_user)
        # First stop: in-app tutorial (once per session until dismissed)
        request.session[SESSION_TUTORIAL_DONE] = False
        return RedirectResponse("/demo/tutorial", status_code=303)

    # 2) Staff / full practice login
    expected_user, expected_pass = _expected_credentials()
    if _creds_match(u, p, expected_user, expected_pass):
        request.session.clear()
        request.session["user"] = u
        # Staff starts on live data; they can enable demo mode themselves
        return RedirectResponse("/dashboard", status_code=303)

    # Failed log-in: return to landing (or dedicated /login) with message
    ref = (request.headers.get("referer") or "").lower()
    if "/login" in ref and "://" in (request.headers.get("referer") or ""):
        return render(
            request,
            "login.html",
            _login_context(request, error="Invalid username or password."),
            status_code=400,
        )
    return RedirectResponse(
        f"/?login_error={url_quote('Invalid username or password.')}#login",
        status_code=303,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
