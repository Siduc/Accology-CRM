from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    APP_TITLE,
    APP_VERSION,
    DB_DIALECT,
    DB_HOST,
    IS_PRODUCTION,
    SESSION_HTTPS_ONLY,
    SESSION_MAX_AGE,
    SESSION_SECRET,
)
from app.database import init_db, ping_database

from app.routers import (
    auth,
    dashboard,
    clients,
    jobs,
    people,
    imports,
    companies_house,
    services,
    lost,
    restore,
)

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Paths that do not require a logged-in session
_PUBLIC_EXACT = frozenset({"/", "/login", "/logout", "/health", "/favicon.ico"})
_PUBLIC_PREFIXES = ("/static/",)


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    """Auth gate + security headers. Must sit inside SessionMiddleware."""
    path = request.url.path

    if not _is_public_path(path) and not request.session.get("user"):
        return RedirectResponse("/", status_code=303)

    response = await call_next(request)

    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Allow self + Chart.js CDN used on client detail charts
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "frame-ancestors 'none';"
    )
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


# SessionMiddleware last so it is outermost and request.session is available
# to security_and_auth (Starlette: last add_middleware runs first).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="crm_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_HTTPS_ONLY,
)


app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(lost.router)
app.include_router(clients.router)
app.include_router(companies_house.router)
app.include_router(jobs.router)
app.include_router(people.router)
app.include_router(imports.router)
app.include_router(services.router)
app.include_router(restore.router)


@app.get("/health")
def health():
    """Render / load balancer health check (public). No secrets in response."""
    db_ok = ping_database()
    status = "ok" if db_ok else "degraded"
    code = 200 if db_ok else 503
    body = {
        "status": status,
        "version": APP_VERSION,
        "database": db_ok,
        "dialect": DB_DIALECT,
    }
    # Host only (never credentials) — helps confirm Render DATABASE_URL wiring
    if DB_HOST:
        body["db_host"] = DB_HOST
    return JSONResponse(body, status_code=code)


@app.on_event("startup")
def on_startup():
    init_db()
