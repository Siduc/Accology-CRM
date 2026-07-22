"""Purchase Ledger UI — Working Capital · Creditors."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.finance import CreditorBill, Supplier, SupplierPayment
from app.services.purchase_ledger import (
    ageing_report,
    bill_age_days,
    create_bill,
    create_supplier,
    list_suppliers,
    migrate_legacy_bills,
    outstanding_bills,
    purchase_home_metrics,
    record_supplier_payment,
    recompute_bill_totals,
    update_supplier,
)
from app.templating import render

router = APIRouter(prefix="/purchase", tags=["purchase"])


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
async def purchase_home(request: Request, db: Session = Depends(get_db)):
    migrate_legacy_bills(db)
    m = purchase_home_metrics(db)
    return render(
        request,
        "purchase/home.html",
        {**m, "today": date.today()},
    )


@router.get("/suppliers", response_class=HTMLResponse)
async def suppliers_list(
    request: Request, q: str = Query(""), db: Session = Depends(get_db)
):
    migrate_legacy_bills(db)
    rows = list_suppliers(db, active_only=False)
    if q:
        needle = q.strip().lower()
        rows = [s for s in rows if needle in (s.name or "").lower()]
    return render(
        request, "purchase/suppliers.html", {"suppliers": rows, "q": q}
    )


@router.get("/suppliers/new", response_class=HTMLResponse)
async def supplier_new_form(request: Request):
    return render(
        request, "purchase/supplier_form.html", {"supplier": None, "error": None}
    )


@router.post("/suppliers/new", response_class=HTMLResponse)
async def supplier_create(
    request: Request,
    name: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    vat_number: str = Form(""),
    payment_terms_days: str = Form("30"),
    default_category: str = Form("supplier"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        days = int(payment_terms_days or 30)
    except ValueError:
        days = 30
    sup = create_supplier(
        db,
        name=name,
        contact_name=contact_name,
        email=email,
        phone=phone,
        vat_number=vat_number,
        payment_terms_days=days,
        default_category=default_category,
        notes=notes,
    )
    return RedirectResponse(f"/purchase/suppliers?created={sup.id}", status_code=303)


@router.get("/suppliers/{supplier_id:int}/edit", response_class=HTMLResponse)
async def supplier_edit_form(
    supplier_id: int, request: Request, db: Session = Depends(get_db)
):
    sup = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not sup:
        return RedirectResponse("/purchase/suppliers", status_code=303)
    return render(
        request, "purchase/supplier_form.html", {"supplier": sup, "error": None}
    )


@router.post("/suppliers/{supplier_id:int}/edit", response_class=HTMLResponse)
async def supplier_edit(
    supplier_id: int,
    request: Request,
    name: str = Form(...),
    contact_name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    vat_number: str = Form(""),
    payment_terms_days: str = Form("30"),
    default_category: str = Form("supplier"),
    is_active: str = Form("yes"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    try:
        days = int(payment_terms_days or 30)
    except ValueError:
        days = 30
    try:
        update_supplier(
            db,
            supplier_id,
            name=name.strip(),
            contact_name=contact_name or None,
            email=email or None,
            phone=phone or None,
            vat_number=vat_number or None,
            payment_terms_days=days,
            default_category=default_category or "supplier",
            is_active=is_active == "yes",
            notes=notes or None,
        )
    except ValueError:
        return RedirectResponse("/purchase/suppliers", status_code=303)
    return RedirectResponse("/purchase/suppliers?updated=1", status_code=303)


@router.get("/bills", response_class=HTMLResponse)
async def bills_list(
    request: Request,
    status: str = Query("outstanding"),
    db: Session = Depends(get_db),
):
    migrate_legacy_bills(db)
    q = db.query(CreditorBill).order_by(CreditorBill.issue_date.desc())
    if status == "outstanding":
        bills = outstanding_bills(db)
        bills.sort(key=lambda b: b.due_date or date.max)
    elif status:
        bills = q.filter(CreditorBill.status == status).limit(300).all()
    else:
        bills = q.limit(300).all()
    today = date.today()
    rows = [
        {"bill": b, "age": bill_age_days(b, today), "balance": float(b.balance or b.amount or 0)}
        for b in bills
    ]
    return render(
        request,
        "purchase/bills.html",
        {"rows": rows, "status": status, "today": today},
    )


@router.get("/bills/new", response_class=HTMLResponse)
async def bill_new_form(
    request: Request,
    supplier_id: int = Query(None),
    db: Session = Depends(get_db),
):
    suppliers = list_suppliers(db, active_only=True)
    return render(
        request,
        "purchase/bill_form.html",
        {
            "suppliers": suppliers,
            "selected_supplier_id": supplier_id,
            "today": date.today(),
            "error": None,
        },
    )


@router.post("/bills/new", response_class=HTMLResponse)
async def bill_create(
    request: Request,
    supplier_id: str = Form(""),
    supplier_name: str = Form(""),
    number: str = Form(""),
    issue_date: str = Form(""),
    due_date: str = Form(""),
    category: str = Form("supplier"),
    description: str = Form(""),
    notes: str = Form(""),
    line_desc_1: str = Form(""),
    line_qty_1: str = Form("1"),
    line_price_1: str = Form("0"),
    line_vat_1: str = Form("0.2"),
    line_desc_2: str = Form(""),
    line_qty_2: str = Form("1"),
    line_price_2: str = Form("0"),
    line_vat_2: str = Form("0.2"),
    line_desc_3: str = Form(""),
    line_qty_3: str = Form("1"),
    line_price_3: str = Form("0"),
    line_vat_3: str = Form("0.2"),
    db: Session = Depends(get_db),
):
    lines = []
    for desc, qty, price, vat in [
        (line_desc_1, line_qty_1, line_price_1, line_vat_1),
        (line_desc_2, line_qty_2, line_price_2, line_vat_2),
        (line_desc_3, line_qty_3, line_price_3, line_vat_3),
    ]:
        if not (desc or "").strip() and _money(price) <= 0:
            continue
        lines.append(
            {
                "description": (desc or description or "Purchase").strip(),
                "qty": _money(qty) or 1,
                "unit_price": _money(price),
                "vat_rate": _money(vat),
            }
        )
    sid = int(supplier_id) if (supplier_id or "").isdigit() else None
    if not lines:
        return RedirectResponse("/purchase/bills/new?error=1", status_code=303)
    bill = create_bill(
        db,
        supplier_id=sid,
        supplier_name=supplier_name,
        number=number,
        issue_date=_parse_date(issue_date),
        due_date=_parse_date(due_date),
        category=category,
        description=description,
        notes=notes,
        lines=lines,
    )
    return RedirectResponse(f"/purchase/bills/{bill.id}", status_code=303)


@router.get("/bills/{bill_id:int}", response_class=HTMLResponse)
async def bill_detail(
    bill_id: int, request: Request, db: Session = Depends(get_db)
):
    bill = (
        db.query(CreditorBill)
        .options(joinedload(CreditorBill.lines), joinedload(CreditorBill.supplier))
        .filter(CreditorBill.id == bill_id)
        .first()
    )
    if not bill:
        return RedirectResponse("/purchase/bills", status_code=303)
    return render(
        request,
        "purchase/bill_detail.html",
        {
            "bill": bill,
            "age": bill_age_days(bill),
            "today": date.today(),
        },
    )


@router.post("/bills/{bill_id:int}/status", response_class=HTMLResponse)
async def bill_status(
    bill_id: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    bill = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
    if bill and status in ("outstanding", "void", "draft", "paid"):
        bill.status = status
        if status == "void":
            bill.balance = 0.0
        elif status == "paid":
            bill.amount_paid = float(bill.total or bill.amount or 0)
            bill.balance = 0.0
            bill.paid_date = date.today()
        else:
            recompute_bill_totals(db, bill)
        db.commit()
    return RedirectResponse(f"/purchase/bills/{bill_id}", status_code=303)


@router.get("/payments", response_class=HTMLResponse)
async def payments_list(request: Request, db: Session = Depends(get_db)):
    pays = (
        db.query(SupplierPayment)
        .order_by(SupplierPayment.payment_date.desc())
        .limit(200)
        .all()
    )
    suppliers = {
        s.id: s
        for s in db.query(Supplier)
        .filter(Supplier.id.in_({p.supplier_id for p in pays if p.supplier_id} or {-1}))
        .all()
    }
    return render(
        request,
        "purchase/payments.html",
        {"payments": pays, "suppliers": suppliers},
    )


@router.get("/payments/new", response_class=HTMLResponse)
async def payment_new_form(
    request: Request,
    supplier_id: int = Query(None),
    bill_id: int = Query(None),
    db: Session = Depends(get_db),
):
    suppliers = list_suppliers(db, active_only=True)
    open_bills = []
    if supplier_id:
        open_bills = [
            b for b in outstanding_bills(db) if b.supplier_id == supplier_id
        ]
    elif bill_id:
        b = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
        if b:
            supplier_id = b.supplier_id
            open_bills = [b]
    return render(
        request,
        "purchase/payment_form.html",
        {
            "suppliers": suppliers,
            "selected_supplier_id": supplier_id,
            "open_bills": open_bills,
            "preselect_bill_id": bill_id,
            "today": date.today(),
        },
    )


@router.post("/payments/new", response_class=HTMLResponse)
async def payment_create(
    request: Request,
    supplier_id: str = Form(""),
    amount: str = Form(...),
    payment_date: str = Form(""),
    method: str = Form("bank"),
    reference: str = Form(""),
    notes: str = Form(""),
    post_to_bank: str = Form(""),
    alloc_bill_id: str = Form(""),
    alloc_amount: str = Form(""),
    db: Session = Depends(get_db),
):
    amt = _money(amount)
    sid = int(supplier_id) if (supplier_id or "").isdigit() else None
    allocations = []
    if (alloc_bill_id or "").isdigit():
        bid = int(alloc_bill_id)
        aamt = _money(alloc_amount)
        if aamt <= 0:
            bill = db.query(CreditorBill).filter(CreditorBill.id == bid).first()
            if bill:
                aamt = min(amt, float(bill.balance or bill.amount or 0))
        if aamt > 0:
            allocations.append((bid, aamt))
    pay = record_supplier_payment(
        db,
        supplier_id=sid,
        amount=amt,
        payment_date=_parse_date(payment_date),
        method=method or "bank",
        reference=reference or None,
        notes=notes or None,
        bill_allocations=allocations or None,
        post_to_bank=post_to_bank == "yes",
    )
    return RedirectResponse(f"/purchase/payments?paid={pay.id}", status_code=303)


@router.get("/ageing", response_class=HTMLResponse)
async def purchase_ageing(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    buckets = ageing_report(db, today)
    bills = outstanding_bills(db)
    total, count = 0.0, 0
    rows = []
    for b in bills:
        age = bill_age_days(b, today)
        bal = float(b.balance or 0)
        total += bal
        count += 1
        rows.append({"bill": b, "age": age, "balance": bal})
    rows.sort(key=lambda r: -r["age"])
    return render(
        request,
        "purchase/ageing.html",
        {
            "buckets": buckets,
            "rows": rows,
            "total": round(total, 2),
            "count": count,
            "today": today,
        },
    )
