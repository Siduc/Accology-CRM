"""Purchase Ledger: suppliers, bills, payments, ageing (Working Capital · Creditors)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.finance import (
    CreditorBill,
    CreditorBillLine,
    Supplier,
    SupplierPayment,
    SupplierPaymentAllocation,
)
from app.services.working_capital import AgeBucket

OPEN_STATUSES = ("outstanding", "part_paid")
VOID_STATUSES = ("void", "draft")


def line_amounts(qty: float, unit_price: float, vat_rate: float) -> Tuple[float, float, float]:
    net = round(float(qty) * float(unit_price), 2)
    vat = round(net * float(vat_rate or 0), 2)
    return net, vat, round(net + vat, 2)


def recompute_bill_totals(db: Session, bill: CreditorBill) -> None:
    lines = (
        db.query(CreditorBillLine).filter(CreditorBillLine.bill_id == bill.id).all()
    )
    if lines:
        subtotal = 0.0
        vat_total = 0.0
        for ln in lines:
            net, vat, gross = line_amounts(
                ln.qty or 1, ln.unit_price or 0, ln.vat_rate or 0
            )
            ln.line_net = net
            ln.line_vat = vat
            ln.line_total = gross
            subtotal += net
            vat_total += vat
        total = round(subtotal + vat_total, 2)
        bill.subtotal = round(subtotal, 2)
        bill.vat_total = round(vat_total, 2)
        bill.vat_amount = bill.vat_total
        bill.total = total
        bill.amount = total
    else:
        # Header-only legacy-style bill
        total = float(bill.total or bill.amount or 0)
        vat = float(bill.vat_total if bill.vat_total is not None else (bill.vat_amount or 0))
        if total and not bill.subtotal:
            bill.subtotal = round(total - vat, 2)
        bill.vat_total = round(vat, 2)
        bill.vat_amount = bill.vat_total
        bill.total = round(total, 2)
        bill.amount = bill.total

    paid = (
        db.query(func.coalesce(func.sum(SupplierPaymentAllocation.amount), 0.0))
        .filter(SupplierPaymentAllocation.bill_id == bill.id)
        .scalar()
    )
    paid_f = round(float(paid or 0), 2)
    alloc_count = (
        db.query(func.count(SupplierPaymentAllocation.id))
        .filter(SupplierPaymentAllocation.bill_id == bill.id)
        .scalar()
    )
    if int(alloc_count or 0) > 0:
        bill.amount_paid = paid_f
    # else keep existing amount_paid (legacy full pay without allocation rows)

    total = float(bill.total or bill.amount or 0)
    bill.total = total
    bill.amount = total
    bill.balance = round(max(0.0, total - float(bill.amount_paid or 0)), 2)

    st = (bill.status or "outstanding").lower()
    if st in ("void", "draft"):
        return
    if bill.balance <= 0.001 and total > 0:
        bill.status = "paid"
        if not bill.paid_date:
            bill.paid_date = date.today()
    elif float(bill.amount_paid or 0) > 0.001:
        bill.status = "part_paid"
        bill.paid_date = None
    else:
        bill.status = "outstanding"
        bill.paid_date = None


def migrate_legacy_bills(db: Session) -> int:
    """Backfill totals/balance/issue_date and create suppliers from names."""
    n = 0
    bills = db.query(CreditorBill).all()
    for b in bills:
        changed = False
        if b.total is None or float(b.total or 0) == 0:
            gross = float(b.amount or 0)
            vat = float(b.vat_amount or b.vat_total or 0)
            b.total = gross
            b.amount = gross
            b.vat_total = vat
            b.vat_amount = vat
            b.subtotal = round(gross - vat, 2)
            changed = True
        if b.balance is None or (
            (b.status or "") == "outstanding" and float(b.balance or 0) == 0 and float(b.total or 0) > 0
        ):
            if (b.status or "") == "paid":
                b.amount_paid = float(b.total or b.amount or 0)
                b.balance = 0.0
            else:
                b.amount_paid = float(b.amount_paid or 0)
                b.balance = round(
                    max(0.0, float(b.total or b.amount or 0) - float(b.amount_paid or 0)),
                    2,
                )
            changed = True
        if not b.issue_date:
            if b.created_at:
                b.issue_date = (
                    b.created_at.date()
                    if isinstance(b.created_at, datetime)
                    else b.created_at
                )
            else:
                b.issue_date = date.today()
            changed = True
        if not b.supplier_id and (b.supplier_name or "").strip():
            name = b.supplier_name.strip()
            sup = (
                db.query(Supplier)
                .filter(func.lower(Supplier.name) == name.lower())
                .first()
            )
            if not sup:
                sup = Supplier(name=name, is_active=True)
                db.add(sup)
                db.flush()
            b.supplier_id = sup.id
            changed = True
        if changed:
            n += 1
    if n:
        db.commit()
    return n


def create_supplier(
    db: Session,
    *,
    name: str,
    contact_name: str = "",
    email: str = "",
    phone: str = "",
    address_line1: str = "",
    town: str = "",
    postcode: str = "",
    vat_number: str = "",
    payment_terms_days: int = 30,
    default_category: str = "supplier",
    notes: str = "",
) -> Supplier:
    sup = Supplier(
        name=(name or "").strip() or "Supplier",
        contact_name=(contact_name or "").strip() or None,
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        address_line1=(address_line1 or "").strip() or None,
        town=(town or "").strip() or None,
        postcode=(postcode or "").strip() or None,
        vat_number=(vat_number or "").strip() or None,
        payment_terms_days=int(payment_terms_days or 30),
        default_category=(default_category or "supplier").strip() or "supplier",
        notes=(notes or "").strip() or None,
        is_active=True,
    )
    db.add(sup)
    db.commit()
    db.refresh(sup)
    return sup


def update_supplier(db: Session, supplier_id: int, **kwargs) -> Supplier:
    sup = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not sup:
        raise ValueError("Supplier not found")
    for key, val in kwargs.items():
        if hasattr(sup, key):
            setattr(sup, key, val)
    sup.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(sup)
    return sup


def list_suppliers(db: Session, *, active_only: bool = True) -> List[Supplier]:
    q = db.query(Supplier).order_by(Supplier.name.asc())
    if active_only:
        q = q.filter(Supplier.is_active.is_(True))
    return q.all()


def get_or_create_supplier_by_name(db: Session, name: str) -> Supplier:
    name = (name or "").strip() or "Supplier"
    sup = (
        db.query(Supplier).filter(func.lower(Supplier.name) == name.lower()).first()
    )
    if sup:
        return sup
    return create_supplier(db, name=name)


def create_bill(
    db: Session,
    *,
    supplier_id: Optional[int] = None,
    supplier_name: str = "",
    number: str = "",
    issue_date: Optional[date] = None,
    due_date: Optional[date] = None,
    category: str = "supplier",
    description: str = "",
    notes: str = "",
    status: str = "outstanding",
    lines: Optional[Sequence[dict]] = None,
    # quick entry fallback
    net: Optional[float] = None,
    vat_rate: float = 0.0,
    gross: Optional[float] = None,
) -> CreditorBill:
    migrate_legacy_bills(db)
    sup = None
    if supplier_id:
        sup = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not sup and supplier_name:
        sup = get_or_create_supplier_by_name(db, supplier_name)
    sname = sup.display_name() if sup else (supplier_name or "Supplier")
    issue = issue_date or date.today()
    terms = int(sup.payment_terms_days or 30) if sup else 30
    due = due_date or (issue + timedelta(days=terms))
    cat = (category or (sup.default_category if sup else "supplier") or "supplier")

    bill = CreditorBill(
        supplier_id=sup.id if sup else None,
        supplier_name=sname,
        number=(number or "").strip() or None,
        description=(description or "").strip() or None,
        notes=(notes or "").strip() or None,
        issue_date=issue,
        due_date=due,
        category=cat.strip().lower(),
        status=status or "outstanding",
        amount=0.0,
        vat_amount=0.0,
        subtotal=0.0,
        vat_total=0.0,
        total=0.0,
        amount_paid=0.0,
        balance=0.0,
    )
    db.add(bill)
    db.flush()

    line_defs = list(lines or [])
    if not line_defs:
        if gross is not None and float(gross) > 0:
            g = float(gross)
            vrate = float(vat_rate or 0)
            if vrate > 0:
                net_v = round(g / (1 + vrate), 2)
                vat_v = round(g - net_v, 2)
            else:
                net_v = g
                vat_v = 0.0
            line_defs = [
                {
                    "description": description or "Purchase",
                    "qty": 1,
                    "unit_price": net_v,
                    "vat_rate": vrate,
                }
            ]
            bill.subtotal = net_v
            bill.vat_total = vat_v
            bill.total = g
        elif net is not None:
            n = float(net)
            vrate = float(vat_rate or 0)
            vat_v = round(n * vrate, 2)
            line_defs = [
                {
                    "description": description or "Purchase",
                    "qty": 1,
                    "unit_price": n,
                    "vat_rate": vrate,
                }
            ]

    for ld in line_defs:
        qty = float(ld.get("qty") or 1)
        price = float(ld.get("unit_price") or 0)
        vrate = float(ld.get("vat_rate") or 0)
        net_v, vat_v, gross_v = line_amounts(qty, price, vrate)
        db.add(
            CreditorBillLine(
                bill_id=bill.id,
                description=(ld.get("description") or "Line").strip(),
                qty=qty,
                unit_price=price,
                vat_rate=vrate,
                line_net=net_v,
                line_vat=vat_v,
                line_total=gross_v,
            )
        )
    db.flush()
    recompute_bill_totals(db, bill)
    db.commit()
    db.refresh(bill)
    return bill


def outstanding_bills(db: Session) -> List[CreditorBill]:
    migrate_legacy_bills(db)
    bills = (
        db.query(CreditorBill)
        .filter(CreditorBill.status.in_(list(OPEN_STATUSES)))
        .all()
    )
    # Also treat balance > 0
    out = []
    for b in bills:
        bal = float(b.balance if b.balance is not None else (b.amount or b.total or 0))
        if bal > 0.001 and (b.status or "") not in VOID_STATUSES:
            out.append(b)
    # Include any with balance>0 even if status odd after migration
    extras = (
        db.query(CreditorBill)
        .filter(CreditorBill.balance > 0.001)
        .filter(CreditorBill.status.notin_(["void", "draft", "paid"]))
        .all()
    )
    seen = {b.id for b in out}
    for b in extras:
        if b.id not in seen:
            out.append(b)
    return out


def creditors_total(db: Session) -> Tuple[float, int]:
    bills = outstanding_bills(db)
    total = round(sum(float(b.balance or 0) for b in bills), 2)
    return total, len(bills)


def bill_age_days(bill: CreditorBill, today: Optional[date] = None) -> int:
    today = today or date.today()
    base = bill.due_date or bill.issue_date or today
    return max(0, (today - base).days)


def ageing_report(db: Session, today: Optional[date] = None) -> List[AgeBucket]:
    today = today or date.today()
    buckets = {
        "0–30": AgeBucket("0–30"),
        "31–60": AgeBucket("31–60"),
        "61–90": AgeBucket("61–90"),
        "90+": AgeBucket("90+"),
    }
    for b in outstanding_bills(db):
        days = bill_age_days(b, today)
        if days <= 30:
            lab = "0–30"
        elif days <= 60:
            lab = "31–60"
        elif days <= 90:
            lab = "61–90"
        else:
            lab = "90+"
        buckets[lab].count += 1
        buckets[lab].amount += float(b.balance or 0)
    return list(buckets.values())


def record_supplier_payment(
    db: Session,
    *,
    supplier_id: Optional[int] = None,
    amount: float,
    payment_date: Optional[date] = None,
    method: str = "bank",
    reference: Optional[str] = None,
    notes: Optional[str] = None,
    bill_allocations: Optional[Sequence[Tuple[int, float]]] = None,
    post_to_bank: bool = False,
) -> SupplierPayment:
    pay = SupplierPayment(
        supplier_id=supplier_id,
        payment_date=payment_date or date.today(),
        amount=round(float(amount), 2),
        method=method or "bank",
        reference=reference,
        notes=notes,
    )
    db.add(pay)
    db.flush()

    if post_to_bank and amount > 0:
        from app.models.finance import BankTransaction
        from app.services.bank_ledger import ensure_default_bank_account

        acc = ensure_default_bank_account(db)
        counterparty = "Supplier"
        if supplier_id:
            s = db.query(Supplier).filter(Supplier.id == supplier_id).first()
            if s:
                counterparty = s.display_name()
        txn = BankTransaction(
            account_id=acc.id,
            txn_date=pay.payment_date,
            description=reference or f"Payment — {counterparty}",
            amount=-abs(float(amount)),
            reference=reference,
            counterparty=counterparty,
            category="supplier",
            source="creditor",
            matched_type="supplier_payment",
            matched_id=None,
        )
        db.add(txn)
        db.flush()
        pay.bank_transaction_id = txn.id
        txn.matched_id = pay.id

    if bill_allocations:
        for bill_id, amt in bill_allocations:
            if amt <= 0:
                continue
            db.add(
                SupplierPaymentAllocation(
                    payment_id=pay.id,
                    bill_id=bill_id,
                    amount=round(float(amt), 2),
                )
            )
        db.flush()
        touched = {bid for bid, amt in bill_allocations if amt > 0}
        for bill_id in touched:
            bill = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
            if bill:
                recompute_bill_totals(db, bill)
                if bill.bank_transaction_id is None and pay.bank_transaction_id:
                    bill.bank_transaction_id = pay.bank_transaction_id

    db.commit()
    db.refresh(pay)
    return pay


def pay_bill_from_bank(
    db: Session,
    bill_id: int,
    *,
    account_id: Optional[int] = None,
    txn_date: Optional[date] = None,
) -> Tuple[CreditorBill, Optional[SupplierPayment]]:
    """Full settlement of a bill via bank (purchase payment + bank outflow)."""
    bill = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
    if not bill:
        raise ValueError("Bill not found")
    bal = float(bill.balance if bill.balance is not None else (bill.amount or 0))
    if bal <= 0.001:
        raise ValueError("Bill already settled")
    pay = record_supplier_payment(
        db,
        supplier_id=bill.supplier_id,
        amount=bal,
        payment_date=txn_date or date.today(),
        method="bank",
        reference=bill.number or f"Bill #{bill.id}",
        notes=f"Pay bill #{bill.id}",
        bill_allocations=[(bill.id, bal)],
        post_to_bank=True,
    )
    db.refresh(bill)
    return bill, pay


def purchase_home_metrics(db: Session, today: Optional[date] = None) -> dict:
    today = today or date.today()
    bills = outstanding_bills(db)
    total = round(sum(float(b.balance or 0) for b in bills), 2)
    overdue = round(
        sum(
            float(b.balance or 0)
            for b in bills
            if b.due_date and b.due_date < today
        ),
        2,
    )
    pay_count = db.query(func.count(SupplierPayment.id)).scalar() or 0
    bill_count = db.query(func.count(CreditorBill.id)).scalar() or 0
    return {
        "outstanding_total": total,
        "outstanding_count": len(bills),
        "overdue_total": overdue,
        "payment_count": int(pay_count),
        "bill_count": int(bill_count),
        "ageing": ageing_report(db, today),
    }
