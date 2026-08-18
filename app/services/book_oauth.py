"""Shared OAuth 2.0 for Sage Business Cloud and QuickBooks Online."""

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
from app.models.book_oauth_token import BookOauthToken
from app.services.secrets_crypto import decrypt_secret, encrypt_secret

logger = logging.getLogger("accountant_crm.book_oauth")

STATE_MAX_AGE_SECONDS = 600
TOKEN_SKEW_SECONDS = 60
PROVIDERS = ("sage", "qbo")


@dataclass
class TokenResult:
    ok: bool
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _provider_cfg(provider: str) -> Dict[str, str]:
    p = (provider or "").strip().lower()
    if p == "sage":
        try:
            _cfg.sage_configured(refresh=True)
        except Exception:
            pass
        return {
            "client_id": (_cfg.SAGE_CLIENT_ID or "").strip(),
            "client_secret": (_cfg.SAGE_CLIENT_SECRET or "").strip(),
            "redirect": (_cfg.SAGE_REDIRECT_URI or "").strip()
            or "http://localhost:8000/oauth/sage/callback",
            "authorize": (_cfg.SAGE_AUTHORISE_URL or "").strip(),
            "token": (_cfg.SAGE_TOKEN_URL or "").strip(),
            "scopes": (_cfg.SAGE_SCOPES or "full_access").strip(),
            "callback_path": "/oauth/sage/callback",
            "settings_path": "/settings/sage",
            "label": "Sage Business Cloud",
        }
    if p == "qbo":
        try:
            _cfg.qbo_configured(refresh=True)
        except Exception:
            pass
        return {
            "client_id": (_cfg.QBO_CLIENT_ID or "").strip(),
            "client_secret": (_cfg.QBO_CLIENT_SECRET or "").strip(),
            "redirect": (_cfg.QBO_REDIRECT_URI or "").strip()
            or "http://localhost:8000/oauth/qbo/callback",
            "authorize": (_cfg.QBO_AUTHORISE_URL or "").strip(),
            "token": (_cfg.QBO_TOKEN_URL or "").strip(),
            "scopes": (_cfg.QBO_SCOPES or "com.intuit.quickbooks.accounting").strip(),
            "callback_path": "/oauth/qbo/callback",
            "settings_path": "/settings/qbo",
            "label": "QuickBooks Online",
        }
    raise ValueError(f"Unknown books provider: {provider}")


def configured(provider: str) -> bool:
    cfg = _provider_cfg(provider)
    return bool(cfg["client_id"] and cfg["client_secret"])


def mask_id(value: Optional[str]) -> str:
    cid = (value or "").strip()
    if not cid:
        return ""
    if len(cid) <= 8:
        return cid[:2] + "…"
    return cid[:4] + "…" + cid[-4:]


def sign_state(*, provider: str, return_to: str = "", redirect_uri: str = "") -> str:
    payload = {
        "p": provider,
        "n": secrets.token_urlsafe(16),
        "exp": int((datetime.utcnow() + timedelta(seconds=STATE_MAX_AGE_SECONDS)).timestamp()),
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
    if int(payload.get("exp") or 0) < int(datetime.utcnow().timestamp()):
        return False, {}, "Authorisation state expired — try again."
    return True, payload, ""


def resolve_redirect(provider: str, request: Any = None) -> str:
    cfg = _provider_cfg(provider)
    configured_uri = cfg["redirect"]
    fallback = f"http://127.0.0.1:8000{cfg['callback_path']}"
    if request is None:
        return configured_uri or fallback
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
            return configured_uri or fallback
        if scheme not in ("http", "https"):
            scheme = "https"
        derived = f"{scheme}://{host.rstrip('/')}{cfg['callback_path']}"
        loop = "localhost" in (configured_uri or "").lower() or "127.0.0.1" in (
            configured_uri or ""
        )
        host_loop = "localhost" in host or host.startswith("127.")
        if configured_uri and loop and not host_loop:
            return derived
        return configured_uri or derived
    except Exception:
        return configured_uri or fallback


def build_authorise_url(provider: str, *, state: str, redirect_uri: str) -> str:
    cfg = _provider_cfg(provider)
    if not cfg["client_id"]:
        raise RuntimeError(f"{cfg['label']} is not configured.")
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": cfg["scopes"],
        "state": state,
    }
    if provider == "sage":
        params["filter"] = "apiv3.1"
        params["country"] = (_cfg.SAGE_COUNTRY or "GB").strip()
    return f"{cfg['authorize']}?{urlencode(params)}"


