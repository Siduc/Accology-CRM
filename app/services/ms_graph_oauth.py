"""Microsoft Graph OAuth 2.0 (authorization code + refresh) for OneDrive."""

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
from app.models.ms_graph_token import MsGraphToken

logger = logging.getLogger("accountant_crm.ms_graph_oauth")

STATE_MAX_AGE_SECONDS = 600
TOKEN_SKEW_SECONDS = 60


def _sync_cfg() -> None:
    """Refresh MS Graph env (MS_GRAPH_CLIENT_ID / SECRET / REDIRECT_URI)."""
    try:
        _cfg.refresh_ms_graph_settings(force_dotenv=True)
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
    error: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def oauth_is_ready() -> bool:
    return _cfg.ms_graph_configured(refresh=True)


def default_redirect_uri() -> str:
    _sync_cfg()
    return (_cfg.MS_GRAPH_REDIRECT_URI or "").strip()


def _is_loopback_uri(uri: str) -> bool:
    u = (uri or "").lower()
    return "localhost" in u or "127.0.0.1" in u or "0.0.0.0" in u


def resolve_redirect_uri(request: Any = None) -> str:
    """
    Redirect URI for authorize + token exchange (must match exactly).

    On Render/production, if env still has a localhost callback (common copy-paste
    from .env), derive https://{host}/oauth/microsoft/callback from the request
    so Connect Microsoft actually leaves Accology for login.microsoftonline.com.
    """
    _sync_cfg()
    configured = (_cfg.MS_GRAPH_REDIRECT_URI or "").strip()
    if request is None:
        return configured or "http://127.0.0.1:8000/oauth/microsoft/callback"

    try:
        scheme = (request.url.scheme or "https").split(",")[0].strip()
        host = (request.url.hostname or "").strip()
        # Render / reverse proxies
        xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        xf_host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or ""
        ).split(",")[0].strip()
        if xf_proto:
            scheme = xf_proto
        if xf_host:
            # host may include port
            host = xf_host
        if not host:
            return configured or "http://127.0.0.1:8000/oauth/microsoft/callback"
        # Prefer https in production when behind TLS-terminating proxy
        if scheme not in ("http", "https"):
            scheme = "https"
        derived = f"{scheme}://{host.rstrip('/')}/oauth/microsoft/callback"
        host_loopback = _is_loopback_uri(host) or host.startswith("127.")
        if not configured:
            return derived
        # Env still points at local machine while user is on accology.co
        if _is_loopback_uri(configured) and not host_loopback:
            logger.warning(
                "MS Graph redirect_uri env is loopback (%s) but request host is %s — "
                "using derived %s (add this URI in Azure App registration)",
                configured,
                host,
                derived,
            )
            return derived
        return configured
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_redirect_uri failed: %s", exc)
        return configured or "http://127.0.0.1:8000/oauth/microsoft/callback"


def mask_client_id(client_id: Optional[str] = None) -> str:
    if client_id is None:
        _sync_cfg()
        cid = _cfg.MS_GRAPH_CLIENT_ID or ""
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
    return (
        _cfg.MS_GRAPH_SCOPES or "offline_access User.Read Files.ReadWrite"
    ).strip()


def sign_state(*, return_to: str = "", redirect_uri: str = "") -> str:
    payload = {
        "n": secrets.token_urlsafe(16),
        "exp": int(
            (datetime.utcnow() + timedelta(seconds=STATE_MAX_AGE_SECONDS)).timestamp()
        ),
        "ret": (return_to or "")[:500],
        # Must match authorize + token exchange redirect_uri exactly
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
            "Microsoft Graph is not configured. Set MS_GRAPH_CLIENT_ID and "
            "MS_GRAPH_CLIENT_SECRET on the server (Render env), then restart."
        )
    _sync_cfg()
    redir = (redirect_uri or default_redirect_uri()).strip()
    if not redir:
        raise RuntimeError(
            "MS_GRAPH_REDIRECT_URI is empty and could not be derived from the request."
        )
    logger.info(
        "MS Graph authorize redirect_uri=%s client_id=%s",
        redir,
        mask_client_id(),
    )
    params = {
        "client_id": (_cfg.MS_GRAPH_CLIENT_ID or "").strip(),
        "response_type": "code",
        "redirect_uri": redir,
        "response_mode": "query",
        "scope": build_scopes(),
        "state": state,
        # Force account picker so a silent SSO failure does not look like "nothing happens"
        "prompt": "select_account",
    }
    base = (_cfg.MS_GRAPH_AUTHORISE_URL or "").strip()
    if not base:
        raise RuntimeError("MS_GRAPH_AUTHORISE_URL is empty.")
    return f"{base}?{urlencode(params)}"


