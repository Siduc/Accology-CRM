"""Working capital drill-downs: WIP, debtors, cash, creditors."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.finance import BankTransaction, CreditorBill
from app.services.practice_tasks import compute_task_horizons, list_tasks
from app.services.working_capital import (
    cash_balance,
    compute_creditors,
    compute_debtors,
    compute_wip,
    compute_wip_type_horizons,
    ensure_default_bank_account,
    job_horizon_key,
    retainer_book,
    wip_amount_for_job,
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
    show: str = "",
    status: str = "",
    client_id: str = "",
    db: Session = Depends(get_db),
):
    today = date.today()
    snap = compute_wip(db, today)
    horizons = compute_wip_type_horizons(db, today)
    bounds = wip_horizon_boundaries(today)
    task_horizon = compute_task_horizons(db, today)

    filter_type = (type or "").strip()
    filter_horizon = (horizon or "").strip()
    filter_status = (status or "").strip()
    filter_client_id = int(client_id) if (client_id or "").isdigit() else None
    # Legacy keys → new keys
    legacy = {
        "overdue": "imminent",
        "eom": "imminent",
        "next_eom": "planning",
        "plus3": "planning",
        "plus3b": "pre_planning",
    }
    if filter_horizon in legacy:
        filter_horizon = legacy[filter_horizon]
    valid_horizons = {"imminent", "planning", "pre_planning", "later"}
    if filter_horizon and filter_horizon not in valid_horizons:
        filter_horizon = ""

    if filter_type.lower() in (
        "cs",
        "confirmation",
        "confirmation statement",
        "confirmation statements",
    ):
        filter_type = "Confirmation Statement"
    elif filter_type.lower() in ("accounts", "account"):
        filter_type = "Accounts"
    elif filter_type.lower() == "tasks":
        filter_type = "Tasks"

    # show= toggles: accounts,cs,tasks (comma list) — default all on
    show_raw = (show or "accounts,cs,tasks").lower()
    show_set = {s.strip() for s in show_raw.split(",") if s.strip()}
    if not show_set:
        show_set = {"accounts", "cs", "tasks"}
    show_accounts = "accounts" in show_set
    show_cs = "cs" in show_set
    show_tasks = "tasks" in show_set

    # Only show list after drilling a tile (or explicit status/client filter)
    show_list = bool(filter_type or filter_horizon or filter_status or filter_client_id)

    horizon_labels = {
        "imminent": "Overdue and Imminent",
        "planning": "Planning",
        "pre_planning": "Pre Planning",
        "later": "Everything else",
    }
    list_status_options = [
        "Overdue",
        "Imminent",
        "Planning",
        "Pre Planning",
        "Later",
        "On hold",
    ]

    def _fmt_d(d):
        if not d:
            return "—"
        if hasattr(d, "strftime"):
            return d.strftime("%d-%m-%Y")
        return str(d)

    from app.services.working_capital import wip_list_status

    # Retainer shares for list amounts (same logic as compute_wip)
    _book = retainer_book(db)
    _monthly_by = _book.get("monthly_by_client") or {}
    _open_counts: dict = {}
    for jj in snap.jobs:
        if jj.client_id and jj.client and jj.client.is_retainer():
            _open_counts[jj.client_id] = _open_counts.get(jj.client_id, 0) + 1

    # Job rows — only build when drilling
    rows = []
    clients_for_filter = []
    if show_list and filter_type != "Tasks":
        seen_c = {}
        for j in snap.jobs:
            if filter_type and filter_type != "Tasks" and not _match_job_type(
                j.type, filter_type
            ):
                continue
            if filter_horizon:
                if job_horizon_key(j, today) != filter_horizon:
                    continue
            due = j.statutory_due_date or j.target_completion
            if due and due < today:
                age = (today - due).days
            else:
                age = 0
            list_st = wip_list_status(j, today)
            if filter_status:
                if filter_status == "Overdue" and list_st != "Overdue":
                    continue
                if filter_status == "Imminent" and list_st != "Imminent":
                    continue
                if filter_status not in ("Overdue", "Imminent") and list_st != filter_status:
                    continue
            if filter_client_id and j.client_id != filter_client_id:
                continue
            is_ret = bool(j.client and j.client.is_retainer())
            fee = float(j.fee or 0)
            wip_amt = wip_amount_for_job(
                j, open_counts=_open_counts, monthly_by_client=_monthly_by
            )
            if j.client and j.client_id not in seen_c:
                seen_c[j.client_id] = j.client
            rows.append(
                {
                    "job": j,
                    "age_days": age,
                    "amount": wip_amt,
                    "fee_display": fee,
                    "is_retainer": is_ret,
                    "list_status": list_st,
                    "due_fmt": _fmt_d(due),
                    "period_end_fmt": _fmt_d(j.period_end),
                }
            )
        rows.sort(key=lambda r: (-r["age_days"], -r["amount"]))
        clients_for_filter = sorted(
            seen_c.values(),
            key=lambda c: (c.display_name() or "").lower(),
        )

    # Task rows — only when drilling tasks (or status filter on tasks)
    task_rows = []
    if show_list and (filter_type == "Tasks" or (not filter_type and filter_horizon and show_tasks)):
        open_tasks = list_tasks(db, include_closed=False, include_hold=False, limit=200)
        from app.services.working_capital import job_horizon_key_for_due

        for t in open_tasks:
            if float(t.fee or 0) <= 0 and filter_type == "Tasks":
                # still show zero-fee when explicitly drilling tasks
                pass
            hk = job_horizon_key_for_due(t.due_on, today)
            if filter_horizon and hk != filter_horizon:
                continue
            if filter_type and filter_type != "Tasks":
                continue
            if filter_client_id and t.client_id != filter_client_id:
                continue
            t_status = t.display_status(today)
            if filter_status and t_status != filter_status:
                continue
            task_rows.append(t)

    filter_fee = round(sum(r["amount"] for r in rows), 2)
    if filter_type == "Tasks" or (
        filter_horizon and filter_type in ("", "Tasks") and task_rows
    ):
        filter_fee = round(
            filter_fee + sum(float(t.fee or 0) for t in task_rows), 2
        )

    filter_label = ""
    if filter_type or filter_horizon or filter_status:
        parts = []
        if filter_type:
            parts.append(
                "Confirmation statements"
                if filter_type == "Confirmation Statement"
                else filter_type
            )
        if filter_horizon:
            parts.append(horizon_labels.get(filter_horizon, filter_horizon))
        if filter_status:
            parts.append(filter_status)
        filter_label = " · ".join(parts)

    # Filter which horizon rows to show based on toggles
    display_horizons = []
    for h in horizons:
        if h.job_type == "Accounts" and show_accounts:
            display_horizons.append(h)
        elif h.job_type == "Confirmation Statement" and show_cs:
            display_horizons.append(h)

    show_query = ",".join(
        s
        for s, on in (
            ("accounts", show_accounts),
            ("cs", show_cs),
            ("tasks", show_tasks),
        )
        if on
    ) or "accounts,cs,tasks"

    return render(
        request,
        "working_capital/wip.html",
        {
            "snap": snap,
            "rows": rows,
            "today": today,
            "total": snap.value,
            "count": snap.count,
            "retainer_count": getattr(snap, "retainer_count", 0) or 0,
            "retainer_monthly": getattr(snap, "retainer_monthly", 0) or 0,
            "retainer_annual": getattr(snap, "retainer_annual", 0) or 0,
            "retainer_job_count": getattr(snap, "retainer_job_count", 0) or 0,
            "jobs_value": getattr(snap, "jobs_value", 0) or 0,
            "tasks_value": getattr(snap, "tasks_value", 0) or 0,
            "horizons": display_horizons,
            "horizon_bounds": bounds,
            "filter_type": filter_type,
            "filter_horizon": filter_horizon,
            "filter_status": filter_status,
            "filter_client_id": filter_client_id,
            "filter_label": filter_label,
            "filter_fee": filter_fee,
            "filter_count": len(rows)
            + (len(task_rows) if filter_type in ("", "Tasks") else 0),
            "show_list": show_list,
            "show_accounts": show_accounts,
            "show_cs": show_cs,
            "show_tasks": show_tasks,
            "show_query": show_query,
            "list_status_options": list_status_options,
            "filter_clients": clients_for_filter,
            "task_horizon": task_horizon,
            "task_rows": task_rows if show_tasks else [],
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
        age = max(0, (today - inv).days) if inv else 0
        rows.append({"job": j, "age_days": age, "amount": float(j.fee or j.gross_amount or 0)})
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
    account = ensure_default_bank_account(db)
    bal = cash_balance(db, account)
    recent = (
        db.query(BankTransaction)
        .filter(BankTransaction.account_id == account.id)
        .order_by(BankTransaction.txn_date.desc(), BankTransaction.id.desc())
        .limit(40)
        .all()
    )
    return render(
        request,
        "working_capital/cash.html",
        {
            "account": account,
            "balance": bal,
            "recent": recent,
            "today": date.today(),
        },
    )


@router.post("/cash/transaction")
async def wc_cash_add_txn(
    request: Request,
    txn_date: str = Form(...),
    description: str = Form(""),
    amount: str = Form("0"),
    db: Session = Depends(get_db),
):
    account = ensure_default_bank_account(db)
    d = _parse_date(txn_date) or date.today()
    amt = _parse_money(amount)
    db.add(
        BankTransaction(
            account_id=account.id,
            txn_date=d,
            description=description or "Manual",
            amount=amt,
        )
    )
    db.commit()
    return RedirectResponse("/working-capital/cash", status_code=303)


@router.get("/creditors", response_class=HTMLResponse)
async def wc_creditors(request: Request, db: Session = Depends(get_db)):
    today = date.today()
    snap = compute_creditors(db, today)
    return render(
        request,
        "working_capital/creditors.html",
        {
            "snap": snap,
            "today": today,
            "total": snap.total,
            "count": snap.count,
        },
    )
