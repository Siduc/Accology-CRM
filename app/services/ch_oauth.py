"""Companies House OAuth 2.0 (authorization code + refresh) for API Filing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.config import (
    CH_OAUTH_API_BASE,
    CH_OAUTH_AUTHORISE_URL,
    CH_OAUTH_CLIENT_ID,
    CH_OAUTH_CLIENT_SECRET,
    CH_OAUTH_EXTRA_SCOPES,
    CH_OAUTH_IDENTITY_BASE,
    CH_OAUTH_REDIRECT_URI,
    CH_OAUTH_TOKEN_URL,
    SESSION_SECRET,
    ch_oauth_configured,
)
from app.models.ch_oauth_token import ChOAuthToken
from app.services.company_numbers import normalize_company_number

logger = logging.getLogger("accountant_crm.ch_oauth")

PROFILE_SCOPE = (
    "https://identity.company-information.service.gov.uk/user/profile.read"
)
# Documented company-scoped filing scope (exercises auth-code consent path).
# CS form submit is not public yet; ROA scope proves company-level OAuth.
DEFAULT_COMPANY_SCOPE_TMPL = (
    "https://api.company-information.service.gov.uk/company/"
    "{company_number}/registered-office-address.update"
)

STATE_MAX_AGE_SECONDS = 600
TOKEN_SKEW_SECONDS = 60
BODY_LOG_CAP = 4000
_EVENT_MAX = 40
_event_lock = threading.Lock()
_events: Deque[Dict[str, Any]] = deque(maxlen=_EVENT_MAX)


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
    http_status: int = 0
    response_body: str = ""
    response_content_type: str = ""


@dataclass
class ProfileResult:
    ok: bool
    email: str = ""
    user_id: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    http_status: int = 0
    response_body: str = ""


def oauth_is_ready() -> bool:
    return ch_oauth_configured()


def default_redirect_uri() -> str:
    return (CH_OAUTH_REDIRECT_URI or "").strip()


def is_loopback_redirect(uri: Optional[str] = None) -> bool:
    """
    CH / AWS edge returns bare 403 Forbidden when redirect_uri is loopback
    (http://127.0.0.1 or http://localhost). Public hostnames reach the app
    (e.g. 400 invalid_client) instead.
    """
    u = (uri if uri is not None else default_redirect_uri()).strip().lower()
    if not u:
        return False
    return (
        "://127.0.0.1" in u
        or "://localhost" in u
        or "://[::1]" in u
        or "://0.0.0.0" in u
    )


def redirect_uri_warning(uri: Optional[str] = None) -> str:
    if is_loopback_redirect(uri):
        return (
            "Companies House blocks OAuth when redirect_uri uses localhost / 127.0.0.1 "
            "(browser shows 403 Forbidden). Use a public HTTPS URL (e.g. cloudflared tunnel "
            "or your Render host) and register the exact same URI on the Developer Hub web client."
        )
    return ""


def mask_client_id(client_id: Optional[str] = None) -> str:
    cid = (client_id if client_id is not None else CH_OAUTH_CLIENT_ID) or ""
    cid = cid.strip()
    if not cid:
        return ""
    if len(cid) <= 8:
        return cid[:2] + "…"
    return cid[:4] + "…" + cid[-4:]


def mask_secret(value: Optional[str], *, head: int = 4, tail: int = 4) -> str:
    s = (value or "").strip()
    if not s:
        return "(empty)"
    if len(s) <= head + tail:
        return f"(len={len(s)})"
    return f"{s[:head]}…{s[-tail:]}(len={len(s)})"


def new_attempt_id() -> str:
    return secrets.token_hex(6)


def _cap_body(text: str, limit: int = BODY_LOG_CAP) -> str:
    t = text or ""
    if len(t) <= limit:
        return t
    return t[:limit] + f"…(+{len(t) - limit} chars)"


def sanitize_authorise_url(url: str) -> Dict[str, Any]:
    """
    Break an OAuth authorise URL into log-safe fields.
    Full state is replaced with length only (HMAC blob is large, not secret).
    """
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    parsed = urlparse(url or "")
    qs = parse_qs(parsed.query, keep_blank_values=True)
    flat: Dict[str, str] = {k: (v[0] if v else "") for k, v in qs.items()}
    state = flat.get("state") or ""
    safe_qs = {
        "response_type": flat.get("response_type") or "",
        "client_id": mask_client_id(flat.get("client_id")),
        "redirect_uri": flat.get("redirect_uri") or "",
        "scope": (flat.get("scope") or "")[:300],
        "state_len": str(len(state)),
    }
    # Rebuild debug URL with state redacted
    rebuild = []
    for k, v in flat.items():
        if k == "state":
            rebuild.append(("state", f"[redacted len={len(state)}]"))
        elif k == "client_id":
            rebuild.append((k, mask_client_id(v)))
        else:
            rebuild.append((k, v))
    debug_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",
            urlencode(rebuild),
            "",
        )
    )
    return {
        "authorise_url_base": f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
        "authorise_redirect_uri": safe_qs["redirect_uri"],
        "authorise_scope": safe_qs["scope"],
        "authorise_client_id": safe_qs["client_id"],
        "authorise_state_len": safe_qs["state_len"],
        "authorise_url_debug": debug_url,
    }


def _append_oauth_file_log(line: str) -> None:
    """Best-effort durable log under project logs/ch_oauth.log."""
    try:
        from app.config import BASE_DIR

        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "ch_oauth.log"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip() + "\n")
    except Exception:  # noqa: BLE001
        # Never break OAuth because logging failed
        pass


def log_event(
    step: str,
    *,
    attempt_id: str = "",
    level: int = logging.INFO,
    **fields: Any,
) -> Dict[str, Any]:
    """Structured OAuth log line + ring buffer for Settings UI + file append."""
    clean: Dict[str, Any] = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "step": step,
        "attempt_id": attempt_id or "",
    }
    for k, v in fields.items():
        if v is None:
            continue
        # never store secrets in the ring buffer
        kl = k.lower()
        if any(
            x in kl
            for x in (
                "client_secret",
                "access_token",
                "refresh_token",
                "authorization",
            )
        ):
            continue
        if kl in ("code", "auth_code") and isinstance(v, str) and len(v) > 8:
            clean[k] = mask_secret(v)
        elif kl == "state" and isinstance(v, str) and len(v) > 24:
            clean[k] = f"[redacted len={len(v)}]"
        elif isinstance(v, str) and len(v) > BODY_LOG_CAP:
            clean[k] = _cap_body(v)
        else:
            clean[k] = v

    parts = [f"CH_OAUTH step={step}"]
    if attempt_id:
        parts.append(f"attempt={attempt_id}")
    for k, v in clean.items():
        if k in ("ts", "step", "attempt_id"):
            continue
        parts.append(f"{k}={v}")
    line = " ".join(parts)
    logger.log(level, line)
    _append_oauth_file_log(f"{clean['ts']} {line}")

    with _event_lock:
        _events.appendleft(clean)
    return clean


def get_recent_events(limit: int = 25) -> List[Dict[str, Any]]:
    with _event_lock:
        return list(_events)[:limit]


def latest_oauth_summary() -> Dict[str, Any]:
    """One-liner for Settings home."""
    ev = get_recent_events(1)
    if not ev:
        return {"step": "", "attempt_id": "", "ts": "", "detail": ""}
    e = ev[0]
    detail_bits = []
    for k in (
        "error",
        "error_description",
        "http_status",
        "authorise_url_debug",
        "redirect_uri",
        "note",
    ):
        if e.get(k):
            detail_bits.append(f"{k}={e[k]}")
    return {
        "step": e.get("step") or "",
        "attempt_id": e.get("attempt_id") or "",
        "ts": e.get("ts") or "",
        "detail": " · ".join(detail_bits)[:240],
    }


def diagnose_stu_from_events(events: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Interpret ring buffer for 'service temporarily unavailable' style failures:
    redirect to CH with no subsequent callback.
    """
    ev = events if events is not None else get_recent_events(40)
    last_start = next((e for e in ev if e.get("step") == "redirect_to_ch"), None)
    last_cb = next(
        (e for e in ev if str(e.get("step") or "").startswith("callback")), None
    )
    last_token_fail = next(
        (e for e in ev if e.get("step") in ("token_exchange_error", "token_exchange_fail")),
        None,
    )
    no_callback = bool(last_start) and (
        not last_cb
        or (last_start.get("ts") or "") > (last_cb.get("ts") or "")
    )
    return {
        "likely_ch_side_stu": no_callback,
        "last_start": last_start,
        "last_callback": last_cb,
        "last_token_fail": last_token_fail,
        "message": (
            "Connect started and Accologise redirected you to Companies House, but "
            "no callback hit this app. “Service temporarily unavailable” is usually "
            "on CH / GOV.UK One Login, or the tunnel hostname no longer matches "
            "CH_OAUTH_REDIRECT_URI."
            if no_callback
            else (
                "A callback was received — check token/profile events for the exact "
                "Companies House response body."
                if last_cb
                else "No OAuth attempts logged yet in this server process."
            )
        ),
    }


def build_scopes(company_number: Optional[str] = None) -> str:
    scopes: List[str] = [PROFILE_SCOPE]
    cn = normalize_company_number(company_number or "") or ""
    if cn:
        tmpl = (CH_OAUTH_EXTRA_SCOPES or "").strip() or DEFAULT_COMPANY_SCOPE_TMPL
        for part in tmpl.split():
            scopes.append(part.replace("{company_number}", cn))
    elif (CH_OAUTH_EXTRA_SCOPES or "").strip() and "{company_number}" not in (
        CH_OAUTH_EXTRA_SCOPES or ""
    ):
        scopes.extend((CH_OAUTH_EXTRA_SCOPES or "").split())
    # de-dupe preserve order
    seen = set()
    out = []
    for s in scopes:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return " ".join(out)


def sign_state(
    *,
    crm_client_id: Optional[int] = None,
    pack_id: Optional[int] = None,
    return_to: str = "",
    company_number: str = "",
    attempt_id: str = "",
) -> str:
    """HMAC-signed state so callback works even if session cookie is dropped."""
    payload = {
        "n": secrets.token_urlsafe(16),
        "exp": int(
            (datetime.utcnow() + timedelta(seconds=STATE_MAX_AGE_SECONDS)).timestamp()
        ),
        "cid": crm_client_id,
        "pid": pack_id,
        "cn": company_number or "",
        "ret": (return_to or "")[:500],
        "aid": (attempt_id or "")[:32],
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(
        (SESSION_SECRET or "dev").encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}.{sig}"


def parse_state(state: str) -> Tuple[bool, Dict[str, Any], str]:
    if not state or "." not in state:
        return False, {}, "Invalid state."
    body, sig = state.rsplit(".", 1)
    expected = hmac.new(
        (SESSION_SECRET or "dev").encode("utf-8"),
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


def build_authorise_url(
    *,
    state: str,
    company_number: Optional[str] = None,
    redirect_uri: Optional[str] = None,
) -> str:
    if not oauth_is_ready():
        raise RuntimeError("Companies House OAuth is not configured.")
    params = {
        "response_type": "code",
        "client_id": (CH_OAUTH_CLIENT_ID or "").strip(),
        "redirect_uri": (redirect_uri or default_redirect_uri()).strip(),
        "scope": build_scopes(company_number),
        "state": state,
    }
    base = (CH_OAUTH_AUTHORISE_URL or "").strip()
    return f"{base}?{urlencode(params)}"


def _post_token(
    form: Dict[str, str], *, attempt_id: str = "", step: str = "token_exchange"
) -> TokenResult:
    url = (CH_OAUTH_TOKEN_URL or "").strip()
    if not url:
        log_event(
            f"{step}_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            error="Token URL not configured",
        )
        return TokenResult(ok=False, error="Token URL not configured.")

    # Log request meta only (never secret / code)
    log_event(
        f"{step}_request",
        attempt_id=attempt_id,
        token_url=url,
        grant_type=form.get("grant_type") or "",
        redirect_uri=form.get("redirect_uri") or "",
        client_id=mask_client_id(form.get("client_id")),
        has_code=bool(form.get("code")),
        has_refresh=bool(form.get("refresh_token")),
        has_secret=bool(form.get("client_secret")),
    )

    body = urlencode(form).encode("utf-8")
    req = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "AccologiseCRM/1.0 (CH-OAuth)",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type") or ""
            status = getattr(resp, "status", 200) or 200
            data = json.loads(raw_text) if raw_text else {}
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        ctype = ""
        try:
            ctype = exc.headers.get("Content-Type") or "" if exc.headers else ""
        except Exception:
            pass
        log_event(
            f"{step}_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            http_status=exc.code,
            content_type=ctype,
            body=_cap_body(err_body),
        )
        return TokenResult(
            ok=False,
            error=f"Token endpoint HTTP {exc.code}: {_cap_body(err_body, 500)}",
            http_status=exc.code,
            response_body=_cap_body(err_body),
            response_content_type=ctype,
        )
    except (URLError, TimeoutError, OSError) as exc:
        log_event(
            f"{step}_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            error=f"network: {exc}",
        )
        return TokenResult(ok=False, error=f"Token request failed: {exc}")
    except json.JSONDecodeError as exc:
        log_event(
            f"{step}_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            http_status=status if "status" in dir() else 0,
            error=f"invalid JSON: {exc}",
            body=_cap_body(raw_text if "raw_text" in dir() else ""),
        )
        return TokenResult(
            ok=False,
            error=f"Token response not JSON: {exc}",
            response_body=_cap_body(raw_text if "raw_text" in dir() else ""),
        )

    access = (data.get("access_token") or "").strip()
    if not access:
        err = (
            data.get("error_description")
            or data.get("error")
            or "No access_token in response."
        )
        log_event(
            f"{step}_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            http_status=status,
            content_type=ctype,
            body=_cap_body(raw_text),
            error=err,
        )
        return TokenResult(
            ok=False,
            error=str(err),
            raw=data if isinstance(data, dict) else {},
            http_status=status,
            response_body=_cap_body(raw_text),
            response_content_type=ctype,
        )
    expires_raw = data.get("expires_in") or 0
    try:
        expires_in = int(expires_raw)
    except (TypeError, ValueError):
        expires_in = 3600
    log_event(
        f"{step}_ok",
        attempt_id=attempt_id,
        http_status=status,
        expires_in=expires_in,
        has_refresh=bool((data.get("refresh_token") or "").strip()),
        scope=(data.get("scope") or "")[:200],
    )
    return TokenResult(
        ok=True,
        access_token=access,
        refresh_token=(data.get("refresh_token") or "").strip(),
        token_type=(data.get("token_type") or "Bearer").strip() or "Bearer",
        expires_in=expires_in,
        scope=(data.get("scope") or "").strip(),
        raw=data if isinstance(data, dict) else {},
        http_status=status,
        response_body="",  # never keep tokens in body field
        response_content_type=ctype,
    )


def exchange_code(
    code: str, *, redirect_uri: Optional[str] = None, attempt_id: str = ""
) -> TokenResult:
    if not oauth_is_ready():
        return TokenResult(ok=False, error="OAuth not configured.")
    if not (code or "").strip():
        return TokenResult(ok=False, error="Missing authorisation code.")
    return _post_token(
        {
            "grant_type": "authorization_code",
            "code": code.strip(),
            "client_id": (CH_OAUTH_CLIENT_ID or "").strip(),
            "client_secret": (CH_OAUTH_CLIENT_SECRET or "").strip(),
            "redirect_uri": (redirect_uri or default_redirect_uri()).strip(),
        },
        attempt_id=attempt_id,
        step="token_exchange",
    )


def refresh_access_token(
    refresh_token: str, *, attempt_id: str = ""
) -> TokenResult:
    if not oauth_is_ready():
        return TokenResult(ok=False, error="OAuth not configured.")
    if not (refresh_token or "").strip():
        return TokenResult(ok=False, error="Missing refresh token.")
    return _post_token(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token.strip(),
            "client_id": (CH_OAUTH_CLIENT_ID or "").strip(),
            "client_secret": (CH_OAUTH_CLIENT_SECRET or "").strip(),
        },
        attempt_id=attempt_id,
        step="token_refresh",
    )


def fetch_user_profile(
    access_token: str, *, attempt_id: str = ""
) -> ProfileResult:
    base = (CH_OAUTH_IDENTITY_BASE or "").rstrip("/")
    url = f"{base}/user/profile"
    log_event("profile_request", attempt_id=attempt_id, url=url)
    req = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "AccologiseCRM/1.0 (CH-OAuth)",
        },
    )
    try:
        with urlopen(req, timeout=20) as resp:
            raw_text = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200) or 200
            data = json.loads(raw_text) if raw_text else {}
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        log_event(
            "profile_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            http_status=exc.code,
            body=_cap_body(err_body),
        )
        return ProfileResult(
            ok=False,
            error=f"Profile HTTP {exc.code}: {_cap_body(err_body, 300)}",
            http_status=exc.code,
            response_body=_cap_body(err_body),
        )
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        log_event(
            "profile_error",
            attempt_id=attempt_id,
            level=logging.ERROR,
            error=str(exc),
        )
        return ProfileResult(ok=False, error=str(exc))

    email = (
        data.get("email")
        or data.get("email_address")
        or (data.get("profile") or {}).get("email")
        or ""
    )
    uid = str(data.get("id") or data.get("user_id") or data.get("sub") or "")
    log_event(
        "profile_ok",
        attempt_id=attempt_id,
        http_status=status,
        has_email=bool(email),
        user_id=uid[:40] if uid else "",
    )
    return ProfileResult(
        ok=True,
        email=str(email or ""),
        user_id=uid,
        raw=data if isinstance(data, dict) else {},
        http_status=status,
    )



