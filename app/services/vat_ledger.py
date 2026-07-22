"""VAT Ledger: invoice-basis output/input VAT and simplified return draft."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.finance import CreditorBill, CreditorBillLine
from app.models.sales import Invoice, InvoiceLine


@dataclass
class VatLine:
    kind: str  # output | input
    doc_id: int
    number: str
    party: str
    issue_date: Optional[date]
    net: float
    vat: float
    gross: float
    href: str


@dataclass
class VatReturnSummary:
    d0: date
    d1: date
    period_label: str
    output_net: float
    output_vat: float
    output_gross: float
    input_net: float
    input_vat: float
    input_gross: float
    box1_output_vat: float
    box4_input_vat: float
    box5_net: float  # positive = due to HMRC
    output_lines: List[VatLine] = field(default_factory=list)
    input_lines: List[VatLine] = field(default_factory=list)
    by_rate_output: Dict[str, float] = field(default_factory=dict)
    by_rate_input: Dict[str, float] = field(default_factory=dict)


def _quarter_bounds(year: int, q: int) -> Tuple[date, date]:
    starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    m0, d0 = starts[q]
    m1, d1 = ends[q]
    return date(year, m0, d0), date(year, m1, d1)


def current_quarter(today: Optional[date] = None) -> Tuple[int, int]:
    today = today or date.today()
    q = (today.month - 1) // 3 + 1
    return today.year, q


def vat_period_bounds(
    period: str = "this_quarter",
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    today: Optional[date] = None,
) -> Tuple[date, date, str]:
    today = today or date.today()
    raw = (period or "this_quarter").strip().lower()

    if from_date and to_date:
        return from_date, to_date, f"{from_date.isoformat()} → {to_date.isoformat()}"

    if raw in ("this_quarter", "current", ""):
        y, q = current_quarter(today)
        d0, d1 = _quarter_bounds(y, q)
        return d0, d1, f"Q{q} {y}"
    if raw in ("last_quarter", "previous"):
        y, q = current_quarter(today)
        q -= 1
        if q < 1:
            q = 4
            y -= 1
        d0, d1 = _quarter_bounds(y, q)
        return d0, d1, f"Q{q} {y}"

    # YYYY-Qn
    if len(raw) >= 6 and "-q" in raw:
        try:
            y_s, q_s = raw.split("-q", 1)
            y, q = int(y_s), int(q_s)
            d0, d1 = _quarter_bounds(y, q)
            return d0, d1, f"Q{q} {y}"
        except ValueError:
            pass

    y, q = current_quarter(today)
    d0, d1 = _quarter_bounds(y, q)
    return d0, d1, f"Q{q} {y}"


def output_vat(db: Session, d0: date, d1: date) -> Tuple[float, float, float, List[VatLine], Dict[str, float]]:
    invs = (
        db.query(Invoice)
        .filter(Invoice.issue_date >= d0, Invoice.issue_date <= d1)
        .filter(Invoice.status.notin_(["void", "written_off", "draft"]))
        .order_by(Invoice.issue_date.asc())
        .all()
    )
    lines_out: List[VatLine] = []
    by_rate: Dict[str, float] = {}
    total_net = total_vat = total_gross = 0.0
    for inv in invs:
        net = float(inv.subtotal or 0)
        vat = float(inv.vat_total or 0)
        gross = float(inv.total or (net + vat))
        total_net += net
        total_vat += vat
        total_gross += gross
        from app.models import Client

        client = db.query(Client).filter(Client.id == inv.client_id).first()
        party = client.display_name() if client else f"Client #{inv.client_id}"
        lines_out.append(
            VatLine(
                kind="output",
                doc_id=inv.id,
                number=inv.number or str(inv.id),
                party=party,
                issue_date=inv.issue_date,
                net=round(net, 2),
                vat=round(vat, 2),
                gross=round(gross, 2),
                href=f"/sales/invoices/{inv.id}",
            )
        )
        for ln in (
            db.query(InvoiceLine).filter(InvoiceLine.invoice_id == inv.id).all()
        ):
            rate = float(ln.vat_rate or 0)
            key = f"{rate * 100:.0f}%"
            line_vat = round(
                float(ln.qty or 0) * float(ln.unit_price or 0) * rate, 2
            )
            by_rate[key] = round(by_rate.get(key, 0) + line_vat, 2)

    return (
        round(total_net, 2),
        round(total_vat, 2),
        round(total_gross, 2),
        lines_out,
        by_rate,
    )


def input_vat(db: Session, d0: date, d1: date) -> Tuple[float, float, float, List[VatLine], Dict[str, float]]:
    bills = (
        db.query(CreditorBill)
        .filter(CreditorBill.issue_date >= d0, CreditorBill.issue_date <= d1)
        .filter(CreditorBill.status.notin_(["void", "draft"]))
        .order_by(CreditorBill.issue_date.asc())
        .all()
    )
    lines_in: List[VatLine] = []
    by_rate: Dict[str, float] = {}
    total_net = total_vat = total_gross = 0.0
    for b in bills:
        net = float(b.subtotal or 0)
        vat = float(b.vat_total if b.vat_total is not None else (b.vat_amount or 0))
        gross = float(b.total or b.amount or (net + vat))
        if net == 0 and gross and vat:
            net = round(gross - vat, 2)
        total_net += net
        total_vat += vat
        total_gross += gross
        party = b.supplier_name or f"Supplier #{b.supplier_id or b.id}"
        lines_in.append(
            VatLine(
                kind="input",
                doc_id=b.id,
                number=b.number or str(b.id),
                party=party,
                issue_date=b.issue_date,
                net=round(net, 2),
                vat=round(vat, 2),
                gross=round(gross, 2),
                href=f"/purchase/bills/{b.id}",
            )
        )
        blines = (
            db.query(CreditorBillLine)
            .filter(CreditorBillLine.bill_id == b.id)
            .all()
        )
        if blines:
            for ln in blines:
                rate = float(ln.vat_rate or 0)
                key = f"{rate * 100:.0f}%"
                by_rate[key] = round(by_rate.get(key, 0) + float(ln.line_vat or 0), 2)
        elif vat:
            by_rate["mixed"] = round(by_rate.get("mixed", 0) + vat, 2)

    return (
        round(total_net, 2),
        round(total_vat, 2),
        round(total_gross, 2),
        lines_in,
        by_rate,
    )


def vat_return_summary(
    db: Session,
    period: str = "this_quarter",
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    today: Optional[date] = None,
) -> VatReturnSummary:
    d0, d1, label = vat_period_bounds(
        period, from_date=from_date, to_date=to_date, today=today
    )
    o_net, o_vat, o_gross, o_lines, o_rates = output_vat(db, d0, d1)
    i_net, i_vat, i_gross, i_lines, i_rates = input_vat(db, d0, d1)
    box5 = round(o_vat - i_vat, 2)
    return VatReturnSummary(
        d0=d0,
        d1=d1,
        period_label=label,
        output_net=o_net,
        output_vat=o_vat,
        output_gross=o_gross,
        input_net=i_net,
        input_vat=i_vat,
        input_gross=i_gross,
        box1_output_vat=o_vat,
        box4_input_vat=i_vat,
        box5_net=box5,
        output_lines=o_lines,
        input_lines=i_lines,
        by_rate_output=o_rates,
        by_rate_input=i_rates,
    )
