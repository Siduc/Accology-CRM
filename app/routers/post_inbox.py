"""Post inbox — scanner drops → review → file / email / learn."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.models.document import DOCUMENT_CATEGORIES
from app.models.post_inbox import POST_ACTIONS, POST_CATEGORIES, PostRule
from app.services import post_inbox as post_svc
from app.templating import render

router = APIRouter(tags=["post-inbox"])


def _user(request: Request) -> str:
    try:
        return str(request.session.get("user") or "")[:80]
    except Exception:
        return ""


@router.get("/post", response_class=HTMLResponse)
async def post_hub(request: Request, db: Session = Depends(get_db)):
    post_svc.ensure_inbox_dirs()
    post_svc.seed_default_rules(db)
    counts = post_svc.inbox_counts(db)
    items = post_svc.list_open_items(db, limit=80)
    holding = post_svc.list_holding_items(db, limit=80)
    recent = post_svc.list_recent_items(db, limit=30)
    dirs = post_svc.ensure_inbox_dirs()
    pending_files = post_svc.count_pending_scan_files()
    return render(
        request,
        "post/hub.html",
        {
            "counts": counts,
            "items": items,
            "holding": holding,
            "recent": recent,
            "inbox_path": str(dirs["inbox"]),
            "root_path": str(dirs["root"]),
            "pending_files": pending_files,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "categories": POST_CATEGORIES,
            "doc_categories": DOCUMENT_CATEGORIES,
        },
    )


@router.post("/post/holding/combine")
async def post_holding_combine(
    request: Request,
    title: str = Form(""),
    order: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Combine selected holding pages into one normal review document.
    Form fields: holding_id (repeated) and/or order=h12,h34,h5
    """
    form = await request.form()
    ids: list[int] = []
    # Explicit order string preferred (from drag strip)
    raw_order = (order or "").strip()
    if raw_order:
        for bit in raw_order.replace(";", ",").replace(" ", ",").split(","):
            bit = bit.strip().lower()
            if bit.startswith("h") and bit[1:].isdigit():
                ids.append(int(bit[1:]))
            elif bit.isdigit():
                ids.append(int(bit))
    if not ids:
        for key in form.keys():
            if key in ("holding_id", "holding_ids"):
                for v in form.getlist(key):
                    s = str(v or "").strip()
                    if s.isdigit():
                        ids.append(int(s))
    ok, msg, new_id = post_svc.combine_holding_into_document(
        db, ids, title=(title or "").strip() or None, classify=True
    )
    if not ok:
        return RedirectResponse(
            f"/post?error={url_quote(msg[:300])}",
            status_code=303,
        )
    if new_id:
        return RedirectResponse(
            f"/post/items/{new_id}?msg={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/post/clear")
async def post_clear(request: Request, db: Session = Depends(get_db)):
    """Delete all post items/batches so a fresh import can start clean."""
    from app.models.post_inbox import PostBatch, PostItem
    from pathlib import Path as _Path

    items = db.query(PostItem).all()
    n_items = len(items)
    for it in items:
        try:
            if it.local_path:
                p = _Path(it.local_path)
                if p.is_file() and "splits" in p.parts:
                    p.unlink(missing_ok=True)
        except Exception:
            pass
        db.delete(it)
    n_batches = db.query(PostBatch).count()
    for b in db.query(PostBatch).all():
        db.delete(b)
    db.commit()
    msg = f"Cleared {n_items} item(s) and {n_batches} batch(es). Re-scan if needed, then Import."
    return RedirectResponse(f"/post?msg={url_quote(msg[:300])}", status_code=303)


@router.post("/post/import")
async def post_import(request: Request, db: Session = Depends(get_db)):
    result = post_svc.import_from_inbox(db)
    if result.get("errors"):
        err = "; ".join(result["errors"][:3])
        return RedirectResponse(
            f"/post?error={url_quote(err[:300])}&msg="
            + url_quote(
                f"Imported {result.get('imported', 0)}, skipped {result.get('skipped', 0)}"
            ),
            status_code=303,
        )
    msg = (
        f"Imported {result.get('imported', 0)} file(s) → "
        f"{result.get('items_created', 0)} document(s) after auto-split, "
        f"skipped {result.get('skipped', 0)}. "
        f"Inbox: {result.get('inbox_path', '')}"
    )
    return RedirectResponse(f"/post?msg={url_quote(msg[:400])}", status_code=303)


