"""Microsoft Graph mail: send + move (archive) for practice emails / tasks."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import GRAPH_API_BASE, MS_GRAPH_MAIL_ARCHIVE_FOLDER

logger = logging.getLogger("accountant_crm.ms_graph_mail")


def _api_base() -> str:
    return (GRAPH_API_BASE or "https://graph.microsoft.com/v1.0").rstrip("/")


def _request(
    method: str,
    path: str,
    access_token: str,
    *,
    data: Optional[bytes] = None,
    content_type: str = "application/json",
    timeout: int = 60,
) -> Tuple[bool, Any, str, int]:
    url = path if path.startswith("http") else f"{_api_base()}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "AccologiseCRM/1.0 (MS-Graph-Mail)",
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = content_type
    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = resp.read()
            if not raw:
                return True, None, "", status
            try:
                return True, json.loads(raw.decode("utf-8")), "", status
            except json.JSONDecodeError:
                return True, raw, "", status
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        return False, None, f"HTTP {exc.code}: {err_body[:500]}", exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return False, None, str(exc), 0


def send_mail(
    access_token: str,
    *,
    to: str,
    subject: str,
    body: str,
    save_to_sent: bool = True,
) -> Tuple[bool, str]:
    """
    Send mail via Graph. Returns (ok, error_or_empty).
    Note: /me/sendMail does not return the created message id.
    """
    to = (to or "").strip()
    if not to:
        return False, "no_recipient_email"
    payload = {
        "message": {
            "subject": subject or "(no subject)",
            "body": {"contentType": "Text", "content": body or ""},
            "toRecipients": [
                {"emailAddress": {"address": to}},
            ],
        },
        "saveToSentItems": bool(save_to_sent),
    }
    ok, _data, err, status = _request(
        "POST",
        "/me/sendMail",
        access_token,
        data=json.dumps(payload).encode("utf-8"),
    )
    if ok or status in (202, 200):
        return True, ""
    return False, err or "sendMail failed"


def get_well_known_folder(
    access_token: str, name: str = "archive"
) -> Tuple[Optional[str], str]:
    """Return folder id for well-known name (archive, inbox, deleteditems, …)."""
    well = (name or "archive").strip().lower() or "archive"
    ok, data, err, _ = _request("GET", f"/me/mailFolders/{well}", access_token)
    if ok and isinstance(data, dict) and data.get("id"):
        return str(data["id"]), ""
    return None, err or f"Folder '{well}' not found"


def ensure_custom_folder(
    access_token: str, display_name: str = "Accologise Processed"
) -> Tuple[Optional[str], str]:
    """Get or create a top-level mail folder by display name."""
    name = (display_name or "Accologise Processed").strip()
    ok, data, err, _ = _request(
        "GET",
        "/me/mailFolders?$top=50",
        access_token,
    )
    if ok and isinstance(data, dict):
        for f in data.get("value") or []:
            if (f.get("displayName") or "").strip().lower() == name.lower():
                return str(f.get("id")), ""
    body = json.dumps({"displayName": name, "isHidden": False}).encode("utf-8")
    ok2, data2, err2, _ = _request(
        "POST",
        "/me/mailFolders",
        access_token,
        data=body,
    )
    if ok2 and isinstance(data2, dict) and data2.get("id"):
        return str(data2["id"]), ""
    return None, err2 or err or "Could not create mail folder"


def resolve_archive_folder_id(access_token: str) -> Tuple[Optional[str], str]:
    """
    Prefer well-known folder from config; if value is not a well-known name,
    treat as custom display name.
    """
    cfg = (MS_GRAPH_MAIL_ARCHIVE_FOLDER or "archive").strip()
    well_known = {
        "archive",
        "inbox",
        "deleteditems",
        "drafts",
        "sentitems",
        "junkemail",
        "outbox",
    }
    if cfg.lower() in well_known:
        return get_well_known_folder(access_token, cfg.lower())
    # Custom folder
    return ensure_custom_folder(access_token, cfg)


def move_message(
    access_token: str, message_id: str, destination_folder_id: str
) -> Tuple[bool, str]:
    mid = (message_id or "").strip()
    dest = (destination_folder_id or "").strip()
    if not mid or not dest:
        return False, "message_id and destination required"
    body = json.dumps({"destinationId": dest}).encode("utf-8")
    ok, _data, err, status = _request(
        "POST",
        f"/me/messages/{mid}/move",
        access_token,
        data=body,
    )
    if ok or status in (200, 201):
        return True, ""
    return False, err or "move failed"


def archive_message(access_token: str, message_id: str) -> Tuple[bool, str]:
    """Move message to configured archive / processed folder."""
    folder_id, err = resolve_archive_folder_id(access_token)
    if not folder_id:
        return False, err or "Archive folder unavailable"
    return move_message(access_token, message_id, folder_id)
