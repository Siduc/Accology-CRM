"""Post inbox — scanner drops → review → file / email / learn."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
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
    recent = post_svc.list_recent_items(db, limit=30)
    dirs = post_svc.ensure_inbox_dirs()
    return render(
        request,
        "post/hub.html",
        {
            "counts": counts,
            "items": items,
            "recent": recent,
            "inbox_path": str(dirs["inbox"]),
            "root_path": str(dirs["root"]),
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
            "categories": POST_CATEGORIES,
            "doc_categories": DOCUMENT_CATEGORIES,
        },
    )


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


@router.get("/post/items/{item_id:int}", response_class=HTMLResponse)
async def post_item_detail(
    item_id: int, request: Request, db: Session = Depends(get_db)
):
    item = post_svc.get_item(db, item_id)
    if not item:
        return RedirectResponse("/post", status_code=303)
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
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.get("/post/items/{item_id:int}/file")
async def post_item_file(item_id: int, db: Session = Depends(get_db)):
    item = post_svc.get_item(db, item_id)
    if not item or not item.local_path:
        return RedirectResponse("/post", status_code=303)
    path = Path(item.local_path)
    if not path.is_file():
        return RedirectResponse(
            f"/post/items/{item_id}?error={url_quote('File missing on disk')}",
            status_code=303,
        )
    media = item.content_type or "application/pdf"
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        content_disposition_type="inline",
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
