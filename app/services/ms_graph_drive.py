"""Microsoft Graph OneDrive file operations for Accologise Documents."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from app.config import GRAPH_API_BASE, MS_GRAPH_MAX_UPLOAD_MB
from app.models.document import CATEGORY_FOLDER
from app.services.ms_graph_oauth import get_valid_access_token, latest_active_token

logger = logging.getLogger("accountant_crm.ms_graph_drive")

# Root folder in the connected OneDrive (readable practice tree)
ROOT_FOLDER_NAME = "Accologise"
# Older installs used this name — still accepted when scanning
LEGACY_ROOT_FOLDER_NAMES = ("Accologise Documents",)
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MiB Graph simple upload limit


def _api_base() -> str:
    return (GRAPH_API_BASE or "https://graph.microsoft.com/v1.0").rstrip("/")


def sanitize_segment(name: str) -> str:
    """Make a single path segment safe for OneDrive."""
    s = (name or "").strip()
    s = re.sub(r'[\\/:*?"<>|]+', "-", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:120] or "Untitled"


def category_folder_name(category: str) -> str:
    return CATEGORY_FOLDER.get(category, sanitize_segment(category) or "Other")


def client_folder_slug(client) -> str:
    """
    Human-readable client folder: company / person name only.
    Easy to scan in OneDrive (no JOB- or cryptic refs).
    """
    name = ""
    if client is not None:
        if hasattr(client, "display_name"):
            try:
                name = client.display_name() or ""
            except Exception:
                name = ""
        if not name:
            name = (
                getattr(client, "company_name", None)
                or getattr(client, "name", None)
                or ""
            )
    base = sanitize_segment(name or "")
    if base:
        return base
    cid = getattr(client, "id", None) if client is not None else None
    return sanitize_segment(f"Client {cid}" if cid else "Client")


def job_folder_slug(job) -> str:
    """
    Human-readable job folder — type / title, not JOB-123.
    e.g. Accounts — 2025-03-31, Confirmation Statement — 2026-07-09
    """
    if job is None:
        return "Job"
    jtype = (getattr(job, "type", None) or "").strip()
    title = (getattr(job, "title", None) or "").strip()
    pe = getattr(job, "period_end", None)

    # Prefer full title when it already carries type + period
    from app.services.dates import uk_dates_in_text

    if title and (not jtype or jtype.lower() in title.lower() or "—" in title or "-" in title):
        base = uk_dates_in_text(title)
    elif jtype and pe is not None and hasattr(pe, "isoformat"):
        from app.services.dates import uk_date, uk_dates_in_text

        base = f"{jtype} — {uk_date(pe)}"
    elif jtype:
        base = jtype
    elif title:
        base = title
    else:
        jid = getattr(job, "id", None)
        base = f"Job {jid}" if jid else "Job"
    return sanitize_segment(base) or "Job"


def job_ref(job) -> str:
    """Legacy helper — prefer job_folder_slug for new paths."""
    return job_folder_slug(job)


def _request(
    method: str,
    path: str,
    access_token: str,
    *,
    data: Optional[bytes] = None,
    content_type: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Tuple[bool, Any, str, int]:
    url = path if path.startswith("http") else f"{_api_base()}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "AccologiseCRM/1.0 (MS-Graph-Drive)",
    }
    if content_type:
        headers["Content-Type"] = content_type
    elif data is not None and method.upper() in ("POST", "PUT", "PATCH"):
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    if data is None and method.upper() in ("GET", "DELETE"):
        headers.setdefault("Accept", "application/json")

    req = Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if not raw:
                return True, None, "", status
            if "application/json" in ctype or (
                raw[:1] in (b"{", b"[") and method.upper() != "GET"
            ):
                try:
                    return True, json.loads(raw.decode("utf-8")), "", status
                except json.JSONDecodeError:
                    return True, raw, "", status
            # binary (download)
            if method.upper() == "GET" and "json" not in ctype:
                return True, raw, "", status
            try:
                return True, json.loads(raw.decode("utf-8")), "", status
            except Exception:
                return True, raw, "", status
    except HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = str(exc)
        return False, None, f"HTTP {exc.code}: {err_body[:500]}", exc.code
    except (URLError, TimeoutError, OSError) as exc:
        return False, None, str(exc), 0


def get_token(db: Session) -> Tuple[Optional[str], Optional[str]]:
    return get_valid_access_token(db)


def ensure_child_folder(
    access_token: str, parent_id: str, name: str
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Get or create a child folder under parent_id."""
    safe = sanitize_segment(name)
    # Try get by path relative to parent
    path = f"/me/drive/items/{parent_id}:/{quote(safe)}:"
    ok, data, err, status = _request("GET", path, access_token)
    if ok and isinstance(data, dict) and data.get("id"):
        return data, ""
    if status not in (0, 404) and not ok:
        # might still be not found encoded differently — try create
        pass

    body = json.dumps(
        {
            "name": safe,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
    ).encode("utf-8")
    ok, data, err, status = _request(
        "POST",
        f"/me/drive/items/{parent_id}/children",
        access_token,
        data=body,
        content_type="application/json",
    )
    if ok and isinstance(data, dict) and data.get("id"):
        return data, ""
    # conflict: fetch again
    if status == 409 or (err and "nameAlreadyExists" in err):
        ok2, data2, err2, _ = _request("GET", path, access_token)
        if ok2 and isinstance(data2, dict) and data2.get("id"):
            return data2, ""
        return None, err2 or err or "Folder conflict"
    return None, err or "Could not create folder"


def ensure_root_folder(db: Session, access_token: str) -> Tuple[Optional[str], str]:
    """
    Ensure practice root under drive root; cache id on token row.

    Prefers ``Accologise``. If an older ``Accologise Documents`` folder already
    exists, reuses it so existing libraries keep working.
    """
    row = latest_active_token(db)
    if row and row.root_folder_id:
        ok, data, err, status = _request(
            "GET", f"/me/drive/items/{row.root_folder_id}", access_token
        )
        if ok and isinstance(data, dict) and data.get("id"):
            return data["id"], ""
        row.root_folder_id = None
        db.commit()

    candidates = (ROOT_FOLDER_NAME,) + tuple(LEGACY_ROOT_FOLDER_NAMES)
    for name in candidates:
        path = f"/me/drive/root:/{quote(name)}:"
        ok, data, err, status = _request("GET", path, access_token)
        if ok and isinstance(data, dict) and data.get("id"):
            if row:
                row.root_folder_id = data["id"]
                db.commit()
            return data["id"], ""

    # Create the new readable root
    body = json.dumps(
        {
            "name": ROOT_FOLDER_NAME,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
    ).encode("utf-8")
    ok, data, err, status = _request(
        "POST",
        "/me/drive/root/children",
        access_token,
        data=body,
        content_type="application/json",
    )
    if ok and isinstance(data, dict) and data.get("id"):
        if row:
            row.root_folder_id = data["id"]
            db.commit()
        return data["id"], ""
    if status == 409:
        path = f"/me/drive/root:/{quote(ROOT_FOLDER_NAME)}:"
        ok2, data2, err2, _ = _request("GET", path, access_token)
        if ok2 and isinstance(data2, dict) and data2.get("id"):
            if row:
                row.root_folder_id = data2["id"]
                db.commit()
            return data2["id"], ""
        return None, err2 or err
    return None, err or "Could not create Accologise root folder"


def ensure_folder_path(
    db: Session, access_token: str, segments: List[str]
) -> Tuple[Optional[str], str, str]:
    """
    Ensure ROOT / seg1 / seg2 / ... exists.
    Returns (folder_id, display_path, error).
    """
    root_id, err = ensure_root_folder(db, access_token)
    if not root_id:
        return None, "", err
    parent = root_id
    parts = [ROOT_FOLDER_NAME]
    for seg in segments:
        safe = sanitize_segment(seg)
        if not safe:
            continue
        item, err = ensure_child_folder(access_token, parent, safe)
        if not item:
            return None, " / ".join(parts), err
        parent = item["id"]
        parts.append(safe)
    return parent, " / ".join(parts), ""


def prospect_folder_slug(prospect) -> str:
    """Folder name for a prospect under Accologise / Prospects / …"""
    name = ""
    if prospect is not None:
        name = (
            getattr(prospect, "company_name", None)
            or getattr(prospect, "contact_name", None)
            or ""
        )
    if hasattr(prospect, "display_name") and callable(prospect.display_name):
        try:
            name = prospect.display_name() or name
        except Exception:
            pass
    slug = re.sub(r'[<>:"/\\|?*]+', " ", str(name or "Unnamed")).strip()
    slug = re.sub(r"\s+", " ", slug)[:80] or "Unnamed"
    return slug


def resolve_storage_folder(
    db: Session,
    access_token: str,
    *,
    client=None,
    job=None,
    prospect=None,
    category: str = "Other",
) -> Tuple[Optional[str], str, str]:
    """
    Readable OneDrive tree:

      Accologise / Clients / {Client Name} / {Job Name} [/ Category]
      Accologise / Prospects / {Prospect Name} [/ Category]

    Job Name is type/title (e.g. Accounts), never JOB-123.
    Returns (folder_id, path, error).
    """
    cat = category_folder_name(category)
    # Resolve client from job when needed
    if job is not None and client is None:
        client = getattr(job, "client", None)
        if client is None and getattr(job, "client_id", None):
            try:
                from app.models.client import Client

                client = (
                    db.query(Client).filter(Client.id == job.client_id).first()
                )
            except Exception:
                client = None

    if job is not None and client is not None:
        segments = ["Clients", client_folder_slug(client), job_folder_slug(job)]
        if cat and cat != "Other":
            segments.append(cat)
    elif client is not None:
        segments = ["Clients", client_folder_slug(client)]
        if cat and cat != "Other":
            segments.append(cat)
    elif prospect is not None:
        segments = ["Prospects", prospect_folder_slug(prospect)]
        if cat and cat != "Other":
            segments.append(cat)
    elif job is not None:
        # Job without client — still avoid JOB-id style
        segments = ["Clients", "Unassigned", job_folder_slug(job)]
        if cat and cat != "Other":
            segments.append(cat)
    else:
        segments = ["Other", cat]
    return ensure_folder_path(db, access_token, segments)


def list_children(
    access_token: str,
    folder_id: str,
    *,
    top: int = 200,
) -> Tuple[List[Dict[str, Any]], str]:
    """List immediate children of a drive folder (files + folders)."""
    items: List[Dict[str, Any]] = []
    page_size = max(1, min(int(top or 200), 200))
    url: Optional[str] = (
        f"/me/drive/items/{folder_id}/children"
        f"?$top={page_size}"
        f"&$select=id,name,size,file,folder,webUrl,eTag,cTag,lastModifiedDateTime"
    )
    while url:
        ok, data, err, _ = _request("GET", url, access_token)
        if not ok or not isinstance(data, dict):
            return items, err or "Could not list folder"
        for row in data.get("value") or []:
            if isinstance(row, dict):
                items.append(row)
        nxt = (data.get("@odata.nextLink") or "").strip()
        url = nxt if nxt else None
    return items, ""


def find_root_folder_id(db: Session, access_token: str) -> Tuple[Optional[str], str]:
    """Prefer new Accologise root; fall back to legacy Accologise Documents."""
    root_id, err = ensure_root_folder(db, access_token)
    if root_id:
        return root_id, ""
    for legacy in LEGACY_ROOT_FOLDER_NAMES:
        path = f"/me/drive/root:/{quote(legacy)}:"
        ok, data, _, _ = _request("GET", path, access_token)
        if ok and isinstance(data, dict) and data.get("id"):
            return data["id"], ""
    return None, err or "Practice root folder not found"


def _unique_name(filename: str) -> str:
    base = sanitize_segment(filename) or "file"
    # keep extension
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1]
        stem = sanitize_segment(filename.rsplit(".", 1)[0]) or "file"
        return f"{stem}.{ext.lower()}"
    return base


def upload_file(
    access_token: str,
    folder_id: str,
    filename: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Tuple[Optional[Dict[str, Any]], str]:
    max_bytes = max(1, int(MS_GRAPH_MAX_UPLOAD_MB or 25)) * 1024 * 1024
    if len(content) > max_bytes:
        return None, f"File exceeds maximum size of {MS_GRAPH_MAX_UPLOAD_MB} MB."

    name = _unique_name(filename)
    if len(content) <= SIMPLE_UPLOAD_LIMIT:
        path = f"/me/drive/items/{folder_id}:/{quote(name)}:/content"
        ok, data, err, _ = _request(
            "PUT",
            path,
            access_token,
            data=content,
            content_type=content_type or "application/octet-stream",
            extra_headers={"Content-Type": content_type or "application/octet-stream"},
        )
        if ok and isinstance(data, dict) and data.get("id"):
            return data, ""
        return None, err or "Upload failed"

    # Upload session for larger files
    body = json.dumps(
        {
            "item": {
                "@microsoft.graph.conflictBehavior": "rename",
                "name": name,
            }
        }
    ).encode("utf-8")
    ok, sess, err, _ = _request(
        "POST",
        f"/me/drive/items/{folder_id}:/{quote(name)}:/createUploadSession",
        access_token,
        data=body,
        content_type="application/json",
    )
    if not ok or not isinstance(sess, dict) or not sess.get("uploadUrl"):
        return None, err or "Could not start upload session"

    upload_url = sess["uploadUrl"]
    total = len(content)
    chunk = 320 * 1024 * 10  # 3.2 MiB
    start = 0
    last: Any = None
    while start < total:
        end = min(start + chunk, total) - 1
        piece = content[start : end + 1]
        headers = {
            "Content-Length": str(len(piece)),
            "Content-Range": f"bytes {start}-{end}/{total}",
        }
        req = Request(upload_url, data=piece, method="PUT", headers=headers)
        try:
            with urlopen(req, timeout=180) as resp:
                raw = resp.read()
                if raw:
                    try:
                        last = json.loads(raw.decode("utf-8"))
                    except Exception:
                        last = None
        except HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = str(exc)
            return None, f"Chunk upload HTTP {exc.code}: {err_body[:400]}"
        start = end + 1

    if isinstance(last, dict) and last.get("id"):
        return last, ""
    return None, "Upload session finished without item id"


def replace_file(
    access_token: str,
    item_id: str,
    content: bytes,
    content_type: str = "application/octet-stream",
) -> Tuple[Optional[Dict[str, Any]], str]:
    max_bytes = max(1, int(MS_GRAPH_MAX_UPLOAD_MB or 25)) * 1024 * 1024
    if len(content) > max_bytes:
        return None, f"File exceeds maximum size of {MS_GRAPH_MAX_UPLOAD_MB} MB."
    if len(content) > SIMPLE_UPLOAD_LIMIT:
        # For large replace, upload session to item
        body = json.dumps({"item": {"@microsoft.graph.conflictBehavior": "replace"}}).encode(
            "utf-8"
        )
        ok, sess, err, _ = _request(
            "POST",
            f"/me/drive/items/{item_id}/createUploadSession",
            access_token,
            data=body,
            content_type="application/json",
        )
        if not ok or not isinstance(sess, dict) or not sess.get("uploadUrl"):
            return None, err or "Could not start replace session"
        upload_url = sess["uploadUrl"]
        total = len(content)
        chunk = 320 * 1024 * 10
        start = 0
        last: Any = None
        while start < total:
            end = min(start + chunk, total) - 1
            piece = content[start : end + 1]
            req = Request(
                upload_url,
                data=piece,
                method="PUT",
                headers={
                    "Content-Length": str(len(piece)),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                },
            )
            try:
                with urlopen(req, timeout=180) as resp:
                    raw = resp.read()
                    if raw:
                        try:
                            last = json.loads(raw.decode("utf-8"))
                        except Exception:
                            last = None
            except HTTPError as exc:
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = str(exc)
                return None, f"Chunk replace HTTP {exc.code}: {err_body[:400]}"
            start = end + 1
        if isinstance(last, dict) and last.get("id"):
            return last, ""
        return None, "Replace session finished without item id"

    ok, data, err, _ = _request(
        "PUT",
        f"/me/drive/items/{item_id}/content",
        access_token,
        data=content,
        content_type=content_type or "application/octet-stream",
        extra_headers={"Content-Type": content_type or "application/octet-stream"},
    )
    if ok and isinstance(data, dict) and data.get("id"):
        return data, ""
    return None, err or "Replace failed"


def download_file(
    access_token: str, item_id: str
) -> Tuple[Optional[bytes], str, str]:
    """Returns (bytes, content_type, error)."""
    ok, data, err, status = _request(
        "GET",
        f"/me/drive/items/{item_id}/content",
        access_token,
        extra_headers={"Accept": "*/*"},
        timeout=180,
    )
    if ok and isinstance(data, (bytes, bytearray)):
        return bytes(data), "application/octet-stream", ""
    if ok and isinstance(data, dict):
        # sometimes metadata returned instead
        return None, "", "Unexpected JSON on download"
    return None, "", err or "Download failed"


def delete_file(access_token: str, item_id: str) -> Tuple[bool, str]:
    ok, _, err, status = _request("DELETE", f"/me/drive/items/{item_id}", access_token)
    if ok or status == 404:
        return True, ""
    return False, err or "Delete failed"


def get_item(access_token: str, item_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    ok, data, err, _ = _request("GET", f"/me/drive/items/{item_id}", access_token)
    if ok and isinstance(data, dict):
        return data, ""
    return None, err or "Item not found"


def create_preview(
    access_token: str, item_id: str
) -> Tuple[Optional[str], str]:
    """
    Graph file preview embed URL (Office Online / PDF viewer).

    POST /me/drive/items/{id}/preview → { getUrl }
    """
    ok, data, err, _ = _request(
        "POST",
        f"/me/drive/items/{item_id}/preview",
        access_token,
        data=b"{}",
        content_type="application/json",
    )
    if ok and isinstance(data, dict):
        url = (data.get("getUrl") or data.get("url") or "").strip()
        if url:
            return url, ""
        return None, "Preview response had no getUrl"
    return None, err or "Could not create OneDrive preview"


def create_view_link(
    access_token: str, item_id: str
) -> Tuple[Optional[str], str]:
    """
    Create a view link for the item (opens in browser without re-uploading).

    Tries organisation, then users, then anonymous (tenant policy dependent).
    """
    last_err = ""
    for scope in ("organization", "users", "anonymous"):
        body = json.dumps({"type": "view", "scope": scope}).encode("utf-8")
        ok, data, err, status = _request(
            "POST",
            f"/me/drive/items/{item_id}/createLink",
            access_token,
            data=body,
            content_type="application/json",
        )
        if ok and isinstance(data, dict):
            link = data.get("link") if isinstance(data.get("link"), dict) else {}
            url = (link.get("webUrl") or data.get("webUrl") or "").strip()
            if url:
                return url, ""
            last_err = "createLink returned no webUrl"
        else:
            last_err = err or f"createLink failed ({status})"
    return None, last_err or "Could not create view link"
