"""Document management — metadata in CRM, bytes in OneDrive."""

from __future__ import annotations

import mimetypes
import os
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models.client import Client
from app.models.document import (
    ALLOWED_EXTENSIONS,
    DOCUMENT_CATEGORIES,
    Document,
    DocumentVersion,
)
from app.models.job import Job
from app.services import ms_graph_drive as drive
from app.services.ms_graph_oauth import connection_status, get_valid_access_token


def list_documents(
    db: Session,
    *,
    q: str = "",
    client_id: Optional[int] = None,
    job_id: Optional[int] = None,
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
    is_key: bool = False,
    uploaded_by: str = "",
) -> Tuple[Optional[Document], str]:
    if not client_id and not job_id:
        return None, "Link the document to a client or a job."

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
    if job_id and not job:
        return None, "Job not found."
    if client_id and not client:
        return None, "Client not found."
    # Prefer job's client if missing
    if job and not client and job.client_id:
        client = job.client or db.query(Client).filter(Client.id == job.client_id).first()
        client_id = client.id if client else client_id

    folder_id, folder_path, ferr = drive.resolve_storage_folder(
        db, token, client=client, job=job, category=cat
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
