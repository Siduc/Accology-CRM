"""Xero OAuth 2.0 (authorization code + refresh) for the practice book."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

import app.config as _cfg
from app.models.xero_token import XeroToken
from app.services.secrets_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger("accountant_crm.xero_oauth")

STATE_MAX_AGE_SECONDS = 600
TOKEN_SKEW_SECONDS = 60


def _sync_cfg() -> None:
    try:
        _cfg.refresh_xero_settings(force_dotenv=True)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class TokenResult:
    ok: bool
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""
    id_token: str = ""
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def oauth_is_ready() -> bool:
    return _cfg.xero_configured(refresh=True)


def default_redirect_uri() -> str:
    _sync_cfg()
    return (_cfg.XERO_REDIRECT_URI or "").strip()


def _is_loopback_uri(uri: str) -> bool:
    u = (uri or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "0.0.0.0" in u


def resolve_redirect_uri(request: Any = None) -> str:
    _sync_cfg()
    configured = (_cfg.XERO_REDIRECT_URI or "").strip()
    fallback = "http://127.0.0.1:8000/oauth/xero/callback"
    if request is None:
        return configured or fallback
    try:
        scheme = (request.url.scheme or "https").split(",")[0].strip()
        host = (request.url.hostname or "").strip()
        xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        xf_host = (
            request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        ).split(",")[0].strip()
        if xf_proto:
            scheme = xf_proto
        if xf_host:
            host = xf_host
        if not host:
            return configured or fallback
        if scheme not in ("http", "https"):
            scheme = "https"
        derived = f"{scheme}://{host.rstrip('/')}/oauth/xero/callback"
        host_loopback = _is_loopback_uri(host) or host.startswith("127.")
        if not configured:
            return derived
        if _is_loopback_uri(configured) and not host_loopback:
            logger.warning(
                "Xero redirect_uri env is loopback (%s) but request host is %s — using %s",
                configured,
                host,
                derived,
            )
            return derived
        return configured
    except Exception as exc:  # noqa: BLE001
        logger.warning("xero resolve_redirect_uri failed: %s", exc)
        return configured or fallback


def mask_client_id(client_id: Optional[str] = None) -> str:
    if client_id is None:
        _sync_cfg()
        cid = _cfg.XERO_CLIENT_ID or ""
    else:
        cid = client_id or ""
    cid = cid.strip()
    if not cid:
        return ""
    if len(cid) <= 8:
        return cid[:2] + "…"
    return cid[:4] + "…" + cid[-4:]


def build_scopes() -> str:
    _sync_cfg()
    return (_cfg.XERO_SCOPES or "").strip()


def sign_state(*, return_to: str = "", redirect_uri: str = "") -> str:
    payload = {
        "n": secrets.token_urlsafe(16),
        "exp": int(
            (datetime.utcnow() + timedelta(seconds=STATE_MAX_AGE_SECONDS)).timestamp()
        ),
        "ret": (return_to or "")[:500],
        "redir": (redirect_uri or "")[:500],
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(
        (_cfg.SESSION_SECRET or "dev").encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}.{sig}"


def parse_state(state: str) -> Tuple[bool, Dict[str, Any], str]:
    if not state or "." not in state:
        return False, {}, "Invalid state."
    body, sig = state.rsplit(".", 1)
    expected = hmac.new(
        (_cfg.SESSION_SECRET or "dev").encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False, {}, "State signature mismatch."
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (json.JSONDecodeError, ValueError, TypeError):
        return False, {}, "State payload corrupt."
    exp = int(payload.get("exp") or 0)
    if exp < int(datetime.utcnow().timestamp()):
        return False, {}, "Authorisation state expired — try again."
    return True, payload, ""


def build_authorise_url(*, state: str, redirect_uri: Optional[str] = None) -> str:
    if not oauth_is_ready():
        raise RuntimeError(
            "Xero is not configured. Set XERO_CLIENT_ID and XERO_CLIENT_SECRET "
            "in .env (or Render env), then restart."
        )
    _sync_cfg()
    redir = (redirect_uri or default_redirect_uri()).strip()
    if not redir:
        raise RuntimeError("XERO_REDIRECT_URI is empty.")
    params = {
        "response_type": "code",
        "client_id": (_cfg.XERO_CLIENT_ID or "").strip(),
        "redirect_uri": redir,
        "scope": build_scopes(),
        "state": state,
    }
    base = (_cfg.XERO_AUTHORISE_URL or "").strip()
    return f"{base}?{urlencode(params)}"


def _basic_auth_header() -> str:
    _sync_cfg()
    raw = f"{(_cfg.XERO_CLIENT_ID or '').strip()}:{(_cfg.XERO_CLIENT_SECRET or '').strip()}"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _post_token(form: Dict[str, str]) -> TokenResult:
    _sync_cfg()
    url = (_cfg.XERO_TOKEN_URL or "").strip()
    if not url:
        return TokenResult(ok=False, error="Xero token URL not configured.")
    body = urlencode(form).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "AccologiseCRM/1.0 (Xero-OAuth)",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw_text) if raw_text else {}
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        return TokenResult(
            ok=False, error=f"Token endpoint HTTP {exc.code}: {err_body[:500]}"
        )
    except (URLError, TimeoutError, OSError) as exc:
        return TokenResult(ok=False, error=f"Token request failed: {exc}")

    if not data.get("access_token"):
        err = data.get("error_description") or data.get("error") or "No access token"
        return TokenResult(ok=False, error=str(err), raw=data)

    return TokenResult(
        ok=True,
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        token_type=str(data.get("token_type") or "Bearer"),
        expires_in=int(data.get("expires_in") or 1800),
        scope=str(data.get("scope") or ""),
        id_token=str(data.get("id_token") or ""),
        raw=data,
    )


def exchange_code(code: str, *, redirect_uri: Optional[str] = None) -> TokenResult:
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": (code or "").strip(),
            "redirect_uri": (redirect_uri or default_redirect_uri()).strip(),
        }
    )


def refresh_access_token(refresh_token: str) -> TokenResult:
    return _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": (refresh_token or "").strip(),
        }
    )


def _http_json(
    method: str,
    url: str,
    access_token: str,
    *,
    tenant_id: str = "",
    data: Optional[bytes] = None,
    timeout: int = 60,
) -> Tuple[bool, Any, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "AccologiseCRM/1.0 (Xero)",
    }
    if tenant_id:
        headers["Xero-tenant-id"] = tenant_id
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return True, (json.loads(raw) if raw else {}), ""
    except HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err = str(exc)
        return False, {}, f"HTTP {exc.code}: {err[:600]}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, {}, str(exc)


def fetch_userinfo(access_token: str) -> Tuple[str, str]:
    ok, data, _ = _http_json(
        "GET", "https://identity.xero.com/connect/userinfo", access_token
    )
    if not ok or not isinstance(data, dict):
        return "", ""
    email = str(data.get("email") or data.get("preferred_username") or "")
    uid = str(data.get("sub") or data.get("xero_userid") or "")
    return email, uid


def fetch_connections(access_token: str) -> Tuple[List[Dict[str, Any]], str]:
    _sync_cfg()
    url = (_cfg.XERO_CONNECTIONS_URL or "https://api.xero.com/connections").strip()
    ok, data, err = _http_json("GET", url, access_token)
    if not ok:
        return [], err
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("connections") or data.get("Items") or []
    else:
        rows = []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tid = str(row.get("tenantId") or row.get("tenant_id") or "").strip()
        if not tid:
            continue
        out.append(
            {
                "id": str(row.get("id") or ""),
                "tenantId": tid,
                "tenantName": str(row.get("tenantName") or row.get("tenant_name") or ""),
                "tenantType": str(row.get("tenantType") or row.get("tenant_type") or ""),
            }
        )
    return out, ""


def _store_token(plain: Optional[str]) -> str:
    if not plain:
        return ""
    try:
        return encrypt_secret(plain) or plain
    except Exception:
        return plain


def _read_token(stored: Optional[str]) -> str:
    if not stored:
        return ""
    try:
        return decrypt_secret(stored) or stored
    except Exception:
        return stored


def save_token(
    db: Session,
    result: TokenResult,
    *,
    xero_email: str = "",
    xero_user_id: str = "",
    tenants: Optional[List[Dict[str, Any]]] = None,
) -> XeroToken:
    for old in db.query(XeroToken).filter(XeroToken.status == "active").all():
        old.status = "revoked"
        old.updated_at = datetime.utcnow()

    expires = None
    if result.expires_in:
        expires = datetime.utcnow() + timedelta(seconds=int(result.expires_in))

    row = XeroToken(
        access_token=_store_token(result.access_token),
        refresh_token=_store_token(result.refresh_token) or None,
        token_type=result.token_type or "Bearer",
        expires_at=expires,
        scope=result.scope or build_scopes(),
        xero_user_id=xero_user_id or None,
        xero_email=xero_email or None,
        tenants_json=json.dumps(tenants or []),
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def revoke_local(db: Session, token_id: Optional[int] = None) -> int:
    q = db.query(XeroToken).filter(XeroToken.status == "active")
    if token_id:
        q = q.filter(XeroToken.id == token_id)
    n = 0
    for row in q.all():
        row.status = "revoked"
        row.updated_at = datetime.utcnow()
        n += 1
    db.commit()
    return n


def latest_active_token(db: Session) -> Optional[XeroToken]:
    return (
        db.query(XeroToken)
        .filter(XeroToken.status == "active")
        .order_by(XeroToken.id.desc())
        .first()
    )


def latest_recoverable_token(db: Session) -> Optional[XeroToken]:
    row = latest_active_token(db)
    if row:
        return row
    return (
        db.query(XeroToken)
        .filter(XeroToken.refresh_token.isnot(None))
        .filter(XeroToken.refresh_token != "")
        .filter(XeroToken.status.in_(("expired", "revoked", "active")))
        .order_by(XeroToken.id.desc())
        .first()
    )


def token_is_fresh(row: XeroToken) -> bool:
    if not row or row.status != "active":
        return False
    if not row.expires_at:
        return True
    return row.expires_at > datetime.utcnow() + timedelta(seconds=TOKEN_SKEW_SECONDS)


def parse_tenants(row: Optional[XeroToken]) -> List[Dict[str, Any]]:
    if not row or not row.tenants_json:
        return []
    try:
        data = json.loads(row.tenants_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def get_valid_access_token(db: Session) -> Tuple[Optional[str], Optional[str], Optional[XeroToken]]:
    row = latest_recoverable_token(db)
    if not row:
        return None, "Xero is not connected. Connect it in Settings → Xero.", None
    if token_is_fresh(row) and row.status == "active":
        return _read_token(row.access_token), None, row
    refresh = _read_token(row.refresh_token)
    if not refresh:
        row.status = "expired"
        row.updated_at = datetime.utcnow()
        db.commit()
        return None, "Xero session expired. Please reconnect in Settings.", row

    result = refresh_access_token(refresh)
    if not result.ok:
        row.status = "expired"
        row.updated_at = datetime.utcnow()
        db.commit()
        return None, f"Xero re-auth required: {result.error}", row

    row.access_token = _store_token(result.access_token)
    if result.refresh_token:
        row.refresh_token = _store_token(result.refresh_token)
    if result.expires_in:
        row.expires_at = datetime.utcnow() + timedelta(seconds=int(result.expires_in))
    if result.scope:
        row.scope = result.scope
    row.status = "active"
    row.updated_at = datetime.utcnow()
    tenants, _ = fetch_connections(result.access_token)
    if tenants:
        row.tenants_json = json.dumps(tenants)
    db.commit()
    db.refresh(row)
    return result.access_token, None, row


def refresh_tenants(db: Session) -> Tuple[List[Dict[str, Any]], str]:
    token, err, row = get_valid_access_token(db)
    if not token or not row:
        return [], err or "Not connected."
    tenants, terr = fetch_connections(token)
    if terr:
        return parse_tenants(row), terr
    row.tenants_json = json.dumps(tenants)
    row.updated_at = datetime.utcnow()
    db.commit()
    return tenants, ""


def connection_status(db: Session) -> Dict[str, Any]:
    _sync_cfg()
    row = latest_recoverable_token(db)
    tenants = parse_tenants(row)
    connected = bool(row and (row.status == "active" or row.refresh_token))
    return {
        "configured": oauth_is_ready(),
        "connected": connected,
        "fresh": bool(row and token_is_fresh(row)),
        "email": (row.xero_email if row else "") or "",
        "expires_at": row.expires_at.isoformat(sep=" ", timespec="minutes")
        if row and row.expires_at
        else "",
        "tenants": tenants,
        "tenant_count": len(tenants),
        "scope": (row.scope if row else "") or "",
    }
