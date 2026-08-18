"""Microsoft Graph mail: send + move (archive) for practice emails / tasks."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
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


def outlook_deeplink_from_id(message_id: str) -> str:
    """Best-effort Outlook-on-the-web link from a Graph item id."""
    mid = (message_id or "").strip()
    if not mid:
        return ""
    return (
        "https://outlook.office.com/mail/deeplink/read/"
        + quote(mid, safe="")
    )


def get_message_web_link(
    access_token: str, message_id: str
) -> Tuple[str, str]:
    """
    Return (webLink, error). Tries Graph item id first, then internetMessageId.
    """
    mid = (message_id or "").strip()
    if not mid:
        return "", "no message id"
    token = (access_token or "").strip()
    if not token:
        return outlook_deeplink_from_id(mid) if "@" not in mid else "", "no token"

    looks_rfc = "@" in mid
    if not looks_rfc:
        ok, data, err, _ = _request(
            "GET",
            f"/me/messages/{quote(mid, safe='')}?$select=id,webLink",
            token,
        )
        if ok and isinstance(data, dict) and data.get("webLink"):
            return str(data["webLink"]).strip(), ""
        if ok and isinstance(data, dict) and data.get("id"):
            return outlook_deeplink_from_id(str(data["id"])), ""
        # Item id dies after a folder move — look in Archive / Processed
        for folder in ("archive", "inbox"):
            okf, dataf, _, _ = _request(
                "GET",
                f"/me/mailFolders/{folder}/messages/{quote(mid, safe='')}?$select=id,webLink",
                token,
            )
            if okf and isinstance(dataf, dict) and (dataf.get("webLink") or dataf.get("id")):
                return (
                    str(dataf.get("webLink") or "").strip()
                    or outlook_deeplink_from_id(str(dataf.get("id"))),
                    "",
                )

    # Search by RFC 822 Message-Id
    rfc = mid
    if not rfc.startswith("<") and "@" in rfc:
        rfc = f"<{rfc}>"
    rfc_esc = rfc.replace("'", "''")
    ok, data, err, _ = _request(
        "GET",
        "/me/messages?$top=1&$select=id,webLink"
        + f"&$filter=internetMessageId eq '{quote(rfc_esc, safe='')}'",
        token,
    )
    if ok and isinstance(data, dict):
        rows = data.get("value") or []
        if rows and isinstance(rows[0], dict):
            link = (rows[0].get("webLink") or "").strip()
            if link:
                return link, ""
            if rows[0].get("id"):
                return outlook_deeplink_from_id(str(rows[0]["id"])), ""
    if not looks_rfc:
        return outlook_deeplink_from_id(mid), ""
    return "", err or "message not found in mailbox"


def send_mail(
    access_token: str,
    *,
    to: str,
    subject: str,
    body: str,
    save_to_sent: bool = True,
    attachments: Optional[list] = None,
    reply_to: str = "",
    cc: Optional[list] = None,
) -> Tuple[bool, str]:
    """
    Send mail via Graph. Returns (ok, error_or_empty).
    Note: /me/sendMail does not return the created message id.

    attachments: optional list of dicts with keys:
      name (str), content (bytes), content_type (str, default application/pdf)
    """
    import base64

    to = (to or "").strip()
    if not to:
        return False, "no_recipient_email"
    message: Dict[str, Any] = {
        "subject": subject or "(no subject)",
        "body": {"contentType": "Text", "content": body or ""},
        "toRecipients": [
            {"emailAddress": {"address": to}},
        ],
    }
    reply = (reply_to or "").strip()
    if reply and "@" in reply:
        message["replyTo"] = [{"emailAddress": {"address": reply, "name": "Accology Payroll"}}]
    cc_rows = []
    for addr in cc or []:
        a = (addr or "").strip()
        if a and "@" in a and a.lower() != to.lower():
            cc_rows.append({"emailAddress": {"address": a}})
    if cc_rows:
        message["ccRecipients"] = cc_rows
    atts_out = []
    for att in attachments or []:
        if not isinstance(att, dict):
            continue
        raw = att.get("content")
        name = (att.get("name") or "attachment.bin").strip() or "attachment.bin"
        if raw is None:
            continue
        if isinstance(raw, str):
            raw_b = raw.encode("utf-8")
        else:
            raw_b = bytes(raw)
        # Graph fileAttachment limit ~3MB for simple contentBytes; warn but still try
        ctype = (att.get("content_type") or "application/octet-stream").strip()
        atts_out.append(
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": name[:200],
                "contentType": ctype,
                "contentBytes": base64.b64encode(raw_b).decode("ascii"),
            }
        )
    if atts_out:
        message["attachments"] = atts_out
    payload = {
        "message": message,
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


def send_mail_as(
    access_token: str,
    mailbox: str,
    *,
    to: str,
    subject: str,
    body: str,
    save_to_sent: bool = True,
    reply_to: str = "",
    cc: Optional[list] = None,
    attachments: Optional[list] = None,
) -> Tuple[bool, str]:
    """Send as a shared mailbox (Send As). Falls back to send_mail if mailbox is empty."""
    box = (mailbox or "").strip()
    if not box:
        return send_mail(
            access_token,
            to=to,
            subject=subject,
            body=body,
            save_to_sent=save_to_sent,
            reply_to=reply_to,
            cc=cc,
        )
    to = (to or "").strip()
    if not to:
        return False, "no_recipient_email"
    message: Dict[str, Any] = {
        "subject": subject or "(no subject)",
        "body": {"contentType": "Text", "content": body or ""},
        "toRecipients": [{"emailAddress": {"address": to}}],
        "from": {"emailAddress": {"address": box, "name": "Accology Pays"}},
    }
    reply = (reply_to or box).strip()
    if reply and "@" in reply:
        message["replyTo"] = [{"emailAddress": {"address": reply, "name": "Accology Payroll"}}]
    cc_rows = []
    for addr in cc or []:
        a = (addr or "").strip()
        if a and "@" in a and a.lower() != to.lower():
            cc_rows.append({"emailAddress": {"address": a}})
    if cc_rows:
        message["ccRecipients"] = cc_rows
    if attachments:
        import base64

        atts_out = []
        for att in attachments:
            if not isinstance(att, dict):
                continue
            raw = att.get("content")
            name = (att.get("name") or "attachment.bin").strip() or "attachment.bin"
            if raw is None:
                continue
            raw_b = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
            atts_out.append(
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": name[:200],
                    "contentType": (att.get("content_type") or "application/pdf").strip(),
                    "contentBytes": base64.b64encode(raw_b).decode("ascii"),
                }
            )
        if atts_out:
            message["attachments"] = atts_out
    payload = {"message": message, "saveToSentItems": bool(save_to_sent)}
    ok, _data, err, status = _request(
        "POST",
        f"/users/{box}/sendMail",
        access_token,
        data=json.dumps(payload).encode("utf-8"),
    )
    if ok or status in (202, 200):
        return True, ""
    # Shared send-as not granted — send from the signed-in mailbox with Reply-To.
    return send_mail(
        access_token,
        to=to,
        subject=subject,
        body=body,
        save_to_sent=save_to_sent,
        reply_to=reply or box,
        cc=cc,
        attachments=attachments,
    )


def create_outlook_draft(
    access_token: str,
    *,
    to: str,
    subject: str,
    body: str,
    reply_to: str = "",
    cc: Optional[list] = None,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    Create a draft in the signed-in mailbox for review/send in Outlook.
    Returns (message_dict_with_webLink, error).
    """
    to = (to or "").strip()
    if not to:
        return None, "no_recipient_email"
    payload: Dict[str, Any] = {
        "subject": subject or "(no subject)",
        "body": {"contentType": "Text", "content": body or ""},
        "toRecipients": [
            {"emailAddress": {"address": to}},
        ],
    }
    reply = (reply_to or "").strip()
    if reply and "@" in reply:
        payload["replyTo"] = [{"emailAddress": {"address": reply, "name": "Accology Payroll"}}]
    cc_rows = []
    for addr in cc or []:
        a = (addr or "").strip()
        if a and "@" in a and a.lower() != to.lower():
            cc_rows.append({"emailAddress": {"address": a}})
    if cc_rows:
        payload["ccRecipients"] = cc_rows
    ok, data, err, status = _request(
        "POST",
        "/me/messages",
        access_token,
        data=json.dumps(payload).encode("utf-8"),
    )
    if ok and isinstance(data, dict) and data.get("id"):
        return data, ""
    if status in (201, 200) and isinstance(data, dict):
        return data, ""
    return None, err or "create draft failed"


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
) -> Tuple[bool, str, Dict[str, str]]:
    """
    Move a message. Graph issues a *new* item id (and webLink) in the destination.
    Returns (ok, error, {id, webLink, internetMessageId}).
    """
    mid = (message_id or "").strip()
    dest = (destination_folder_id or "").strip()
    extra: Dict[str, str] = {}
    if not mid or not dest:
        return False, "message_id and destination required", extra
    body = json.dumps({"destinationId": dest}).encode("utf-8")
    ok, data, err, status = _request(
        "POST",
        f"/me/messages/{quote(mid, safe='')}/move",
        access_token,
        data=body,
    )
    if ok or status in (200, 201):
        if isinstance(data, dict):
            if data.get("id"):
                extra["id"] = str(data["id"])
            if data.get("webLink"):
                extra["webLink"] = str(data["webLink"]).strip()
            if data.get("internetMessageId"):
                extra["internetMessageId"] = str(data["internetMessageId"]).strip()
        return True, "", extra
    return False, err or "move failed", extra


def archive_message(
    access_token: str, message_id: str
) -> Tuple[bool, str, Dict[str, str]]:
    """Move message to configured archive / processed folder."""
    folder_id, err = resolve_archive_folder_id(access_token)
    if not folder_id:
        return False, err or "Archive folder unavailable", {}
    return move_message(access_token, message_id, folder_id)
