"""Application configuration from environment variables.

Production database:
  - DATABASE_URL (or aliases) from OS env / .env via python-dotenv
  - Never hard-coded
  - Required when ENV=production (no silent SQLite fallback)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Earliest dotenv + logging (idempotent)
from app.env_bootstrap import (
    DOTENV_FILES_LOADED,
    DOTENV_LOADED,
    DOTENV_PATH,
    PROJECT_ROOT,
    bootstrap_environment,
)

bootstrap_environment()

logger = logging.getLogger("accountant_crm.config")

BASE_DIR = PROJECT_ROOT

# Env keys checked for a database URL (first non-empty wins)
_DB_URL_KEYS = (
    "DATABASE_URL",  # Render / standard
    "POSTGRES_URL",
    "POSTGRESQL_URL",
    "SQLALCHEMY_DATABASE_URI",
)


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

APP_TITLE = _env("APP_TITLE", "Accologise") or "Accologise"
APP_VERSION = _env("APP_VERSION", "1.0.0") or "1.0.0"


def normalize_database_url(url: str, *, require_ssl: bool = True) -> str:
    """Normalise URL for SQLAlchemy + psycopg3; add sslmode=require for Postgres."""
    url = _strip_wrapping_quotes(url)
    if not url:
        return url

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]

    if require_ssl and url.startswith("postgresql"):
        url = _ensure_sslmode(url)

    return url


def _ensure_sslmode(url: str) -> str:
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


def _read_raw_database_url() -> Tuple[Optional[str], str]:
    """
    Read the first non-empty database URL from known env keys.

    Returns (raw_url_or_None, source_label).
    """
    for key in _DB_URL_KEYS:
        raw = _env(key)
        if raw:
            return _strip_wrapping_quotes(raw), key
    return None, "unset"


def _resolve_database_url() -> Tuple[str, str]:
    """
    Resolve final SQLAlchemy URL and source label.

    Production: must come from env (or aliases). No SQLite fallback.
    Development: SQLite file if no URL set.
    """
    raw, source = _read_raw_database_url()

    if raw:
        if raw.lower().startswith("sqlite"):
            logger.info(
                "Database URL source=%s dialect=sqlite (explicit)",
                source,
            )
            return raw, source
        normalised = normalize_database_url(raw, require_ssl=True)
        host = database_host(normalised)
        logger.info(
            "Database URL source=%s dialect=postgresql host=%s sslmode=require",
            source,
            host or "(unknown)",
        )
        return normalised, source

    if IS_PRODUCTION:
        checked = ", ".join(_DB_URL_KEYS)
        dotenv_note = (
            f"dotenv_loaded={DOTENV_LOADED} files={DOTENV_FILES_LOADED or 'none'} "
            f"primary_path={DOTENV_PATH} exists={DOTENV_PATH.is_file()}"
        )
        msg = (
            "DATABASE_URL is required when ENV=production.\n"
            f"  ENV={ENV!r}\n"
            f"  Checked empty keys: {checked}\n"
            f"  {dotenv_note}\n"
            "  Set DATABASE_URL on the host (Render dashboard / blueprint) "
            "or in project .env / .env.production."
        )
        logger.error(msg)
        raise RuntimeError(msg)

    url = _default_sqlite_url()
    logger.info(
        "Database URL source=sqlite_default dialect=sqlite path=%s",
        BASE_DIR / "crm.db",
    )
    return url, "sqlite_default"


DATABASE_URL, DATABASE_URL_SOURCE = _resolve_database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")
DB_DIALECT = "sqlite" if IS_SQLITE else "postgresql"
DB_HOST = database_host(DATABASE_URL)

logger.info(
    "Config ready ENV=%s production=%s dialect=%s host=%s source=%s",
    ENV,
    IS_PRODUCTION,
    DB_DIALECT,
    DB_HOST or "(local)",
    DATABASE_URL_SOURCE,
)

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

# Companies House OAuth 2.0 (API Filing / Software Filing web client)
CH_OAUTH_CLIENT_ID = _env("CH_OAUTH_CLIENT_ID")
CH_OAUTH_CLIENT_SECRET = _env("CH_OAUTH_CLIENT_SECRET")
CH_OAUTH_REDIRECT_URI = _env(
    "CH_OAUTH_REDIRECT_URI",
    "http://127.0.0.1:8000/oauth/companies-house/callback",
)
CH_OAUTH_IDENTITY_BASE = (
    _env(
        "CH_OAUTH_IDENTITY_BASE",
        "https://identity.company-information.service.gov.uk",
    )
    or "https://identity.company-information.service.gov.uk"
)
CH_OAUTH_API_BASE = (
    _env(
        "CH_OAUTH_API_BASE",
        "https://api.company-information.service.gov.uk",
    )
    or "https://api.company-information.service.gov.uk"
)
CH_OAUTH_AUTHORISE_URL = _env(
    "CH_OAUTH_AUTHORISE_URL",
    f"{CH_OAUTH_IDENTITY_BASE.rstrip('/')}/oauth2/authorise",
) or f"{CH_OAUTH_IDENTITY_BASE.rstrip('/')}/oauth2/authorise"
CH_OAUTH_TOKEN_URL = _env(
    "CH_OAUTH_TOKEN_URL",
    f"{CH_OAUTH_IDENTITY_BASE.rstrip('/')}/oauth2/token",
) or f"{CH_OAUTH_IDENTITY_BASE.rstrip('/')}/oauth2/token"
# Optional extra scopes; may include {company_number} placeholder
CH_OAUTH_EXTRA_SCOPES = _env("CH_OAUTH_EXTRA_SCOPES") or ""


def _env_bool_early(name: str, default: bool = False) -> bool:
    raw = (_env(name) or "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


CH_OAUTH_ENABLED = _env_bool_early("CH_OAUTH_ENABLED", True) and bool(
    (CH_OAUTH_CLIENT_ID or "").strip() and (CH_OAUTH_CLIENT_SECRET or "").strip()
)


def ch_oauth_configured() -> bool:
    return bool(
        (CH_OAUTH_CLIENT_ID or "").strip()
        and (CH_OAUTH_CLIENT_SECRET or "").strip()
        and (CH_OAUTH_REDIRECT_URI or "").strip()
    )


# Asana (PAT — single user “me”)
ASANA_ACCESS_TOKEN = _env("ASANA_ACCESS_TOKEN")
ASANA_WORKSPACE_GID = _env("ASANA_WORKSPACE_GID")
ASANA_PROJECT_GID = _env("ASANA_PROJECT_GID")

# Practice identity (letters / email footers)
PRACTICE_NAME = _env("PRACTICE_NAME", "Accologise Practice") or "Accologise Practice"
PRACTICE_EMAIL = _env("PRACTICE_EMAIL", "") or ""
PRACTICE_PHONE = _env("PRACTICE_PHONE", "") or ""

# Debt chasing — LIVE MODE DEFAULT OFF (no client emails until you enable)
def _env_bool(name: str, default: bool = False) -> bool:
    raw = (_env(name) or "").lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


CHASE_LIVE_MODE = _env_bool("CHASE_LIVE_MODE", False)
# Soft enable when token present (override with ASANA_ENABLED=false)
ASANA_ENABLED = _env_bool("ASANA_ENABLED", True) and bool(ASANA_ACCESS_TOKEN)
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM") or PRACTICE_EMAIL or SMTP_USER
SMTP_FROM_NAME = _env("SMTP_FROM_NAME", PRACTICE_NAME) or PRACTICE_NAME
SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)

# Server
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
PORT = int(_env("PORT", "8000") or "8000")
