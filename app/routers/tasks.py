"""Practice task ledger routes + Outlook/Grok import."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client, Job
from app.models.practice_task import PracticeTask
from app.services.practice_tasks import (
    TASK_PRIORITIES,
    TASK_STATUSES,
    complete_task,
    create_task,
    list_tasks,
    reorder_tasks,
)
from app.services import task_import as task_imp
from app.templating import render

router = APIRouter(tags=["tasks"])


def _parse_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_money(value: str) -> float:
    try:
        return float((value or "0").replace("£", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def _user(request: Request) -> str:
    return (request.session.get("user") or "").strip() or "user"


def _active_clients(db: Session, limit: int = 400):
    return (
        db.query(Client)
        .filter(Client.overall_status.notin_(["Inactive"]))
        .order_by(Client.company_name)
        .limit(limit)
        .all()
    )


def _safe_return_path(value: str, default: str = "/tasks") -> str:
    """Only allow same-app relative paths for post-save redirects."""
    dest = (value or "").strip() or default
    if not dest.startswith("/") or dest.startswith("//"):
        return default
    if "\n" in dest or "\r" in dest:
        return default
    return dest


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_list(
    request: Request,
    status: str = "",
    client_id: str = "",
    priority: str = "",
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    tasks = list_tasks(
        db,
        status=status or "",
        client_id=cid,
        priority=priority or "",
        include_closed=bool(status),
    )
    clients = _active_clients(db)
    total_fees = round(sum(float(t.fee or 0) for t in tasks if not t.is_closed()), 2)
    open_n = sum(1 for t in tasks if not t.is_closed())
    email_n = sum(1 for t in tasks if t.is_from_email() and not t.is_closed())
    overdue_n = sum(1 for t in tasks if t.is_overdue())
    unlinked_n = sum(1 for t in tasks if not t.client_id and not t.is_closed())
    return render(
        request,
        "tasks/list.html",
        {
            "tasks": tasks,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "status": status,
            "priority": priority,
            "clients": clients,
            "filter_client_id": cid,
            "total_fees": total_fees,
            "open_n": open_n,
            "email_n": email_n,
            "overdue_n": overdue_n,
            "unlinked_n": unlinked_n,
            "today": date.today(),
            "import_msg": request.query_params.get("import_msg", ""),
            "msg": request.query_params.get("msg", ""),
            # Drag reorder when not filtered to a single client (full open list)
            "reorder_enabled": not cid,
        },
    )


@router.post("/tasks/reorder")
async def tasks_reorder(request: Request, db: Session = Depends(get_db)):
    """JSON body: {\"order\": [task_id, …]} top → bottom. Returns JSON."""
    from fastapi.responses import JSONResponse

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "Invalid JSON"}
        )
    raw = data.get("order") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return JSONResponse(
            status_code=400, content={"ok": False, "error": "order must be a list of ids"}
        )
    n = reorder_tasks(db, raw)
    return JSONResponse(content={"ok": True, "updated": n, "count": len(raw)})


@router.get("/tasks/import", response_class=HTMLResponse)
async def tasks_import_get(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "tasks/import.html",
        {
            "preview": False,
            "rows": [],
            "paste_text": "",
            "counts": {},
            "clients": _active_clients(db, 500),
            "priorities": TASK_PRIORITIES,
            "error": request.query_params.get("error", ""),
            "review_json": "",
        },
    )


async def _read_upload_or_paste(
    paste_text: str, file: Optional[UploadFile]
) -> tuple:
    """Return (text, source_kind) where source_kind is paste|csv|xlsx."""
    if file and file.filename:
        raw = await file.read()
        name = (file.filename or "").lower()
        if name.endswith((".xlsx", ".xlsm")):
            try:
                from app.services.import_csv import excel_bytes_to_csv_text

                return excel_bytes_to_csv_text(raw), "csv"
            except Exception as exc:
                raise ValueError(f"Could not read Excel: {exc}") from exc
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(enc), "csv"
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace"), "csv"
    return (paste_text or "").strip(), "paste"