def _post_token(provider: str, form: Dict[str, str]) -> TokenResult:
    cfg = _provider_cfg(provider)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "AccologiseCRM/1.0",
    }
    body_form = dict(form)
    if provider == "qbo":
        raw = f"{cfg['client_id']}:{cfg['client_secret']}"
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
    else:
        body_form["client_id"] = cfg["client_id"]
        body_form["client_secret"] = cfg["client_secret"]
    req = Request(
        cfg["token"],
        data=urlencode(body_form).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err = str(exc)
        return TokenResult(ok=False, error=f"Token HTTP {exc.code}: {err[:500]}")
    except (URLError, TimeoutError, OSError) as exc:
        return TokenResult(ok=False, error=str(exc))
    if not data.get("access_token"):
        return TokenResult(
            ok=False,
            error=str(data.get("error_description") or data.get("error") or "No token"),
        )
    return TokenResult(
        ok=True,
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        token_type=str(data.get("token_type") or "Bearer"),
        expires_in=int(data.get("expires_in") or 3600),
        scope=str(data.get("scope") or cfg["scopes"]),
        extra=data if isinstance(data, dict) else {},
    )


def exchange_code(provider: str, code: str, *, redirect_uri: str) -> TokenResult:
    return _post_token(
        provider,
        {
            "grant_type": "authorization_code",
            "code": (code or "").strip(),
            "redirect_uri": redirect_uri,
        },
    )


def refresh_access_token(provider: str, refresh_token: str) -> TokenResult:
    return _post_token(
        provider,
        {"grant_type": "refresh_token", "refresh_token": (refresh_token or "").strip()},
    )


def http_json(
    method: str,
    url: str,
    access_token: str,
    *,
    extra_headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 60,
) -> Tuple[bool, Any, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": "AccologiseCRM/1.0",
    }
    if extra_headers:
        headers.update(extra_headers)
    if data is not None:
        headers.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw:
                return True, {}, ""
            try:
                return True, json.loads(raw), ""
            except json.JSONDecodeError:
                return True, raw, ""
    except HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err = str(exc)
        return False, {}, f"HTTP {exc.code}: {err[:700]}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, {}, str(exc)


def _store(plain: Optional[str]) -> str:
    if not plain:
        return ""
    try:
        return encrypt_secret(plain) or plain
    except Exception:
        return plain


def _read(stored: Optional[str]) -> str:
    if not stored:
        return ""
    try:
        return decrypt_secret(stored) or stored
    except Exception:
        return stored


def save_token(
    db: Session,
    provider: str,
    result: TokenResult,
    *,
    user_email: str = "",
    user_id: str = "",
    tenants: Optional[List[Dict[str, Any]]] = None,
) -> BookOauthToken:
    for old in (
        db.query(BookOauthToken)
        .filter(BookOauthToken.provider == provider, BookOauthToken.status == "active")
        .all()
    ):
        old.status = "revoked"
        old.updated_at = datetime.utcnow()
    expires = None
    if result.expires_in:
        expires = datetime.utcnow() + timedelta(seconds=int(result.expires_in))
    row = BookOauthToken(
        provider=provider,
        access_token=_store(result.access_token),
        refresh_token=_store(result.refresh_token) or None,
        token_type=result.token_type or "Bearer",
        expires_at=expires,
        scope=result.scope,
        user_email=user_email or None,
        user_id=user_id or None,
        tenants_json=json.dumps(tenants or []),
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def revoke_local(db: Session, provider: str) -> int:
    n = 0
    for row in (
        db.query(BookOauthToken)
        .filter(BookOauthToken.provider == provider, BookOauthToken.status == "active")
        .all()
    ):
        row.status = "revoked"
        row.updated_at = datetime.utcnow()
        n += 1
    db.commit()
    return n


def latest_row(db: Session, provider: str) -> Optional[BookOauthToken]:
    return (
        db.query(BookOauthToken)
        .filter(BookOauthToken.provider == provider)
        .filter(BookOauthToken.refresh_token.isnot(None))
        .order_by(BookOauthToken.id.desc())
        .first()
    )


def parse_tenants(row: Optional[BookOauthToken]) -> List[Dict[str, Any]]:
    if not row or not row.tenants_json:
        return []
    try:
        data = json.loads(row.tenants_json)
    except (TypeError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def token_is_fresh(row: BookOauthToken) -> bool:
    if not row or row.status != "active":
        return False
    if not row.expires_at:
        return True
    return row.expires_at > datetime.utcnow() + timedelta(seconds=TOKEN_SKEW_SECONDS)


def get_valid_access_token(
    db: Session, provider: str
) -> Tuple[Optional[str], Optional[str], Optional[BookOauthToken]]:
    row = latest_row(db, provider)
    if not row:
        return None, f"{_provider_cfg(provider)['label']} is not connected.", None
    if token_is_fresh(row):
        return _read(row.access_token), None, row
    refresh = _read(row.refresh_token)
    if not refresh:
        row.status = "expired"
        db.commit()
        return None, "Session expired. Reconnect in Settings.", row
    result = refresh_access_token(provider, refresh)
    if not result.ok:
        row.status = "expired"
        db.commit()
        return None, result.error, row
    row.access_token = _store(result.access_token)
    if result.refresh_token:
        row.refresh_token = _store(result.refresh_token)
    if result.expires_in:
        row.expires_at = datetime.utcnow() + timedelta(seconds=int(result.expires_in))
    row.status = "active"
    row.updated_at = datetime.utcnow()
    db.commit()
    return result.access_token, None, row


def connection_status(db: Session, provider: str) -> Dict[str, Any]:
    cfg = _provider_cfg(provider)
    row = latest_row(db, provider)
    tenants = parse_tenants(row)
    return {
        "provider": provider,
        "label": cfg["label"],
        "configured": configured(provider),
        "connected": bool(row and (row.status == "active" or row.refresh_token)),
        "fresh": bool(row and token_is_fresh(row)),
        "email": (row.user_email if row else "") or "",
        "expires_at": row.expires_at.isoformat(sep=" ", timespec="minutes")
        if row and row.expires_at
        else "",
        "tenants": tenants,
        "tenant_count": len(tenants),
        "client_mask": mask_id(cfg["client_id"]),
        "secret_set": bool(cfg["client_secret"]),
        "redirect_uri": cfg["redirect"],
        "scopes": cfg["scopes"],
    }


def fetch_sage_businesses(access_token: str) -> Tuple[List[Dict[str, Any]], str]:
    base = (_cfg.SAGE_API_BASE or "https://api.accounting.sage.com/v3.1").rstrip("/")
    ok, data, err = http_json("GET", f"{base}/businesses", access_token)
    if not ok:
        return [], err
    items = []
    rows = data.get("$items") if isinstance(data, dict) else data
    if isinstance(data, dict) and not rows:
        rows = data.get("items") or data.get("businesses") or []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        bid = str(row.get("id") or "").strip()
        if not bid:
            continue
        items.append(
            {
                "tenantId": bid,
                "tenantName": str(row.get("displayed_as") or row.get("name") or bid),
                "tenantType": "business",
            }
        )
    return items, ""


def add_qbo_realm(db: Session, realm_id: str, name: str = "") -> None:
    row = latest_row(db, "qbo")
    if not row:
        return
    tenants = parse_tenants(row)
    if any(str(t.get("tenantId")) == realm_id for t in tenants):
        if name:
            for t in tenants:
                if t.get("tenantId") == realm_id and name:
                    t["tenantName"] = name
        else:
            return
    else:
        tenants.append(
            {
                "tenantId": realm_id,
                "tenantName": name or f"QuickBooks {realm_id[-6:]}",
                "tenantType": "qbo",
            }
        )
    row.tenants_json = json.dumps(tenants)
    db.commit()
