"""Working capital drill-downs: WIP, debtors, cash, creditors."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.finance import BankTransaction, CreditorBill
from app.services.working_capital import (
    cash_balance,
    compute_creditors,
    compute_debtors,
    compute_wip,
    compute_wip_type_horizons,
    ensure_default_bank_account,
    job_horizon_key,
    wip_horizon_boundaries,
    _match_job_type,
)
from app.templating import render

router = APIRouter(prefix="/working-capital", tags=["working-capital"])


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
async def wc_home(request: Request):
    return RedirectResponse("/dashboard#working-capital", status_code=303)


@router.get("/wip", response_class=HTMLResponse)
async def wc_wip(
    request: Request,
    type: str = "",
    horizon: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    snap = compute_wip(db, today)
    horizons = compute_wip_type_horizons(db, today)
    bounds = wip_horizon_boundaries(today)

    filter_type = (type or "").strip()
    filter_horizon = (horizon or "").strip()
    valid_horizons = {"overdue", "eom", "next_eom", "plus3", "plus3b", "later"}
    if filter_horizon and filter_horizon not in valid_horizons:
        filter_horizon = ""
    # Normalise type aliases from query string
    if filter_type.lower() in ("cs", "confirmation", "confirmation statement", "confirmation statements"):
        filter_type = "Confirmation Statement"
    elif filter_type.lower() in ("accounts", "account"):
        filter_type = "Accounts"

    horizon_labels = {
        "overdue": "Overdue",
        "eom": "End of month",
        "next_eom": "End of next month",
        "plus3": "Following 3 months",
        "plus3b": "Next 3 months",
        "later": "Later / undated",
    }

    def _fmt_d(d):
        if not d:
            return "—"
        if hasattr(d, "strftime"):
            return d.strftime("%d-%m-%Y")
        return str(d)

    # Enrich jobs with age days for template
    rows = []
    for j in snap.jobs:
        if filter_type and not _match_job_type(j.type, filter_type):
            continue
        if filter_horizon:
            if job_horizon_key(j, today) != filter_horizon:
                continue
        due = j.statutory_due_date or j.target_completion
        if due and due < today:
            age = (today - due).days
        else:
            age = 0
        rows.append(
            {
                "job": j,
                "age_days": age,
                "amount": float(j.fee or 0),
                "due_fmt": _fmt_d(due),
                "period_end_fmt": _fmt_d(j.period_end),
            }
        )
    rows.sort(key=lambda r: (-r["age_days"], -r["amount"]))

    filter_fee = round(sum(r["amount"] for r in rows), 2)
    filter_label = ""
    if filter_type or filter_horizon:
        parts = []
        if filter_type:
            parts.append(
                "Confirmation statements"
                if filter_type == "Confirmation Statement"
                else filter_type
            )
        if filter_horizon:
            parts.append(horizon_labels.get(filter_horizon, filter_horizon))
        filter_label = " · ".join(parts)

    return render(
        request,
        "working_capital/wip.html",
        {
            "snap": snap,
            "rows": rows,
            "today": today,
            "total": snap.value,
            "count": snap.count,
            "horizons": horizons,
            "horizon_bounds": bounds,
            "filter_type": filter_type,
            "filter_horizon": filter_horizon,
            "filter_label": filter_label,
            "filter_fee": filter_fee,
            "filter_count": len(rows),
        },
    )


@router.get("/debtors", response_class=HTMLResponse)
async def wc_debtors(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    snap = compute_debtors(db, today)
    rows = []
    for j in snap.jobs:
        inv = j.period_end or j.actual_completion
        if j.updated_at and not inv:
            inv = j.updated_at.date() if hasattr(j.updated_at, "date") else j.updated_at
        if not inv:
            inv = today
        age = max(0, (today - inv).days)
        amt = float(j.gross_amount) if j.gross_amount else float(j.fee or 0)
        rows.append({"job": j, "age_days": age, "amount": amt, "inv_date": inv})
    rows.sort(key=lambda r: (-r["age_days"], -r["amount"]))
    return render(
        request,
        "working_capital/debtors.html",
        {
            "snap": snap,
            "rows": rows,
            "today": today,
            "total": snap.total,
            "count": snap.count,
        },
    )


@router.get("/cash", response_class=HTMLResponse)
async def wc_cash(request: Request, db: Session = Depends(get_db)):
    """Legacy Cash URL → Bank Ledger (Working Capital · Cash)."""
    from app.services.bank_ledger import ensure_default_bank_account as primary

    acc = primary(db)
    return RedirectResponse(f"/bank/{acc.id}", status_code=303)


@router.post("/cash/opening", response_class=HTMLResponse)
async def wc_cash_opening(
    request: Request,
    opening_balance: str = Form("0"),
    db: Session = Depends(get_db),
):
    from app.services.bank_ledger import ensure_default_bank_account as primary
    from app.services.bank_ledger import set_opening_balance

    acc = primary(db)
    set_opening_balance(db, acc.id, _parse_money(opening_balance))
    return RedirectResponse(f"/bank/{acc.id}", status_code=303)


@router.post("/cash/transaction", response_class=HTMLResponse)
async def wc_cash_txn(
    request: Request,
    txn_date: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    direction: str = Form("in"),
    db: Session = Depends(get_db),
):
    from app.services.bank_ledger import add_transaction, ensure_default_bank_account as primary

    acc = primary(db)
    amt = abs(_parse_money(amount))
    if (direction or "in").lower() in ("out", "payment", "-"):
        amt = -amt
    add_transaction(
        db,
        acc.id,
        txn_date=_parse_date(txn_date) or date.today(),
        amount=amt,
        description=description,
        source="manual",
    )
    return RedirectResponse(f"/bank/{acc.id}", status_code=303)


@router.get("/creditors", response_class=HTMLResponse)
async def wc_creditors(request: Request, db: Session = Depends(get_db)):
    """Legacy Creditors URL → Purchase Ledger (Working Capital · Creditors)."""
    return RedirectResponse("/purchase", status_code=303)


@router.post("/creditors/add", response_class=HTMLResponse)
async def wc_creditors_add(
    request: Request,
    supplier_name: str = Form(...),
    description: str = Form(""),
    amount: str = Form("0"),
    vat_amount: str = Form("0"),
    due_date: str = Form(""),
    category: str = Form("supplier"),
    db: Session = Depends(get_db),
):
    """Legacy add → Purchase Ledger bill."""
    from app.services.purchase_ledger import create_bill

    gross = _parse_money(amount)
    vat = _parse_money(vat_amount)
    net = max(0.0, gross - vat)
    create_bill(
        db,
        supplier_name=supplier_name,
        description=description,
        due_date=_parse_date(due_date),
        category=category,
        lines=[
            {
                "description": description or "Purchase",
                "qty": 1,
                "unit_price": net if net else gross,
                "vat_rate": (vat / net) if net > 0 else 0.0,
            }
        ],
    )
    return RedirectResponse("/purchase/bills?status=outstanding", status_code=303)


@router.post("/creditors/{bill_id:int}/paid", response_class=HTMLResponse)
async def wc_creditors_paid(
    bill_id: int,
    request: Request,
    post_to_bank: str = Form(""),
    db: Session = Depends(get_db),
):
    bill = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
    if not bill:
        return RedirectResponse("/purchase", status_code=303)
    if post_to_bank == "yes":
        from app.services.bank_ledger import pay_creditor_from_bank

        try:
            pay_creditor_from_bank(db, bill_id)
        except ValueError:
            bill.status = "paid"
            bill.paid_date = date.today()
            bill.amount_paid = float(bill.total or bill.amount or 0)
            bill.balance = 0.0
            db.commit()
    else:
        bill.status = "paid"
        bill.paid_date = date.today()
        bill.amount_paid = float(bill.total or bill.amount or 0)
        bill.balance = 0.0
        db.commit()
    return RedirectResponse("/purchase/bills?status=outstanding", status_code=303)
