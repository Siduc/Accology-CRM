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

# Demo-only visitor login (always anonymised; cannot switch to live data)
# Share DEMO_AUTH_USERNAME / DEMO_AUTH_PASSWORD with prospects; keep staff AUTH_* private.
DEMO_AUTH_USERNAME = _env("DEMO_AUTH_USERNAME", "demo") or "demo"
DEMO_AUTH_PASSWORD = _env("DEMO_AUTH_PASSWORD", "") or ""

# Cookie / session
SESSION_COOKIE_NAME = "crm_session"
SESSION_MAX_AGE = int(_env("SESSION_MAX_AGE", str(60 * 60 * 12)) or str(60 * 60 * 12))
SESSION_HTTPS_ONLY = IS_PRODUCTION

# Companies House
COMPANIES_HOUSE_API_KEY = _env("COMPANIES_HOUSE_API_KEY")

# Selective Outlook → Task push (API key for Power Automate / Outlook)
# Preferred: TASK_IMPORT_API_KEY (Power Automate docs)
# Aliases: ACCOLOGISE_TASK_PUSH_KEY, TASK_PUSH_API_KEY
TASK_PUSH_API_KEY = (
    _env("TASK_IMPORT_API_KEY")
    or _env("ACCOLOGISE_TASK_PUSH_KEY")
    or _env("TASK_PUSH_API_KEY")
    or ""
)

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


# Scanned post inbox (local folder the dedicated scanner writes to).
# Override with POST_INBOX_PATH. On Render/Linux set this env var if post import is used.
# Subfolders inbox / processing / done / failed / splits are created automatically.
def _default_post_inbox_path() -> str:
    import os
    from pathlib import Path

    if os.name == "nt":
        # Local Windows practice machine — Documents\Accologise Post
        return str(Path.home() / "Documents" / "Accologise Post")
    return "/var/data/post-inbox"


POST_INBOX_PATH = _env("POST_INBOX_PATH", _default_post_inbox_path()) or _default_post_inbox_path()

# Brother ADF often feeds from the back of the stack and may save pages upside-down.
# Applied on import before split/OCR. Override with POST_SCAN_REVERSE_ORDER=0 / POST_SCAN_ROTATE_180=0.
POST_SCAN_REVERSE_ORDER = _env_bool("POST_SCAN_REVERSE_ORDER", True)
POST_SCAN_ROTATE_180 = _env_bool("POST_SCAN_ROTATE_180", True)

# Companies House XML Gateway (Software Filing / presenter account)
# Apply: https://www.gov.uk/guidance/apply-to-file-with-companies-house-using-software
CH_XML_PRESENTER_ID = _env("CH_XML_PRESENTER_ID") or ""
CH_XML_PRESENTER_AUTH = _env("CH_XML_PRESENTER_AUTH") or ""
# Live gateway (production). Test/sandbox when CH_XML_GATEWAY_TEST=1
CH_XML_GATEWAY_URL = (
    _env(
        "CH_XML_GATEWAY_URL",
        "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway",
    )
    or "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway"
)
CH_XML_GATEWAY_TEST_URL = (
    _env(
        "CH_XML_GATEWAY_TEST_URL",
        "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway",
    )
    or "https://xmlgw.companieshouse.gov.uk/v1-0/xmlgw/Gateway"
)
CH_XML_GATEWAY_TEST = _env_bool_early("CH_XML_GATEWAY_TEST", True)
# Package / product reference registered with CH for your software (when issued)
CH_XML_PACKAGE_REFERENCE = _env("CH_XML_PACKAGE_REFERENCE") or "0000"
CH_XML_PRODUCT = _env("CH_XML_PRODUCT") or "Accologise"
CH_XML_PRODUCT_VERSION = _env("CH_XML_PRODUCT_VERSION") or "1.0"
# Live submit is OFF by default — export/preview only until presenter + schema signed off
CH_XML_SUBMIT_LIVE = _env_bool_early("CH_XML_SUBMIT_LIVE", False)