def _post_token(form: Dict[str, str]) -> TokenResult:
    _sync_cfg()
    url = (_cfg.MS_GRAPH_TOKEN_URL or "").strip()
    if not url:
        return TokenResult(ok=False, error="Token URL not configured.")
    body = urlencode(form).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "AccologiseCRM/1.0 (MS-Graph-OAuth)",
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
        expires_in=int(data.get("expires_in") or 3600),
        scope=str(data.get("scope") or ""),
        raw=data,
    )


def exchange_code(code: str, *, redirect_uri: Optional[str] = None) -> TokenResult:
    _sync_cfg()
    return _post_token(
        {
            "client_id": (_cfg.MS_GRAPH_CLIENT_ID or "").strip(),
            "client_secret": (_cfg.MS_GRAPH_CLIENT_SECRET or "").strip(),
            "code": (code or "").strip(),
            "redirect_uri": (redirect_uri or default_redirect_uri()).strip(),
            "grant_type": "authorization_code",
            "scope": build_scopes(),
        }
    )


def refresh_access_token(refresh_token: str) -> TokenResult:
    _sync_cfg()
    return _post_token(
        {
            "client_id": (_cfg.MS_GRAPH_CLIENT_ID or "").strip(),
            "client_secret": (_cfg.MS_GRAPH_CLIENT_SECRET or "").strip(),
            "refresh_token": (refresh_token or "").strip(),
            "grant_type": "refresh_token",
            "scope": build_scopes(),
        }
    )


def graph_get(path: str, access_token: str) -> Tuple[bool, Dict[str, Any], str]:
    base = (_cfg.GRAPH_API_BASE or "https://graph.microsoft.com/v1.0").rstrip("/")
    url = path if path.startswith("http") else f"{base}{path}"
    req = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "AccologiseCRM/1.0 (MS-Graph)",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw) if raw else {}
            return True, data, ""
    except HTTPError as exc:
        try:
            err = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err = str(exc)
        return False, {}, f"HTTP {exc.code}: {err[:400]}"
    except (URLError, TimeoutError, OSError) as exc:
        return False, {}, str(exc)


def fetch_me(access_token: str) -> Tuple[bool, str, str, str]:
    ok, data, err = graph_get("/me", access_token)
    if not ok:
        return False, "", "", err
    email = (
        data.get("mail")
        or data.get("userPrincipalName")
        or data.get("displayName")
        or ""
    )
    uid = str(data.get("id") or "")
    return True, str(email), uid, ""


def fetch_drive(access_token: str) -> Tuple[bool, str, str]:
    ok, data, err = graph_get("/me/drive", access_token)
    if not ok:
        return False, "", err
    return True, str(data.get("id") or ""), ""


