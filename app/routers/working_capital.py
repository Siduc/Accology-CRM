"""Working capital drill-downs: WIP, debtors, cash, creditors."""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.finance import BankTransaction, CreditorBill
from app.services.working_capital import (
    cash_balance,
    compute_creditors,
    compute_debtors,
    compute_wip,
    compute_wip_age_home,
    compute_pe_year_layout,
    compute_wip_book,
    compute_wip_type_totals_for_band,
    ensure_default_bank_account,
    job_period_end_bucket,
    job_service_kind,
    job_type_bucket,
    job_wip_band,
    retainer_book,
    wip_amount_for_job,
    wip_jobs,
    wip_list_status,
    _client_is_retainer,
    _match_job_type,
    _job_fee,
    _job_is_completed,
    _job_is_open,
)
from app.templating import render

router = APIRouter(prefix="/working-capital", tags=["working-capital"])

VALID_BANDS = {"today", "m1", "m2", "m3", "later"}


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
    status: str = "",
    client_id: str = "",
    pe_year: str = "",
    pe_slice: str = "",
    db: Session = Depends(get_db),
):
    """
    WIP desk:
      Home: Today · next 3 calendar months · everything else · Total
      Drill age band → type tiles (Accounts / CS / Other) + filtered list
      Foot: WIP book by period end year + prospects
    """
    today = date.today()
    snap = compute_wip(db, today)
    age_home = compute_wip_age_home(db, today)
    wip_book = compute_wip_book(db, today)

    filter_type = (type or "").strip()
    filter_horizon = (horizon or "").strip()
    filter_status = (status or "").strip()
    filter_client_id = int(client_id) if (client_id or "").isdigit() else None
    filter_pe = (pe_year or "").strip().lower()
    pe_map = {
        "2027": "pe_2027",
        "pe_2027": "pe_2027",
        "2026": "pe_2026",
        "pe_2026": "pe_2026",
        "2025": "pe_2025",
        "pe_2025": "pe_2025",
        "2024": "pe_2024_prior",
        "2024prior": "pe_2024_prior",
        "pe_2024_prior": "pe_2024_prior",
        "prior": "pe_2024_prior",
    }
    filter_pe_key = pe_map.get(filter_pe, "")
    filter_pe_slice = (pe_slice or "").strip().lower()
    valid_pe_slices = {
        "completed",
        "accounts",
        "cs",
        "vat",
        "other",
        "year",
    }
    if filter_pe_slice and filter_pe_slice not in valid_pe_slices:
        filter_pe_slice = ""

    legacy = {
        "overdue": "today",
        "eom": "today",
        "imminent": "today",
        "planning": "m1",
        "pre_planning": "m2",
        "next_eom": "m1",
        "plus3": "m1",
        "plus3b": "m2",
        "tasks": "",  # tasks no longer a home band
    }
    if filter_horizon in legacy:
        filter_horizon = legacy[filter_horizon]
    if filter_horizon and filter_horizon not in VALID_BANDS:
        filter_horizon = ""

    if filter_type.lower() in (
        "cs",
        "confirmation",
        "confirmation statement",
        "confirmation statements",
        "confirmation+statement",
    ):
        filter_type = "Confirmation Statement"
    elif filter_type.lower() in ("accounts", "account"):
        filter_type = "Accounts"
    elif filter_type.lower() in ("other", "others"):
        filter_type = "Other"
    elif filter_type:
        # keep free text for other job types match via _match or exact bucket
        if filter_type not in ("Accounts", "Confirmation Statement", "Other"):
            filter_type = "Other" if filter_type else ""

    show_age_home = (
        not filter_horizon
        and not filter_status
        and not filter_client_id
        and not filter_type
        and not filter_pe_key
    )
    show_band_drill = bool(filter_horizon)  # age band → type tiles + list
    # PE year drill: 1-2-2-1 layout (not age home, not wip book strip)
    show_pe_year_home = bool(filter_pe_key) and not filter_horizon
    show_list = bool(
        filter_horizon
        or filter_status
        or filter_client_id
        or filter_type
        or filter_pe_slice
    )
    # When PE year selected without slice, show layout only (no list until tile click)
    # When pe_slice set, show list under layout

    band_labels = {k: v["label"] for k, v in age_home["bands"].items()}
    list_status_options = [
        "Today",
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

    _book = retainer_book(db)
    _monthly_by = _book.get("monthly_by_client") or {}
    _open_counts: dict = {}
    for jj in snap.jobs:
        if jj.client_id and jj.client and jj.client.is_retainer():
            _open_counts[jj.client_id] = _open_counts.get(jj.client_id, 0) + 1

    type_totals = []
    if filter_horizon:
        type_totals = compute_wip_type_totals_for_band(db, filter_horizon, today)

    pe_layout = None
    if show_pe_year_home:
        pe_layout = compute_pe_year_layout(db, filter_pe_key, today)

    rows = []
    clients_for_filter = []
    # Age-band list (open WIP only)
    if show_list and filter_horizon:
        seen_c = {}
        for j in snap.jobs:
            if job_wip_band(j, today) != filter_horizon:
                continue
            tb = job_type_bucket(j)
            if filter_type:
                if filter_type == "Other":
                    if tb != "Other":
                        continue
                elif not _match_job_type(j.type, filter_type) and tb != filter_type:
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
                if filter_status == "Today" and list_st != "Today":
                    continue
                if filter_status not in (
                    "Overdue",
                    "Imminent",
                    "Today",
                ) and list_st != filter_status:
                    continue
            if filter_client_id and j.client_id != filter_client_id:
                continue
            is_ret = bool(j.client and j.client.is_retainer())
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
                    "is_retainer": is_ret,
                    "list_status": list_st,
                    "due_fmt": _fmt_d(due),
                    "period_end_fmt": _fmt_d(j.period_end),
                    "type_bucket": tb,
                }
            )
        rows.sort(key=lambda r: (-r["age_days"], -r["amount"]))
        clients_for_filter = sorted(
            seen_c.values(),
            key=lambda c: (c.display_name() or "").lower(),
        )

    # PE-year slice list (includes completed when slice=completed|year)
    # 2027 outstanding/year = live open WIP ledger (not only PE-date 2027 jobs).
    if filter_pe_key and filter_pe_slice:
        from app.models import Job

        if filter_pe_key == "pe_2027" and filter_pe_slice in (
            "accounts",
            "cs",
            "vat",
            "other",
            "year",
        ):
            pe_jobs = list(wip_jobs(db))
        elif filter_pe_key == "pe_2027" and filter_pe_slice == "completed":
            pe_jobs = (
                db.query(Job)
                .options(joinedload(Job.client))
                .filter(Job.status.notin_(["Cancelled"]))
                .all()
            )
            pe_jobs = [
                j
                for j in pe_jobs
                if job_period_end_bucket(j) == "pe_2027" and _job_is_completed(j)
            ]
        else:
            pe_jobs = (
                db.query(Job)
                .options(joinedload(Job.client))
                .filter(Job.status.notin_(["Cancelled"]))
                .all()
            )
            pe_jobs = [j for j in pe_jobs if job_period_end_bucket(j) == filter_pe_key]

        slice_jobs = []
        for j in pe_jobs:
            kind = job_service_kind(j)
            if filter_pe_slice == "completed":
                if filter_pe_key != "pe_2027" and not _job_is_completed(j):
                    continue
            elif filter_pe_slice == "year":
                pass
            elif filter_pe_slice in ("accounts", "cs", "vat", "other"):
                if filter_pe_key == "pe_2027":
                    if kind != filter_pe_slice:
                        continue
                elif not (_job_is_open(j) and kind == filter_pe_slice):
                    continue
            else:
                continue
            slice_jobs.append(j)

        _rbook = retainer_book(db)
        _monthly = _rbook.get("monthly_by_client") or {}
        _oc: dict = {}
        for jj in slice_jobs:
            if jj.client_id and _client_is_retainer(jj.client):
                _oc[jj.client_id] = _oc.get(jj.client_id, 0) + 1

        seen_c = {}
        for j in slice_jobs:
            due = j.statutory_due_date or j.target_completion
            if due and due < today:
                age = (today - due).days
            else:
                age = 0
            list_st = j.status or "—"
            if j.client and j.client_id not in seen_c:
                seen_c[j.client_id] = j.client
            if _client_is_retainer(j.client) and filter_pe_slice != "completed":
                amt = wip_amount_for_job(
                    j, open_counts=_oc, monthly_by_client=_monthly
                )
            elif filter_pe_slice == "completed" and _client_is_retainer(j.client):
                amt = 0.0
            else:
                amt = _job_fee(j)
            rows.append(
                {
                    "job": j,
                    "age_days": age,
                    "amount": amt,
                    "is_retainer": bool(j.client and j.client.is_retainer()),
                    "list_status": list_st,
                    "due_fmt": _fmt_d(due),
                    "period_end_fmt": _fmt_d(j.period_end),
                    "type_bucket": job_service_kind(j),
                }
            )
        rows.sort(key=lambda r: (-r["age_days"], -r["amount"]))
        clients_for_filter = sorted(
            seen_c.values(),
            key=lambda c: (c.display_name() or "").lower(),
        )

    filter_fee = round(sum(r["amount"] for r in rows), 2)
    filter_label_parts = []
    if filter_horizon:
        filter_label_parts.append(band_labels.get(filter_horizon, filter_horizon))
    if filter_pe_key:
        pe_labels = {
            "pe_2027": "2027 period end",
            "pe_2026": "2026 period end",
            "pe_2025": "2025 period end",
            "pe_2024_prior": "2024 & earlier period end",
        }
        filter_label_parts.append(pe_labels.get(filter_pe_key, filter_pe_key))
    if filter_type:
        filter_label_parts.append(
            "Confirmation statements"
            if filter_type == "Confirmation Statement"
            else filter_type
        )
    if filter_status:
        filter_label_parts.append(filter_status)
    if filter_pe_slice:
        slice_labs = {
            "completed": "Jobs completed",
            "accounts": "Accounts outstanding",
            "cs": "Confirmation statements outstanding",
            "vat": "VAT outstanding",
            "other": "Other outstanding",
            "year": "Jobs for the year",
        }
        filter_label_parts.append(slice_labs.get(filter_pe_slice, filter_pe_slice))
    filter_label = " · ".join(filter_label_parts)

    type_query = ""
    if filter_type == "Accounts":
        type_query = "Accounts"
    elif filter_type == "Confirmation Statement":
        type_query = "Confirmation+Statement"
    elif filter_type == "Other":
        type_query = "Other"

    mid_box_class = {
        "m1": "wc-box-wip",
        "m2": "wc-box-debtors",
        "m3": "wc-box-cash",
        "later": "wc-box-creditors",
    }
    mid_tile_class = {
        "m1": "tile-wip",
        "m2": "tile-debtors",
        "m3": "tile-cash",
        "later": "tile-creditors",
    }
    type_box_class = {
        "Accounts": "wc-box-wip",
        "Confirmation Statement": "wc-box-debtors",
        "Other": "wc-box-cash",
    }
    type_tile_class = {
        "Accounts": "tile-wip",
        "Confirmation Statement": "tile-debtors",
        "Other": "tile-cash",
    }

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
            "jobs_value": getattr(snap, "jobs_value", 0) or 0,
            "tasks_value": getattr(snap, "tasks_value", 0) or 0,
            "filter_type": filter_type,
            "filter_horizon": filter_horizon,
            "filter_status": filter_status,
            "filter_client_id": filter_client_id,
            "filter_label": filter_label,
            "filter_fee": filter_fee,
            "filter_count": len(rows),
            "show_age_home": show_age_home,
            "show_band_drill": show_band_drill,
            "show_pe_year_home": show_pe_year_home,
            "show_list": show_list,
            "age_home": age_home,
            "today_band": age_home["today"],
            "mid_bands": age_home["mid"],
            "all_bands": [
                age_home["today"],
                *age_home["mid"],
            ],
            "type_totals": type_totals,
            "mid_box_class": mid_box_class,
            "mid_tile_class": mid_tile_class,
            "type_box_class": type_box_class,
            "type_tile_class": type_tile_class,
            "type_query": type_query,
            "list_status_options": list_status_options,
            "filter_clients": clients_for_filter,
            "band_labels": band_labels,
            "wip_book": wip_book,
            "filter_pe_key": filter_pe_key,
            "filter_pe_slice": filter_pe_slice,
            "pe_layout": pe_layout,
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
