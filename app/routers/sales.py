"""Sales Ledger UI: invoices, payments, ageing, chase, quotes."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
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
    DEFAULT_SALES_VAT_RATE,
    ageing_bucket_key,
    ageing_report,
    apply_default_vat_to_sales,
    backfill_invoices_from_jobs,
    build_legal_export_zip,
    chase_pipeline_rows,
    chase_status_summary,
    clear_debtors_ledger,
    coerce_sales_vat_rate,
    create_invoice,
    create_quote,
    delete_invoice,
    debtor_age_tiles,
    debtors_total,
    import_opening_balances,
    sales_day_book,
    invoice_age_days,
    invoice_from_quote,
    invoice_overdue_days,
    outstanding_invoices,
    record_payment,
    seed_services,
    suggested_chase_action,
    update_invoice,
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


def _practice_branding() -> dict:
    """Practice name / logo / letterhead for invoice documents."""
    from app.services.branding import practice_branding_context

    return practice_branding_context()


def _parse_invoice_lines_from_form(form) -> list[dict]:
    """Collect line_desc_N / line_qty_N / … from a multi-line invoice form."""
    lines: list[dict] = []
    # Support up to 12 lines on edit
    for n in range(1, 13):
        desc = (form.get(f"line_desc_{n}") or "").strip()
        svc = (form.get(f"line_service_{n}") or "").strip()
        qty = form.get(f"line_qty_{n}") or "1"
        price = form.get(f"line_price_{n}") or "0"
        # Blank VAT field → practice default 20%; explicit 0 stays zero-rated
        vat_raw = form.get(f"line_vat_{n}")
        if vat_raw is None or str(vat_raw).strip() == "":
            from app.services.sales_ledger import DEFAULT_SALES_VAT_RATE

            vat_val = DEFAULT_SALES_VAT_RATE
        else:
            vat_val = _money(vat_raw)
        if not desc and _money(price) <= 0 and not svc:
            continue
        sid = int(svc) if svc.isdigit() else None
        if sid and not desc:
            # filled later by caller if needed
            pass
        lines.append(
            {
                "service_id": sid,
                "description": desc or "Service",
                "qty": _money(qty) or 1,
                "unit_price": _money(price),
                "vat_rate": vat_val,
            }
        )
    return lines


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
            "msg": request.query_params.get("msg", ""),
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/backfill", response_class=HTMLResponse)
async def sales_backfill(request: Request, db: Session = Depends(get_db)):
    result = backfill_invoices_from_jobs(db)
    return RedirectResponse(
        f"/sales?backfill_created={result['created']}&backfill_skipped={result['skipped']}",
        status_code=303,
    )


async def _read_csv_upload(csv_file: UploadFile | None, csv_data: str) -> str:
    if csv_file and csv_file.filename:
        content = await csv_file.read()
        for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return content.decode(enc)
            except UnicodeDecodeError:
                continue
        return content.decode("utf-8", errors="replace")
    return csv_data or ""


@router.get("/opening-balances", response_class=HTMLResponse)
async def opening_balances_page(
    request: Request,
    db: Session = Depends(get_db),
    msg: str = "",
    error: str = "",
):
    total, count = debtors_total(db)
    return render(
        request,
        "sales/opening_balances.html",
        {
            "debtors_total": total,
            "debtors_count": count,
            "as_at": date.today().strftime("%d/%m/%Y"),
            "msg": msg,
            "error": error,
            "result": request.query_params.get("result", ""),
        },
    )


@router.post("/opening-balances/clear")
async def opening_balances_clear(request: Request, db: Session = Depends(get_db)):
    result = clear_debtors_ledger(db)
    msg = (
        f"Cleared {result['invoices_deleted']} invoices, "
        f"{result['payments_deleted']} payments. "
        f"Debtors now £{result['debtors_remaining']:,.2f}."
    )
    from urllib.parse import quote

    return RedirectResponse(
        f"/sales/opening-balances?msg={quote(msg)}",
        status_code=303,
    )


@router.post("/opening-balances/import", response_class=HTMLResponse)
async def opening_balances_import(
    request: Request,
    csv_file: UploadFile = File(None),
    csv_data: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        text = await _read_csv_upload(csv_file, csv_data)
    except Exception as exc:  # noqa: BLE001
        total, count = debtors_total(db)
        return render(
            request,
            "sales/opening_balances.html",
            {
                "debtors_total": total,
                "debtors_count": count,
                "as_at": date.today().strftime("%d/%m/%Y"),
                "msg": "",
                "error": str(exc),
                "result": "",
            },
            status_code=400,
        )
    result = import_opening_balances(db, text, as_at=date.today())
    total, count = debtors_total(db)
    summary = (
        f"Created {result.get('created', 0)} · "
        f"updated {result.get('updated', 0)} · "
        f"skipped {result.get('skipped', 0)} · "
        f"gross £{result.get('total_gross', 0):,.2f} · "
        f"debtors now £{total:,.2f} ({count} invoices). "
        f"Date column: {result.get('date_column', '?')}"
    )
    if result.get("date_fallbacks"):
        summary += (
            f" · {result['date_fallbacks']} row(s) fell back to today "
            f"(see issues — re-check Invoice Date format)"
        )
    err_block = ""
    if result.get("errors"):
        err_block = "\n".join(result["errors"][:40])
    return render(
        request,
        "sales/opening_balances.html",
        {
            "debtors_total": total,
            "debtors_count": count,
            "as_at": date.today().strftime("%d/%m/%Y"),
            "msg": summary,
            "error": "",
            "result": err_block or "(no row errors)",
        },
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
    line_vat_1: str = Form("0.2"),
    line_desc_2: str = Form(""),
    line_service_2: str = Form(""),
    line_qty_2: str = Form("1"),
    line_price_2: str = Form("0"),
    line_vat_2: str = Form("0.2"),
    line_desc_3: str = Form(""),
    line_service_3: str = Form(""),
    line_qty_3: str = Form("1"),
    line_price_3: str = Form("0"),
    line_vat_3: str = Form("0.2"),
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
                "vat_rate": coerce_sales_vat_rate(vat),
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
    ctx = {
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
        "msg": request.query_params.get("msg", ""),
        "error": request.query_params.get("error", ""),
    }
    ctx.update(_practice_branding())
    return render(request, "sales/invoice_detail.html", ctx)


@router.get("/invoices/{invoice_id:int}/edit", response_class=HTMLResponse)
async def invoice_edit_form(
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
    clients = (
        db.query(Client)
        .filter(Client.overall_status != "Inactive")
        .order_by(Client.company_name)
        .all()
    )
    # Ensure current client appears even if inactive
    if client and all(c.id != client.id for c in clients):
        clients = [client] + list(clients)
    services = (
        db.query(Service)
        .filter(Service.is_active.is_(True))
        .order_by(Service.name)
        .all()
    )
    # Pad lines to at least 5 editable rows
    lines = list(inv.lines or [])
    while len(lines) < 5:
        lines.append(None)
    ctx = {
        "inv": inv,
        "client": client,
        "job": job,
        "clients": clients,
        "services": services,
        "edit_lines": lines,
        "error": request.query_params.get("error", ""),
        "today": date.today(),
    }
    ctx.update(_practice_branding())
    return render(request, "sales/invoice_edit.html", ctx)


@router.post("/invoices/{invoice_id:int}/edit", response_class=HTMLResponse)
async def invoice_edit_save(
    invoice_id: int,
    request: Request,
    client_id: int = Form(...),
    number: str = Form(""),
    issue_date: str = Form(""),
    due_date: str = Form(""),
    notes: str = Form(""),
    status: str = Form("sent"),
    db: Session = Depends(get_db),
):
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)

    form = await request.form()
    lines = _parse_invoice_lines_from_form(form)
    # Resolve service names into descriptions when blank
    for row in lines:
        sid = row.get("service_id")
        if sid and (not row.get("description") or row["description"] == "Service"):
            s = db.query(Service).filter(Service.id == sid).first()
            if s:
                row["description"] = s.name

    if not lines:
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}/edit?error={url_quote('Add at least one line')}",
            status_code=303,
        )

    try:
        update_invoice(
            db,
            inv,
            client_id=client_id,
            number=number or inv.number,
            issue_date=_parse_date(issue_date) or inv.issue_date,
            due_date=_parse_date(due_date),
            notes=notes,
            status=status,
            lines=lines,
        )
    except ValueError as e:
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}/edit?error={url_quote(str(e))}",
            status_code=303,
        )

    return RedirectResponse(
        f"/sales/invoices/{invoice_id}?msg={url_quote('Invoice updated')}",
        status_code=303,
    )


def _do_invoice_discard(
    db: Session,
    inv: Invoice,
    *,
    reason: str = "Not billed",
    remember: str = "",
) -> RedirectResponse:
    """Shared discard logic (void + mark job not billable)."""
    from app.services.sales_ledger import discard_invoice_do_not_bill

    if (inv.status or "").lower() in ("paid", "part_paid"):
        return RedirectResponse(
            f"/sales/invoices/{inv.id}?error={url_quote('Cannot discard a paid invoice')}",
            status_code=303,
        )
    if float(inv.amount_paid or 0) > 0:
        return RedirectResponse(
            f"/sales/invoices/{inv.id}?error={url_quote('Cannot discard - payment already allocated')}",
            status_code=303,
        )

    job_id = inv.job_id
    client_id = inv.client_id
    discard_invoice_do_not_bill(db, inv, reason=(reason or "Not billed").strip()[:120])

    if (remember or "").strip().lower() in ("1", "yes", "on", "true") and job_id:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job and job.client_id and job.type:
            try:
                from app.services.client_billing import remember_from_job

                remember_from_job(
                    db,
                    client_id=int(job.client_id),
                    job_type=job.type or "",
                    fee=float(job.fee or 0),
                    on_done="none",
                    commit=True,
                )
            except Exception:
                pass

    if job_id:
        dest = f"/jobs/{job_id}?msg={url_quote('Invoice discarded - job marked not billed')}"
    elif client_id:
        dest = f"/clients/{client_id}?msg={url_quote('Invoice discarded - not billed')}"
    else:
        dest = f"/sales/invoices?msg={url_quote('Invoice discarded')}"
    return RedirectResponse(dest, status_code=303)


@router.post("/invoices/{invoice_id:int}/status", response_class=HTMLResponse)
async def invoice_status(
    invoice_id: int,
    request: Request,
    status: str = Form(...),
    reason: str = Form("Not billed"),
    remember: str = Form(""),
    xero_note: str = Form(""),
    db: Session = Depends(get_db),
):
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)

    # Allow discard via the status form (works even if /discard route was missing)
    st = (status or "").strip().lower()
    if st in ("do_not_bill", "discard", "not_billed", "not-billed"):
        return _do_invoice_discard(db, inv, reason=reason, remember=remember)

    if st in ("draft", "sent", "void", "written_off", "paid", "part_paid"):
        inv.status = st
        if st in ("void", "written_off"):
            inv.balance = 0.0
            # Void from dropdown also clears job billing link when unpaid
            if st == "void" and inv.job_id and float(inv.amount_paid or 0) <= 0:
                job = db.query(Job).filter(Job.id == inv.job_id).first()
                if job and (job.billing_status or "").lower() in (
                    "invoiced",
                    "draft",
                    "",
                ):
                    job.billing_status = "not_billable"
                    job.invoice_reference = None
        if st == "sent":
            # Dual-run with Xero: finalise without emailing the client
            if (xero_note or "").strip() in ("1", "yes", "on", "true"):
                tag = "Finalised in CRM (issued via Xero — no Accology email)"
                notes = (inv.notes or "").strip()
                if tag not in notes:
                    inv.notes = (notes + "\n" + tag).strip() if notes else tag
            if inv.job_id:
                job = db.query(Job).filter(Job.id == inv.job_id).first()
                if job:
                    job.billing_status = "invoiced"
                    job.invoice_reference = inv.number
        db.commit()
        msg = ""
        if st == "sent" and (xero_note or "").strip() in ("1", "yes", "on", "true"):
            msg = "?msg=" + url_quote(
                "Finalised — no email sent. You can record a payment (or use Paid in Xero)."
            )
        return RedirectResponse(f"/sales/invoices/{invoice_id}{msg}", status_code=303)
    return RedirectResponse(f"/sales/invoices/{invoice_id}", status_code=303)


@router.post("/invoices/{invoice_id:int}/xero-paid", response_class=HTMLResponse)
async def invoice_mark_paid_in_xero(
    invoice_id: int,
    request: Request,
    payment_date: str = Form(""),
    reference: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Dual-ledger helper: finalise a draft (issued in Xero, no email) and record
    full payment against the balance — e.g. Frans INV-0801 already paid in Xero.
    """
    inv = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(Invoice.id == invoice_id)
        .first()
    )
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)
    if (inv.status or "").lower() in ("void", "written_off"):
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}?error={url_quote('Cannot pay a void invoice')}",
            status_code=303,
        )

    # Finalise draft without email
    if (inv.status or "").lower() == "draft":
        inv.status = "sent"
        tag = "Finalised in CRM (issued via Xero — no Accology email)"
        notes = (inv.notes or "").strip()
        if tag not in notes:
            inv.notes = (notes + "\n" + tag).strip() if notes else tag
        if inv.job_id:
            job = db.query(Job).filter(Job.id == inv.job_id).first()
            if job:
                job.billing_status = "invoiced"
                job.invoice_reference = inv.number
        db.commit()
        db.refresh(inv)

    bal = float(inv.balance or 0)
    if bal <= 0.001 and (inv.status or "").lower() == "paid":
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}?msg={url_quote('Already paid')}",
            status_code=303,
        )
    if bal <= 0.001:
        # Totals may be wrong; use total - paid
        bal = max(0.0, float(inv.total or 0) - float(inv.amount_paid or 0))
    if bal <= 0.001:
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}?error={url_quote('Nothing left to allocate')}",
            status_code=303,
        )

    pdate = _parse_date(payment_date) or date.today()
    ref = (reference or "").strip() or f"Xero paid · {inv.number}"
    record_payment(
        db,
        client_id=int(inv.client_id),
        amount=bal,
        payment_date=pdate,
        method="bank",
        reference=ref,
        notes=f"Mirrored from Xero for {inv.number} (dual-run — no Accology email)",
        invoice_allocations=[(inv.id, bal)],
        post_to_bank=False,  # cash already in Xero bank; avoid double bank entry
    )
    return RedirectResponse(
        f"/sales/invoices/{invoice_id}?msg={url_quote(f'Finalised and paid {bal:.2f} (Xero mirror)')}",
        status_code=303,
    )


