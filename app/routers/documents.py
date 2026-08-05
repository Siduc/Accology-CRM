"""Document management routes — OneDrive-backed."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.models.document import DOCUMENT_CATEGORIES
from app.services import branding as branding_svc
from app.services import documents as docs_svc
from app.templating import render

router = APIRouter(tags=["documents"])

_DOCX_MEDIA = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _user(request: Request) -> str:
    return (request.session.get("user") or "").strip() or "user"


def _flash_url(path: str, **params) -> str:
    qs = "&".join(f"{k}={url_quote(str(v))}" for k, v in params.items() if v is not None)
    if not qs:
        return path
    return f"{path}{'&' if '?' in path else '?'}{qs}"


@router.post("/documents/scan-onedrive")
async def documents_scan_onedrive(
    request: Request,
    client_id: str = Form(""),
    job_id: str = Form(""),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Scan OneDrive Accologise Documents / Clients / … for files added outside CRM
    and create Document links on the matching client (and job when possible).
    """
    # Ensure token is refreshed before scanning (avoids false “not connected”)
    docs_svc.docs_connection(db)
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    result = docs_svc.scan_onedrive_for_documents(
        db, client_id=cid, job_id=jid
    )
    dest = (return_to or "").strip() or "/documents"
    if not dest.startswith("/"):
        dest = "/documents"
    if not result.get("ok"):
        return RedirectResponse(
            _flash_url(dest, error=result.get("error") or "Scan failed"),
            status_code=303,
        )
    msg = (
        f"OneDrive scan: {result.get('created', 0)} new link(s), "
        f"{result.get('skipped', 0)} already linked, "
        f"{result.get('clients_matched', 0)} client folder(s)."
    )
    return RedirectResponse(_flash_url(dest, msg=msg), status_code=303)


@router.post("/documents/{doc_id:int}/attach-job")
async def document_attach_job(
    doc_id: int,
    job_id: str = Form(...),
    return_to: str = Form(""),
    db: Session = Depends(get_db),
):
    """Link a scanned/uploaded document to a job (no re-upload)."""
    jid = int(job_id) if (job_id or "").isdigit() else 0
    ok, msg = docs_svc.attach_document_to_job(db, doc_id, jid)
    dest = (return_to or "").strip()
    if not dest.startswith("/"):
        dest = f"/jobs/{jid}" if jid else f"/documents/{doc_id}"
    if not ok:
        return RedirectResponse(
            _flash_url(dest, error=msg), status_code=303
        )
    return RedirectResponse(_flash_url(dest, msg=msg), status_code=303)


@router.get("/documents/letterhead.docx")
async def open_letterhead_docx(request: Request):
    """
    Accology Limited letterhead as a Word document.
    Attachment disposition so the browser / OS opens it in Word (or downloads).
    """
    path = branding_svc.letterhead_docx_path()
    if not path or not path.is_file():
        # Rebuild if the static file is missing
        try:
            import importlib.util

            script = (
                Path(__file__).resolve().parents[2]
                / "scripts"
                / "build_letterhead_docx.py"
            )
            if script.is_file():
                spec = importlib.util.spec_from_file_location(
                    "build_letterhead_docx", script
                )
                mod = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(mod)
                path = mod.build()
        except Exception:
            path = None
    if not path or not Path(path).is_file():
        return RedirectResponse(
            _flash_url("/documents", error="Letterhead Word file is not available."),
            status_code=303,
        )
    filename = "Accology-letterhead.docx"
    return FileResponse(
        path=str(path),
        media_type=_DOCX_MEDIA,
        filename=filename,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


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
    try:
        doc = docs_svc.get_document(db, doc_id)
    except Exception:
        doc = None
    if not doc:
        return RedirectResponse(
            f"/documents?error={url_quote('Document not found')}", status_code=303
        )
    try:
        status = docs_svc.docs_connection(db)
    except Exception:
        status = {
            "configured": False,
            "connected": False,
            "fresh": False,
            "email": "",
        }
    clients = (
        db.query(Client)
        .order_by(Client.company_name.asc())
        .limit(500)
        .all()
    )
    jobs = []
    if doc.client_id:
        try:
            jobs = (
                db.query(Job)
                .filter(Job.client_id == doc.client_id)
                .order_by(Job.id.desc())
                .limit(100)
                .all()
            )
        except Exception:
            jobs = []
    # Prefer Graph embed for Office; PDF/image use local stream
    embed_url, embed_kind, embed_err = None, "none", ""
    open_url = None
    try:
        embed_url, embed_kind, embed_err = docs_svc.resolve_preview_embed_url(db, doc)
    except Exception as exc:
        embed_kind, embed_err = "none", str(exc)[:200]
    try:
        open_url, _ = docs_svc.resolve_open_url(db, doc)
    except Exception:
        open_url = (doc.onedrive_web_url or "").strip() or None
    try:
        versions = list(doc.versions or [])
    except Exception:
        versions = []
    return render(
        request,
        "documents/detail.html",
        {
            "doc": doc,
            "versions": versions,
            "categories": DOCUMENT_CATEGORIES,
            "clients": clients,
            "jobs": jobs,
            "conn": status,
            "open_url": open_url or f"/documents/{doc.id}/open",
            "embed_url": embed_url,
            "embed_kind": embed_kind,
            "embed_err": embed_err or "",
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


@router.get("/documents/{doc_id:int}/open")
async def document_open(
    request: Request,
    doc_id: int,
    db: Session = Depends(get_db),
):
    """
    Open the live file in OneDrive / Office Online (primary action).
    Does not download a second local copy.

    Uses 302 to the Microsoft URL, or a small HTML hop page if preferred
    (``?hop=1``) so popup blockers / intermediate errors are visible.
    """
    doc = docs_svc.get_document(db, doc_id)
    if not doc:
        return RedirectResponse("/documents?error=not+found", status_code=303)
    url, err = docs_svc.resolve_open_url(db, doc)
    if not url:
        return RedirectResponse(
            f"/documents/{doc_id}?error={url_quote(err or 'Open failed')}",
            status_code=303,
        )
    # Absolute Microsoft URL — open directly
    hop = (request.query_params.get("hop") or "").strip() in ("1", "true", "yes")
    if hop or not url.startswith("http"):
        # Safe landing page with explicit link (helps if redirect is blocked)
        safe = (
            url.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
        )
        title = (doc.title or "Document").replace("<", "").replace(">", "")
        html = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={safe}">
<title>Opening {title}…</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:1rem;line-height:1.5}}
a.btn{{display:inline-block;background:#2563eb;color:#fff;padding:.65rem 1.1rem;border-radius:8px;text-decoration:none;font-weight:600}}
</style>
</head><body>
<h1>Opening in OneDrive…</h1>
<p>If nothing happens, click below (you may need to sign in to Microsoft).</p>
<p><a class="btn" href="{safe}" rel="noopener">Open in OneDrive</a></p>
<p><a href="/documents/{doc_id}">← Back to document</a></p>
<script>window.location.replace({url!r});</script>
</body></html>"""
        return HTMLResponse(content=html)
    return RedirectResponse(url, status_code=302)


@router.get("/documents/{doc_id:int}/download")
async def document_download(doc_id: int, db: Session = Depends(get_db)):
    """Secondary action — save a local copy (avoid unless needed)."""
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
    # PDF / images stream inline; Office types should open via /open or Graph embed
    kind = doc.preview_kind()
    if kind not in ("pdf", "image"):
        url, err = docs_svc.resolve_open_url(db, doc)
        if url:
            return RedirectResponse(url, status_code=303)
        return Response(status_code=415, content=err or "No in-app preview for this type")
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
