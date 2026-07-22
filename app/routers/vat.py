"""VAT Ledger UI — output/input VAT and draft return."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.vat_ledger import vat_period_bounds, vat_return_summary
from app.templating import render

router = APIRouter(prefix="/vat", tags=["vat"])


def _parse_date(value: str):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


@router.get("", response_class=HTMLResponse)
async def vat_home(
    request: Request,
    period: str = Query("this_quarter"),
    from_date: str = Query(""),
    to_date: str = Query(""),
    db: Session = Depends(get_db),
):
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    if fd and td:
        period = "custom"
    summary = vat_return_summary(
        db, period if period != "custom" else "this_quarter", from_date=fd, to_date=td
    )
    return render(
        request,
        "vat/home.html",
        {
            "summary": summary,
            "period": period,
            "from_date": from_date,
            "to_date": to_date,
        },
    )


@router.get("/return", response_class=HTMLResponse)
async def vat_return_page(
    request: Request,
    period: str = Query("this_quarter"),
    from_date: str = Query(""),
    to_date: str = Query(""),
    db: Session = Depends(get_db),
):
    fd = _parse_date(from_date)
    td = _parse_date(to_date)
    summary = vat_return_summary(
        db, period if not (fd and td) else "this_quarter", from_date=fd, to_date=td
    )
    return render(
        request,
        "vat/return.html",
        {
            "summary": summary,
            "period": period,
            "from_date": from_date or summary.d0.isoformat(),
            "to_date": to_date or summary.d1.isoformat(),
        },
    )
