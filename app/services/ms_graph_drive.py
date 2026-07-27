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

ROOT_FOLDER_NAME = "Accologise Documents"
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
    name = ""
    if client is not None:
        if hasattr(client, "display_name"):
            name = client.display_name() or ""
        else:
            name = getattr(client, "company_name", None) or getattr(
                client, "name", ""
            ) or ""
        cn = (getattr(client, "company_number", None) or "").strip()
        cid = getattr(client, "id", None)
        if cn:
            return sanitize_segment(f"{name} – {cn}")
        if cid:
            return sanitize_segment(f"{name} – C{cid}")
    return sanitize_segment(name or "Client")


def job_ref(job) -> str:
    jid = getattr(job, "id", None) if job is not None else None
    if jid:
        return f"JOB-{jid}"
    return "JOB-unknown"


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
    """Ensure Accologise Documents under drive root; cache id on token row."""
    row = latest_active_token(db)
    if row and row.root_folder_id:
        # verify exists
        ok, data, err, status = _request(
            "GET", f"/me/drive/items/{row.root_folder_id}", access_token
        )
        if ok and isinstance(data, dict) and data.get("id"):
            return data["id"], ""
        # stale cache
        row.root_folder_id = None
        db.commit()

    # try by path
    path = f"/me/drive/root:/{quote(ROOT_FOLDER_NAME)}:"
    ok, data, err, status = _request("GET", path, access_token)
    if ok and isinstance(data, dict) and data.get("id"):
        if row:
            row.root_folder_id = data["id"]
            db.commit()
        return data["id"], ""

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
        ok2, data2, err2, _ = _request("GET", path, access_token)
        if ok2 and isinstance(data2, dict) and data2.get("id"):
            if row:
                row.root_folder_id = data2["id"]
                db.commit()
            return data2["id"], ""
        return None, err2 or err
    return None, err or "Could not create Accologise Documents folder"


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


def resolve_storage_folder(
    db: Session,
    access_token: str,
    *,
    client=None,
    job=None,
    category: str = "Other",
) -> Tuple[Optional[str], str, str]:
    """
    Jobs path when job set; else Clients path.
    Returns (folder_id, path, error).
    """
    cat = category_folder_name(category)
    if job is not None:
        segments = ["Jobs", job_ref(job), cat]
    elif client is not None:
        segments = ["Clients", client_folder_slug(client), cat]
    else:
        segments = ["Other", cat]
    return ensure_folder_path(db, access_token, segments)


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