@router.post("/tasks/import", response_class=HTMLResponse)
async def tasks_import_post(
    request: Request,
    action: str = Form("preview"),
    paste_text: str = Form(""),
    review_json: str = Form(""),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    clients = _active_clients(db, 500)
    act = (action or "preview").strip().lower()

    if act == "commit":
        try:
            rows = task_imp.deserialize_review(review_json)
        except Exception:
            return RedirectResponse(
                f"/tasks/import?error={url_quote('Review data expired — paste again.')}",
                status_code=303,
            )
        form = dict(await request.form())
        rows = task_imp.apply_review_overrides(rows, form)
        result = task_imp.commit_rows(db, rows, uploaded_by=_user(request))
        msg = (
            f"{result['created']} task(s) created"
            f", {result['skipped_dupe']} duplicate(s) skipped"
            f", {result['skipped']} other skipped"
        )
        if result["errors"]:
            msg += f" · {len(result['errors'])} error(s)"
        return RedirectResponse(
            f"/tasks?import_msg={url_quote(msg)}", status_code=303
        )

    # Preview
    try:
        text, kind = await _read_upload_or_paste(paste_text, file)
    except ValueError as exc:
        return render(
            request,
            "tasks/import.html",
            {
                "preview": False,
                "rows": [],
                "paste_text": paste_text,
                "counts": {},
                "clients": clients,
                "priorities": TASK_PRIORITIES,
                "error": str(exc),
                "review_json": "",
            },
        )

    if not text:
        return render(
            request,
            "tasks/import.html",
            {
                "preview": False,
                "rows": [],
                "paste_text": paste_text,
                "counts": {},
                "clients": clients,
                "priorities": TASK_PRIORITIES,
                "error": "Paste a list or upload a CSV/Excel file.",
                "review_json": "",
            },
        )

    if kind == "csv" or (
        "\t" in text
        or (text.count(",") >= 2 and "\n" in text and re_looks_like_csv(text))
    ):
        # Prefer CSV if has header-ish first line
        if kind == "csv" or _looks_like_csv(text):
            rows = task_imp.parse_task_csv(text)
            if not rows:
                rows = task_imp.parse_task_paste(text)
        else:
            rows = task_imp.parse_task_paste(text)
    else:
        rows = task_imp.parse_task_paste(text)

    if not rows:
        return render(
            request,
            "tasks/import.html",
            {
                "preview": False,
                "rows": [],
                "paste_text": paste_text or text[:2000],
                "counts": {},
                "clients": clients,
                "priorities": TASK_PRIORITIES,
                "error": "No tasks found in the pasted text or file.",
                "review_json": "",
            },
        )

    rows = task_imp.enrich_rows(db, rows)
    counts = task_imp.summary_counts(rows)
    return render(
        request,
        "tasks/import.html",
        {
            "preview": True,
            "rows": rows,
            "paste_text": paste_text if kind == "paste" else "",
            "counts": counts,
            "clients": clients,
            "priorities": TASK_PRIORITIES,
            "error": "",
            "review_json": task_imp.serialize_review(rows),
        },
    )


def re_looks_like_csv(text: str) -> bool:
    return _looks_like_csv(text)


def _looks_like_csv(text: str) -> bool:
    first = (text or "").splitlines()[0].lower() if text else ""
    headers = ("subject", "title", "task", "client", "company", "due", "priority")
    return any(h in first for h in headers)


@router.get("/tasks/new", response_class=HTMLResponse)
async def task_new_form(
    request: Request,
    client_id: str = "",
    job_id: str = "",
    db: Session = Depends(get_db),
):
    clients = _active_clients(db)
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    jobs = []
    if cid:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == cid)
            .filter(Job.status.notin_(["Cancelled"]))
            .order_by(Job.id.desc())
            .limit(50)
            .all()
        )
    ret = _safe_return_path(request.query_params.get("next") or "/tasks")
    return render(
        request,
        "tasks/form.html",
        {
            "task": None,
            "clients": clients,
            "jobs": jobs,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "pre_client_id": cid,
            "pre_job_id": jid,
            "next_url": ret,
        },
    )


@router.post("/tasks/new")
async def task_create_route(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    due_on: str = Form(""),
    period_end: str = Form(""),
    client_id: str = Form(""),
    job_id: str = Form(""),
    notes: str = Form(""),
    priority: str = Form("Medium"),
    source_email_date: str = Form(""),
    outlook_message_id: str = Form(""),
    next: str = Form("/tasks"),
    db: Session = Depends(get_db),
):
    cid = int(client_id) if (client_id or "").isdigit() else None
    jid = int(job_id) if (job_id or "").isdigit() else None
    task = create_task(
        db,
        title=(title or "").strip() or "Task",
        description=(description or "").strip() or None,
        fee=_parse_money(fee),
        status=status if status in TASK_STATUSES else "Planned",
        due_on=_parse_date(due_on),
        period_end=_parse_date(period_end),
        client_id=cid,
        job_id=jid,
        notes=(notes or "").strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "Medium",
        source_email_date=_parse_date(source_email_date),
    )
    oid = (outlook_message_id or "").strip()
    if oid:
        task.outlook_message_id = oid
        task.outlook_archive_status = "none"
        db.commit()
    dest = _safe_return_path(next, "/tasks")
    return RedirectResponse(dest, status_code=303)


