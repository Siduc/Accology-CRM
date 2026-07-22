"""Sales Ledger UI: invoices, payments, ageing, chase, quotes."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload

from app.config import CHASE_LIVE_MODE
from app.database import get_db
from app.models import Client, Job
from app.models.sales import (
    DebtChaseAction,
    Invoice,
    Payment,
    Quote,
    Service,
)
from app.services.chase_emails import (
    STAGE_LABELS,
    STAGE_ORDER,
    build_chase_email,
    send_email,
    smtp_configured,
    stage_for_days,
)
from app.services.sales_ledger import (
    CHASE_TYPES,
    ageing_report,
    backfill_invoices_from_jobs,
    build_legal_export_zip,
    chase_pipeline_rows,
    chase_status_summary,
    create_invoice,
    create_quote,
    debtors_total,
    invoice_age_days,
    invoice_from_quote,
    invoice_overdue_days,
    outstanding_invoices,
    record_payment,
    seed_services,
    suggested_chase_action,
)
from app.templating import render

router = APIRouter(prefix="/sales", tags=["sales"])

EMAIL_STAGES = set(STAGE_ORDER)


def _session_user(request: Request) -> str | None:
    user = request.session.get("user") if hasattr(request, "session") else None
    return str(user) if user else None


def _client_email(client: Client | None) -> str:
    if not client:
        return ""
    return (client.email or "").strip()


def _parse_date(value: str):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _money(value: str) -> float:
    try:
        return float((value or "0").replace("£", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@router.get("", response_class=HTMLResponse)
async def sales_home(request: Request, db: Session = Depends(get_db)):
    seed_services(db)
    total, count = debtors_total(db)
    ageing = ageing_report(db)
    overdue = sum(b.amount for b in ageing if b.label != "0–30")
    chase_sum = chase_status_summary(db)
    chase_sum["live_mode"] = CHASE_LIVE_MODE
    return render(
        request,
        "sales/home.html",
        {
            "debtors_total": total,
            "debtors_count": count,
            "overdue_total": round(overdue, 2),
            "ageing": ageing,
            "invoice_count": db.query(Invoice).count(),
            "payment_count": db.query(Payment).count(),
            "quote_count": db.query(Quote).count(),
            "chase_summary": chase_sum,
            "chase_live": CHASE_LIVE_MODE,
        },
    )


@router.post("/backfill", response_class=HTMLResponse)
async def sales_backfill(request: Request, db: Session = Depends(get_db)):
    result = backfill_invoices_from_jobs(db)
    return RedirectResponse(
        f"/sales?backfill_created={result['created']}&backfill_skipped={result['skipped']}",
        status_code=303,
    )


@router.get("/invoices", response_class=HTMLResponse)
async def invoice_list(
    request: Request,
    status: str = Query(""),
    q: str = Query(""),
    db: Session = Depends(get_db),
):
    seed_services(db)
    query = db.query(Invoice).order_by(Invoice.issue_date.desc())
    if status == "outstanding":
        query = query.filter(Invoice.balance > 0.001).filter(
            Invoice.status.notin_(["void", "written_off", "draft"])
        )
    elif status:
        query = query.filter(Invoice.status == status)
    invoices = query.limit(500).all()
    if q:
        needle = q.strip().lower()
        clients = {
            c.id: c
            for c in db.query(Client).filter(Client.id.in_({i.client_id for i in invoices})).all()
        }
        invoices = [
            i
            for i in invoices
            if needle in (i.number or "").lower()
            or needle in (clients.get(i.client_id).display_name().lower() if clients.get(i.client_id) else "")
        ]
    client_map = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({i.client_id for i in invoices} or {-1}))
        .all()
    }
    today = date.today()
    rows = [
        {
            "inv": i,
            "client": client_map.get(i.client_id),
            "age": invoice_age_days(i, today),
        }
        for i in invoices
    ]
    return render(
        request,
        "sales/invoices.html",
        {"rows": rows, "status": status, "q": q, "today": today},
    )


@router.get("/invoices/new", response_class=HTMLResponse)
async def invoice_new_form(
    request: Request,
    client_id: int = Query(None),
    job_id: int = Query(None),
    db: Session = Depends(get_db),
):
    seed_services(db)
    clients = (
        db.query(Client)
        .filter(Client.overall_status != "Inactive")
        .order_by(Client.company_name)
        .all()
    )
    services = db.query(Service).filter(Service.is_active.is_(True)).order_by(Service.name).all()
    job = db.query(Job).filter(Job.id == job_id).first() if job_id else None
    return render(
        request,
        "sales/invoice_form.html",
        {
            "clients": clients,
            "services": services,
            "selected_client_id": client_id or (job.client_id if job else None),
            "job": job,
            "error": None,
            "today": date.today(),
        },
    )


@router.post("/invoices/new", response_class=HTMLResponse)
async def invoice_create(
    request: Request,
    client_id: int = Form(...),
    job_id: str = Form(""),
    issue_date: str = Form(""),
    due_date: str = Form(""),
    notes: str = Form(""),
    line_desc_1: str = Form(""),
    line_service_1: str = Form(""),
    line_qty_1: str = Form("1"),
    line_price_1: str = Form("0"),
    line_vat_1: str = Form("0"),
    line_desc_2: str = Form(""),
    line_service_2: str = Form(""),
    line_qty_2: str = Form("1"),
    line_price_2: str = Form("0"),
    line_vat_2: str = Form("0"),
    line_desc_3: str = Form(""),
    line_service_3: str = Form(""),
    line_qty_3: str = Form("1"),
    line_price_3: str = Form("0"),
    line_vat_3: str = Form("0"),
    db: Session = Depends(get_db),
):
    lines = []
    for desc, svc, qty, price, vat in [
        (line_desc_1, line_service_1, line_qty_1, line_price_1, line_vat_1),
        (line_desc_2, line_service_2, line_qty_2, line_price_2, line_vat_2),
        (line_desc_3, line_service_3, line_qty_3, line_price_3, line_vat_3),
    ]:
        if not (desc or "").strip() and _money(price) <= 0:
            continue
        sid = int(svc) if (svc or "").isdigit() else None
        if sid and not (desc or "").strip():
            s = db.query(Service).filter(Service.id == sid).first()
            desc = s.name if s else "Service"
        lines.append(
            {
                "service_id": sid,
                "description": (desc or "Service").strip(),
                "qty": _money(qty) or 1,
                "unit_price": _money(price),
                "vat_rate": _money(vat),
            }
        )
    if not lines:
        return RedirectResponse("/sales/invoices/new?error=1", status_code=303)
    jid = int(job_id) if (job_id or "").isdigit() else None
    inv = create_invoice(
        db,
        client_id=client_id,
        job_id=jid,
        issue_date=_parse_date(issue_date),
        due_date=_parse_date(due_date),
        notes=notes or None,
        source="job" if jid else "manual",
        status="sent",
        lines=lines,
    )
    return RedirectResponse(f"/sales/invoices/{inv.id}", status_code=303)


@router.get("/invoices/{invoice_id:int}", response_class=HTMLResponse)
async def invoice_detail(
    invoice_id: int, request: Request, db: Session = Depends(get_db)
):
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)
    client = db.query(Client).filter(Client.id == inv.client_id).first()
    job = db.query(Job).filter(Job.id == inv.job_id).first() if inv.job_id else None
    chase = (
        db.query(DebtChaseAction)
        .filter(DebtChaseAction.invoice_id == inv.id)
        .order_by(DebtChaseAction.action_date.desc())
        .all()
    )
    today = date.today()
    age = invoice_age_days(inv, today)
    overdue = invoice_overdue_days(inv, today)
    suggest = suggested_chase_action(overdue)
    return render(
        request,
        "sales/invoice_detail.html",
        {
            "inv": inv,
            "client": client,
            "job": job,
            "chase": chase,
            "age": age,
            "overdue": overdue,
            "chase_types": CHASE_TYPES,
            "stage_labels": STAGE_LABELS,
            "suggest": suggest,
            "today": today,
            "chase_live": CHASE_LIVE_MODE,
            "smtp_ok": smtp_configured(),
            "client_email": _client_email(client),
        },
    )


@router.post("/invoices/{invoice_id:int}/status", response_class=HTMLResponse)
async def invoice_status(
    invoice_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if inv and status in ("draft", "sent", "void", "written_off"):
        inv.status = status
        if status in ("void", "written_off"):
            inv.balance = 0.0
        db.commit()
    return RedirectResponse(f"/sales/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id:int}/chase", response_class=HTMLResponse)
async def invoice_chase(
    invoice_id: int,
    request: Request,
    action_type: str = Form(...),
    notes: str = Form(""),
    next_action_date: str = Form(""),
    channel: str = Form("note"),
    db: Session = Depends(get_db),
):
    """Log a chase note / call / hold (not an automated email send)."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/chase", status_code=303)
    at = (action_type or "note").strip().lower()
    ch = (channel or "note").strip().lower()
    stage = at if at in EMAIL_STAGES or at == "hold" else None
    if at in EMAIL_STAGES:
        # Email stages should use the dedicated send endpoint; treat as note log
        ch = ch if ch in ("note", "call", "voice") else "note"
    db.add(
        DebtChaseAction(
            invoice_id=inv.id,
            client_id=inv.client_id,
            action_type=at,
            stage=stage,
            channel=ch,
            action_date=date.today(),
            notes=notes or None,
            next_action_date=_parse_date(next_action_date),
            send_status="logged",
            created_by=_session_user(request),
        )
    )
    db.commit()
    return RedirectResponse(f"/sales/invoices/{invoice_id}?chase_logged=1", status_code=303)


