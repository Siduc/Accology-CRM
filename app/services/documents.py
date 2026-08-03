"""Document management — metadata in CRM, bytes in OneDrive."""

from __future__ import annotations

import mimetypes
import os
import re
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.client import Client
from app.models.document import (
    ALLOWED_EXTENSIONS,
    CATEGORY_FOLDER,
    DOCUMENT_CATEGORIES,
    Document,
    DocumentVersion,
)
from app.models.job import Job
from app.models.prospecting import Prospect
from app.services import ms_graph_drive as drive
from app.services.ms_graph_oauth import connection_status, get_valid_access_token


def list_documents(
    db: Session,
    *,
    q: str = "",
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    prospect_id: Optional[int] = None,
    category: str = "",
    is_key: Optional[bool] = None,
    limit: int = 200,
) -> List[Document]:
    query = (
        db.query(Document)
        .options(joinedload(Document.client), joinedload(Document.job))
        .filter(Document.status == "active")
    )
    if client_id:
        query = query.filter(Document.client_id == client_id)
    if job_id:
        query = query.filter(Document.job_id == job_id)
    if prospect_id:
        query = query.filter(Document.prospect_id == prospect_id)
    if category and category in DOCUMENT_CATEGORIES:
        query = query.filter(Document.category == category)
    if is_key is True:
        query = query.filter(Document.is_key.is_(True))
    qq = (q or "").strip()
    if qq:
        like = f"%{qq}%"
        query = query.outerjoin(Client, Document.client_id == Client.id)
        query = query.filter(
            or_(
                Document.title.ilike(like),
                Document.description.ilike(like),
                Document.tags.ilike(like),
                Document.original_filename.ilike(like),
                Client.company_name.ilike(like),
            )
        )
    return (
        query.order_by(Document.is_key.desc(), Document.uploaded_at.desc())
        .limit(limit)
        .all()
    )


def get_document(db: Session, doc_id: int) -> Optional[Document]:
    return (
        db.query(Document)
        .options(
            joinedload(Document.client),
            joinedload(Document.job),
            joinedload(Document.versions),
        )
        .filter(Document.id == doc_id, Document.status == "active")
        .first()
    )


def _guess_content_type(filename: str, declared: Optional[str] = None) -> str:
    if declared and declared != "application/octet-stream":
        return declared
    ct, _ = mimetypes.guess_type(filename or "")
    return ct or "application/octet-stream"


