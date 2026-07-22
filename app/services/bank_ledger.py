"""Bank Ledger: multi-account cashbook, import, match, reconciliation."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.models.finance import BankAccount, BankTransaction, CreditorBill

NOMINAL_CATEGORIES = [
    ("client_receipt", "Client receipt (Sales)"),
    ("supplier", "Supplier / purchase"),
    ("vat", "VAT"),
    ("wages", "Wages / salaries"),
    ("rent", "Rent"),
    ("software", "Software / subscriptions"),
    ("insurance", "Insurance"),
    ("drawings", "Drawings"),
    ("bank_charges", "Bank charges"),
    ("transfer", "Transfer (own accounts)"),
    ("other_income", "Other income"),
    ("other_expense", "Other expense"),
]

CATEGORY_LABELS = {c: lab for c, lab in NOMINAL_CATEGORIES}


@dataclass
class AccountRow:
    account: BankAccount
    balance: float
    txn_count: int


@dataclass
class LedgerRow:
    txn: BankTransaction
    running_balance: float


@dataclass
class ParsedTxn:
    txn_date: date
    description: str
    amount: float  # signed
    reference: str = ""
    counterparty: str = ""
    balance_after: Optional[float] = None
    raw: str = ""
    import_hash: str = ""
    is_duplicate: bool = False


@dataclass
class ImportResult:
    created: int = 0
    skipped_dupes: int = 0
    errors: List[str] = field(default_factory=list)


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).lower() in ("1", "true", "yes", "on")


def ensure_default_bank_account(db: Session) -> BankAccount:
    """Primary account (create Practice account if none)."""
    primary = (
        db.query(BankAccount)
        .filter(BankAccount.is_primary.is_(True))
        .order_by(BankAccount.id.asc())
        .first()
    )
    if primary:
        return primary
    acc = db.query(BankAccount).order_by(BankAccount.id.asc()).first()
    if acc:
        acc.is_primary = True
        if acc.is_active is None:
            acc.is_active = True
        db.commit()
        db.refresh(acc)
        return acc
    acc = BankAccount(
        name="Practice account",
        bank_name="Practice",
        opening_balance=0.0,
        is_active=True,
        is_primary=True,
        currency="GBP",
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def get_account(db: Session, account_id: int) -> Optional[BankAccount]:
    return db.query(BankAccount).filter(BankAccount.id == account_id).first()


def list_accounts(db: Session, *, active_only: bool = True) -> List[AccountRow]:
    ensure_default_bank_account(db)
    q = db.query(BankAccount).order_by(
        BankAccount.is_primary.desc(), BankAccount.name.asc()
    )
    accounts = q.all()
    rows: List[AccountRow] = []
    for acc in accounts:
        if active_only and not _as_bool(acc.is_active, True):
            continue
        bal = account_balance(db, acc)
        cnt = (
            db.query(BankTransaction)
            .filter(BankTransaction.account_id == acc.id)
            .count()
        )
        rows.append(AccountRow(account=acc, balance=bal, txn_count=int(cnt or 0)))
    return rows


def total_cash(db: Session) -> float:
    return round(sum(r.balance for r in list_accounts(db, active_only=True)), 2)


def account_balance(
    db: Session, account: Optional[BankAccount] = None, account_id: Optional[int] = None
) -> float:
    acc = account
    if acc is None and account_id is not None:
        acc = get_account(db, account_id)
    if acc is None:
        acc = ensure_default_bank_account(db)
    total_txn = (
        db.query(BankTransaction)
        .filter(BankTransaction.account_id == acc.id)
        .with_entities(BankTransaction.amount)
        .all()
    )
    return round(
        float(acc.opening_balance or 0) + sum(float(r[0] or 0) for r in total_txn), 2
    )


def book_balance_as_of(db: Session, account: BankAccount, as_of: date) -> float:
    total_txn = (
        db.query(BankTransaction)
        .filter(BankTransaction.account_id == account.id)
        .filter(BankTransaction.txn_date <= as_of)
        .with_entities(BankTransaction.amount)
        .all()
    )
    return round(
        float(account.opening_balance or 0)
        + sum(float(r[0] or 0) for r in total_txn),
        2,
    )


def ledger_rows(
    db: Session,
    account_id: int,
    *,
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    reconciled: Optional[bool] = None,
    newest_first: bool = True,
) -> List[LedgerRow]:
    acc = get_account(db, account_id)
    if not acc:
        return []
    q = (
        db.query(BankTransaction)
        .filter(BankTransaction.account_id == account_id)
        .order_by(BankTransaction.txn_date.asc(), BankTransaction.id.asc())
    )
    all_txns = q.all()
    running = float(acc.opening_balance or 0)
    rows: List[LedgerRow] = []
    for t in all_txns:
        running = round(running + float(t.amount or 0), 2)
        if from_date and t.txn_date and t.txn_date < from_date:
            continue
        if to_date and t.txn_date and t.txn_date > to_date:
            continue
        if reconciled is not None:
            if _as_bool(t.reconciled, False) != reconciled:
                continue
        rows.append(LedgerRow(txn=t, running_balance=running))
    if newest_first:
        rows.reverse()
    return rows


def set_opening_balance(db: Session, account_id: int, amount: float) -> BankAccount:
    acc = get_account(db, account_id)
    if not acc:
        raise ValueError("Account not found")
    acc.opening_balance = round(float(amount), 2)
    db.commit()
    db.refresh(acc)
    return acc


def set_primary_account(db: Session, account_id: int) -> BankAccount:
    acc = get_account(db, account_id)
    if not acc:
        raise ValueError("Account not found")
    for a in db.query(BankAccount).all():
        a.is_primary = a.id == account_id
    db.commit()
    db.refresh(acc)
    return acc


def create_account(
    db: Session,
    *,
    name: str,
    bank_name: str = "",
    sort_code: str = "",
    account_number: str = "",
    opening_balance: float = 0.0,
    currency: str = "GBP",
    make_primary: bool = False,
    notes: str = "",
) -> BankAccount:
    ensure_default_bank_account(db)
    acc = BankAccount(
        name=(name or "Bank account").strip() or "Bank account",
        bank_name=(bank_name or "").strip() or None,
        sort_code=(sort_code or "").strip() or None,
        account_number=(account_number or "").strip() or None,
        currency=(currency or "GBP").strip() or "GBP",
        opening_balance=round(float(opening_balance or 0), 2),
        is_active=True,
        is_primary=False,
        notes=(notes or "").strip() or None,
    )
    db.add(acc)
    db.flush()
    if make_primary or db.query(BankAccount).count() == 1:
        set_primary_account(db, acc.id)
    else:
        db.commit()
        db.refresh(acc)
    return acc


def update_account(
    db: Session,
    account_id: int,
    *,
    name: str,
    bank_name: str = "",
    sort_code: str = "",
    account_number: str = "",
    currency: str = "GBP",
    is_active: bool = True,
    make_primary: bool = False,
    notes: str = "",
) -> BankAccount:
    acc = get_account(db, account_id)
    if not acc:
        raise ValueError("Account not found")
    acc.name = (name or acc.name or "Bank account").strip()
    acc.bank_name = (bank_name or "").strip() or None
    acc.sort_code = (sort_code or "").strip() or None
    acc.account_number = (account_number or "").strip() or None
    acc.currency = (currency or "GBP").strip() or "GBP"
    acc.is_active = bool(is_active)
    acc.notes = (notes or "").strip() or None
    db.commit()
    if make_primary:
        set_primary_account(db, acc.id)
    else:
        db.refresh(acc)
    return acc


def add_transaction(
    db: Session,
    account_id: int,
    *,
    txn_date: Optional[date] = None,
    amount: float,
    description: str = "",
    reference: str = "",
    counterparty: str = "",
    category: str = "",
    source: str = "manual",
    notes: str = "",
    import_hash: Optional[str] = None,
    matched_type: Optional[str] = None,
    matched_id: Optional[int] = None,
) -> BankTransaction:
    acc = get_account(db, account_id)
    if not acc:
        raise ValueError("Account not found")
    txn = BankTransaction(
        account_id=account_id,
        txn_date=txn_date or date.today(),
        description=(description or "").strip() or "Transaction",
        amount=round(float(amount), 2),
        reference=(reference or "").strip() or None,
        counterparty=(counterparty or "").strip() or None,
        category=(category or "").strip() or None,
        source=(source or "manual").strip() or "manual",
        notes=(notes or "").strip() or None,
        import_hash=import_hash,
        matched_type=matched_type,
        matched_id=matched_id,
        reconciled=False,
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def txn_import_hash(
    account_id: int,
    txn_date: date,
    amount: float,
    description: str,
    reference: str = "",
) -> str:
    raw = f"{account_id}|{txn_date.isoformat()}|{amount:.2f}|{(description or '').strip().lower()}|{(reference or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_money(value: str) -> Optional[float]:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "-":
        return None
    s = s.replace("£", "").replace(",", "").replace(" ", "")
    # parentheses = negative
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _parse_uk_or_iso_date(value: str) -> Optional[date]:
    s = (value or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s[:10] if len(s) >= 10 and fmt.startswith("%Y") else s, fmt).date()
        except ValueError:
            continue
    # try first 10 chars ISO
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (h or "").strip().lower())


def detect_csv_format(headers: Sequence[str]) -> str:
    norms = {_norm_header(h) for h in headers}
    # NatWest-style often has Date, Type, Description, Value, Balance or Amount
    if "value" in norms or ("type" in norms and "description" in norms and "date" in norms):
        return "natwest"
    if "debit" in norms or "credit" in norms:
        return "generic"
    if "amount" in norms and "date" in norms:
        return "generic"
    return "generic"


def parse_bank_csv(
    text: str,
    *,
    fmt: str = "auto",
    account_id: int = 0,
) -> List[ParsedTxn]:
    text = (text or "").lstrip("\ufeff").strip()
    if not text:
        return []
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    # find header row (first non-empty)
    header_idx = 0
    headers = rows[0]
    for i, row in enumerate(rows[:5]):
        joined = ",".join(row).lower()
        if "date" in joined and (
            "amount" in joined
            or "value" in joined
            or "debit" in joined
            or "description" in joined
        ):
            header_idx = i
            headers = row
            break
    data_rows = rows[header_idx + 1 :]
    if fmt == "auto":
        fmt = detect_csv_format(headers)
    col = {_norm_header(h): i for i, h in enumerate(headers) if h is not None}

    def cell(row: list, *names: str) -> str:
        for n in names:
            idx = col.get(_norm_header(n))
            if idx is not None and idx < len(row):
                return (row[idx] or "").strip()
        return ""

    parsed: List[ParsedTxn] = []
    for raw_row in data_rows:
        if not raw_row or all(not (c or "").strip() for c in raw_row):
            continue
        d_str = cell(raw_row, "date", "transactiondate", "posted date", "valuedate")
        d = _parse_uk_or_iso_date(d_str)
        if not d:
            continue
        desc = cell(
            raw_row,
            "description",
            "narrative",
            "details",
            "transactiondescription",
            "memo",
        )
        ref = cell(raw_row, "reference", "ref", "transactionreference", "check")
        counterparty = cell(raw_row, "counterparty", "payee", "payer", "name")

        amount = None
        if fmt == "natwest" or "value" in col:
            amount = _parse_money(cell(raw_row, "value", "amount", "transactionamount"))
        if amount is None:
            debit = _parse_money(cell(raw_row, "debit", "money out", "withdrawals", "paid out"))
            credit = _parse_money(
                cell(raw_row, "credit", "money in", "deposits", "paid in")
            )
            if debit is not None or credit is not None:
                amount = (credit or 0.0) - abs(debit or 0.0)
        if amount is None:
            amount = _parse_money(cell(raw_row, "amount", "value"))
        if amount is None:
            continue

        # NatWest sometimes uses Type + positive Amount for both; check Type
        ttype = cell(raw_row, "type", "transactiontype", "creditdebit").lower()
        if ttype in ("d/d", "debit", "payment", "withdrawal", "so", "direct debit") and amount > 0:
            # only flip if no separate debit column already applied
            if "debit" not in col and "credit" not in col:
                amount = -abs(amount)
        elif ttype in ("credit", "deposit", "receipt") and amount < 0:
            amount = abs(amount)

        bal_after = _parse_money(cell(raw_row, "balance", "runningbalance", "accountbalance"))
        h = txn_import_hash(account_id, d, round(amount, 2), desc, ref)
        parsed.append(
            ParsedTxn(
                txn_date=d,
                description=desc or "Imported",
                amount=round(amount, 2),
                reference=ref,
                counterparty=counterparty,
                balance_after=bal_after,
                raw=",".join(raw_row),
                import_hash=h,
            )
        )
    return parsed


def mark_duplicates(
    db: Session, account_id: int, rows: List[ParsedTxn]
) -> List[ParsedTxn]:
    existing = {
        r[0]
        for r in db.query(BankTransaction.import_hash)
        .filter(BankTransaction.account_id == account_id)
        .filter(BankTransaction.import_hash.isnot(None))
        .all()
        if r[0]
    }
    for row in rows:
        if not row.import_hash:
            row.import_hash = txn_import_hash(
                account_id, row.txn_date, row.amount, row.description, row.reference
            )
        row.is_duplicate = row.import_hash in existing
    return rows


def import_transactions(
    db: Session,
    account_id: int,
    rows: Sequence[ParsedTxn],
    *,
    skip_duplicates: bool = True,
) -> ImportResult:
    result = ImportResult()
    mark_duplicates(db, account_id, list(rows))
    for row in rows:
        if skip_duplicates and row.is_duplicate:
            result.skipped_dupes += 1
            continue
        try:
            add_transaction(
                db,
                account_id,
                txn_date=row.txn_date,
                amount=row.amount,
                description=row.description,
                reference=row.reference,
                counterparty=row.counterparty,
                source="import",
                import_hash=row.import_hash,
            )
            result.created += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{row.txn_date} {row.description}: {exc}")
    return result


def set_reconciled(
    db: Session, txn_ids: Sequence[int], cleared: bool = True
) -> int:
    n = 0
    now = datetime.utcnow()
    for tid in txn_ids:
        t = db.query(BankTransaction).filter(BankTransaction.id == tid).first()
        if not t:
            continue
        t.reconciled = bool(cleared)
        t.reconciled_at = now if cleared else None
        n += 1
    db.commit()
    return n


def reconciliation_summary(
    db: Session,
    account_id: int,
    statement_balance: float,
    as_of: Optional[date] = None,
) -> dict:
    acc = get_account(db, account_id)
    if not acc:
        return {}
    as_of = as_of or date.today()
    book = book_balance_as_of(db, acc, as_of)
    unrec = (
        db.query(BankTransaction)
        .filter(BankTransaction.account_id == account_id)
        .filter(BankTransaction.txn_date <= as_of)
        .filter(
            (BankTransaction.reconciled.is_(False))
            | (BankTransaction.reconciled.is_(None))
        )
        .count()
    )
    return {
        "account": acc,
        "as_of": as_of,
        "book_balance": book,
        "statement_balance": round(float(statement_balance), 2),
        "difference": round(float(statement_balance) - book, 2),
        "unreconciled_count": int(unrec or 0),
    }


def match_receipt_to_sales(
    db: Session,
    txn_id: int,
    *,
    client_id: int,
    invoice_allocations: Optional[Sequence[Tuple[int, float]]] = None,
    notes: str = "",
):
    """Post bank money-in to Sales Ledger (Payment + optional allocations)."""
    from app.services.sales_ledger import record_payment

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        raise ValueError("Transaction not found")
    if float(txn.amount or 0) <= 0:
        raise ValueError("Receipt match requires a money-in transaction")
    if txn.matched_type:
        raise ValueError("Transaction already matched")

    pay = record_payment(
        db,
        client_id=client_id,
        amount=float(txn.amount),
        payment_date=txn.txn_date,
        method="bank",
        reference=txn.reference or txn.description,
        notes=notes or f"Matched bank txn #{txn.id}",
        invoice_allocations=invoice_allocations,
        post_to_bank=False,
    )
    pay.bank_transaction_id = txn.id
    txn.matched_type = "payment"
    txn.matched_id = pay.id
    txn.category = "client_receipt"
    if not txn.counterparty:
        from app.models import Client

        c = db.query(Client).filter(Client.id == client_id).first()
        if c:
            txn.counterparty = c.display_name()
    db.commit()
    db.refresh(txn)
    return pay


def match_payment_to_creditor(db: Session, txn_id: int, bill_id: int) -> CreditorBill:
    """Match bank money-out to a purchase bill (partial or full via supplier payment)."""
    from app.services.purchase_ledger import record_supplier_payment, recompute_bill_totals

    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    bill = db.query(CreditorBill).filter(CreditorBill.id == bill_id).first()
    if not txn:
        raise ValueError("Transaction not found")
    if not bill:
        raise ValueError("Bill not found")
    if float(txn.amount or 0) >= 0:
        raise ValueError("Creditor match requires a money-out transaction")
    if txn.matched_type:
        raise ValueError("Transaction already matched")
    bal = float(bill.balance if bill.balance is not None else (bill.amount or 0))
    if bal <= 0.001 or (bill.status or "") == "paid":
        raise ValueError("Bill already settled")

    pay_amt = min(abs(float(txn.amount)), bal)
    pay = record_supplier_payment(
        db,
        supplier_id=bill.supplier_id,
        amount=pay_amt,
        payment_date=txn.txn_date or date.today(),
        method="bank",
        reference=txn.reference or txn.description,
        notes=f"Matched bank txn #{txn.id}",
        bill_allocations=[(bill.id, pay_amt)],
        post_to_bank=False,
    )
    pay.bank_transaction_id = txn.id
    bill.bank_transaction_id = txn.id
    recompute_bill_totals(db, bill)
    txn.matched_type = "supplier_payment"
    txn.matched_id = pay.id
    txn.category = txn.category or bill.category or "supplier"
    if not txn.counterparty:
        txn.counterparty = bill.supplier_name
    db.commit()
    db.refresh(bill)
    return bill


def match_payment_to_nominal(
    db: Session, txn_id: int, category: str, notes: str = ""
) -> BankTransaction:
    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        raise ValueError("Transaction not found")
    if txn.matched_type and txn.matched_type not in ("nominal",):
        raise ValueError("Transaction already matched to another type")
    cat = (category or "other_expense").strip()
    if cat not in CATEGORY_LABELS:
        cat = "other_expense" if float(txn.amount or 0) < 0 else "other_income"
    txn.matched_type = "nominal"
    txn.matched_id = None
    txn.category = cat
    if notes:
        txn.notes = ((txn.notes or "") + " " + notes).strip()
    db.commit()
    db.refresh(txn)
    return txn


def pay_creditor_from_bank(
    db: Session,
    bill_id: int,
    *,
    account_id: Optional[int] = None,
    txn_date: Optional[date] = None,
) -> Tuple[CreditorBill, Optional[BankTransaction]]:
    """Settle purchase bill via Purchase Ledger payment + bank outflow."""
    from app.services.purchase_ledger import pay_bill_from_bank

    bill, pay = pay_bill_from_bank(db, bill_id, account_id=account_id, txn_date=txn_date)
    txn = None
    if pay and pay.bank_transaction_id:
        txn = (
            db.query(BankTransaction)
            .filter(BankTransaction.id == pay.bank_transaction_id)
            .first()
        )
    return bill, txn


def delete_transaction(db: Session, txn_id: int) -> bool:
    txn = db.query(BankTransaction).filter(BankTransaction.id == txn_id).first()
    if not txn:
        return False
    if txn.matched_type:
        raise ValueError("Unmatch before deleting")
    if _as_bool(txn.reconciled, False):
        raise ValueError("Unreconcile before deleting")
    db.delete(txn)
    db.commit()
    return True


def recent_transactions(db: Session, limit: int = 5) -> List[BankTransaction]:
    return (
        db.query(BankTransaction)
        .order_by(BankTransaction.txn_date.desc(), BankTransaction.id.desc())
        .limit(limit)
        .all()
    )
