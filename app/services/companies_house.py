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


def _ch_get_json(
    path: str, *, api_key: Optional[str] = None, company_number: str = ""
) -> CHFetchResult:
    """Low-level GET against the public Companies House Data API."""
    cn = company_number or ""
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

    url = f"{CH_API_BASE}{path}"
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
                ok=False, company_number=cn, error=f"Not found at CH: {path}"
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
    return _ch_get_json(f"/company/{cn}", api_key=api_key, company_number=cn)


def fetch_company_officers(
    company_number: str, api_key: Optional[str] = None, *, items_per_page: int = 100
) -> CHFetchResult:
    """GET /company/{cn}/officers — active officers list (paginated first page)."""
    cn = normalize_company_number(company_number)
    if not cn:
        return CHFetchResult(ok=False, company_number=cn, error="Missing company number")
    path = f"/company/{cn}/officers?items_per_page={int(items_per_page)}"
    return _ch_get_json(path, api_key=api_key, company_number=cn)


def fetch_company_pscs(
    company_number: str, api_key: Optional[str] = None, *, items_per_page: int = 100
) -> CHFetchResult:
    """GET /company/{cn}/persons-with-significant-control"""
    cn = normalize_company_number(company_number)
    if not cn:
        return CHFetchResult(ok=False, company_number=cn, error="Missing company number")
    path = (
        f"/company/{cn}/persons-with-significant-control"
        f"?items_per_page={int(items_per_page)}"
    )
    return _ch_get_json(path, api_key=api_key, company_number=cn)