@router.get("/payments", response_class=HTMLResponse)
async def payment_list(request: Request, db: Session = Depends(get_db)):
    payments = db.query(Payment).order_by(Payment.payment_date.desc()).limit(200).all()
    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({p.client_id for p in payments} or {-1}))
        .all()
    }
    return render(
        request,
        "sales/payments.html",
        {"payments": payments, "clients": clients},
    )


@router.get("/payments/new", response_class=HTMLResponse)
async def payment_new_form(
    request: Request,
    client_id: int = Query(None),
    invoice_id: int = Query(None),
    db: Session = Depends(get_db),
):
    clients = db.query(Client).order_by(Client.company_name).all()
    open_inv = []
    if client_id:
        open_inv = (
            db.query(Invoice)
            .filter(Invoice.client_id == client_id, Invoice.balance > 0.001)
            .order_by(Invoice.issue_date)
            .all()
        )
    return render(
        request,
        "sales/payment_form.html",
        {
            "clients": clients,
            "selected_client_id": client_id,
            "open_invoices": open_inv,
            "preselect_invoice_id": invoice_id,
            "today": date.today(),
        },
    )


@router.post("/payments/new", response_class=HTMLResponse)
async def payment_create(
    request: Request,
    client_id: int = Form(...),
    amount: str = Form(...),
    payment_date: str = Form(""),
    method: str = Form("bank"),
    reference: str = Form(""),
    notes: str = Form(""),
    post_to_bank: str = Form(""),
    alloc_invoice_id: str = Form(""),
    alloc_amount: str = Form(""),
    db: Session = Depends(get_db),
):
    amt = _money(amount)
    allocations = []
    if (alloc_invoice_id or "").isdigit() and _money(alloc_amount) > 0:
        allocations.append((int(alloc_invoice_id), _money(alloc_amount)))
    elif (alloc_invoice_id or "").isdigit():
        inv = db.query(Invoice).filter(Invoice.id == int(alloc_invoice_id)).first()
        if inv:
            allocations.append((inv.id, min(amt, float(inv.balance or 0))))
    pay = record_payment(
        db,
        client_id=client_id,
        amount=amt,
        payment_date=_parse_date(payment_date),
        method=method or "bank",
        reference=reference or None,
        notes=notes or None,
        invoice_allocations=allocations or None,
        post_to_bank=post_to_bank == "yes",
    )
    return RedirectResponse(f"/sales/payments?paid={pay.id}", status_code=303)


