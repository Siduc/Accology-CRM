"""Document management routes — OneDrive-backed."""

from __future__ import annotations

from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.models.document import DOCUMENT_CATEGORIES
from app.services import documents as docs_svc
from app.templating import render

router = APIRouter(tags=["documents"])


def _user(request: Request) -> str:
    return (request.session.get("user") or "").strip() or "user"


def _flash_url(path: str, **params) -> str:
    qs = "&".join(f"{k}={url_quote(str(v))}" for k, v in params.items() if v is not None)
    if not qs:
        return path
    return f"{path}{'&' if '?' in path else '?'}{qs}"


@router.get("/documents", response_class=HTMLResponse)
async def documents_list(
    request: Request,
    q: str = "",
    client_id: str = "",
    job_id: str = "",
    category: str = "",
    key_only: str = "",
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    is_key = True if key_only in ("1", "true", "yes", "on") else None
    rows = docs_svc.list_documents(
        db,
        q=q,
        client_id=cid,
        job_id=jid,
        category=category,
        is_key=is_key,
    )
    clients = (
        db.query(Client)
        .order_by(Client.company_name.asc())
        .limit(500)
        .all()
    )
    jobs = []
    if cid:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == cid)
            .order_by(Job.id.desc())
            .limit(100)
            .all()
        )
    status = docs_svc.docs_connection(db)
    counts = docs_svc.summary_counts(db)
    return render(
        request,
        "documents/list.html",
        {
            "documents": rows,
            "clients": clients,
            "jobs": jobs,
            "categories": DOCUMENT_CATEGORIES,
            "q": q or "",
            "filter_client_id": cid,
            "filter_job_id": jid,
            "filter_category": category or "",
            "key_only": bool(is_key),
            "conn": status,
            "counts": counts,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/documents/new", response_class=HTMLResponse)
async def documents_new_form(
    request: Request,
    client_id: str = "",
    job_id: str = "",
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    clients = (
        db.query(Client)
        .order_by(Client.company_name.asc())
        .limit(500)
        .all()
    )
    jobs_q = db.query(Job).order_by(Job.id.desc())
    if cid:
        jobs_q = jobs_q.filter(Job.client_id == cid)
    jobs = jobs_q.limit(200).all()
    status = docs_svc.docs_connection(db)
    return render(
        request,
        "documents/form.html",
        {
            "clients": clients,
            "jobs": jobs,
            "categories": DOCUMENT_CATEGORIES,
            "client_id": cid,
            "job_id": jid,
            "conn": status,
            "error": request.query_params.get("error", ""),
            "doc": None,
        },
    )


@router.post("/documents/new")
async def documents_create(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    category: str = Form("Other"),
    client_id: str = Form(""),
    job_id: str = Form(""),
    is_key: str = Form(""),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    content = await file.read()
    filename = file.filename or "upload.bin"
    doc, err = docs_svc.create_document(
        db,
        filename=filename,
        content=content,
        content_type=file.content_type or "",
        title=title,
        description=description,
        tags=tags,
        category=category,
        client_id=cid,
        job_id=jid,
        is_key=is_key in ("1", "true", "on", "yes"),
        uploaded_by=_user(request),
    )
    if err:
        ret = f"/documents/new?error={url_quote(err)}"
        if cid:
            ret += f"&client_id={cid}"
        if jid:
            ret += f"&job_id={jid}"
        return RedirectResponse(ret, status_code=303)
    return RedirectResponse(
        f"/documents/{doc.id}?msg={url_quote('Uploaded')}", status_code=303
    )


@router.get("/documents/{doc_id:int}", response_class=HTMLResponse)
async def document_detail(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse(
            f"/documents?error={url_quote('Document not found')}", status_code=303
        )
    status = docs_svc.docs_connection(db)
    clients = (
        db.query(Client)
        .order_by(Client.company_name.asc())
        .limit(500)
        .all()
    )
    jobs = []
    if doc.client_id:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == doc.client_id)
            .order_by(Job.id.desc())
            .limit(100)
            .all()
        )
    return render(
        request,
        "documents/detail.html",
        {
            "doc": doc,
            "versions": doc.versions or [],
            "categories": DOCUMENT_CATEGORIES,
            "clients": clients,
            "jobs": jobs,
            "conn": status,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/documents/{doc_id:int}/edit")
async def document_edit(
    doc_id: int,
    title: str = Form(""),
    description: str = Form(""),
    tags: str = Form(""),
    category: str = Form(""),
    is_key: str = Form(""),
    client_id: str = Form(""),
    job_id: str = Form(""),
    db: Session = Depends(get_db),
):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    docs_svc.update_metadata(
        db,
        doc,
        title=title,
        description=description,
        tags=tags,
        category=category,
        is_key=is_key in ("1", "true", "on", "yes"),
        client_id=cid,
        job_id=jid,
    )
    return RedirectResponse(
        f"/documents/{doc_id}?msg={url_quote('Saved')}", status_code=303
    )


@router.post("/documents/{doc_id:int}/replace")
async def document_replace(
    request: Request,
    doc_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    content = await file.read()
    doc, err = docs_svc.replace_document(
        db,
        doc,
        filename=file.filename or "upload.bin",
        content=content,
        content_type=file.content_type or "",
        uploaded_by=_user(request),
    )
    if err:
        return RedirectResponse(
            f"/documents/{doc_id}?error={url_quote(err)}", status_code=303
        )
    return RedirectResponse(
        f"/documents/{doc_id}?msg={url_quote('New version uploaded')}",
        status_code=303,
    )


@router.post("/documents/{doc_id:int}/key")
async def document_toggle_key(doc_id: int, db: Session = Depends(get_db)):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    docs_svc.toggle_key(db, doc)
    return RedirectResponse(f"/documents/{doc_id}", status_code=303)


@router.post("/documents/{doc_id:int}/delete")
async def document_delete(
    doc_id: int,
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    ok, err = docs_svc.delete_document(db, doc)
    ret = (return_to or "").strip() or "/documents"
    if not ok:
        sep = "&" if "?" in ret else "?"
        return RedirectResponse(
            f"{ret}{sep}error={url_quote(err)}", status_code=303
        )
    sep = "&" if "?" in ret else "?"
    return RedirectResponse(f"{ret}{sep}msg=deleted", status_code=303)


@router.get("/documents/{doc_id:int}/download")
async def document_download(doc_id: int, db: Session = Depends(get_db)):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    data, ct, err = docs_svc.download_bytes(db, doc)
    if data is None:
        return RedirectResponse(
            f"/documents/{doc_id}?error={url_quote(err or 'Download failed')}",
            status_code=303,
        )
    filename = doc.original_filename or f"document-{doc.id}"
    return Response(
        content=data,
        media_type=ct or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/documents/{doc_id:int}/preview")
async def document_preview(doc_id: int, db: Session = Depends(get_db)):
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return Response(status_code=404, content="Not found")
    data, ct, err = docs_svc.download_bytes(db, doc)
    if data is None:
        return Response(status_code=502, content=err or "Preview failed")
    filename = doc.original_filename or f"document-{doc.id}"
    return Response(
        content=data,
        media_type=ct or "application/octet-stream",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "private, max-age=60",
        },
    )
