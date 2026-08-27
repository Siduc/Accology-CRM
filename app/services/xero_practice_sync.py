"""Accology Limited ↔ Xero: CRM owns invoices; Xero owns the bank feed.

Do not import Xero's project/analysis invoice mess.
Do not import anything before PRACTICE_XERO_CUTOFF (opening-balance cut-off).
Bank: pull Xero feed since cut-off, skip duplicates.
Sales: raise in CRM, push to Xero; align paid status on matching invoice numbers.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import PRACTICE_XERO_CUTOFF
from app.models import Client
from app.models.finance import BankAccount, BankTransaction
from app.models.sales import Invoice, Payment, PaymentAllocation
from app.services.bank_code import code_from_description
from app.services.bank_ledger import ensure_default_bank_account
from app.services.client_playbook import get_playbook
from app.services.sales_ledger import recompute_invoice_totals
from app.services.xero_books import _page_list, xero_get, xero_put
from app.services.xero_oauth import get_valid_access_token

PRACTICE_NAME = "accology limited"
XERO_INV_PREFIX = "xero-inv:"
XERO_BT_PREFIX = "xero-bt:"


def cutoff_date() -> date:
    try:
        return date.fromisoformat((PRACTICE_XERO_CUTOFF or "2026-07-09")[:10])
    except ValueError:
        return date(2026, 7, 9)


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"\b(limited|ltd|llp|plc)\b\.?", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _xero_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if s.startswith("/Date("):
        try:
            ms = int(re.search(r"-?\d+", s).group(0))
            return datetime.utcfromtimestamp(ms / 1000.0).date()
        except Exception:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def practice_client(db: Session) -> Optional[Client]:
    want = _norm(PRACTICE_NAME)
    for c in db.query(Client).all():
        name = c.company_name or ""
        if "pay" in name.lower():
            continue
        if _norm(name) == want:
            return c
    return None


def practice_tenant_id(db: Session) -> Tuple[str, str]:
    client = practice_client(db)
    if not client:
        return "", "Accology Limited is not on the CRM."
    pb = get_playbook(db, client.id)
    tid = (pb.source_org_id if pb else "") or ""
    if not tid:
        return "", "Link Accology Limited to its Xero organisation on the Playbook."
    return tid, ""


def undo_bad_full_bank_import(db: Session) -> Dict[str, int]:
    """Remove the first over-wide Xero bank dump (pre-cut-off history)."""
    n_txn = (
        db.query(BankTransaction)
        .filter(BankTransaction.source == "xero")
        .delete(synchronize_session=False)
    )
    n_acc = 0
    for acc in db.query(BankAccount).filter(BankAccount.xero_account_id.isnot(None)).all():
        left = (
            db.query(BankTransaction)
            .filter(BankTransaction.account_id == acc.id)
            .count()
        )
        if left == 0 and (acc.name or "").strip().upper() == "ACCOLOGY LIMITED":
            db.delete(acc)
            n_acc += 1
    db.commit()
    return {"transactions_removed": int(n_txn or 0), "accounts_removed": n_acc}


def _token(db: Session) -> Tuple[Optional[str], str, str]:
    tid, err = practice_tenant_id(db)
    if err:
        return None, "", err
    token, terr, _ = get_valid_access_token(db)
    if not token:
        return None, tid, terr or "Xero is not connected."
    return token, tid, ""


def _practice_bank_account(db: Session, xero_account_id: str = "") -> BankAccount:
    acc = ensure_default_bank_account(db)
    if xero_account_id and not acc.xero_account_id:
        acc.xero_account_id = xero_account_id
        db.commit()
    return acc


def sync_bank_from_xero(db: Session) -> Dict[str, Any]:
    """Pull Xero bank feed since cut-off into the practice bank account. Skip dupes."""
    since = cutoff_date()
    out: Dict[str, Any] = {
        "ok": False,
        "since": since.isoformat(),
        "accounts": 0,
        "created": 0,
        "skipped": 0,
        "error": "",
    }
    token, tenant_id, err = _token(db)
    if err:
        out["error"] = err
        return out
    accounts, aerr = _page_list(
        token, tenant_id, "/Accounts", "Accounts", params={"where": 'Type=="BANK"'}
    )
    if aerr and not accounts:
        out["error"] = aerr
        return out
    primary = None
    for acc in accounts:
        xid = str(acc.get("AccountID") or "").strip()
        name = (acc.get("Name") or "").upper()
        if "ACCOLOGY" in name or not primary:
            primary = acc
            if "ACCOLOGY" in name:
                break
    if not primary:
        out["error"] = "No Xero bank account found."
        return out
    xid = str(primary.get("AccountID") or "").strip()
    crm_acc = _practice_bank_account(db, xid)
    out["accounts"] = 1
    out["account_name"] = crm_acc.name

    lines, stmt_err = _bank_statement_lines(
        token, tenant_id, xid, since, date.today()
    )
    if stmt_err or not lines:
        # Statement report needs an extra scope. Fallback: spend-money + invoice receipts.
        spends, _ = _spend_money_lines(token, tenant_id, since)
        recpts, _ = _payment_receipt_lines(token, tenant_id, since)
        lines = spends + recpts
        if not lines:
            out["error"] = stmt_err or "No Xero bank lines since the cut-off."
            return out
        out["via"] = "spend_and_receipts"

    existing_hash = {
        h
        for (h,) in db.query(BankTransaction.import_hash).filter(
            BankTransaction.import_hash.isnot(None)
        )
    }
    existing_sig = {
        (
            t.txn_date,
            round(float(t.amount or 0), 2),
            (t.description or "").strip().lower()[:80],
        )
        for t in db.query(BankTransaction)
        .filter(BankTransaction.account_id == crm_acc.id)
        .all()
    }

    for raw in lines:
        when = raw.get("date")
        if when and when < since:
            continue
        amount = round(float(raw.get("amount") or 0), 2)
        if abs(amount) < 0.005:
            continue
        desc = (raw.get("description") or "Xero bank")[:200]
        ih = raw.get("import_hash")
        if ih and ih in existing_hash:
            out["skipped"] += 1
            continue
        sig = (when, amount, desc.strip().lower()[:80])
        if sig in existing_sig:
            out["skipped"] += 1
            continue
        if amount > 0 and _near_match_receipt(existing_sig, when, amount):
            out["skipped"] += 1
            continue
        db.add(
            BankTransaction(
                account_id=crm_acc.id,
                txn_date=when or date.today(),
                description=desc,
                amount=amount,
                reference=(raw.get("reference") or "")[:80] or None,
                counterparty=(raw.get("counterparty") or None),
                category=code_from_description(desc, amount=amount),
                source="xero",
                import_hash=ih,
                reconciled=True,
                notes="Xero bank feed",
            )
        )
        if ih:
            existing_hash.add(ih)
        existing_sig.add(sig)
        out["created"] += 1
    db.commit()
    out["ok"] = True
    return out


def _near_match_receipt(existing_sig, when: Optional[date], amount: float) -> bool:
    if not when:
        return False
    for d, amt, _desc in existing_sig:
        if d is None:
            continue
        if abs(float(amt) - amount) < 0.02 and abs((d - when).days) <= 5:
            return True
    return False


def _spend_money_lines(token: str, tenant_id: str, since: date) -> Tuple[List[Dict[str, Any]], str]:
    where = f'Date>=DateTime({since.year},{since.month:02d},{since.day:02d})'
    txns, err = _page_list(
        token,
        tenant_id,
        "/BankTransactions",
        "BankTransactions",
        params={"where": where},
        max_pages=50,
    )
    lines = []
    for raw in txns:
        typ = str(raw.get("Type") or "").upper()
        total = float(raw.get("Total") or 0)
        if typ.startswith("RECEIVE"):
            amount = abs(total)
        else:
            amount = -abs(total)
        when = _xero_date(raw.get("Date"))
        tid = str(raw.get("BankTransactionID") or "").strip()
        desc = (
            ((raw.get("Contact") or {}).get("Name") or "")
            or (raw.get("Reference") or "")
            or typ
        )[:200]
        lines.append(
            {
                "date": when,
                "description": desc,
                "reference": (raw.get("Reference") or "")[:80],
                "amount": amount,
                "counterparty": ((raw.get("Contact") or {}).get("Name") or "")[:120],
                "import_hash": f"{XERO_BT_PREFIX}{tid}" if tid else None,
            }
        )
    return lines, err or ""


def _payment_receipt_lines(token: str, tenant_id: str, since: date) -> Tuple[List[Dict[str, Any]], str]:
    where = f'Date>=DateTime({since.year},{since.month:02d},{since.day:02d})'
    pays, err = _page_list(
        token, tenant_id, "/Payments", "Payments", params={"where": where}, max_pages=20
    )
    lines = []
    for p in pays:
        if str(p.get("PaymentType") or "").upper() != "ACCRECPAYMENT":
            continue
        if str(p.get("Status") or "").upper() not in ("AUTHORISED", "PAID", ""):
            continue
        amount = float(p.get("Amount") or p.get("BankAmount") or 0)
        if amount <= 0.005:
            continue
        inv = (p.get("Invoice") or {}).get("InvoiceNumber") or ""
        pid = str(p.get("PaymentID") or "").strip()
        lines.append(
            {
                "date": _xero_date(p.get("Date")),
                "description": f"Receipt {inv}".strip() or "Xero receipt",
                "reference": (p.get("Reference") or inv or "")[:80],
                "amount": abs(amount),
                "counterparty": inv,
                "import_hash": f"xero-pay:{pid}" if pid else None,
            }
        )
    return lines, err or ""


def _bank_statement_lines(
    token: str, tenant_id: str, bank_account_id: str, since: date, until: date
) -> Tuple[List[Dict[str, Any]], str]:
    """Xero Bank Statement report — the feed, including receipts Spend/Receive omit."""
    ok, st, err = xero_get(
        token,
        tenant_id,
        "/Reports/BankStatement",
        params={
            "bankAccountID": bank_account_id,
            "fromDate": since.isoformat(),
            "toDate": until.isoformat(),
        },
    )
    if not ok:
        msg = err or "Bank statement report failed."
        if "401" in msg or "403" in msg:
            msg = (
                "Xero will not share the bank statement yet. Reconnect Xero and "
                "allow accounting.reports.banksummary.read (and invoice/payment "
                "scopes when you want sales alignment). "
                "Spend-money-only import is disabled because it missed all receipts."
            )
        return [], msg
    lines: List[Dict[str, Any]] = []
    reps = (st or {}).get("Reports") or []
    if not reps:
        return [], "Xero returned an empty bank statement."

    def walk(rows: List[Any]) -> None:
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            if r.get("Rows"):
                walk(r.get("Rows") or [])
                continue
            cells = [(c or {}).get("Value") for c in (r.get("Cells") or [])]
            if len(cells) < 4:
                continue
            label = str(cells[0] or "").strip().lower()
            if "opening" in label or "closing" in label or "balance" == label:
                continue
            when = _xero_date(cells[0])
            if not when:
                continue
            desc = str(cells[1] or cells[2] or "Xero").strip()
            ref = str(cells[2] or "").strip()
            spent = _money(cells[3] if len(cells) > 3 else None)
            received = _money(cells[4] if len(cells) > 4 else None)
            amount = 0.0
            if received and abs(received) > 0.005:
                amount = abs(received)
            elif spent and abs(spent) > 0.005:
                amount = -abs(spent)
            else:
                continue
            key = f"xero-stmt:{when.isoformat()}:{amount:.2f}:{desc[:40]}"
            lines.append(
                {
                    "date": when,
                    "description": desc[:200],
                    "reference": ref[:80],
                    "amount": amount,
                    "counterparty": desc[:120],
                    "import_hash": key,
                }
            )

    walk(reps[0].get("Rows") or [])
    return lines, ""


def _money(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    s = str(value).replace("£", "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def align_invoices_since_cutoff(db: Session) -> Dict[str, Any]:
    """Match CRM invoices (since cut-off) to Xero by number. Do not import Xero invoices."""
    since = cutoff_date()
    out: Dict[str, Any] = {
        "ok": False,
        "since": since.isoformat(),
        "crm_since": 0,
        "matched": 0,
        "crm_not_in_xero": [],
        "xero_not_in_crm": [],
        "paid_updated": 0,
        "error": "",
    }
    token, tenant_id, err = _token(db)
    if err:
        out["error"] = err
        return out
    where = (
        f'Type=="ACCREC" AND Date>=DateTime({since.year},{since.month:02d},{since.day:02d})'
    )
    xero_invoices, xerr = _page_list(
        token, tenant_id, "/Invoices", "Invoices", params={"where": where}
    )
    if xerr and not xero_invoices:
        out["error"] = xerr
        if "401" in xerr or "403" in xerr:
            out["error"] = (
                "Xero will not share invoices until you Reconnect and allow "
                "invoice read/write. Bank sync does not need that."
            )
        return out
    by_num = {}
    for raw in xero_invoices:
        num = (raw.get("InvoiceNumber") or "").strip().upper()
        if num:
            by_num[num] = raw
    crm_inv = (
        db.query(Invoice)
        .filter(Invoice.issue_date >= since)
        .filter((Invoice.issuer.is_(None)) | (Invoice.issuer != "accology_pays"))
        .filter(Invoice.source != "opening_balance")
        .all()
    )
    out["crm_since"] = len(crm_inv)
    by_xid = {}
    for raw in xero_invoices:
        xid = str(raw.get("InvoiceID") or "").strip()
        if xid:
            by_xid[xid] = raw
    seen = set()
    for inv in crm_inv:
        num = (inv.number or "").strip().upper()
        raw = None
        # Prefer the Xero invoice already bound on the CRM row — numbers collide.
        key = inv.import_key or ""
        if key.startswith(XERO_INV_PREFIX):
            raw = by_xid.get(key.split(":", 1)[-1])
        if raw is None:
            raw = by_num.get(num)
            if raw is not None:
                xtot = float(raw.get("Total") or 0)
                ctot = float(inv.total or 0)
                if ctot > 0.005 and abs(xtot - ctot) > 0.05:
                    raw = None
        if not raw:
            if (inv.status or "") not in ("void", "draft"):
                out["crm_not_in_xero"].append(inv.number)
            continue
        xnum = (raw.get("InvoiceNumber") or "").strip().upper()
        if xnum:
            seen.add(xnum)
        xid = str(raw.get("InvoiceID") or "").strip()
        if xid and not (inv.import_key or "").startswith(XERO_INV_PREFIX):
            inv.import_key = f"{XERO_INV_PREFIX}{xid}"
        due = float(raw.get("AmountDue") or 0)
        paid = float(raw.get("AmountPaid") or 0)
        xst = str(raw.get("Status") or "").upper()
        if xst == "VOIDED":
            continue
        if paid > 0.005 and (inv.amount_paid or 0) + 0.005 < paid:
            inv.amount_paid = paid
            inv.balance = due
            if due <= 0.005:
                inv.status = "paid"
            elif paid > 0.005:
                inv.status = "part_paid"
            out["paid_updated"] += 1
        out["matched"] += 1
    for num, raw in by_num.items():
        if num in seen:
            continue
        if str(raw.get("Status") or "").upper() in ("VOIDED", "DRAFT", "DELETED"):
            continue
        out["xero_not_in_crm"].append(num)
    db.commit()
    out["ok"] = True
    return out


def push_invoice_to_xero(db: Session, invoice_id: int) -> Dict[str, Any]:
    """Create or update the CRM invoice on Xero Accology Limited (simple lines, no projects)."""
    inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not inv:
        return {"ok": False, "error": "Invoice not found."}
    if (inv.issuer or "accology") == "accology_pays":
        return {"ok": False, "error": "Accology Pays invoices stay off Accology Limited Xero."}
    if inv.issue_date and inv.issue_date < cutoff_date():
        return {"ok": False, "error": "This invoice is before the opening-balance cut-off."}
    token, tenant_id, err = _token(db)
    if err:
        return {"ok": False, "error": err}
    client = db.query(Client).filter(Client.id == inv.client_id).first()
    contact_name = client.display_name() if client else "Unknown"
    ok, contacts, cerr = xero_get(
        token,
        tenant_id,
        "/Contacts",
        params={"where": f'Name=="{contact_name.replace(chr(34), "")}"'},
    )
    contact = {"Name": contact_name}
    if ok and isinstance(contacts, dict):
        rows = contacts.get("Contacts") or []
        if rows:
            contact = {"ContactID": rows[0].get("ContactID")}
    lines = []
    for ln in inv.lines or []:
        tax = "OUTPUT2" if float(ln.vat_rate or 0) > 0.001 else "NONE"
        lines.append(
            {
                "Description": (ln.description or "Fee")[:4000],
                "Quantity": float(ln.qty or 1),
                "UnitAmount": float(ln.unit_price or 0),
                "TaxType": tax,
            }
        )
    if not lines:
        return {"ok": False, "error": "Invoice has no lines."}
    payload = {
        "Invoices": [
            {
                "Type": "ACCREC",
                "InvoiceNumber": inv.number,
                "Contact": contact,
                "Date": (inv.issue_date or date.today()).isoformat(),
                "DueDate": (inv.due_date or inv.issue_date or date.today()).isoformat(),
                "LineAmountTypes": "Exclusive",
                "Status": "AUTHORISED" if (inv.status or "") != "draft" else "DRAFT",
                "Reference": f"CRM {inv.number}",
                "LineItems": lines,
            }
        ]
    }
    existing_id = ""
    if (inv.import_key or "").startswith(XERO_INV_PREFIX):
        existing_id = inv.import_key.split(":", 1)[-1]
    if existing_id:
        payload["Invoices"][0]["InvoiceID"] = existing_id
    ok, data, perr = xero_put(token, tenant_id, "/Invoices", payload)
    if not ok:
        return {"ok": False, "error": perr or "Xero rejected the invoice."}
    created = (data or {}).get("Invoices") or []
    if created and created[0].get("InvoiceID"):
        inv.import_key = f"{XERO_INV_PREFIX}{created[0]['InvoiceID']}"
        db.commit()
    return {"ok": True, "number": inv.number, "xero_id": (created or [{}])[0].get("InvoiceID")}


def peek_xero_invoice_seq_max(db: Session) -> int:
    """Highest Accology Limited Xero sales-invoice sequence since the cut-off.

    Used so CRM allocates INV-0804 rather than the unused INV-0001 gap.
    Returns 0 when Xero is unavailable (caller falls back to CRM max).
    """
    from app.services.sales_ledger import invoice_number_seq

    token, tenant_id, err = _token(db)
    if err:
        return 0
    since = cutoff_date()
    where = (
        f'Type=="ACCREC" AND Date>=DateTime({since.year},{since.month:02d},{since.day:02d})'
    )
    rows, _xerr = _page_list(
        token,
        tenant_id,
        "/Invoices",
        "Invoices",
        params={"where": where},
        max_pages=15,
    )
    mx = 0
    for raw in rows or []:
        seq = invoice_number_seq(raw.get("InvoiceNumber") or "")
        if seq > mx:
            mx = seq
    return mx


def maybe_push_invoice_on_send(db: Session, invoice: Invoice) -> str:
    """Push a newly issued CRM invoice to Xero. Never blocks the CRM send."""
    if invoice is None:
        return ""
    if (invoice.issuer or "accology") == "accology_pays":
        return ""
    if (invoice.status or "").lower() in ("void", "written_off", "draft"):
        return ""
    result = push_invoice_to_xero(db, invoice.id)
    if result.get("ok"):
        return "Pushed to Xero Accology Limited."
    return f"Xero push skipped: {result.get('error') or 'unknown'}"[:240]


def sync_practice_ledgers(db: Session, *, months: int = 24) -> Dict[str, Any]:
    """Bank pull since cut-off + invoice number alignment. Never imports Xero invoice books."""
    undo = {"transactions_removed": 0, "accounts_removed": 0}
    leftover = (
        db.query(BankTransaction)
        .filter(BankTransaction.source == "xero")
        .filter(BankTransaction.txn_date < cutoff_date())
        .count()
    )
    if leftover:
        undo = undo_bad_full_bank_import(db)
    bank = sync_bank_from_xero(db)
    invoices = align_invoices_since_cutoff(db)
    err = ""
    if not bank.get("ok") and bank.get("error"):
        err = bank["error"]
    elif not invoices.get("ok") and invoices.get("error"):
        err = invoices["error"]
    return {
        "ok": bool(bank.get("ok") or invoices.get("ok")),
        "error": err,
        "cutoff": cutoff_date().isoformat(),
        "cleaned": undo,
        "bank": bank,
        "invoices": invoices,
    }