@router.get("/ageing", response_class=HTMLResponse)
async def sales_ageing(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    buckets = ageing_report(db, today)
    invs = outstanding_invoices(db)
    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({i.client_id for i in invs} or {-1}))
        .all()
    }
    rows = sorted(
        [
            {
                "inv": i,
                "client": clients.get(i.client_id),
                "age": invoice_age_days(i, today),
            }
            for i in invs
        ],
        key=lambda r: -r["age"],
    )
    total, count = debtors_total(db)
    return render(
        request,
        "sales/ageing.html",
        {
            "buckets": buckets,
            "rows": rows,
            "total": total,
            "count": count,
            "today": today,
        },
    )


@router.get("/chase", response_class=HTMLResponse)
async def sales_chase(
    request: Request,
    msg: str = Query(""),
    db: Session = Depends(get_db),
):
    today = date.today()
    pipeline = chase_pipeline_rows(db, today)
    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({r["inv"].client_id for r in pipeline} or {-1}))
        .all()
    }
    rows = []
    for r in pipeline:
        inv = r["inv"]
        client = clients.get(inv.client_id)
        rows.append(
            {
                **r,
                "client": client,
                "client_email": _client_email(client),
                "stage_label": STAGE_LABELS.get(r["suggest"], r["suggest"]),
            }
        )
    summary = chase_status_summary(db, today)
    summary["live_mode"] = CHASE_LIVE_MODE
    return render(
        request,
        "sales/chase.html",
        {
            "rows": rows,
            "chase_types": CHASE_TYPES,
            "stage_labels": STAGE_LABELS,
            "stage_order": STAGE_ORDER,
            "today": today,
            "summary": summary,
            "chase_live": CHASE_LIVE_MODE,
            "smtp_ok": smtp_configured(),
            "msg": msg,
        },
    )


