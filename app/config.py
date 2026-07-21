"""Application configuration from environment variables.

Loads project-root `.env` via python-dotenv at import time (before any reads).
Existing OS / host env vars take precedence over `.env` (override=False).
On Render, set DATABASE_URL (and ENV=production) in the service dashboard or blueprint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from dotenv import load_dotenv

logger = logging.getLogger("accountant_crm.config")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env as early as possible so AUTH_*, DATABASE_URL, etc. are available.
# Does nothing if the file is missing (e.g. Render injects env vars instead).
# override=False: real environment (Render) always wins over a local .env file.
load_dotenv(BASE_DIR / ".env", override=False)

# development | production
ENV = (_env("ENV") or _env("ENVIRONMENT") or "development").lower()
IS_PRODUCTION = ENV == "production"

APP_TITLE = _env("APP_TITLE", "Accountant CRM") or "Accountant CRM"
APP_VERSION = _env("APP_VERSION", "1.0.0") or "1.0.0"


def normalize_database_url(url: str, *, require_ssl: bool = True) -> str:
    """
    Normalise a database URL for SQLAlchemy + psycopg3.

    - Strip wrapping quotes (common when pasting into dashboards)
    - Render often supplies postgres:// → postgresql+psycopg://
    - Append sslmode=require for Postgres when missing (Render best practice)
    """
    url = _strip_wrapping_quotes(url)
    if not url:
        return url

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    # Prefer psycopg3 driver for SQLAlchemy 2
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    if require_ssl and url.startswith("postgresql"):
        url = _ensure_sslmode(url)

    return url


def _ensure_sslmode(url: str) -> str:
    """Add sslmode=require if the URL has no sslmode query param."""
    # urlparse needs a standard scheme for reliable parsing
    parse_url = url
    driver_prefix = ""
    if parse_url.startswith("postgresql+psycopg://"):
        driver_prefix = "postgresql+psycopg://"
        parse_url = "postgresql://" + parse_url[len("postgresql+psycopg://") :]
    elif parse_url.startswith("postgresql://"):
        driver_prefix = "postgresql://"
        parse_url = parse_url  # already fine

    parsed = urlparse(parse_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "sslmode" not in {k.lower() for k in query}:
        query["sslmode"] = "require"
    new_query = urlencode(query)
    rebuilt = urlunparse(
        (
            "postgresql",
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )
    # Restore driver scheme
    if driver_prefix.startswith("postgresql+psycopg"):
        return "postgresql+psycopg://" + rebuilt[len("postgresql://") :]
    return rebuilt


def database_host(url: str | None = None) -> str | None:
    """Hostname only — safe for logs (never includes password)."""
    raw = url or DATABASE_URL
    if not raw or raw.startswith("sqlite"):
        return None
    try:
        parse_url = raw
        if parse_url.startswith("postgresql+psycopg://"):
            parse_url = "postgresql://" + parse_url[len("postgresql+psycopg://") :]
        return urlparse(parse_url).hostname
    except Exception:  # noqa: BLE001
        return None


def _default_sqlite_url() -> str:
    # Forward slashes work on Windows with SQLAlchemy
    path = (BASE_DIR / "crm.db").as_posix()
    return f"sqlite:///{path}"


_raw_db = _env("DATABASE_URL")
if _raw_db:
    _raw_db = _strip_wrapping_quotes(_raw_db)

if _raw_db:
    # Always SSL-normalise remote Postgres; SQLite URLs pass through unchanged
    if _raw_db.lower().startswith("sqlite"):
        DATABASE_URL = _raw_db
    else:
        DATABASE_URL = normalize_database_url(_raw_db, require_ssl=True)
elif IS_PRODUCTION:
    raise RuntimeError(
        "DATABASE_URL is required when ENV=production (use Render Postgres)."
    )
else:
    DATABASE_URL = _default_sqlite_url()

IS_SQLITE = DATABASE_URL.startswith("sqlite")
DB_DIALECT = "sqlite" if IS_SQLITE else "postgresql"
DB_HOST = database_host(DATABASE_URL)

# Auth — from environment / .env only (no hard-coded production secrets)
if IS_PRODUCTION:
    AUTH_USERNAME = _env("AUTH_USERNAME")
    AUTH_PASSWORD = _env("AUTH_PASSWORD")
    SESSION_SECRET = _env("SESSION_SECRET")
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise RuntimeError(
            "AUTH_USERNAME and AUTH_PASSWORD are required when ENV=production."
        )
    if not SESSION_SECRET or len(SESSION_SECRET) < 16:
        raise RuntimeError(
            "SESSION_SECRET (min 16 chars) is required when ENV=production."
        )
else:
    # Dev defaults only if .env / OS env do not set them (first-run convenience)
    AUTH_USERNAME = _env("AUTH_USERNAME", "accountant") or "accountant"
    AUTH_PASSWORD = _env("AUTH_PASSWORD", "password123") or "password123"
    SESSION_SECRET = (
        _env("SESSION_SECRET", "dev-only-change-me-in-production")
        or "dev-only-change-me-in-production"
    )

# Cookie / session
SESSION_COOKIE_NAME = "crm_session"
SESSION_MAX_AGE = int(_env("SESSION_MAX_AGE", str(60 * 60 * 12)) or str(60 * 60 * 12))
SESSION_HTTPS_ONLY = IS_PRODUCTION  # Secure cookies behind Render TLS

# Companies House
COMPANIES_HOUSE_API_KEY = _env("COMPANIES_HOUSE_API_KEY")

# Server
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
PORT = int(_env("PORT", "8000") or "8000")