def _http_get_probe(url: str, *, timeout: int = 20) -> Dict[str, Any]:
    """GET without following redirects; capture status, location, body."""
    import urllib.request as _ur

    class _NoRedirect(_ur.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N803
            return None

    req = Request(
        url,
        method="GET",
        headers={
            "User-Agent": "AccologiseCRM/1.0 (CH-OAuth-Probe)",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        },
    )
    try:
        opener = _ur.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "http_status": getattr(resp, "status", 200) or 200,
                    "location": resp.headers.get("Location") or "",
                    "content_type": resp.headers.get("Content-Type") or "",
                    "body": _cap_body(body, 1500),
                }
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            loc = ""
            try:
                loc = exc.headers.get("Location") or "" if exc.headers else ""
            except Exception:
                pass
            return {
                "ok": 300 <= int(exc.code or 0) < 400,
                "http_status": exc.code,
                "location": loc,
                "content_type": (
                    (exc.headers.get("Content-Type") if exc.headers else "") or ""
                ),
                "body": _cap_body(body, 1500),
            }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "http_status": 0,
            "location": "",
            "content_type": "",
            "body": "",
            "error": str(exc),
        }


def probe_oauth_configuration() -> Dict[str, Any]:
    """Server-side diagnostics: config + CH authorise + redirect health + token ping."""
    attempt_id = new_attempt_id()
    redir = default_redirect_uri()
    result: Dict[str, Any] = {
        "attempt_id": attempt_id,
        "configured": oauth_is_ready(),
        "client_id_mask": mask_client_id(),
        "secret_set": bool((CH_OAUTH_CLIENT_SECRET or "").strip()),
        "redirect_uri": redir,
        "loopback": is_loopback_redirect(redir),
        "authorise_url": (CH_OAUTH_AUTHORISE_URL or "").strip(),
        "token_url": (CH_OAUTH_TOKEN_URL or "").strip(),
        "authorise": {},
        "redirect_health": {},
        "token_ping": {},
        "interpretation": [],
    }
    log_event(
        "probe_start",
        attempt_id=attempt_id,
        client_id=mask_client_id(),
        redirect_uri=redir,
        loopback=is_loopback_redirect(redir),
    )

    if not oauth_is_ready():
        result["interpretation"].append(
            "OAuth not configured (missing client id/secret)."
        )
        log_event(
            "probe_done", attempt_id=attempt_id, ok=False, reason="not_configured"
        )
        return result

    if is_loopback_redirect(redir):
        result["interpretation"].append(
            "Redirect URI is loopback — CH returns 403 Forbidden in the browser."
        )

    state = sign_state(**{"return_to": "/settings", "attempt_id": attempt_id})
    try:
        auth_url = build_authorise_url(state=state)
    except RuntimeError as exc:
        result["interpretation"].append(str(exc))
        log_event("probe_done", attempt_id=attempt_id, ok=False, error=str(exc))
        return result

    auth = _http_get_probe(auth_url)
    result["authorise"] = {
        "http_status": auth.get("http_status"),
        "location": (auth.get("location") or "")[:300],
        "content_type": auth.get("content_type") or "",
        "body": auth.get("body") or "",
        "error": auth.get("error") or "",
        "url_host": urlparse(auth_url).netloc,
    }
    log_event(
        "probe_authorise",
        attempt_id=attempt_id,
        level=logging.INFO
        if auth.get("http_status") in (302, 303)
        else logging.WARNING,
        http_status=auth.get("http_status"),
        location=(auth.get("location") or "")[:200],
        body=_cap_body(auth.get("body") or "", 800),
        error=auth.get("error") or "",
    )

    st = int(auth.get("http_status") or 0)
    body_l = (auth.get("body") or "").lower()
    if st in (302, 303):
        result["interpretation"].append(
            f"Authorise probe: OK (HTTP {st} redirect). Server can reach CH identity."
        )
    elif st == 403:
        result["interpretation"].append(
            "Authorise probe: 403 Forbidden — usually loopback redirect_uri."
        )
    elif st == 400:
        result["interpretation"].append(
            "Authorise probe: 400 — check web client_id and redirect_uri on Hub. Body: "
            + _cap_body(auth.get("body") or "", 200)
        )
    elif (
        st >= 500
        or "temporarily unavailable" in body_l
        or "service unavailable" in body_l
    ):
        result["interpretation"].append(
            "Authorise probe: CH appears unavailable (5xx / STU). Matches browser "
            "service temporarily unavailable."
        )
    elif auth.get("error"):
        result["interpretation"].append(
            f"Authorise probe network error: {auth.get('error')}"
        )
    else:
        result["interpretation"].append(
            f"Authorise probe: HTTP {st}. Body: {_cap_body(auth.get('body') or '', 200)}"
        )

    try:
        parsed = urlparse(redir)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        health_url = origin.rstrip("/") + "/health"
        health: Dict[str, Any]
        try:
            with urlopen(
                Request(
                    health_url,
                    headers={"User-Agent": "AccologiseCRM/1.0"},
                ),
                timeout=12,
            ) as r:
                hb = r.read().decode("utf-8", errors="replace")
                health = {
                    "http_status": getattr(r, "status", 200) or 200,
                    "body": hb,
                    "error": "",
                }
        except Exception as e:  # noqa: BLE001
            health = {"http_status": 0, "body": "", "error": str(e)}
        result["redirect_health"] = {
            "url": health_url,
            "http_status": health.get("http_status"),
            "body": _cap_body(health.get("body") or "", 300),
            "error": health.get("error") or "",
        }
        log_event(
            "probe_redirect_health",
            attempt_id=attempt_id,
            url=health_url,
            http_status=health.get("http_status"),
            body=_cap_body(health.get("body") or "", 200),
            error=health.get("error") or "",
        )
        hb = health.get("body") or ""
        if health.get("http_status") == 200 and "ok" in hb.lower():
            result["interpretation"].append(
                "Redirect host /health: OK — tunnel or host reaches Accologise."
            )
        else:
            result["interpretation"].append(
                "Redirect host /health failed — tunnel down or hostname changed. "
                f"status={health.get('http_status')} err={health.get('error') or 'n/a'}"
            )
    except Exception as exc:  # noqa: BLE001
        result["redirect_health"] = {"error": str(exc)}
        result["interpretation"].append(f"Redirect health check error: {exc}")

    ping = _post_token(
        {
            "grant_type": "authorization_code",
            "code": "accologise-probe-invalid",
            "client_id": (CH_OAUTH_CLIENT_ID or "").strip(),
            "client_secret": (CH_OAUTH_CLIENT_SECRET or "").strip(),
            "redirect_uri": redir,
        },
        attempt_id=attempt_id,
        step="token_ping",
    )
    result["token_ping"] = {
        "ok_expected_fail": not ping.ok,
        "http_status": ping.http_status,
        "error": ping.error,
        "body": _cap_body(ping.response_body, 800),
    }
    if ping.http_status == 400 or (
        ping.error and "invalid" in (ping.error or "").lower()
    ):
        result["interpretation"].append(
            "Token endpoint: reachable (rejected probe code as expected)."
        )
    elif ping.http_status >= 500 or "unavailable" in (
        ping.response_body or ""
    ).lower() or "unavailable" in (ping.error or "").lower():
        result["interpretation"].append(
            "Token endpoint: service error / unavailable — CH-side issue."
        )
    elif ping.http_status == 0:
        result["interpretation"].append(
            f"Token endpoint: network failure — {ping.error}"
        )
    else:
        result["interpretation"].append(
            f"Token endpoint: HTTP {ping.http_status}: {_cap_body(ping.error, 200)}"
        )

    log_event(
        "probe_done",
        attempt_id=attempt_id,
        authorise_status=result["authorise"].get("http_status"),
        health_status=(result.get("redirect_health") or {}).get("http_status"),
        token_status=ping.http_status,
    )
    return result