@router.get("/tasks/{task_id:int}/edit", response_class=HTMLResponse)
async def task_edit_form(
    task_id: int,
    request: Request,
    next: str = "",
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if not task:
        return RedirectResponse("/tasks", status_code=303)
    clients = _active_clients(db)
    jobs = []
    if task.client_id:
        jobs = (
            db.query(Job)
            .filter(Job.client_id == task.client_id)
            .order_by(Job.id.desc())
            .limit(50)
            .all()
        )
    # Prefer explicit next=, else referer if it was a task list, else /tasks
    ret = _safe_return_path(next)
    if not (next or "").strip():
        ref = (request.headers.get("referer") or "").strip()
        if "/tasks" in ref and "/edit" not in ref:
            # Keep path+query of the list they came from
            try:
                from urllib.parse import urlparse

                p = urlparse(ref)
                if p.path.startswith("/tasks"):
                    ret = _safe_return_path(
                        p.path + (("?" + p.query) if p.query else ""),
                        "/tasks",
                    )
            except Exception:
                ret = "/tasks"
        else:
            ret = "/tasks"
    return render(
        request,
        "tasks/form.html",
        {
            "task": task,
            "clients": clients,
            "jobs": jobs,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "pre_client_id": task.client_id,
            "pre_job_id": task.job_id,
            "next_url": ret,
        },
    )


@router.post("/tasks/{task_id:int}/edit")
async def task_update(
    task_id: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    fee: str = Form("0"),
    status: str = Form("Planned"),
    due_on: str = Form(""),
    period_end: str = Form(""),
    client_id: str = Form(""),
    job_id: str = Form(""),
    notes: str = Form(""),
    priority: str = Form("Medium"),
    source_email_date: str = Form(""),
    outlook_message_id: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if not task:
        return RedirectResponse("/tasks", status_code=303)
    task.title = (title or "").strip() or task.title
    task.description = (description or "").strip() or None
    task.fee = _parse_money(fee)
    task.status = status if status in TASK_STATUSES else task.status
    task.due_on = _parse_date(due_on)
    task.period_end = _parse_date(period_end)
    task.client_id = int(client_id) if (client_id or "").isdigit() else None
    task.job_id = int(job_id) if (job_id or "").isdigit() else None
    task.notes = (notes or "").strip() or None
    task.priority = priority if priority in TASK_PRIORITIES else (task.priority or "Medium")
    task.source_email_date = _parse_date(source_email_date)
    oid = (outlook_message_id or "").strip()
    if oid != (task.outlook_message_id or ""):
        task.outlook_message_id = oid or None
        task.outlook_archive_status = "none" if oid else None
        task.outlook_archived_at = None
    task.updated_at = datetime.utcnow()
    db.commit()
    # After save, return to the list (or page) the user came from
    dest = _safe_return_path(next, "/tasks")
    return RedirectResponse(dest, status_code=303)


@router.post("/tasks/{task_id:int}/complete")
async def task_complete(
    task_id: int,
    next: str = Form("/tasks"),
    archive_outlook: str = Form("1"),
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    archive_note = ""
    if task:
        _t, archive_note = complete_task(
            db,
            task,
            archive_outlook=archive_outlook not in ("0", "false", "no", "off"),
        )
    dest = (next or "/tasks").strip()
    if archive_note:
        sep = "&" if "?" in dest else "?"
        dest = f"{dest}{sep}msg={url_quote('Completed · ' + archive_note)}"
    elif task:
        sep = "&" if "?" in dest else "?"
        dest = f"{dest}{sep}msg={url_quote('Task completed')}"
    return RedirectResponse(dest, status_code=303)


@router.post("/tasks/{task_id:int}/priority")
async def task_set_priority(
    task_id: int,
    priority: str = Form("Medium"),
    next: str = Form("/tasks"),
    db: Session = Depends(get_db),
):
    task = db.query(PracticeTask).filter(PracticeTask.id == task_id).first()
    if task and priority in TASK_PRIORITIES:
        task.priority = priority
        task.updated_at = datetime.utcnow()
        db.commit()
    return RedirectResponse((next or "/tasks").strip(), status_code=303)
