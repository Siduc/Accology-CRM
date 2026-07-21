"""Application configuration from environment variables.

Production database connection:
  - DATABASE_URL is read only from the environment (or project `.env` via python-dotenv)
  - Never hard-coded
  - Required when ENV=production

Render injects DATABASE_URL; local production tests can set ENV + DATABASE_URL in the shell
or in `.env`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from dotenv import load_dotenv

logger = logging.getLogger("accountant_crm.config")

BASE_DIR = Path(__file__).resolve().parent.parent


def load_environment() -> bool:
    """
    Load project-root `.env` into os.environ via python-dotenv.

    Safe to call multiple times (also invoked from app/__init__.py and entrypoints).
    Returns True if a .env file was found.
    Existing OS / host variables always win (override=False) so Render
    dashboard values are not overwritten by a stray .env on disk.
    """
    env_path = BASE_DIR / ".env"
    # encoding=utf-8 helps Windows .env files with BOM
    return bool(load_dotenv(env_path, override=False, encoding="utf-8"))


# Must run before any _env() / DATABASE_URL reads below.
# (app/__init__.py already loads .env; this is the authoritative second pass.)
_DOTENV_LOADED = load_environment()


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
    parse_url = url
    use_psycopg = False
    if parse_url.startswith("postgresql+psycopg://"):
        use_psycopg = True
        parse_url = "postgresql://" + parse_url[len("postgresql+psycopg://") :]

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
    if use_psycopg:
        return "postgresql+psycopg://" + rebuilt[len("postgresql://") :]
    return rebuilt


def database_host(url: str | None = None) -> str | None:
    """Hostname only — safe for logs (never includes password)."""
    raw = url if url is not None else DATABASE_URL
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
    path = (BASE_DIR / "crm.db").as_posix()
    return f"sqlite:///{path}"


def _resolve_database_url() -> str:
    """
    Resolve DATABASE_URL from the environment (after dotenv).

    Production: required, must be Postgres (or any non-empty URL you set).
    Development: falls back to local SQLite if unset.
    """
    raw = _env("DATABASE_URL")
    if raw:
        raw = _strip_wrapping_quotes(raw)
    if raw:
        if raw.lower().startswith("sqlite"):
            return raw
        return normalize_database_url(raw, require_ssl=True)
    if IS_PRODUCTION:
        raise RuntimeError(
            "DATABASE_URL is required when ENV=production. "
            "Set it in the host environment (e.g. Render) or in .env."
        )
    return _default_sqlite_url()


DATABASE_URL = _resolve_database_url()
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
    AUTH_USERNAME = _env("AUTH_USERNAME", "accountant") or "accountant"
    AUTH_PASSWORD = _env("AUTH_PASSWORD", "password123") or "password123"
    SESSION_SECRET = (
        _env("SESSION_SECRET", "dev-only-change-me-in-production")
        or "dev-only-change-me-in-production"
    )

# Cookie / session
SESSION_COOKIE_NAME = "crm_session"
SESSION_MAX_AGE = int(_env("SESSION_MAX_AGE", str(60 * 60 * 12)) or str(60 * 60 * 12))
SESSION_HTTPS_ONLY = IS_PRODUCTION

# Companies House
COMPANIES_HOUSE_API_KEY = _env("COMPANIES_HOUSE_API_KEY")

# Server
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
PORT = int(_env("PORT", "8000") or "8000")