def save_token(
    db: Session,
    result: TokenResult,
    *,
    crm_client_id: Optional[int] = None,
    company_number: Optional[str] = None,
    scope_requested: str = "",
) -> ChOAuthToken:
    cn = normalize_company_number(company_number or "") or None
    expires_at = None
    if result.expires_in:
        expires_at = datetime.utcnow() + timedelta(seconds=int(result.expires_in))

    profile = (
        fetch_user_profile(result.access_token)
        if result.access_token
        else None
    )

    # Reuse active row for same client/company when present
    q = db.query(ChOAuthToken).filter(ChOAuthToken.status == "active")
    if crm_client_id:
        q = q.filter(ChOAuthToken.client_id == crm_client_id)
    else:
        q = q.filter(ChOAuthToken.client_id.is_(None))
    if cn:
        q = q.filter(ChOAuthToken.company_number == cn)
    else:
        q = q.filter(ChOAuthToken.company_number.is_(None))
    row = q.order_by(ChOAuthToken.id.desc()).first()
    if not row:
        row = ChOAuthToken(client_id=crm_client_id, company_number=cn)
        db.add(row)

    row.access_token = result.access_token
    if result.refresh_token:
        row.refresh_token = result.refresh_token
    row.token_type = result.token_type or "Bearer"
    row.expires_at = expires_at
    row.scope = result.scope or scope_requested or row.scope
    row.status = "active"
    row.updated_at = datetime.utcnow()
    if profile and profile.ok:
        if profile.email:
            row.ch_user_email = profile.email
        if profile.user_id:
            row.ch_user_id = profile.user_id
    db.commit()
    db.refresh(row)
    return row