@router.get("/chase/preview/{invoice_id:int}", response_class=HTMLResponse)
async def chase_preview(
    invoice_id: int,
    request: Request,
    stage: str = Query(""),
    db: Session = Depends(get_db),
):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/chase", status_code=303)
    client = db.query(Client).filter(Client.id == inv.client_id).first()
    today = date.today()
    overdue = invoice_overdue_days(inv, today)
    st = (stage or stage_for_days(overdue) or "polite").strip().lower()
    if st not in EMAIL_STAGES:
        st = "polite"
    to, subject, body = build_chase_email(
        stage=st,
        client_name=client.display_name() if client else f"Client {inv.client_id}",
        client_email=_client_email(client),
        invoice_number=inv.number or str(inv.id),
        balance=float(inv.balance or 0),
        issue_date=str(inv.issue_date or ""),
        due_date=str(inv.due_date or ""),
        age_days=overdue,
    )
    return render(
        request,
        "sales/chase_preview.html",
        {
            "inv": inv,
            "client": client,
            "stage": st,
            "stage_label": STAGE_LABELS.get(st, st),
            "email_to": to,
            "email_subject": subject,
            "email_body": body,
            "overdue": overdue,
            "chase_live": CHASE_LIVE_MODE,
            "smtp_ok": smtp_configured(),
            "stage_order": STAGE_ORDER,
            "stage_labels": STAGE_LABELS,
        },
    )