@router.post("/invoices/{invoice_id:int}/discard", response_class=HTMLResponse)
async def invoice_discard_not_bill(
    invoice_id: int,
    request: Request,
    reason: str = Form("Not billed"),
    remember: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    After Done -> draft: discard invoice and mark job not billable.
    Does not send - voids the draft and unlinks the job for billing.
    """
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)
    return _do_invoice_discard(db, inv, reason=reason, remember=remember)


@router.post("/invoices/{invoice_id:int}/delete", response_class=HTMLResponse)
async def invoice_delete_permanent(
    invoice_id: int,
    request: Request,
    confirm: str = Form(""),
    allow_payments: str = Form(""),
    db: Session = Depends(get_db),
):
    """
    Permanently delete an invoice so its number can be reused (Xero sequence).
    Prefer void/discard for audit; use delete when the number must be free again.
    """
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)
    if (confirm or "").strip().lower() not in ("1", "yes", "on", "true", "delete"):
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}?error={url_quote('Confirm delete to free the invoice number')}",
            status_code=303,
        )
    num = inv.number
    ok, msg = delete_invoice(
        db,
        inv,
        allow_with_payments=(allow_payments or "").strip().lower()
        in ("1", "yes", "on", "true"),
    )
    if not ok:
        return RedirectResponse(
            f"/sales/invoices/{invoice_id}?error={url_quote(msg[:300])}",
            status_code=303,
        )
    return RedirectResponse(
        f"/sales/invoices?msg={url_quote(msg[:300])}",
        status_code=303,
    )


@router.post("/apply-default-vat", response_class=HTMLResponse)
async def sales_apply_default_vat(
    request: Request,
    db: Session = Depends(get_db),
):
    """Set standard 20% VAT on sales lines currently at 0% (and service defaults)."""
    result = apply_default_vat_to_sales(
        db, rate=DEFAULT_SALES_VAT_RATE, only_zero_lines=True, include_void=False
    )
    msg = (
        f"Applied {DEFAULT_SALES_VAT_RATE * 100:.0f}% VAT to "
        f"{result['lines_updated']} line(s) on {result['invoices_touched']} invoice(s); "
        f"{result['services_updated']} service default(s) updated."
    )
    return RedirectResponse(
        f"/sales?msg={url_quote(msg[:400])}",
        status_code=303,
    )


@router.post("/invoices/{invoice_id}/discard", response_class=HTMLResponse)
async def invoice_discard_not_bill_loose(
    invoice_id: str,
    request: Request,
    reason: str = Form("Not billed"),
    remember: str = Form(""),
    db: Session = Depends(get_db),
):
    """Same as discard but accepts non-int path segments (defensive)."""
    try:
        iid = int(str(invoice_id).strip())
    except ValueError:
        return RedirectResponse("/sales/invoices", status_code=303)
    inv = db.query(Invoice).filter(Invoice.id == iid).first()
    if not inv:
        return RedirectResponse("/sales/invoices", status_code=303)
    return _do_invoice_discard(db, inv, reason=reason, remember=remember)


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
        # Include drafts — dual-run with Xero: pay CRM invoice even if not emailed
        open_inv = (
            db.query(Invoice)
            .filter(
                Invoice.client_id == client_id,
                Invoice.balance > 0.001,
                Invoice.status.notin_(["void", "written_off"]),
            )
            .order_by(Invoice.issue_date)
            .all()
        )
    # If a specific invoice was requested but not in list (e.g. zero balance), still load client
    preselect = invoice_id
    if invoice_id and not client_id:
        inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if inv:
            client_id = inv.client_id
            open_inv = (
                db.query(Invoice)
                .filter(
                    Invoice.client_id == client_id,
                    Invoice.balance > 0.001,
                    Invoice.status.notin_(["void", "written_off"]),
                )
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
            "preselect_invoice_id": preselect,
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
async def sales_ageing(
    request: Request,
    bucket: str = Query(""),
    client_id: str = Query(""),
    db: Session = Depends(get_db),
):
    """
    Debtors drill-down: age tiles (Total / 30 / 60 / 90 / Older) then filtered list.
    """
    today = date.today()
    tiles = debtor_age_tiles(db, today)
    total, count = debtors_total(db)
    filter_bucket = (bucket or "").strip().lower()
    if filter_bucket in ("0-30", "0–30", "30", "30days", "d30"):
        filter_bucket = "d30"
    elif filter_bucket in ("31-60", "31–60", "60", "60days", "d60"):
        filter_bucket = "d60"
    elif filter_bucket in ("61-90", "61–90", "90", "90days", "d90"):
        filter_bucket = "d90"
    elif filter_bucket in ("90+", "older", "90plus"):
        filter_bucket = "older"
    elif filter_bucket in ("total", "all"):
        # Total tile removed — show all ages as list if requested
        filter_bucket = "all"
    elif filter_bucket and filter_bucket not in ("all", "d30", "d60", "d90", "older"):
        filter_bucket = ""

    filter_client_id = int(client_id) if (client_id or "").isdigit() else None
    show_list = bool(filter_bucket)

    bucket_labels = {
        "all": "All outstanding",
        "d30": "30 days (0–30)",
        "d60": "60 days (31–60)",
        "d90": "90 days (61–90)",
        "older": "Older (90+)",
    }

    rows = []
    filter_clients = []
    if show_list:
        invs = outstanding_invoices(db)
        clients = {
            c.id: c
            for c in db.query(Client)
            .filter(Client.id.in_({i.client_id for i in invs if i.client_id} or {-1}))
            .all()
        }
        seen = {}
        for i in invs:
            age = invoice_age_days(i, today)
            bkey = ageing_bucket_key(age)
            if filter_bucket not in ("", "all") and bkey != filter_bucket:
                continue
            if filter_client_id and i.client_id != filter_client_id:
                continue
            cl = clients.get(i.client_id)
            if cl and i.client_id not in seen:
                seen[i.client_id] = cl
            rows.append(
                {
                    "inv": i,
                    "client": cl,
                    "age": age,
                    "bucket": bkey,
                }
            )
        rows.sort(key=lambda r: (-r["age"], -(r["inv"].balance or 0)))
        filter_clients = sorted(
            seen.values(), key=lambda c: (c.display_name() or "").lower()
        )

    filter_fee = round(sum(float(r["inv"].balance or 0) for r in rows), 2)
    filter_label = bucket_labels.get(filter_bucket, "") if show_list else ""

    # Sales day book: b/Fwd + Invoices − Receipts = c/Fwd (year or overall)
    day_period = (request.query_params.get("day_period") or "").strip()
    day_year = None
    if day_period.isdigit() and len(day_period) == 4:
        day_year = int(day_period)
    elif day_period in ("", "ytd"):
        day_year = today.year
    # day_period == "overall" → year None
    if day_period == "overall":
        day_year = None
    try:
        day_book = sales_day_book(db, year=day_year, today=today)
    except Exception:
        day_book = None
    day_years = list(range(today.year, today.year - 5, -1))

    return render(
        request,
        "sales/ageing.html",
        {
            "tiles": tiles,
            "rows": rows,
            "total": total,
            "count": count,
            "today": today,
            "filter_bucket": filter_bucket,
            "filter_client_id": filter_client_id,
            "filter_clients": filter_clients,
            "filter_label": filter_label,
            "filter_fee": filter_fee,
            "filter_count": len(rows),
            "show_list": show_list,
            "day_book": day_book,
            "day_years": day_years,
            "day_period": day_period or "ytd",
            "day_year": day_year,
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
    line_vat_1: str = Form("0.2"),
    line_desc_2: str = Form(""),
    line_service_2: str = Form(""),
    line_qty_2: str = Form("1"),
    line_price_2: str = Form("0"),
    line_vat_2: str = Form("0.2"),
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
                "vat_rate": coerce_sales_vat_rate(vat),
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