def revoke_local(
    db: Session,
    token_id: Optional[int] = None,
    *,
    crm_client_id: Optional[int] = None,
) -> int:
    q = db.query(ChOAuthToken).filter(ChOAuthToken.status == "active")
    if token_id:
        q = q.filter(ChOAuthToken.id == token_id)
    elif crm_client_id is not None:
        q = q.filter(ChOAuthToken.client_id == crm_client_id)
    else:
        return 0
    n = 0
    for row in q.all():
        row.status = "revoked"
        row.updated_at = datetime.utcnow()
        n += 1
    if n:
        db.commit()
    return n


def latest_token_for_client(
    db: Session, crm_client_id: int
) -> Optional[ChOAuthToken]:
    return (
        db.query(ChOAuthToken)
        .filter(ChOAuthToken.client_id == crm_client_id)
        .filter(ChOAuthToken.status == "active")
        .order_by(ChOAuthToken.id.desc())
        .first()
    )


def latest_practice_token(db: Session) -> Optional[ChOAuthToken]:
    return (
        db.query(ChOAuthToken)
        .filter(ChOAuthToken.client_id.is_(None))
        .filter(ChOAuthToken.status == "active")
        .order_by(ChOAuthToken.id.desc())
        .first()
    )


def list_active_tokens(db: Session, limit: int = 20) -> List[ChOAuthToken]:
    return (
        db.query(ChOAuthToken)
        .filter(ChOAuthToken.status == "active")
        .order_by(ChOAuthToken.updated_at.desc())
        .limit(limit)
        .all()
    )


