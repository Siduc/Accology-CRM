"""FastAPI application entry — production DB URL comes from app.config (env / dotenv)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# Bootstrap dotenv/logging, then config (DATABASE_URL), then database / routers.
from app.env_bootstrap import bootstrap_environment

bootstrap_environment()

from app.config import (  # noqa: E402
    APP_TITLE,
    APP_VERSION,
    DATABASE_URL_SOURCE,
    DB_DIALECT,
    DB_HOST,
    ENV,
    IS_PRODUCTION,
    SESSION_HTTPS_ONLY,
    SESSION_MAX_AGE,
    SESSION_SECRET,
)
from app.database import init_db, ping_database  # noqa: E402

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
    groups,
    working_capital,
    settings,
    sales,
    bank,
    purchase,
    vat,
    asana_integration,
    notes,
    cs,
    ch_oauth,
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
_PUBLIC_EXACT = frozenset(
    {
        "/",
        "/login",
        "/logout",
        "/health",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/sw.js",
        # CH OAuth redirect (signed state; no CRM session required)
        "/oauth/companies-house/callback",
    }
)
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
        "manifest-src 'self'; "
        "worker-src 'self'; "
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
app.include_router(groups.router)
app.include_router(working_capital.router)
app.include_router(bank.router)
app.include_router(purchase.router)
app.include_router(vat.router)
app.include_router(asana_integration.router)
app.include_router(notes.router)
app.include_router(cs.router)
app.include_router(ch_oauth.router)
app.include_router(settings.router)
app.include_router(sales.router)


@app.get("/manifest.webmanifest")
def web_manifest():
    """PWA manifest (public)."""
    path = static_dir / "manifest.webmanifest"
    return FileResponse(
        path,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/sw.js")
def service_worker():
    """Service worker at root scope (public)."""
    path = static_dir / "sw.js"
    return FileResponse(
        path,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/favicon.ico")
def favicon():
    """Browser tab icon (public)."""
    path = static_dir / "icons" / "favicon-32.png"
    return FileResponse(path, media_type="image/png")


@app.get("/health")
def health():
    """Render / load balancer health check (public). No secrets in response."""
    db_ok = ping_database()
    status = "ok" if db_ok else "degraded"
    code = 200 if db_ok else 503
    body = {
        "status": status,
        "version": APP_VERSION,
        "env": ENV,
        "database": db_ok,
        "dialect": DB_DIALECT,
        "db_source": DATABASE_URL_SOURCE,  # env key name only, never the URL
    }
    # Host only (never credentials) — confirms DATABASE_URL wiring on Render
    if DB_HOST:
        body["db_host"] = DB_HOST
    return JSONResponse(body, status_code=code)


@app.on_event("startup")
def on_startup():
    init_db()
