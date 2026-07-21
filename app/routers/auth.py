"""Login / logout — credentials from AUTH_USERNAME / AUTH_PASSWORD (env / .env)."""

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.templating import render

router = APIRouter(tags=["auth"])


def _expected_credentials() -> tuple[str, str]:
    """Read login credentials from environment (populated by dotenv in app.config)."""
    # Import config first so load_dotenv has run
    from app import config  # noqa: F401

    username = (os.environ.get("AUTH_USERNAME") or "").strip()
    password = (os.environ.get("AUTH_PASSWORD") or "").strip()
    # Fall back to config module values (includes dev defaults if env empty)
    if not username:
        username = (config.AUTH_USERNAME or "").strip()
    if not password:
        password = (config.AUTH_PASSWORD or "").strip()
    return username, password


@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    expected_user, expected_pass = _expected_credentials()
    if (
        expected_user
        and expected_pass
        and username.strip() == expected_user
        and password == expected_pass
    ):
        request.session.clear()
        request.session["user"] = username.strip()
        return RedirectResponse("/dashboard", status_code=303)
    return render(
        request,
        "login.html",
        {"error": "Invalid username or password."},
        status_code=400,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)
