"""FastAPI application entry — production DB URL comes from app.config (env / dotenv)."""

from pathlib import Path
import logging
import secrets

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
    TASK_PUSH_API_KEY,
)
from app.database import init_db, ping_database  # noqa: E402

logger = logging.getLogger("accountant_crm.auth")

from app.routers import (
    auth,
    website,
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
    assistant,
    vat,
    asana_integration,
    notes,
    cs,
    ch_oauth,
    ms_graph_oauth,
    documents,
    emails,
    tasks,
    api_tasks,
    csv_exchange,
    prospecting,
    notifications,
    api_prospecting,
)

app = FastAPI(
    title=APP_TITLE,
    version=APP_VERSION,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None if IS_PRODUCTION else "/redoc",
)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Paths that do not require a logged-in browser session (HTML UI only)
_PUBLIC_EXACT = frozenset(
    {
        "/",  # Accology public site
        "/login",
        "/logout",
        "/contact",
        "/contact/thanks",
        "/health",
        "/favicon.ico",
        "/manifest.webmanifest",
        "/sw.js",
        "/oauth/companies-house/callback",
        "/oauth/microsoft/callback",
        "/demo/tour",
    }
)
_PUBLIC_PREFIXES = ("/static/",)


def _is_public_path(path: str) -> bool:
    p = (path or "").rstrip("/") or "/"
    if path in _PUBLIC_EXACT or p in _PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def _is_api_path(path: str) -> bool:
    """Machine API — never use browser session redirects."""
    p = (path or "").strip()
    return p == "/api" or p.startswith("/api/")


def _header_api_key(request: Request) -> str:
    """Read API key from common Power Automate / HTTP client header shapes."""
    # Starlette headers are case-insensitive
    for name in ("x-api-key", "api-key", "x-api_key"):
        val = (request.headers.get(name) or "").strip()
        if val:
            return val
    auth = (request.headers.get("authorization") or "").strip()
    if not auth:
        return ""
    lower = auth.lower()
    if lower.startswith("bearer "):
        return auth[7:].strip()
    if lower.startswith("apikey "):
        return auth[7:].strip()
    # Raw key in Authorization (no scheme)
    if " " not in auth:
        return auth
    return ""


def _api_key_configured() -> str:
    return (TASK_PUSH_API_KEY or "").strip()


def _api_key_matches(provided: str) -> bool:
    expected = _api_key_configured()
    if not expected or not provided:
        return False
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


def _json_api_error(status: int, *messages: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"ok": False, "errors": [m for m in messages if m]},
    )


def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    # Allow Microsoft OneDrive / Office Online embeds and opens
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https://*.sharepoint.com https://*.onedrive.com https://*.live.com; "
        "connect-src 'self' https://cdn.jsdelivr.net https://*.microsoft.com https://*.sharepoint.com; "
        "frame-src 'self' https://*.sharepoint.com https://*.onedrive.com "
        "https://*.office.com https://*.officeapps.live.com https://*.live.com "
        "https://view.officeapps.live.com; "
        "object-src 'self'; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "frame-ancestors 'self';"
    )
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


@app.middleware("http")
async def security_and_auth(request: Request, call_next):
    """
    Auth gate (inside SessionMiddleware).

    Critical: /api/* is for Power Automate / machine clients.
    - NEVER 303-redirect API paths to /
    - Session cookie is NOT required for /api/*
    - POST /api/v1/tasks/from-email requires X-API-Key (checked here + in router)
    """
    path = request.url.path or ""
    method = request.method or "GET"

    # ─── API paths: short-circuit before any browser session redirect ───
    if _is_api_path(path):
        provided = _header_api_key(request)
        expected = _api_key_configured()
        # Temporary diagnostics (no secret values logged)
        logger.info(
            "API request method=%s path=%s has_x_api_key=%s key_len=%s "
            "env_key_configured=%s env_key_len=%s key_match=%s "
            "header_names=%s",
            method,
            path,
            bool(provided),
            len(provided),
            bool(expected),
            len(expected),
            _api_key_matches(provided) if provided and expected else False,
            sorted({h.lower() for h in request.headers.keys()}),
        )

        # Task push POST must have a valid key at the gate (never redirect)
        norm = path.rstrip("/") or "/"
        if (
            norm.endswith("/tasks/from-email")
            or norm.endswith("/prospecting/from-email")
        ) and method == "POST":
            if not expected:
                return _security_headers(
                    _json_api_error(
                        503,
                        "TASK_IMPORT_API_KEY is not set on the server. "
                        "Add it to the environment and restart.",
                    )
                )
            if not provided:
                return _security_headers(
                    _json_api_error(
                        401,
                        "Missing X-API-Key header. "
                        "In Power Automate set header name X-API-Key to your TASK_IMPORT_API_KEY value.",
                    )
                )
            if not _api_key_matches(provided):
                return _security_headers(
                    _json_api_error(
                        401,
                        "Invalid X-API-Key (does not match TASK_IMPORT_API_KEY on the server).",
                    )
                )
            # Stash for router (optional)
            request.state.api_key_ok = True

        try:
            response = await call_next(request)
        except Exception:
            logger.exception("API handler error path=%s", path)
            return _security_headers(
                _json_api_error(500, "Internal server error on API request.")
            )
        return _security_headers(response)

    # ─── Browser UI: session required (HTML redirects OK) ───
    if not _is_public_path(path) and not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)

    from app.services.demo_mode import (
        SESSION_KEY as _DEMO_KEY,
        SESSION_LOCKED_KEY as _DEMO_LOCK,
        should_block_export,
    )

    # Demo-only logins stay locked in demo mode for the whole session
    if request.session.get(_DEMO_LOCK):
        request.session[_DEMO_KEY] = True

    if request.session.get(_DEMO_KEY) and method in ("GET", "POST", "HEAD"):
        if should_block_export(path):
            return RedirectResponse(
                "/settings?demo_msg=export_blocked#settings-demo",
                status_code=303,
            )

    response = await call_next(request)
    return _security_headers(response)


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


# Public Accology site at / ; CRM login at /login
app.include_router(website.router)
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
app.include_router(ms_graph_oauth.router)
app.include_router(documents.router)
app.include_router(notifications.router)
app.include_router(emails.router)
app.include_router(tasks.router)
app.include_router(api_tasks.router)
app.include_router(api_prospecting.router)
app.include_router(csv_exchange.router)
app.include_router(settings.router)
app.include_router(sales.router)
app.include_router(prospecting.router)
app.include_router(assistant.router)


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
