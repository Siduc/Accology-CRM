"""Companies House Public Data API client."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import BASE_DIR
from app.services.company_numbers import normalize_company_number

CH_API_BASE = "https://api.company-information.service.gov.uk"
API_KEY_FILE = BASE_DIR / "companies_house_api_key.txt"


@dataclass
class CHFetchResult:
    ok: bool
    company_number: str
    profile: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def clean_api_key(raw: str) -> str:
    """Strip paste noise: whitespace, quotes, Bearer prefix, accidental URLs."""
    key = (raw or "").strip()
    # remove surrounding quotes
    if (key.startswith('"') and key.endswith('"')) or (
        key.startswith("'") and key.endswith("'")
    ):
        key = key[1:-1].strip()
    # common paste prefixes
    for prefix in ("Bearer ", "bearer ", "Basic ", "API_KEY=", "api_key="):
        if key.startswith(prefix):
            key = key[len(prefix) :].strip()
    # collapse internal whitespace/newlines from bad paste
    key = re.sub(r"\s+", "", key)
    return key


def validate_api_key(key: str) -> Optional[str]:
    """Return an error message if the key looks wrong, else None."""
    if not key:
        return "API key is empty."
    if key.startswith("http://") or key.startswith("https://"):
        return (
            "That looks like a web address, not an API key. "
            "Paste the long key string from Companies House Developer Hub "
            "(not this CRM page URL)."
        )
    if "companies-house" in key.lower() or "127.0.0.1" in key:
        return (
            "That does not look like a Companies House API key. "
            "Copy the key from the Developer Hub application page."
        )
    if len(key) < 16:
        return "API key seems too short — check you copied the whole key."
    return None


def get_api_key() -> Optional[str]:
    """Resolve API key: config/env first, then local file (dev only)."""
    from app.config import COMPANIES_HOUSE_API_KEY, IS_PRODUCTION

    env = clean_api_key(COMPANIES_HOUSE_API_KEY or os.environ.get("COMPANIES_HOUSE_API_KEY", "") or "")
    if env and not validate_api_key(env):
        return env
    if IS_PRODUCTION:
        return None
    if API_KEY_FILE.exists():
        key = clean_api_key(API_KEY_FILE.read_text(encoding="utf-8"))
        if key and not validate_api_key(key):
            return key
    return None


def save_api_key(key: str) -> str:
    """
    Save API key. Returns empty string on success, or an error message.
    In production, keys must be set via COMPANIES_HOUSE_API_KEY env var.
    """
    from app.config import IS_PRODUCTION

    cleaned = clean_api_key(key)
    err = validate_api_key(cleaned)
    if err:
        return err
    if IS_PRODUCTION:
        return (
            "On production, set COMPANIES_HOUSE_API_KEY in the host environment "
            "(e.g. Render dashboard) instead of saving a file."
        )
    try:
        API_KEY_FILE.write_text(cleaned + "\n", encoding="utf-8")
    except OSError as exc:
        return f"Could not write key file: {exc}"
    return ""


def has_api_key() -> bool:
    return bool(get_api_key())


def _authorization_header(api_key: str) -> str:
    # CH: HTTP Basic with API key as username and empty password
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_api_key(api_key: Optional[str] = None) -> CHFetchResult:
    """Call CH with a well-known company to verify the key works."""
    return fetch_company_profile("00000006", api_key=api_key)


def fetch_company_profile(
    company_number: str, api_key: Optional[str] = None
) -> CHFetchResult:
    """
    GET /company/{company_number}

    Auth: HTTP Basic, API key as username, empty password.
    """
    cn = normalize_company_number(company_number)
    if not cn:
        return CHFetchResult(ok=False, company_number=cn, error="Missing company number")

    key = clean_api_key(api_key or get_api_key() or "")
    if not key:
        return CHFetchResult(
            ok=False,
            company_number=cn,
            error=(
                "No valid Companies House API key configured. "
                "Paste the REST API key from the Developer Hub (not a URL)."
            ),
        )
    key_err = validate_api_key(key)
    if key_err:
        return CHFetchResult(ok=False, company_number=cn, error=key_err)

    url = f"{CH_API_BASE}/company/{cn}"
    req = Request(
        url,
        headers={
            "Authorization": _authorization_header(key),
            "Accept": "application/json",
            "User-Agent": "AccountantCRM/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return CHFetchResult(ok=True, company_number=cn, profile=data)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        if exc.code == 404:
            return CHFetchResult(
                ok=False, company_number=cn, error=f"Company {cn} not found at CH"
            )
        if exc.code == 401:
            return CHFetchResult(
                ok=False,
                company_number=cn,
                error=(
                    "Companies House rejected the API key (401). "
                    "Check you created a REST API key (not Streaming/Web), "
                    "copied the full key, and saved it again in the CRM."
                ),
            )
        if exc.code == 429:
            return CHFetchResult(
                ok=False,
                company_number=cn,
                error="Companies House rate limit hit — try again shortly",
            )
        # CH often returns: {"error":"Invalid Authorization","type":"ch:service"}
        if "Invalid Authorization" in detail or "ch:service" in detail:
            return CHFetchResult(
                ok=False,
                company_number=cn,
                error=(
                    "Companies House: invalid authorization. "
                    "Usually the wrong value was saved as the API key "
                    "(e.g. a URL). Re-copy the REST API key from the "
                    "Developer Hub and save it again."
                ),
            )
        return CHFetchResult(
            ok=False,
            company_number=cn,
            error=f"HTTP {exc.code}: {detail}",
        )
    except URLError as exc:
        return CHFetchResult(
            ok=False, company_number=cn, error=f"Network error: {exc.reason}"
        )
    except Exception as exc:  # noqa: BLE001
        return CHFetchResult(ok=False, company_number=cn, error=str(exc))


def summarize_profile_dates(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the dates we care about for the UI preview."""
    accounts = profile.get("accounts") or {}
    next_acc = accounts.get("next_accounts") or {}
    cs = profile.get("confirmation_statement") or {}
    return {
        "company_name": profile.get("company_name"),
        "company_status": profile.get("company_status"),
        "accounts_period_end": next_acc.get("period_end_on")
        or accounts.get("next_made_up_to"),
        "accounts_due": next_acc.get("due_on") or accounts.get("next_due"),
        "accounts_overdue": bool(next_acc.get("overdue") or accounts.get("overdue")),
        "cs_made_up_to": cs.get("next_made_up_to"),
        "cs_due": cs.get("next_due"),
        "cs_overdue": bool(cs.get("overdue")),
    }