def ch_xml_gateway_configured() -> bool:
    """Presenter ID + authentication code present (env)."""
    return bool(
        (CH_XML_PRESENTER_ID or "").strip() and (CH_XML_PRESENTER_AUTH or "").strip()
    )


# Microsoft Graph / OneDrive (delegated OAuth for practice OneDrive)
# Exact env names (required for Connect):
#   MS_GRAPH_CLIENT_ID
#   MS_GRAPH_CLIENT_SECRET
#   MS_GRAPH_REDIRECT_URI
_MS_GRAPH_REDIRECT_DEFAULT = "http://127.0.0.1:8000/oauth/microsoft/callback"

MS_GRAPH_CLIENT_ID = _env("MS_GRAPH_CLIENT_ID")
MS_GRAPH_CLIENT_SECRET = _env("MS_GRAPH_CLIENT_SECRET")
MS_GRAPH_TENANT_ID = _env("MS_GRAPH_TENANT_ID", "common") or "common"
MS_GRAPH_REDIRECT_URI = _env(
    "MS_GRAPH_REDIRECT_URI",
    _MS_GRAPH_REDIRECT_DEFAULT,
)
MS_GRAPH_SCOPES = (
    _env(
        "MS_GRAPH_SCOPES",
        "offline_access User.Read Files.ReadWrite Mail.Send Mail.ReadWrite",
    )
    or "offline_access User.Read Files.ReadWrite Mail.Send Mail.ReadWrite"
)
_MS_TENANT = (MS_GRAPH_TENANT_ID or "common").strip()
MS_GRAPH_AUTHORISE_URL = _env(
    "MS_GRAPH_AUTHORISE_URL",
    f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/authorize",
) or f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/authorize"
MS_GRAPH_TOKEN_URL = _env(
    "MS_GRAPH_TOKEN_URL",
    f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/token",
) or f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/token"
GRAPH_API_BASE = (
    _env("GRAPH_API_BASE", "https://graph.microsoft.com/v1.0")
    or "https://graph.microsoft.com/v1.0"
)
MS_GRAPH_MAX_UPLOAD_MB = int(_env("MS_GRAPH_MAX_UPLOAD_MB", "25") or "25")
# Well-known folder name for archive-on-task-complete: archive | deleteditems | …
MS_GRAPH_MAIL_ARCHIVE_FOLDER = (
    _env("MS_GRAPH_MAIL_ARCHIVE_FOLDER", "archive") or "archive"
)
MS_GRAPH_ENABLED = _env_bool_early("MS_GRAPH_ENABLED", True) and bool(
    (MS_GRAPH_CLIENT_ID or "").strip() and (MS_GRAPH_CLIENT_SECRET or "").strip()
)


# MS Graph keys re-applied from project .env on each refresh (so redirect URI
# changes like http→https are picked up without a full process restart).
_MS_GRAPH_ENV_KEYS = (
    "MS_GRAPH_CLIENT_ID",
    "MS_GRAPH_CLIENT_SECRET",
    "MS_GRAPH_TENANT_ID",
    "MS_GRAPH_REDIRECT_URI",
    "MS_GRAPH_SCOPES",
    "MS_GRAPH_AUTHORISE_URL",
    "MS_GRAPH_TOKEN_URL",
    "MS_GRAPH_MAX_UPLOAD_MB",
    "MS_GRAPH_MAIL_ARCHIVE_FOLDER",
    "MS_GRAPH_ENABLED",
    "GRAPH_API_BASE",
)


def _apply_ms_graph_keys_from_dotenv() -> None:
    """
    Force-load MS_GRAPH_* (and GRAPH_API_BASE) from project ``.env``.

    Unlike general bootstrap (which never overrides non-empty OS env), this
    **does** overwrite process env for these keys when the file has a value —
    so editing ``MS_GRAPH_REDIRECT_URI`` (e.g. http → https) takes effect on
    the next Connect click. Host/Render secrets set only in the environment
    (not in .env) are left alone when the key is absent from the file.
    """
    try:
        from dotenv import dotenv_values
        from app.env_bootstrap import DOTENV_PATH, DOTENV_PRODUCTION_PATH
    except Exception:  # noqa: BLE001
        return

    merged: dict = {}
    for path in (DOTENV_PATH, DOTENV_PRODUCTION_PATH):
        if not path.is_file():
            continue
        try:
            vals = dotenv_values(path, encoding="utf-8") or {}
        except Exception:  # noqa: BLE001
            continue
        for k, v in vals.items():
            if k in _MS_GRAPH_ENV_KEYS and v is not None and str(v).strip() != "":
                merged[k] = str(v).strip()

    for k, v in merged.items():
        os.environ[k] = v