def save_token(
    db: Session,
    result: TokenResult,
    *,
    ms_user_email: str = "",
    ms_user_id: str = "",
    drive_id: str = "",
) -> MsGraphToken:
    # Soft-revoke previous active tokens
    for old in (
        db.query(MsGraphToken).filter(MsGraphToken.status == "active").all()
    ):
        old.status = "revoked"
        old.updated_at = datetime.utcnow()

    expires = None
    if result.expires_in:
        expires = datetime.utcnow() + timedelta(seconds=int(result.expires_in))

    row = MsGraphToken(
        access_token=result.access_token,
        refresh_token=result.refresh_token or None,
        token_type=result.token_type or "Bearer",
        expires_at=expires,
        scope=result.scope or build_scopes(),
        ms_user_email=ms_user_email or None,
        ms_user_id=ms_user_id or None,
        drive_id=drive_id or None,
        status="active",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def revoke_local(db: Session, token_id: Optional[int] = None) -> int:
    q = db.query(MsGraphToken).filter(MsGraphToken.status == "active")
    if token_id:
        q = q.filter(MsGraphToken.id == token_id)
    n = 0
    for row in q.all():
        row.status = "revoked"
        row.updated_at = datetime.utcnow()
        n += 1
    db.commit()
    return n


def latest_active_token(db: Session) -> Optional[MsGraphToken]:
    return (
        db.query(MsGraphToken)
        .filter(MsGraphToken.status == "active")
        .order_by(MsGraphToken.id.desc())
        .first()
    )


def latest_recoverable_token(db: Session) -> Optional[MsGraphToken]:
    """
    Prefer an active token; otherwise an expired/revoked row that still has a
    refresh_token (Microsoft refresh often still works after we marked expired).
    """
    row = latest_active_token(db)
    if row:
        return row
    return (
        db.query(MsGraphToken)
        .filter(MsGraphToken.refresh_token.isnot(None))
        .filter(MsGraphToken.refresh_token != "")
        .filter(MsGraphToken.status.in_(("expired", "revoked", "active")))
        .order_by(MsGraphToken.id.desc())
        .first()
    )


def token_is_fresh(row: MsGraphToken) -> bool:
    if not row or row.status != "active":
        return False
    if not row.expires_at:
        return True
    return row.expires_at > datetime.utcnow() + timedelta(seconds=TOKEN_SKEW_SECONDS)


def get_valid_access_token(db: Session) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (access_token, error).
    Refreshes when near expiry or when status was previously marked expired
    but a refresh_token is still present.
    """
    row = latest_recoverable_token(db)
    if not row:
        return None, "Microsoft OneDrive is not connected. Connect it in Settings."
    if token_is_fresh(row) and row.status == "active":
        return row.access_token, None
    if not row.refresh_token:
        row.status = "expired"
        row.updated_at = datetime.utcnow()
        db.commit()
        return None, "Microsoft session expired. Please reconnect in Settings."

    result = refresh_access_token(row.refresh_token)
    if not result.ok:
        row.status = "expired"
        row.updated_at = datetime.utcnow()
        db.commit()
        return None, f"Microsoft re-auth required: {result.error}"

    row.access_token = result.access_token
    if result.refresh_token:
        row.refresh_token = result.refresh_token
    if result.expires_in:
        row.expires_at = datetime.utcnow() + timedelta(seconds=int(result.expires_in))
    if result.scope:
        row.scope = result.scope
    row.status = "active"
    row.updated_at = datetime.utcnow()
    db.commit()
    return row.access_token, None


def connection_status(db: Session) -> Dict[str, Any]:
    configured = oauth_is_ready()
    row = latest_active_token(db)
    recoverable = latest_recoverable_token(db) if not row else row
    # Attempt silent recovery so Settings / docs / email come back without
    # forcing the user to re-consent when the refresh token is still valid.
    if not (row and token_is_fresh(row)) and recoverable and recoverable.refresh_token:
        token, _err = get_valid_access_token(db)
        if token:
            row = latest_active_token(db)
    fresh = bool(row and token_is_fresh(row))
    return {
        "configured": configured,
        "connected": bool(row and row.status == "active"),
        "fresh": fresh,
        "needs_reauth": bool(
            not fresh
            and (
                (row and row.status in ("expired", "active"))
                or (recoverable and recoverable.status in ("expired", "revoked"))
            )
        ),
        "email": (row.ms_user_email if row else None)
        or (recoverable.ms_user_email if recoverable else None)
        or "",
        "drive_id": (row.drive_id if row else None) or "",
        "root_folder_id": (row.root_folder_id if row else None) or "",
        "token_id": row.id if row else None,
        "expires_at": row.expires_at if row else None,
        "status": (row.status if row else "none"),
        "client_mask": mask_client_id(),
        "secret_set": bool((_cfg.MS_GRAPH_CLIENT_SECRET or "").strip()),
        "redirect_uri": default_redirect_uri(),
    }


def probe_connection(db: Session) -> Dict[str, Any]:
    token, err = get_valid_access_token(db)
    if not token:
        return {"ok": False, "error": err or "Not connected"}
    ok_me, email, uid, me_err = fetch_me(token)
    if not ok_me:
        return {"ok": False, "error": me_err}
    ok_d, drive_id, d_err = fetch_drive(token)
    if not ok_d:
        return {"ok": False, "error": d_err, "email": email}
    row = latest_active_token(db)
    if row:
        row.ms_user_email = email or row.ms_user_email
        row.ms_user_id = uid or row.ms_user_id
        row.drive_id = drive_id or row.drive_id
        row.updated_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "email": email, "user_id": uid, "drive_id": drive_id}


def list_active_tokens(db: Session, limit: int = 10) -> List[MsGraphToken]:
    return (
        db.query(MsGraphToken)
        .filter(MsGraphToken.status == "active")
        .order_by(MsGraphToken.id.desc())
        .limit(limit)
        .all()
    )
