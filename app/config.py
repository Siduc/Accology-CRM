"""Application configuration from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


BASE_DIR = Path(__file__).resolve().parent.parent

# development | production
ENV = (_env("ENV") or _env("ENVIRONMENT") or "development").lower()
IS_PRODUCTION = ENV == "production"

APP_TITLE = _env("APP_TITLE", "Accountant CRM") or "Accountant CRM"
APP_VERSION = _env("APP_VERSION", "1.0.0") or "1.0.0"


def normalize_database_url(url: str) -> str:
    """Render uses postgres://; SQLAlchemy + psycopg3 prefer postgresql+psycopg://."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _default_sqlite_url() -> str:
    return f"sqlite:///{BASE_DIR / 'crm.db'}"


_raw_db = _env("DATABASE_URL")
if _raw_db:
    DATABASE_URL = normalize_database_url(_raw_db)
elif IS_PRODUCTION:
    raise RuntimeError(
        "DATABASE_URL is required when ENV=production (use Render Postgres)."
    )
else:
    DATABASE_URL = _default_sqlite_url()

IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Auth — never hard-code production credentials
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