def refresh_ms_graph_settings(*, force_dotenv: bool = True) -> dict:
    """
    Re-read Microsoft Graph settings from the environment / project ``.env``.

    Uses exactly:
      MS_GRAPH_CLIENT_ID
      MS_GRAPH_CLIENT_SECRET
      MS_GRAPH_REDIRECT_URI

    Called from Settings and OAuth so a process that started before the keys
    were added (or with blank placeholders) still picks them up.
    """
    global MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, MS_GRAPH_TENANT_ID
    global MS_GRAPH_REDIRECT_URI, MS_GRAPH_SCOPES, MS_GRAPH_AUTHORISE_URL
    global MS_GRAPH_TOKEN_URL, GRAPH_API_BASE, MS_GRAPH_MAX_UPLOAD_MB
    global MS_GRAPH_MAIL_ARCHIVE_FOLDER, MS_GRAPH_ENABLED, _MS_TENANT

    if force_dotenv:
        try:
            from app.env_bootstrap import bootstrap_environment

            bootstrap_environment(force=True)
        except Exception:  # noqa: BLE001
            pass
        # Always re-apply MS Graph keys from .env (http↔https redirect updates)
        _apply_ms_graph_keys_from_dotenv()

    MS_GRAPH_CLIENT_ID = _env("MS_GRAPH_CLIENT_ID")
    MS_GRAPH_CLIENT_SECRET = _env("MS_GRAPH_CLIENT_SECRET")
    MS_GRAPH_TENANT_ID = _env("MS_GRAPH_TENANT_ID", "common") or "common"
    MS_GRAPH_REDIRECT_URI = (
        _env("MS_GRAPH_REDIRECT_URI", _MS_GRAPH_REDIRECT_DEFAULT)
        or _MS_GRAPH_REDIRECT_DEFAULT
    ).strip()
    MS_GRAPH_SCOPES = (
        _env(
            "MS_GRAPH_SCOPES",
            "offline_access User.Read Files.ReadWrite Mail.Send Mail.ReadWrite",
        )
        or "offline_access User.Read Files.ReadWrite Mail.Send Mail.ReadWrite"
    )
    _MS_TENANT = (MS_GRAPH_TENANT_ID or "common").strip()
    MS_GRAPH_AUTHORISE_URL = _env(
        "MS_GRAPH_AUTHORISE_URL",
        f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/authorize",
    ) or f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/authorize"
    MS_GRAPH_TOKEN_URL = _env(
        "MS_GRAPH_TOKEN_URL",
        f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/token",
    ) or f"https://login.microsoftonline.com/{_MS_TENANT}/oauth2/v2.0/token"
    GRAPH_API_BASE = (
        _env("GRAPH_API_BASE", "https://graph.microsoft.com/v1.0")
        or "https://graph.microsoft.com/v1.0"
    )
    try:
        MS_GRAPH_MAX_UPLOAD_MB = int(_env("MS_GRAPH_MAX_UPLOAD_MB", "25") or "25")
    except ValueError:
        MS_GRAPH_MAX_UPLOAD_MB = 25
    MS_GRAPH_MAIL_ARCHIVE_FOLDER = (
        _env("MS_GRAPH_MAIL_ARCHIVE_FOLDER", "archive") or "archive"
    )
    MS_GRAPH_ENABLED = _env_bool_early("MS_GRAPH_ENABLED", True) and bool(
        (MS_GRAPH_CLIENT_ID or "").strip() and (MS_GRAPH_CLIENT_SECRET or "").strip()
    )

    return {
        "client_id_set": bool((MS_GRAPH_CLIENT_ID or "").strip()),
        "secret_set": bool((MS_GRAPH_CLIENT_SECRET or "").strip()),
        "redirect_uri": (MS_GRAPH_REDIRECT_URI or "").strip(),
        "enabled": bool(MS_GRAPH_ENABLED),
        "configured": ms_graph_configured(refresh=False),
    }


