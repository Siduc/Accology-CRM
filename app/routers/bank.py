"""Bank Ledger UI — Working Capital · Cash."""

from __future__ import annotations

from datetime import date, datetime  # date used in match form
from urllib.parse import quote as url_quote

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Client
from app.models.finance import CreditorBill
from app.models.sales import Invoice
from app.services.bank_ledger import (
    CATEGORY_LABELS,
    NOMINAL_CATEGORIES,
    add_transaction,
    create_account,
    delete_transaction,
    ensure_default_bank_account,
    get_account,
    import_transactions,
    ledger_rows,
    list_accounts,
    mark_duplicates,
    match_payment_to_creditor,
    match_payment_to_nominal,
    match_receipt_to_sales,
    parse_bank_csv,
    reconciliation_summary,
    set_opening_balance,
    set_primary_account,
    set_reconciled,
    total_cash,
    update_account,
)
from app.services.sales_ledger import outstanding_invoices
from app.templating import render

router = APIRouter(prefix="/bank", tags=["bank"])


def _parse_date(value: str):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_money(value: str) -> float:
    try:
        return float((value or "0").replace("£", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@router.get("", response_class=HTMLResponse)
async def bank_home(request: Request, db: Session = Depends(get_db)):
    ensure_default_bank_account(db)
    rows = list_accounts(db, active_only=False)
    active = [r for r in rows if r.account.is_active is not False]
    return render(
        request,
        "bank/home.html",
        {
            "rows": rows,
            "active_rows": active,
            "total_cash": total_cash(db),
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.get("/accounts/new", response_class=HTMLResponse)
async def bank_account_new_form(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "bank/account_form.html",
        {"account": None, "error": None, "today": date.today()},
    )


@router.post("/accounts/new", response_class=HTMLResponse)
async def bank_account_create(
    request: Request,
    name: str = Form(...),
    bank_name: str = Form(""),
    sort_code: str = Form(""),
    account_number: str = Form(""),
    opening_balance: str = Form("0"),
    currency: str = Form("GBP"),
    make_primary: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    acc = create_account(
        db,
        name=name,
        bank_name=bank_name,
        sort_code=sort_code,
        account_number=account_number,
        opening_balance=_parse_money(opening_balance),
        currency=currency or "GBP",
        make_primary=make_primary == "yes",
        notes=notes,
    )
    return RedirectResponse(f"/bank/{acc.id}?msg=created", status_code=303)


@router.get("/accounts/{account_id:int}/edit", response_class=HTMLResponse)
async def bank_account_edit_form(
    account_id: int, request: Request, db: Session = Depends(get_db)
):
    acc = get_account(db, account_id)
    if not acc:
        return RedirectResponse("/bank", status_code=303)
    return render(
        request,
        "bank/account_form.html",
        {"account": acc, "error": None, "today": date.today()},
    )


@router.post("/accounts/{account_id:int}/edit", response_class=HTMLResponse)
async def bank_account_update(
    account_id: int,
    request: Request,
    name: str = Form(...),
    bank_name: str = Form(""),
    sort_code: str = Form(""),
    account_number: str = Form(""),
    currency: str = Form("GBP"),
    is_active: str = Form("yes"),
    make_primary: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        update_account(
            db,
            account_id,
            name=name,
            bank_name=bank_name,
            sort_code=sort_code,
            account_number=account_number,
            currency=currency,
            is_active=is_active == "yes",
            make_primary=make_primary == "yes",
            notes=notes,
        )
    except ValueError:
        return RedirectResponse("/bank", status_code=303)
    return RedirectResponse(f"/bank/{account_id}?msg=updated", status_code=303)


@router.post("/accounts/{account_id:int}/primary", response_class=HTMLResponse)
async def bank_set_primary(
    account_id: int, request: Request, db: Session = Depends(get_db)
):
    try:
        set_primary_account(db, account_id)
    except ValueError:
        pass
    return RedirectResponse("/bank?msg=primary", status_code=303)


@router.get("/{account_id:int}", response_class=HTMLResponse)
async def bank_account_ledger(
    account_id: int,
    request: Request,
    reconciled: str = Query(""),
    db: Session = Depends(get_db),
):
    acc = get_account(db, account_id)
    if not acc:
        return RedirectResponse("/bank", status_code=303)
    rec_filter = None
    if reconciled == "0":
        rec_filter = False
    elif reconciled == "1":
        rec_filter = True
    rows = ledger_rows(db, account_id, reconciled=rec_filter, newest_first=True)
    from app.services.bank_ledger import account_balance

    bal = account_balance(db, acc)
    return render(
        request,
        "bank/account.html",
        {
            "account": acc,
            "rows": rows,
            "balance": bal,
            "today": date.today(),
            "categories": NOMINAL_CATEGORIES,
            "category_labels": CATEGORY_LABELS,
            "reconciled_filter": reconciled,
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/{account_id:int}/opening", response_class=HTMLResponse)
async def bank_opening(
    account_id: int,
    request: Request,
    opening_balance: str = Form("0"),
    db: Session = Depends(get_db),
):
    try:
        set_opening_balance(db, account_id, _parse_money(opening_balance))
    except ValueError:
        return RedirectResponse("/bank", status_code=303)
    return RedirectResponse(f"/bank/{account_id}?msg=opening", status_code=303)


@router.post("/{account_id:int}/transactions", response_class=HTMLResponse)
async def bank_add_txn(
    account_id: int,
    request: Request,
    txn_date: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    direction: str = Form("in"),
    reference: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    amt = abs(_parse_money(amount))
    if (direction or "in").lower() in ("out", "payment", "-"):
        amt = -amt
    if amt == 0:
        return RedirectResponse(f"/bank/{account_id}?msg=zero", status_code=303)
    cat = (category or "").strip()
    if not cat:
        cat = "other_income" if amt > 0 else "other_expense"
    try:
        add_transaction(
            db,
            account_id,
            txn_date=_parse_date(txn_date) or date.today(),
            amount=amt,
            description=description,
            reference=reference,
            category=cat,
            source="manual",
            notes=notes,
        )
    except ValueError:
        return RedirectResponse("/bank", status_code=303)
    return RedirectResponse(f"/bank/{account_id}?msg=added", status_code=303)


@router.get("/{account_id:int}/import", response_class=HTMLResponse)
async def bank_import_form(
    account_id: int, request: Request, db: Session = Depends(get_db)
):
    acc = get_account(db, account_id)
    if not acc:
        return RedirectResponse("/bank", status_code=303)
    return render(
        request,
        "bank/import.html",
        {
            "account": acc,
            "preview": None,
            "csv_format": "auto",
            "error": None,
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/{account_id:int}/import", response_class=HTMLResponse)
async def bank_import_post(
    account_id: int,
    request: Request,
    csv_format: str = Form("auto"),
    csv_data: str = Form(""),
    action: str = Form("preview"),
    csv_file: UploadFile = File(None),
    db: Session = Depends(get_db),
):
    acc = get_account(db, account_id)
    if not acc:
        return RedirectResponse("/bank", status_code=303)

    text = (csv_data or "").strip()
    if csv_file is not None and getattr(csv_file, "filename", None):
        raw = await csv_file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")

    if not text.strip():
        return render(
            request,
            "bank/import.html",
            {
                "account": acc,
                "preview": None,
                "csv_format": csv_format,
                "error": "Paste CSV or choose a file.",
                "msg": "",
            },
            status_code=400,
        )

    parsed = parse_bank_csv(text, fmt=csv_format or "auto", account_id=account_id)
    mark_duplicates(db, account_id, parsed)

    if action == "commit":
        result = import_transactions(db, account_id, parsed, skip_duplicates=True)
        msg = url_quote(
            f"Imported {result.created}, skipped {result.skipped_dupes} duplicates"
        )
        return RedirectResponse(f"/bank/{account_id}?msg={msg}", status_code=303)

    return render(
        request,
        "bank/import.html",
        {
            "account": acc,
            "preview": parsed,
            "csv_text": text,
            "csv_format": csv_format,
            "error": None,
            "msg": "",
            "dupes": sum(1 for p in parsed if p.is_duplicate),
            "new_count": sum(1 for p in parsed if not p.is_duplicate),
        },
    )


@router.get("/transactions/{txn_id:int}/match", response_class=HTMLResponse)
async def bank_match_form(
    txn_id: int, request: Request, db: Session = Depends(get_db)
):
    from app.models.finance import BankTransaction

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        return RedirectResponse("/bank", status_code=303)
    acc = get_account(db, txn.account_id)
    clients = (
        db.query(Client)
        .filter(Client.overall_status != "Inactive")
        .order_by(Client.company_name)
        .limit(500)
        .all()
    )
    open_inv = outstanding_invoices(db)
    from app.services.purchase_ledger import outstanding_bills as open_purchase_bills

    open_bills = sorted(
        open_purchase_bills(db),
        key=lambda b: b.due_date or date.today(),
    )
    return render(
        request,
        "bank/match.html",
        {
            "txn": txn,
            "account": acc,
            "clients": clients,
            "open_invoices": open_inv,
            "open_bills": open_bills,
            "categories": NOMINAL_CATEGORIES,
            "error": request.query_params.get("error", ""),
        },
    )


@router.post("/transactions/{txn_id:int}/match", response_class=HTMLResponse)
async def bank_match_post(
    txn_id: int,
    request: Request,
    match_kind: str = Form(...),
    client_id: str = Form(""),
    invoice_id: str = Form(""),
    alloc_amount: str = Form(""),
    bill_id: str = Form(""),
    category: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.models.finance import BankTransaction

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        return RedirectResponse("/bank", status_code=303)
    aid = txn.account_id
    kind = (match_kind or "").strip().lower()
    try:
        if kind == "sales":
            cid = int(client_id) if (client_id or "").isdigit() else 0
            if not cid:
                raise ValueError("Select a client")
            allocations = []
            if (invoice_id or "").isdigit():
                inv = db.query(Invoice).filter(Invoice.id == int(invoice_id)).first()
                amt = _parse_money(alloc_amount)
                if amt <= 0 and inv:
                    amt = min(float(txn.amount), float(inv.balance or 0))
                if amt > 0:
                    allocations.append((int(invoice_id), amt))
            match_receipt_to_sales(
                db,
                txn_id,
                client_id=cid,
                invoice_allocations=allocations or None,
                notes=notes,
            )
        elif kind == "creditor":
            bid = int(bill_id) if (bill_id or "").isdigit() else 0
            if not bid:
                raise ValueError("Select a creditor bill")
            match_payment_to_creditor(db, txn_id, bid)
        elif kind == "nominal":
            match_payment_to_nominal(db, txn_id, category or "other_expense", notes)
        else:
            raise ValueError("Unknown match type")
    except ValueError as exc:
        return RedirectResponse(
            f"/bank/transactions/{txn_id}/match?error={url_quote(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(f"/bank/{aid}?msg=matched", status_code=303)


@router.post("/transactions/{txn_id:int}/reconcile", response_class=HTMLResponse)
async def bank_txn_reconcile_toggle(
    txn_id: int,
    request: Request,
    cleared: str = Form("1"),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    from app.models.finance import BankTransaction

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        return RedirectResponse("/bank", status_code=303)
    set_reconciled(db, [txn_id], cleared=cleared != "0")
    if next:
        return RedirectResponse(next, status_code=303)
    return RedirectResponse(f"/bank/{txn.account_id}", status_code=303)


@router.post("/transactions/{txn_id:int}/delete", response_class=HTMLResponse)
async def bank_txn_delete(
    txn_id: int, request: Request, db: Session = Depends(get_db)
):
    from app.models.finance import BankTransaction

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        return RedirectResponse("/bank", status_code=303)
    aid = txn.account_id
    try:
        delete_transaction(db, txn_id)
        msg = "deleted"
    except ValueError as exc:
        msg = url_quote(str(exc))
    return RedirectResponse(f"/bank/{aid}?msg={msg}", status_code=303)


@router.get("/{account_id:int}/reconcile", response_class=HTMLResponse)
async def bank_reconcile_page(
    account_id: int,
    request: Request,
    as_of: str = Query(""),
    statement_balance: str = Query(""),
    db: Session = Depends(get_db),
):
    acc = get_account(db, account_id)
    if not acc:
        return RedirectResponse("/bank", status_code=303)
    as_of_d = _parse_date(as_of) or date.today()
    stmt = _parse_money(statement_balance) if statement_balance else None
    summary = None
    if statement_balance != "":
        summary = reconciliation_summary(db, account_id, stmt or 0, as_of_d)
    rows = ledger_rows(
        db, account_id, to_date=as_of_d, reconciled=False, newest_first=True
    )
    all_to_date = ledger_rows(
        db, account_id, to_date=as_of_d, newest_first=True
    )
    return render(
        request,
        "bank/reconcile.html",
        {
            "account": acc,
            "as_of": as_of_d,
            "statement_balance": statement_balance,
            "summary": summary,
            "unreconciled": rows,
            "all_rows": all_to_date,
            "today": date.today(),
            "msg": request.query_params.get("msg", ""),
        },
    )


@router.post("/{account_id:int}/reconcile", response_class=HTMLResponse)
async def bank_reconcile_clear(
    account_id: int,
    request: Request,
    as_of: str = Form(""),
    statement_balance: str = Form(""),
    txn_ids: list[str] = Form(None),
    db: Session = Depends(get_db),
):
    ids = []
    form = await request.form()
    # collect txn_ids[] style
    for key, val in form.multi_items():
        if key in ("txn_ids", "txn_ids[]") and str(val).isdigit():
            ids.append(int(val))
    if ids:
        set_reconciled(db, ids, cleared=True)
    q = []
    if as_of:
        q.append(f"as_of={as_of}")
    if statement_balance != "":
        q.append(f"statement_balance={url_quote(statement_balance)}")
    q.append("msg=cleared")
    return RedirectResponse(
        f"/bank/{account_id}/reconcile?{'&'.join(q)}", status_code=303
    )