def token_is_fresh(row: ChOAuthToken) -> bool:
    if not row or row.status != "active" or not row.access_token:
        return False
    if not row.expires_at:
        return True
    return row.expires_at > datetime.utcnow() + timedelta(seconds=TOKEN_SKEW_SECONDS)


def get_valid_access_token(
    db: Session,
    *,
    crm_client_id: Optional[int] = None,
    company_number: Optional[str] = None,
) -> Tuple[Optional[str], Optional[ChOAuthToken], str]:
    """
    Return (access_token, row, error).
    Prefers client-linked token; falls back to company_number match.
    """
    row: Optional[ChOAuthToken] = None
    if crm_client_id:
        row = latest_token_for_client(db, crm_client_id)
    if not row and company_number:
        cn = normalize_company_number(company_number) or ""
        if cn:
            row = (
                db.query(ChOAuthToken)
                .filter(ChOAuthToken.company_number == cn)
                .filter(ChOAuthToken.status == "active")
                .order_by(ChOAuthToken.id.desc())
                .first()
            )
    if not row:
        return None, None, "No active Companies House OAuth token for this company."

    if token_is_fresh(row):
        return row.access_token, row, ""

    if not row.refresh_token:
        row.status = "expired"
        row.updated_at = datetime.utcnow()
        db.commit()
        return None, row, "Access token expired and no refresh token is stored."

    refreshed = refresh_access_token(row.refresh_token)
    if not refreshed.ok:
        return None, row, refreshed.error or "Token refresh failed."

    row.access_token = refreshed.access_token
    if refreshed.refresh_token:
        row.refresh_token = refreshed.refresh_token
    if refreshed.expires_in:
        row.expires_at = datetime.utcnow() + timedelta(seconds=int(refreshed.expires_in))
    if refreshed.scope:
        row.scope = refreshed.scope
    row.status = "active"
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return row.access_token, row, ""


def api_base() -> str:
    return (CH_OAUTH_API_BASE or "").rstrip("/")