@router.post("/post/reimport-done")
async def post_reimport_done(request: Request, db: Session = Depends(get_db)):
    """
    Re-import scans sitting in the processed (done/) folder after a bad split
    or delete — uses improved court/blank-page logic.
    """
    result = post_svc.reimport_from_done(db, limit=20, force=True)
    bits = [
        f"Moved {result.get('moved_from_done', 0)} from done/",
        f"imported {result.get('imported', 0)}",
        f"→ {result.get('items_created', 0)} document(s)",
    ]
    if result.get("skipped"):
        bits.append(f"skipped {result.get('skipped')}")
    msg = " · ".join(bits)
    errs = (result.get("errors") or []) + (result.get("reimport_errors") or [])
    if errs:
        return RedirectResponse(
            f"/post?error={url_quote('; '.join(errs[:3])[:300])}&msg={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(f"/post?msg={url_quote(msg[:400])}", status_code=303)


@router.post("/post/batches/{batch_id:int}/keep-together")
async def post_batch_keep_together(
    batch_id: int,
    db: Session = Depends(get_db),
):
    """Force whole multi-page scan into one review document and learn keywords."""
    batch = post_svc.get_batch(db, batch_id) if hasattr(post_svc, "get_batch") else None
    if batch is None:
        from app.models.post_inbox import PostBatch

        batch = db.query(PostBatch).filter(PostBatch.id == batch_id).first()
    if not batch:
        return RedirectResponse(
            f"/post?error={url_quote('Batch not found')}", status_code=303
        )
    n = int(batch.page_count or 0)
    if n < 1:
        n = 1
    ranges = f"1-{n}" if n > 1 else "1"
    ok, msg = post_svc.reprocess_batch(db, batch_id, ranges_spec=ranges)
    if ok:
        # Reinforce learning
        try:
            from app.models.post_inbox import PostItem

            it = (
                db.query(PostItem)
                .filter(PostItem.batch_id == batch_id)
                .order_by(PostItem.id.desc())
                .first()
            )
            if it:
                post_svc.learn_keep_together_from_item(db, it)
        except Exception:
            pass
        msg = f"Kept as one document ({n} pages). {msg}"
    if not ok:
        return RedirectResponse(
            f"/post?error={url_quote(msg[:300])}", status_code=303
        )
    # Prefer open the new single item
    try:
        from app.models.post_inbox import PostItem

        it = (
            db.query(PostItem)
            .filter(
                PostItem.batch_id == batch_id,
                PostItem.status.in_(["inbox", "suggested"]),
            )
            .order_by(PostItem.id.desc())
            .first()
        )
        if it:
            return RedirectResponse(
                f"/post/items/{it.id}?msg={url_quote(msg[:300])}",
                status_code=303,
            )
    except Exception:
        pass
    return RedirectResponse(f"/post?msg={url_quote(msg[:400])}", status_code=303)


@router.post("/post/reclassify")
async def post_reclassify(request: Request, db: Session = Depends(get_db)):
    """Re-OCR image scans (Grok vision) and re-match clients for open items."""
    result = post_svc.reclassify_open_items(db, limit=40, use_vision=True)
    if not result.get("ok"):
        err = "; ".join(result.get("errors") or ["Re-read failed"])
        return RedirectResponse(
            f"/post?error={url_quote(err[:300])}",
            status_code=303,
        )
    msg = (
        f"Re-read {result.get('updated', 0)} of {result.get('total', 0)} item(s) · "
        f"{result.get('matched', 0)} with a suggested client"
    )
    if result.get("errors"):
        msg += " · some errors: " + "; ".join(result["errors"][:2])
    return RedirectResponse(f"/post?msg={url_quote(msg[:400])}", status_code=303)


@router.get("/post/items/{item_id:int}", response_class=HTMLResponse)
async def post_item_detail(
    item_id: int, request: Request, db: Session = Depends(get_db)
):
    item = post_svc.get_item(db, item_id)
    if not item:
        return RedirectResponse("/post", status_code=303)
    # Prefer same-batch holding pages first, then rest of holding area
    holding_same = (
        post_svc.list_holding_items(db, batch_id=item.batch_id, limit=60)
        if item.batch_id
        else []
    )
    holding_all = post_svc.list_holding_items(db, limit=80)
    seen = {h.id for h in holding_same}
    holding = list(holding_same) + [h for h in holding_all if h.id not in seen]
    page_count = post_svc.item_pdf_page_count(item)
    clients = (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive", "Former"]))
        .order_by(Client.company_name)
        .limit(600)
        .all()
    )
    jobs = []
    cid = item.client_id or item.suggested_client_id
    if cid:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == cid)
            .order_by(Job.id.desc())
            .limit(80)
            .all()
        )
    # Other open docs in this batch (for quick "go attach there" from holding)
    siblings = []
    if item.batch_id:
        siblings = [
            it
            for it in post_svc.list_open_items(db, limit=80)
            if it.batch_id == item.batch_id and it.id != item.id
        ]
    return render(
        request,
        "post/review.html",
        {
            "item": item,
            "clients": clients,
            "jobs": jobs,
            "categories": POST_CATEGORIES,
            "doc_categories": DOCUMENT_CATEGORIES,
            "actions": POST_ACTIONS,
            "holding": holding,
            "page_count": page_count,
            "siblings": siblings,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/post/items/{item_id:int}/file")
async def post_item_file(item_id: int, db: Session = Depends(get_db)):
    """Serve the item PDF for iframe preview / open. Never redirect to HTML pages
    (that made preview show the CRM shell inside the iframe)."""
    item = post_svc.get_item(db, item_id)
    if not item:
        return HTMLResponse(
            "<!doctype html><html><body style='font-family:system-ui;padding:1.5rem'>"
            "<h2>Not found</h2><p>That post item does not exist.</p></body></html>",
            status_code=404,
        )
    path = Path(item.local_path) if item.local_path else None
    if not path or not path.is_file():
        # Rebuild split from archived multi-page scan when possible
        if post_svc.ensure_item_file(db, item):
            path = Path(item.local_path) if item.local_path else None
    if not path or not path.is_file():
        return HTMLResponse(
            "<!doctype html><html><body style='font-family:system-ui;padding:1.5rem;"
            "background:#111827;color:#e2e8f0'>"
            "<h2 style='margin-top:0'>Preview unavailable</h2>"
            "<p>The PDF for this item is missing on disk "
            f"(expected <code>{item.local_path or '—'}</code>).</p>"
            "<p>Re-import the original scan from the Post hub, or re-split this item "
            "from the archived multi-page file.</p>"
            "</body></html>",
            status_code=404,
        )
    media = item.content_type or "application/pdf"
    if path.suffix.lower() == ".pdf":
        media = "application/pdf"
    # No-store so iframe preview always reflects page detach/attach (holding area edits)
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        content_disposition_type="inline",
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.post("/post/items/{item_id:int}/delete")
async def post_item_delete(item_id: int, db: Session = Depends(get_db)):
    """Permanent delete — no client required (tests / junk / bad splits)."""
    ok, msg = post_svc.apply_item_action(
        db,
        item_id,
        action="delete",
        reviewed_by="",
    )
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post?msg={url_quote(msg[:200])}",
        status_code=303,
    )


@router.post("/post/items/{item_id:int}/action")
async def post_item_action(
    item_id: int,
    request: Request,
    action: str = Form(...),
    client_id: str = Form(""),
    job_id: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    learn: str = Form(""),
    learn_keywords: str = Form(""),
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    ok, msg = post_svc.apply_item_action(
        db,
        item_id,
        action=action,
        client_id=cid,
        job_id=jid,
        category=category,
        notes=notes,
        learn=(learn or "").lower() in ("1", "yes", "on", "true"),
        learn_keywords=learn_keywords,
        reviewed_by=_user(request),
    )
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/post/items/{item_id:int}/split")
async def post_item_split(
    item_id: int,
    breaks: str = Form(""),
    db: Session = Depends(get_db),
):
    """Split after page numbers, e.g. 3,7,12"""
    parts = []
    for bit in (breaks or "").replace(";", ",").split(","):
        bit = bit.strip()
        if bit.isdigit():
            parts.append(int(bit))
    ok, msg = post_svc.split_item(db, item_id, parts)
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/post/items/{item_id:int}/detach-to-holding")
async def post_item_detach_to_holding(
    item_id: int,
    request: Request,
    pages: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Move selected pages from this document into the holding area.
    pages: comma-separated 1-based page numbers within this item's PDF,
    or repeated form fields named page (checkbox values).
    """
    form = await request.form()
    page_nums: list[int] = []
    # checkbox name="page" value="1" etc.
    for key in form.keys():
        if key in ("page", "pages"):
            for v in form.getlist(key):
                s = str(v or "").strip()
                if s.isdigit():
                    page_nums.append(int(s))
    # also accept pages=1,3,5 free text
    for bit in (pages or "").replace(";", ",").split(","):
        bit = bit.strip()
        if bit.isdigit():
            page_nums.append(int(bit))
    ok, msg = post_svc.detach_pages_to_holding(db, item_id, page_nums)
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post/items/{item_id}?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/post/items/{item_id:int}/attach-holding")
async def post_item_attach_holding(
    item_id: int,
    request: Request,
    position: str = Form("end"),
    insert_at: str = Form(""),
    db: Session = Depends(get_db),
):
    """Attach selected holding-area page(s) onto this document (start/end or insert_at)."""
    form = await request.form()
    holding_ids: list[int] = []
    for key in form.keys():
        if key in ("holding_id", "holding_ids"):
            for v in form.getlist(key):
                s = str(v or "").strip()
                if s.isdigit():
                    holding_ids.append(int(s))
    at: int | None = None
    if (insert_at or "").strip().lstrip("-").isdigit():
        at = int(insert_at)
    ok, msg = post_svc.attach_holding_to_item(
        db,
        item_id,
        holding_ids,
        position=position or "end",
        insert_at=at,
    )
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post/items/{item_id}?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.get("/post/items/{item_id:int}/thumb")
async def post_item_thumb(
    item_id: int,
    page: int = 1,
    db: Session = Depends(get_db),
):
    """Medium PNG thumbnail of a PDF page (for holding tray / page order UI)."""
    item = post_svc.get_item(db, item_id)
    if not item:
        return Response(status_code=404)
    data = post_svc.item_thumbnail_png(db, item, page=max(1, int(page or 1)), max_width=220)
    if not data:
        return Response(status_code=404)
    return Response(
        content=data,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=120",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/post/items/{item_id:int}/page-order")
async def post_item_page_order(
    item_id: int,
    request: Request,
    order: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Rebuild document from a page sequence.
    order: comma-separated tokens p1,p2,h45,p3 (current pages + holding ids).
    """
    form = await request.form()
    tokens: list[str] = []
    # Accept order=p1,h2,p3 or repeated token fields
    raw = (order or "").strip()
    if not raw:
        for key in form.keys():
            if key in ("order", "token", "seq"):
                for v in form.getlist(key):
                    raw = (raw + "," + str(v)).strip(",")
    for bit in raw.replace(";", ",").replace(" ", ",").split(","):
        bit = bit.strip()
        if bit:
            tokens.append(bit)
    ok, msg = post_svc.apply_page_sequence(db, item_id, tokens)
    if not ok:
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post/items/{item_id}?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/post/batches/{batch_id:int}/reprocess")
async def post_batch_reprocess(
    batch_id: int,
    ranges: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Re-run split on a multi-page scan.
    Optional ranges e.g. 1-8, 10, 12-19 (recommended when auto mix-up pages).
    Empty ranges = auto (blank pages + letterhead change).
    """
    ok, msg = post_svc.reprocess_batch(db, batch_id, ranges_spec=ranges or "")
    if not ok:
        return RedirectResponse(
            f"/post?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/post?msg={url_quote(msg[:400])}",
        status_code=303,
    )


@router.get("/post/rules", response_class=HTMLResponse)
async def post_rules_page(request: Request, db: Session = Depends(get_db)):
    post_svc.seed_default_rules(db)
    rules = post_svc.list_rules(db)
    clients = (
        db.query(Client)
        .order_by(Client.company_name)
        .limit(400)
        .all()
    )
    return render(
        request,
        "post/rules.html",
        {
            "rules": rules,
            "clients": clients,
            "actions": POST_ACTIONS,
            "categories": POST_CATEGORIES,
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/post/rules/new")
async def post_rule_create(
    name: str = Form(...),
    keywords: str = Form(...),
    action: str = Form("review"),
    category: str = Form(""),
    match_mode: str = Form("any"),
    client_id: str = Form(""),
    priority: str = Form("100"),
    db: Session = Depends(get_db),
):
    try:
        pri = int(priority or "100")
    except ValueError:
        pri = 100
    cid = int(client_id) if (client_id or "").isdigit() else None
    rule = PostRule(
        name=(name or "Rule").strip()[:120],
        keywords=(keywords or "").strip(),
        action=(action or "review").strip(),
        category=(category or "").strip() or None,
        match_mode="all" if match_mode == "all" else "any",
        client_id=cid,
        priority=pri,
        is_active=True,
        learned=False,
    )
    db.add(rule)
    db.commit()
    return RedirectResponse(
        f"/post/rules?msg={url_quote('Rule saved')}", status_code=303
    )


@router.post("/post/rules/{rule_id:int}/toggle")
async def post_rule_toggle(rule_id: int, db: Session = Depends(get_db)):
    rule = db.query(PostRule).filter(PostRule.id == rule_id).first()
    if rule:
        rule.is_active = not bool(rule.is_active)
        db.commit()
    return RedirectResponse("/post/rules", status_code=303)
