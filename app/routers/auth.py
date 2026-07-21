from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import AUTH_USERNAME, AUTH_PASSWORD
from app.templating import render

router = APIRouter(tags=["auth"])


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
    if username == AUTH_USERNAME and password == AUTH_PASSWORD:
        request.session.clear()
        request.session["user"] = username
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
    response = RedirectResponse("/", status_code=303)
    return response