def download_cs_bundle(company_number: str, api_key: Optional[str] = None) -> CHFetchResult:
    """
    Download CS-relevant public data: profile + officers + PSCs.
    result.profile is a dict with keys profile, officers, pscs.
    """
    cn = normalize_company_number(company_number)
    if not cn:
        return CHFetchResult(ok=False, company_number=cn, error="Missing company number")

    prof = fetch_company_profile(cn, api_key=api_key)
    if not prof.ok:
        return prof

    officers = fetch_company_officers(cn, api_key=api_key)
    pscs = fetch_company_pscs(cn, api_key=api_key)

    bundle = {
        "profile": prof.profile,
        "officers": officers.profile if officers.ok else {"items": [], "error": officers.error},
        "pscs": pscs.profile if pscs.ok else {"items": [], "error": pscs.error},
        "fetched_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    return CHFetchResult(ok=True, company_number=cn, profile=bundle)


def _ch_get_json_query(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    api_key: Optional[str] = None,
) -> CHFetchResult:
    """GET with query string (search / advanced search)."""
    from urllib.parse import urlencode

    q = {k: v for k, v in (params or {}).items() if v not in (None, "", [])}
    suffix = f"?{urlencode(q, doseq=True)}" if q else ""
    return _ch_get_json(f"{path}{suffix}", api_key=api_key)


def search_companies(
    q: str, *, items_per_page: int = 20, start_index: int = 0, api_key: Optional[str] = None
) -> CHFetchResult:
    """GET /search/companies"""
    q = (q or "").strip()
    if not q:
        return CHFetchResult(ok=False, company_number="", error="Search query required")
    return _ch_get_json_query(
        "/search/companies",
        {
            "q": q,
            "items_per_page": min(int(items_per_page), 100),
            "start_index": max(0, int(start_index)),
        },
        api_key=api_key,
    )


def advanced_search_companies(
    *,
    incorporated_from: Optional[str] = None,
    incorporated_to: Optional[str] = None,
    company_status: Optional[str] = None,
    sic_codes: Optional[str] = None,
    location: Optional[str] = None,
    company_name_includes: Optional[str] = None,
    size: int = 50,
    start_index: int = 0,
    api_key: Optional[str] = None,
) -> CHFetchResult:
    """
    GET /advanced-search/companies
    Dates as YYYY-MM-DD. sic_codes comma-separated.
    """
    params: Dict[str, Any] = {
        "size": min(max(int(size), 1), 100),
        "start_index": max(0, int(start_index)),
    }
    if incorporated_from:
        params["incorporated_from"] = incorporated_from
    if incorporated_to:
        params["incorporated_to"] = incorporated_to
    if company_status:
        params["company_status"] = company_status
    if sic_codes:
        params["sic_codes"] = sic_codes
    if location:
        params["location"] = location
    if company_name_includes:
        params["company_name_includes"] = company_name_includes
    return _ch_get_json_query("/advanced-search/companies", params, api_key=api_key)


def fetch_filing_history(
    company_number: str,
    *,
    category: Optional[str] = None,
    items_per_page: int = 25,
    start_index: int = 0,
    api_key: Optional[str] = None,
) -> CHFetchResult:
    """GET /company/{cn}/filing-history"""
    cn = normalize_company_number(company_number)
    if not cn:
        return CHFetchResult(ok=False, company_number=cn, error="Missing company number")
    params: Dict[str, Any] = {
        "items_per_page": min(int(items_per_page), 100),
        "start_index": max(0, int(start_index)),
    }
    if category:
        params["category"] = category
    from urllib.parse import urlencode

    path = f"/company/{cn}/filing-history?{urlencode(params)}"
    return _ch_get_json(path, api_key=api_key, company_number=cn)


def fetch_document_metadata(
    company_number: str, transaction_id: str, *, api_key: Optional[str] = None
) -> CHFetchResult:
    """GET /company/{cn}/filing-history/{tx}/document"""
    cn = normalize_company_number(company_number)
    tx = (transaction_id or "").strip()
    if not cn or not tx:
        return CHFetchResult(
            ok=False, company_number=cn or "", error="Company number and transaction id required"
        )
    path = f"/company/{cn}/filing-history/{tx}/document"
    return _ch_get_json(path, api_key=api_key, company_number=cn)


def download_document_content(
    document_id: str, *, api_key: Optional[str] = None
) -> tuple[bool, bytes, str, str]:
    """
    GET document-api …/document/{id}/content
    Returns (ok, body_bytes, content_type, error).
    """
    doc_id = (document_id or "").strip().rstrip("/")
    if not doc_id:
        return False, b"", "", "Missing document id"
    # Accept full links or bare ids
    if "document-api" in doc_id and "/document/" in doc_id:
        # extract id segment
        try:
            part = doc_id.split("/document/")[1].split("/")[0]
            doc_id = part
        except Exception:
            pass
    key = clean_api_key(api_key or get_api_key() or "")
    if not key:
        return False, b"", "", "No Companies House API key"
    url = (
        f"https://document-api.company-information.service.gov.uk"
        f"/document/{doc_id}/content"
    )
    req = Request(
        url,
        headers={
            "Authorization": _authorization_header(key),
            "Accept": "application/pdf, application/xhtml+xml, application/xml, */*",
            "User-Agent": "AccountantCRM/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=60) as resp:
            body = resp.read()
            ctype = resp.headers.get("Content-Type") or "application/pdf"
            return True, body, ctype, ""
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return False, b"", "", f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, b"", "", str(exc)


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


# Map CH company type codes → Accologise client_type options
_CH_TYPE_TO_CLIENT_TYPE = {
    "ltd": "Limited Company",
    "private-limited-guarant-nsc": "Limited Company",
    "private-limited-guarant-nsc-limited-exemption": "Limited Company",
    "private-limited-shares-section-30-exemption": "Limited Company",
    "private-unlimited": "Limited Company",
    "private-unlimited-nsc": "Limited Company",
    "plc": "PLC",
    "llp": "LLP",
    "limited-partnership": "Partnership",
    "scottish-partnership": "Partnership",
    "scottish-limited-partnership": "Partnership",
    "old-public-company": "PLC",
}


def client_type_from_ch(ch_type: Optional[str]) -> str:
    """Map Companies House company type to CRM client_type label."""
    raw = (ch_type or "").strip().lower()
    if not raw:
        return "Limited Company"
    if raw in _CH_TYPE_TO_CLIENT_TYPE:
        return _CH_TYPE_TO_CLIENT_TYPE[raw]
    if "llp" in raw:
        return "LLP"
    if "plc" in raw or "public" in raw:
        return "PLC"
    if "partnership" in raw:
        return "Partnership"
    if "ltd" in raw or "limited" in raw:
        return "Limited Company"
    return "Other"


def client_fields_from_profile(profile: Dict[str, Any]) -> Dict[str, str]:
    """
    Map a CH company profile into form field values for New Client.

    Does not create a client — only returns strings for the form draft.
    """
    profile = profile or {}
    addr = profile.get("registered_office_address") or {}
    cn = normalize_company_number(profile.get("company_number") or "") or (
        str(profile.get("company_number") or "").strip()
    )
    ch_status = (profile.get("company_status") or "").strip().lower()
    # Prefer Active for live companies; leave status choice for user otherwise
    if ch_status in ("active", "open"):
        overall_status = "Active"
    elif ch_status in ("dissolved", "liquidation", "converted-closed", "closed"):
        overall_status = "Former"
    else:
        overall_status = "Active"

    sic = profile.get("sic_codes") or []
    if isinstance(sic, list):
        sic_txt = ", ".join(str(x) for x in sic if x)
    else:
        sic_txt = str(sic) if sic else ""

    notes_parts = [
        f"Pulled from Companies House ({profile.get('company_status') or 'status unknown'})."
    ]
    if sic_txt:
        notes_parts.append(f"SIC: {sic_txt}")
    if profile.get("date_of_creation"):
        notes_parts.append(f"Incorporated: {profile.get('date_of_creation')}")

    return {
        "company_name": (profile.get("company_name") or "").strip(),
        "company_number": cn,
        "address_line1": (addr.get("address_line_1") or "").strip(),
        "address_line2": (addr.get("address_line_2") or "").strip(),
        "town": (addr.get("locality") or "").strip(),
        "postcode": (addr.get("postal_code") or "").strip(),
        "client_type": client_type_from_ch(profile.get("type")),
        "overall_status": overall_status,
        "notes": " ".join(notes_parts),
        "ch_company_status": (profile.get("company_status") or "").strip(),
        "ch_company_type": (profile.get("type") or "").strip(),
    }