def _validate_upload(filename: str, size: int) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return (
            f"File type '{ext or 'unknown'}' is not allowed. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
    from app.config import MS_GRAPH_MAX_UPLOAD_MB

    max_b = max(1, int(MS_GRAPH_MAX_UPLOAD_MB or 25)) * 1024 * 1024
    if size > max_b:
        return f"File exceeds maximum size of {MS_GRAPH_MAX_UPLOAD_MB} MB."
    if size <= 0:
        return "Empty file."
    return None


def create_document(
    db: Session,
    *,
    filename: str,
    content: bytes,
    content_type: str = "",
    title: str = "",
    description: str = "",
    tags: str = "",
    category: str = "Other",
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    prospect_id: Optional[int] = None,
    is_key: bool = False,
    uploaded_by: str = "",
) -> Tuple[Optional[Document], str]:
    if not client_id and not job_id and not prospect_id:
        return None, "Link the document to a client, job, or prospect."

    verr = _validate_upload(filename, len(content))
    if verr:
        return None, verr

    cat = category if category in DOCUMENT_CATEGORIES else "Other"
    token, err = get_valid_access_token(db)
    if not token:
        return None, err or "Microsoft OneDrive is not connected."

    client = (
        db.query(Client).filter(Client.id == client_id).first() if client_id else None
    )
    job = db.query(Job).filter(Job.id == job_id).first() if job_id else None
    prospect = (
        db.query(Prospect).filter(Prospect.id == prospect_id).first()
        if prospect_id
        else None
    )
    if job_id and not job:
        return None, "Job not found."
    if client_id and not client:
        return None, "Client not found."
    if prospect_id and not prospect:
        return None, "Prospect not found."
    # Prefer job's client if missing
    if job and not client and job.client_id:
        client = job.client or db.query(Client).filter(Client.id == job.client_id).first()
        client_id = client.id if client else client_id

    folder_id, folder_path, ferr = drive.resolve_storage_folder(
        db, token, client=client, job=job, prospect=prospect, category=cat
    )
    if not folder_id:
        return None, ferr or "Could not prepare OneDrive folder."

    ct = _guess_content_type(filename, content_type)
    item, uerr = drive.upload_file(token, folder_id, filename, content, ct)
    if not item:
        return None, uerr or "Upload to OneDrive failed."

    display_title = (title or "").strip() or os.path.splitext(filename)[0] or filename
    path = f"{folder_path} / {item.get('name') or filename}"
    doc = Document(
        title=display_title[:240],
        description=(description or "").strip() or None,
        tags=(tags or "").strip() or None,
        category=cat,
        client_id=client_id,
        job_id=job_id,
        prospect_id=prospect_id,
        is_key=bool(is_key),
        original_filename=filename,
        content_type=ct,
        size_bytes=int(item.get("size") or len(content)),
        onedrive_item_id=str(item.get("id") or ""),
        onedrive_path=path,
        onedrive_web_url=item.get("webUrl"),
        onedrive_etag=item.get("eTag") or item.get("cTag"),
        version=1,
        uploaded_by=(uploaded_by or "").strip() or None,
        uploaded_at=datetime.utcnow(),
        status="active",
    )
    db.add(doc)
    db.flush()
    db.add(
        DocumentVersion(
            document_id=doc.id,
            version=1,
            onedrive_item_id=doc.onedrive_item_id,
            original_filename=filename,
            size_bytes=doc.size_bytes,
            content_type=ct,
            uploaded_by=doc.uploaded_by,
            uploaded_at=doc.uploaded_at,
            note="Initial upload",
        )
    )
    db.commit()
    db.refresh(doc)
    return doc, ""


def replace_document(
    db: Session,
    doc: Document,
    *,
    filename: str,
    content: bytes,
    content_type: str = "",
    uploaded_by: str = "",
) -> Tuple[Optional[Document], str]:
    verr = _validate_upload(filename, len(content))
    if verr:
        return None, verr
    token, err = get_valid_access_token(db)
    if not token:
        return None, err or "Microsoft OneDrive is not connected."
    if not doc.onedrive_item_id:
        return None, "Document has no OneDrive item."

    ct = _guess_content_type(filename, content_type)
    item, rerr = drive.replace_file(token, doc.onedrive_item_id, content, ct)
    if not item:
        return None, rerr or "Replace on OneDrive failed."

    new_ver = int(doc.version or 1) + 1
    doc.version = new_ver
    doc.original_filename = filename
    doc.content_type = ct
    doc.size_bytes = int(item.get("size") or len(content))
    doc.onedrive_item_id = str(item.get("id") or doc.onedrive_item_id)
    doc.onedrive_web_url = item.get("webUrl") or doc.onedrive_web_url
    doc.onedrive_etag = item.get("eTag") or item.get("cTag") or doc.onedrive_etag
    doc.replaced_at = datetime.utcnow()
    doc.updated_at = datetime.utcnow()
    if uploaded_by:
        doc.uploaded_by = uploaded_by

    db.add(
        DocumentVersion(
            document_id=doc.id,
            version=new_ver,
            onedrive_item_id=doc.onedrive_item_id,
            original_filename=filename,
            size_bytes=doc.size_bytes,
            content_type=ct,
            uploaded_by=uploaded_by or doc.uploaded_by,
            uploaded_at=datetime.utcnow(),
            note="Replaced file",
        )
    )
    db.commit()
    db.refresh(doc)
    return doc, ""


def update_metadata(
    db: Session,
    doc: Document,
    *,
    title: str = "",
    description: str = "",
    tags: str = "",
    category: str = "",
    is_key: Optional[bool] = None,
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
) -> Document:
    if title is not None and str(title).strip():
        doc.title = str(title).strip()[:240]
    doc.description = (description or "").strip() or None
    doc.tags = (tags or "").strip() or None
    if category and category in DOCUMENT_CATEGORIES:
        doc.category = category
    if is_key is not None:
        doc.is_key = bool(is_key)
    if client_id is not None:
        doc.client_id = client_id or None
    if job_id is not None:
        doc.job_id = job_id or None
    doc.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return doc


def toggle_key(db: Session, doc: Document) -> Document:
    doc.is_key = not bool(doc.is_key)
    doc.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(doc)
    return doc


def delete_document(db: Session, doc: Document) -> Tuple[bool, str]:
    token, err = get_valid_access_token(db)
    if doc.onedrive_item_id:
        if not token:
            return False, err or "Microsoft OneDrive is not connected."
        ok, derr = drive.delete_file(token, doc.onedrive_item_id)
        if not ok:
            return False, derr or "Could not delete file from OneDrive."
    doc.status = "deleted"
    doc.updated_at = datetime.utcnow()
    db.commit()
    return True, ""


def download_bytes(
    db: Session, doc: Document
) -> Tuple[Optional[bytes], str, str]:
    token, err = get_valid_access_token(db)
    if not token:
        return None, "", err or "Microsoft OneDrive is not connected."
    if not doc.onedrive_item_id:
        return None, "", "No OneDrive item."
    data, ct, derr = drive.download_file(token, doc.onedrive_item_id)
    if data is None:
        return None, "", derr
    return data, doc.content_type or ct or "application/octet-stream", ""


def refresh_onedrive_web_url(db: Session, doc: Document) -> Optional[str]:
    """Refresh and persist webUrl from Graph (opens Office Online / OneDrive)."""
    if not doc.onedrive_item_id:
        return (doc.onedrive_web_url or "").strip() or None
    token, err = get_valid_access_token(db)
    if not token:
        return (doc.onedrive_web_url or "").strip() or None
    item, ierr = drive.get_item(token, doc.onedrive_item_id)
    if not item:
        return (doc.onedrive_web_url or "").strip() or None
    web = (item.get("webUrl") or "").strip()
    if web and web != (doc.onedrive_web_url or ""):
        doc.onedrive_web_url = web
        doc.updated_at = datetime.utcnow()
        try:
            db.commit()
        except Exception:
            db.rollback()
    return web or (doc.onedrive_web_url or "").strip() or None


def _norm_folder_key(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = s.replace("–", "-").replace("—", "-")
    return s


def _match_client_for_folder(clients: List[Client], folder_name: str) -> Optional[Client]:
    key = _norm_folder_key(folder_name)
    if not key:
        return None
    # Exact slug match first
    for c in clients:
        if _norm_folder_key(drive.client_folder_slug(c)) == key:
            return c
    # Loose: folder starts with company name or vice versa
    for c in clients:
        nm = ""
        try:
            nm = c.display_name() if hasattr(c, "display_name") else (c.company_name or "")
        except Exception:
            nm = c.company_name or ""
        nk = _norm_folder_key(nm)
        if nk and (nk == key or key.startswith(nk) or nk.startswith(key)):
            return c
    return None


def _match_job_for_folder(jobs: List[Job], folder_name: str) -> Optional[Job]:
    key = _norm_folder_key(folder_name)
    if not key:
        return None
    # Category folders are not jobs
    cat_keys = {_norm_folder_key(v) for v in CATEGORY_FOLDER.values()} | {
        _norm_folder_key(c) for c in DOCUMENT_CATEGORIES
    }
    if key in cat_keys:
        return None
    for j in jobs:
        if _norm_folder_key(drive.job_folder_slug(j)) == key:
            return j
    for j in jobs:
        jt = _norm_folder_key(j.type or "")
        title = _norm_folder_key(j.title or "")
        if jt and (jt == key or key.startswith(jt) or jt in key):
            return j
        if title and (title == key or key in title or title in key):
            return j
    return None


def _category_from_folder(folder_name: str) -> str:
    key = _norm_folder_key(folder_name)
    for cat, seg in CATEGORY_FOLDER.items():
        if _norm_folder_key(seg) == key or _norm_folder_key(cat) == key:
            return cat
    for cat in DOCUMENT_CATEGORIES:
        if _norm_folder_key(cat) == key:
            return cat
    return "Other"


def _register_drive_file(
    db: Session,
    *,
    item: dict,
    client_id: Optional[int],
    job_id: Optional[int],
    category: str,
    path_prefix: str,
    uploaded_by: str = "onedrive-scan",
) -> Optional[Document]:
    """Create a CRM Document row for a Graph file item if not already linked."""
    item_id = str(item.get("id") or "").strip()
    if not item_id or item.get("folder") is not None:
        return None
    if not item.get("file"):
        return None

    existing = (
        db.query(Document)
        .filter(Document.onedrive_item_id == item_id)
        .first()
    )
    if existing:
        # Revive if soft-deleted and ensure client/job links
        changed = False
        if existing.status != "active":
            existing.status = "active"
            changed = True
        if client_id and not existing.client_id:
            existing.client_id = client_id
            changed = True
        if job_id and not existing.job_id:
            existing.job_id = job_id
            changed = True
        if changed:
            existing.updated_at = datetime.utcnow()
            db.commit()
        return None  # not "new"

    name = (item.get("name") or "file").strip()
    title = os.path.splitext(name)[0] or name
    ct = ""
    try:
        ct = (item.get("file") or {}).get("mimeType") or ""
    except Exception:
        ct = ""
    if not ct:
        ct = _guess_content_type(name)
    path = f"{path_prefix} / {name}" if path_prefix else name
    cat = category if category in DOCUMENT_CATEGORIES else "Other"
    doc = Document(
        title=title[:240],
        description="Linked from OneDrive (manual upload scan)",
        tags="onedrive-scan",
        category=cat,
        client_id=client_id,
        job_id=job_id,
        is_key=False,
        original_filename=name,
        content_type=ct,
        size_bytes=int(item.get("size") or 0),
        onedrive_item_id=item_id,
        onedrive_path=path,
        onedrive_web_url=item.get("webUrl"),
        onedrive_etag=item.get("eTag") or item.get("cTag"),
        version=1,
        uploaded_by=uploaded_by,
        uploaded_at=datetime.utcnow(),
        status="active",
    )
    db.add(doc)
    db.flush()
    db.add(
        DocumentVersion(
            document_id=doc.id,
            version=1,
            onedrive_item_id=item_id,
            original_filename=name,
            size_bytes=doc.size_bytes,
            content_type=ct,
            uploaded_by=uploaded_by,
            uploaded_at=doc.uploaded_at,
            note="Imported by OneDrive scan",
        )
    )
    db.commit()
    db.refresh(doc)
    return doc


def scan_onedrive_for_documents(
    db: Session,
    *,
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
    max_files: int = 500,
) -> dict:
    """
    Walk Accologise / Clients / … on OneDrive and link any files that are not
    already in the CRM (e.g. dropped in manually in Explorer / browser).

    Matching:
      Clients / {Client Name} → Client
      … / {Job Name} → Job (Accounts, Confirmation Statement, …)
      … / {Category} → document category
    """
    token, err = get_valid_access_token(db)
    if not token:
        return {
            "ok": False,
            "error": err or "Microsoft OneDrive is not connected.",
            "created": 0,
            "skipped": 0,
            "clients_matched": 0,
        }

    root_id, rerr = drive.find_root_folder_id(db, token)
    if not root_id:
        # ensure creates Accologise if missing
        root_id, rerr = drive.ensure_root_folder(db, token)
    if not root_id:
        return {
            "ok": False,
            "error": rerr or "Could not open Accologise root on OneDrive.",
            "created": 0,
            "skipped": 0,
            "clients_matched": 0,
        }

    children, cerr = drive.list_children(token, root_id)
    if cerr and not children:
        return {
            "ok": False,
            "error": cerr,
            "created": 0,
            "skipped": 0,
            "clients_matched": 0,
        }

    clients_folder_id = None
    root_name = drive.ROOT_FOLDER_NAME
    for ch in children:
        if ch.get("folder") and (ch.get("name") or "").strip().lower() == "clients":
            clients_folder_id = ch.get("id")
            break
    if not clients_folder_id:
        # ensure Clients exists for future uploads
        item, e = drive.ensure_child_folder(token, root_id, "Clients")
        if not item:
            return {
                "ok": False,
                "error": e or "No Clients folder on OneDrive yet.",
                "created": 0,
                "skipped": 0,
                "clients_matched": 0,
            }
        clients_folder_id = item["id"]

    all_clients = db.query(Client).order_by(Client.company_name.asc()).all()
    if client_id:
        all_clients = [c for c in all_clients if c.id == client_id]

    created = 0
    skipped = 0
    matched_clients = 0
    files_seen = 0

    client_folders, _ = drive.list_children(token, clients_folder_id)
    for cfold in client_folders:
        if not cfold.get("folder"):
            continue
        cname = (cfold.get("name") or "").strip()
        client = _match_client_for_folder(all_clients, cname)
        if not client:
            continue
        if client_id and client.id != client_id:
            continue
        matched_clients += 1
        client_path = f"{root_name} / Clients / {cname}"
        client_jobs = (
            db.query(Job)
            .filter(Job.client_id == client.id)
            .order_by(Job.id.desc())
            .all()
        )
        if job_id:
            client_jobs = [j for j in client_jobs if j.id == job_id]

        # Walk client folder: files + job subfolders + category folders
        level1, _ = drive.list_children(token, str(cfold.get("id")))
        for item in level1:
            if files_seen >= max_files:
                break
            if item.get("file"):
                files_seen += 1
                doc = _register_drive_file(
                    db,
                    item=item,
                    client_id=client.id,
                    job_id=None,
                    category="Other",
                    path_prefix=client_path,
                )
                if doc:
                    created += 1
                else:
                    skipped += 1
                continue
            if not item.get("folder"):
                continue

            sub_name = (item.get("name") or "").strip()
            job = _match_job_for_folder(client_jobs, sub_name)
            cat_here = _category_from_folder(sub_name)

            # Category folder directly under client
            if not job and cat_here != "Other":
                level2, _ = drive.list_children(token, str(item.get("id")))
                for f in level2:
                    if files_seen >= max_files:
                        break
                    if not f.get("file"):
                        continue
                    files_seen += 1
                    doc = _register_drive_file(
                        db,
                        item=f,
                        client_id=client.id,
                        job_id=None,
                        category=cat_here,
                        path_prefix=f"{client_path} / {sub_name}",
                    )
                    if doc:
                        created += 1
                    else:
                        skipped += 1
                continue

            # Job folder (or unknown folder treated as job-less bag)
            job_path = f"{client_path} / {sub_name}"
            jid = job.id if job else None
            if job_id and jid != job_id:
                continue
            level2, _ = drive.list_children(token, str(item.get("id")))
            for sub in level2:
                if files_seen >= max_files:
                    break
                if sub.get("file"):
                    files_seen += 1
                    doc = _register_drive_file(
                        db,
                        item=sub,
                        client_id=client.id,
                        job_id=jid,
                        category="Other",
                        path_prefix=job_path,
                    )
                    if doc:
                        created += 1
                    else:
                        skipped += 1
                    continue
                if not sub.get("folder"):
                    continue
                cat2 = _category_from_folder(sub.get("name") or "")
                level3, _ = drive.list_children(token, str(sub.get("id")))
                for f in level3:
                    if files_seen >= max_files:
                        break
                    if not f.get("file"):
                        continue
                    files_seen += 1
                    doc = _register_drive_file(
                        db,
                        item=f,
                        client_id=client.id,
                        job_id=jid,
                        category=cat2,
                        path_prefix=f"{job_path} / {sub.get('name')}",
                    )
                    if doc:
                        created += 1
                    else:
                        skipped += 1

        if files_seen >= max_files:
            break

    return {
        "ok": True,
        "error": "",
        "created": created,
        "skipped": skipped,
        "clients_matched": matched_clients,
        "files_seen": files_seen,
    }


def resolve_open_url(db: Session, doc: Document) -> Tuple[Optional[str], str]:
    """
    URL to open the live file in OneDrive / Office Online (no local download).

    Returns (url, error). Order:
      1. Fresh Graph webUrl
      2. Organisation/users view link (createLink)
      3. Graph preview getUrl (Microsoft viewer)
      4. Stored webUrl
    """
    if not doc.onedrive_item_id and not (doc.onedrive_web_url or "").strip():
        return None, "This document has no OneDrive item."

    token, terr = get_valid_access_token(db)
    stored = (doc.onedrive_web_url or "").strip()

    if token and doc.onedrive_item_id:
        item, _ = drive.get_item(token, doc.onedrive_item_id)
        if item:
            web = (item.get("webUrl") or "").strip()
            if web:
                if web != stored:
                    doc.onedrive_web_url = web
                    doc.updated_at = datetime.utcnow()
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                return web, ""

        link, _ = drive.create_view_link(token, doc.onedrive_item_id)
        if link:
            return link, ""

        prev, _ = drive.create_preview(token, doc.onedrive_item_id)
        if prev:
            return prev, ""

    if stored:
        return stored, ""

    if not token:
        return (
            None,
            terr or "Microsoft OneDrive is not connected. Reconnect in Settings.",
        )
    return None, "Could not resolve OneDrive open link. Reconnect Microsoft and try again."


def resolve_preview_embed_url(
    db: Session, doc: Document
) -> Tuple[Optional[str], str, str]:
    """
    Best in-browser preview strategy.

    Returns (url_or_path, kind, error) where kind is:
      office_embed | onedrive | pdf | image | none
    """
    kind = doc.preview_kind()
    if kind == "pdf":
        return f"/documents/{doc.id}/preview", "pdf", ""
    if kind == "image":
        return f"/documents/{doc.id}/preview", "image", ""

    # Office / other: try Graph preview embed, else open OneDrive webUrl
    if doc.onedrive_item_id:
        token, err = get_valid_access_token(db)
        if token:
            prev, perr = drive.create_preview(token, doc.onedrive_item_id)
            if prev:
                return prev, "office_embed", ""
            # Fall through to web open if preview API fails
            _ = perr
        else:
            _ = err

    open_url, oerr = resolve_open_url(db, doc)
    if open_url:
        return open_url, "onedrive", ""
    return None, "none", oerr or "No preview available."


def summary_counts(db: Session) -> dict:
    docs = (
        db.query(Document).filter(Document.status == "active").all()
    )
    by_cat = {}
    key_n = 0
    for d in docs:
        by_cat[d.category or "Other"] = by_cat.get(d.category or "Other", 0) + 1
        if d.is_key:
            key_n += 1
    return {
        "total": len(docs),
        "key": key_n,
        "by_category": by_cat,
    }


def docs_connection(db: Session) -> dict:
    return connection_status(db)