@router.post("/chase/send/{invoice_id:int}", response_class=HTMLResponse)
async def chase_send(
    invoice_id: int,
    request: Request,
    stage: str = Form("polite"),
    email_to: str = Form(""),
    email_subject: str = Form(""),
    email_body: str = Form(""),
    force_dry_run: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Send (or dry-run) an escalating chase email.
    When CHASE_LIVE_MODE is false, always logs as dry_run / blocked_not_live — never SMTP.
    """
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/chase", status_code=303)
    client = db.query(Client).filter(Client.id == inv.client_id).first()
    today = date.today()
    overdue = invoice_overdue_days(inv, today)
    st = (stage or "polite").strip().lower()
    if st not in EMAIL_STAGES:
        st = "polite"

    to = (email_to or "").strip() or _client_email(client)
    subject = (email_subject or "").strip()
    body = (email_body or "").strip()
    if not subject or not body:
        to2, subject2, body2 = build_chase_email(
            stage=st,
            client_name=client.display_name() if client else f"Client {inv.client_id}",
            client_email=to,
            invoice_number=inv.number or str(inv.id),
            balance=float(inv.balance or 0),
            issue_date=str(inv.issue_date or ""),
            due_date=str(inv.due_date or ""),
            age_days=overdue,
        )
        to = to or to2
        subject = subject or subject2
        body = body or body2

    want_live = CHASE_LIVE_MODE and force_dry_run != "1"
    if want_live:
        ok, status = send_email(to, subject, body)
        send_status = status if ok else (status if status.startswith("failed") else status)
    else:
        # Practice mode: never hit SMTP
        if not CHASE_LIVE_MODE:
            send_status = "blocked_not_live"
        else:
            send_status = "dry_run"
        ok = False

    db.add(
        DebtChaseAction(
            invoice_id=inv.id,
            client_id=inv.client_id,
            action_type=st,
            stage=st,
            channel="email",
            action_date=today,
            notes=f"Email chase ({st}): {send_status}"
            + (f" → {to}" if to else " (no recipient)"),
            email_to=to or None,
            email_subject=subject,
            email_body=body,
            send_status=send_status,
            next_action_date=today + timedelta(days=7),
            created_by=_session_user(request),
        )
    )
    db.commit()
    flag = "sent" if send_status == "sent" else send_status
    return RedirectResponse(
        f"/sales/chase?msg={url_quote(f'Invoice {inv.number}: {flag}')}",
        status_code=303,
    )


@router.post("/chase/batch", response_class=HTMLResponse)
async def chase_batch(
    request: Request,
    stage_filter: str = Form(""),
    dry_run: str = Form("1"),
    db: Session = Depends(get_db),
):
    """
    Batch prepare/send for a stage band. Default is dry-run even when live mode is on
    unless dry_run is explicitly cleared and CHASE_LIVE_MODE is true.
    """
    today = date.today()
    pipeline = chase_pipeline_rows(db, today)
    sf = (stage_filter or "").strip().lower()
    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({r["inv"].client_id for r in pipeline} or {-1}))
        .all()
    }
    processed = 0
    live_attempt = CHASE_LIVE_MODE and dry_run != "1"
    for r in pipeline:
        if r["on_hold"]:
            continue
        st = r["suggest"]
        if sf and st != sf:
            continue
        if st not in EMAIL_STAGES:
            continue
        inv = r["inv"]
        client = clients.get(inv.client_id)
        to, subject, body = build_chase_email(
            stage=st,
            client_name=client.display_name() if client else f"Client {inv.client_id}",
            client_email=_client_email(client),
            invoice_number=inv.number or str(inv.id),
            balance=float(inv.balance or 0),
            issue_date=str(inv.issue_date or ""),
            due_date=str(inv.due_date or ""),
            age_days=r["overdue"],
        )
        if live_attempt:
            ok, status = send_email(to, subject, body)
            send_status = status
        else:
            send_status = "dry_run" if CHASE_LIVE_MODE else "blocked_not_live"
        db.add(
            DebtChaseAction(
                invoice_id=inv.id,
                client_id=inv.client_id,
                action_type=st,
                stage=st,
                channel="email",
                action_date=today,
                notes=f"Batch chase ({st}): {send_status}",
                email_to=to or None,
                email_subject=subject,
                email_body=body,
                send_status=send_status,
                next_action_date=today + timedelta(days=7),
                created_by=_session_user(request),
            )
        )
        processed += 1
    db.commit()
    mode = "live" if live_attempt else ("dry-run" if CHASE_LIVE_MODE else "practice")
    return RedirectResponse(
        f"/sales/chase?msg={url_quote(f'Batch {mode}: {processed} invoices logged')}",
        status_code=303,
    )


@router.get("/chase/export")
async def chase_legal_export(
    request: Request,
    min_days: int = Query(60),
    solicitor: str = Query("Thomas Higgins"),
    client_id: int = Query(None),
    db: Session = Depends(get_db),
):
    """ZIP pack for legal handover (Thomas Higgins or alternative solicitor)."""
    data = build_legal_export_zip(
        db,
        min_days=max(1, min_days),
        client_id=client_id,
        solicitor_name=(solicitor or "Thomas Higgins").strip() or "Thomas Higgins",
    )
    # Log export action on each included invoice
    today = date.today()
    invs = outstanding_invoices(db)
    user = _session_user(request)
    for inv in invs:
        if invoice_overdue_days(inv, today) < max(1, min_days):
            continue
        if client_id and inv.client_id != client_id:
            continue
        db.add(
            DebtChaseAction(
                invoice_id=inv.id,
                client_id=inv.client_id,
                action_type="export",
                stage="legal",
                channel="export",
                action_date=today,
                notes=f"Legal handover pack → {solicitor or 'Thomas Higgins'}",
                send_status="exported",
                created_by=user,
            )
        )
    db.commit()
    fname = f"legal-handover-{today.isoformat()}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/quotes", response_class=HTMLResponse)
async def quote_list(request: Request, db: Session = Depends(get_db)):
    quotes = db.query(Quote).order_by(Quote.issue_date.desc()).limit(200).all()
    clients = {
        c.id: c
        for c in db.query(Client)
        .filter(Client.id.in_({q.client_id for q in quotes} or {-1}))
        .all()
    }
    return render(
        request, "sales/quotes.html", {"quotes": quotes, "clients": clients}
    )


@router.get("/quotes/new", response_class=HTMLResponse)
async def quote_new_form(
    request: Request,
    client_id: int = Query(None),
    db: Session = Depends(get_db),
):
    seed_services(db)
    clients = db.query(Client).order_by(Client.company_name).all()
    services = db.query(Service).filter(Service.is_active.is_(True)).order_by(Service.name).all()
    return render(
        request,
        "sales/quote_form.html",
        {
            "clients": clients,
            "services": services,
            "selected_client_id": client_id,
            "today": date.today(),
        },
    )


@router.post("/quotes/new", response_class=HTMLResponse)
async def quote_create(
    request: Request,
    client_id: int = Form(...),
    notes: str = Form(""),
    line_desc_1: str = Form(""),
    line_service_1: str = Form(""),
    line_qty_1: str = Form("1"),
    line_price_1: str = Form("0"),
    line_vat_1: str = Form("0"),
    line_desc_2: str = Form(""),
    line_service_2: str = Form(""),
    line_qty_2: str = Form("1"),
    line_price_2: str = Form("0"),
    line_vat_2: str = Form("0"),
    db: Session = Depends(get_db),
):
    lines = []
    for desc, svc, qty, price, vat in [
        (line_desc_1, line_service_1, line_qty_1, line_price_1, line_vat_1),
        (line_desc_2, line_service_2, line_qty_2, line_price_2, line_vat_2),
    ]:
        if not (desc or "").strip() and _money(price) <= 0:
            continue
        sid = int(svc) if (svc or "").isdigit() else None
        if sid and not (desc or "").strip():
            s = db.query(Service).filter(Service.id == sid).first()
            desc = s.name if s else "Service"
        lines.append(
            {
                "service_id": sid,
                "description": (desc or "Service").strip(),
                "qty": _money(qty) or 1,
                "unit_price": _money(price),
                "vat_rate": _money(vat),
            }
        )
    if not lines:
        return RedirectResponse("/sales/quotes/new", status_code=303)
    q = create_quote(
        db,
        client_id=client_id,
        lines=lines,
        notes=notes or None,
        status="sent",
        valid_until=date.today() + timedelta(days=30),
    )
    return RedirectResponse(f"/sales/quotes?created={q.id}", status_code=303)


@router.post("/quotes/{quote_id:int}/invoice", response_class=HTMLResponse)
async def quote_to_invoice(
    quote_id: int, request: Request, db: Session = Depends(get_db)
):
    q = db.query(Quote).filter(Quote.id == quote_id).first()
    if not q:
        return RedirectResponse("/sales/quotes", status_code=303)
    # load lines
    from app.models.sales import QuoteLine

    q.lines = db.query(QuoteLine).filter(QuoteLine.quote_id == q.id).all()
    inv = invoice_from_quote(db, q)
    return RedirectResponse(f"/sales/invoices/{inv.id}", status_code=303)