def ms_graph_configured(*, refresh: bool = True) -> bool:
    """
    True when the three required Microsoft Graph env vars are present:

      MS_GRAPH_CLIENT_ID
      MS_GRAPH_CLIENT_SECRET
      MS_GRAPH_REDIRECT_URI
    """
    if refresh:
        refresh_ms_graph_settings(force_dotenv=True)
    return bool(
        (MS_GRAPH_CLIENT_ID or "").strip()
        and (MS_GRAPH_CLIENT_SECRET or "").strip()
        and (MS_GRAPH_REDIRECT_URI or "").strip()
    )


# Startup log (no secrets)
logger.info(
    "MS Graph config client_id_set=%s secret_set=%s redirect=%s configured=%s",
    bool((MS_GRAPH_CLIENT_ID or "").strip()),
    bool((MS_GRAPH_CLIENT_SECRET or "").strip()),
    MS_GRAPH_REDIRECT_URI or "(default)",
    ms_graph_configured(refresh=False),
)


# Asana (PAT — single user “me”)
ASANA_ACCESS_TOKEN = _env("ASANA_ACCESS_TOKEN")
ASANA_WORKSPACE_GID = _env("ASANA_WORKSPACE_GID")
ASANA_PROJECT_GID = _env("ASANA_PROJECT_GID")

# Practice identity (letters / email footers)
PRACTICE_NAME = _env("PRACTICE_NAME", "Accology") or "Accology"
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


# Practice structured email (template send). Defaults to CHASE_LIVE_MODE if unset.
def practice_email_live() -> bool:
    raw = (_env("PRACTICE_EMAIL_LIVE") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(CHASE_LIVE_MODE)


# Soft enable when token present (override with ASANA_ENABLED=false)
ASANA_ENABLED = _env_bool("ASANA_ENABLED", True) and bool(ASANA_ACCESS_TOKEN)

# Accologise AI assistant (SpaceXAI / xAI — server-side only)
XAI_API_KEY = _env("XAI_API_KEY") or ""
AI_MODEL = _env("AI_MODEL", "grok-4.5") or "grok-4.5"
AI_PLAN_SECRET = _env("AI_PLAN_SECRET") or SESSION_SECRET
# Enabled when key present unless explicitly disabled
AI_ASSISTANT_ENABLED = _env_bool("AI_ASSISTANT_ENABLED", True) and bool(XAI_API_KEY)
# Allow heuristic-only mode (CH + rules, no LLM) when key missing but CH configured
AI_ASSISTANT_HEURISTIC = _env_bool("AI_ASSISTANT_HEURISTIC", True)
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")
SMTP_USER = _env("SMTP_USER")
SMTP_PASSWORD = _env("SMTP_PASSWORD")
SMTP_FROM = _env("SMTP_FROM") or PRACTICE_EMAIL or SMTP_USER
SMTP_FROM_NAME = _env("SMTP_FROM_NAME", PRACTICE_NAME) or PRACTICE_NAME
SMTP_USE_TLS = _env_bool("SMTP_USE_TLS", True)

# Staff notifications (website prospects, future alerts)
# Email is optional; CRM inbox is always written on website contact.
NOTIFY_WEBSITE_PROSPECT_EMAIL = _env_bool("NOTIFY_WEBSITE_PROSPECT_EMAIL", False)
# Where to send staff alerts (defaults to PRACTICE_EMAIL / SMTP_FROM)
NOTIFY_ALERT_EMAIL = (_env("NOTIFY_ALERT_EMAIL") or PRACTICE_EMAIL or "").strip()

# Server
HOST = _env("HOST", "0.0.0.0") or "0.0.0.0"
PORT = int(_env("PORT", "8000") or "8000")
