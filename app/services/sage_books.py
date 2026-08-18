"""Sage Business Cloud pull + journal post into the client Current pack."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

import app.config as _cfg
from app.models.client import Client
from app.services import client_connections
from app.services.book_oauth import fetch_sage_businesses, get_valid_access_token, http_json
from app.services.client_playbook import get_or_create_playbook, is_sales_ledger_only
from app.services.xero_books import (
    _source_dir,
    _write_csv,
    default_as_at,
    load_journal_draft,
)

PROVIDER = "sage"


def _base() -> str:
    return (_cfg.SAGE_API_BASE or "https://api.accounting.sage.com/v3.1").rstrip("/")


def _get(token: str, business_id: str, path: str, params: Optional[Dict[str, str]] = None) -> tuple:
    url = path if path.startswith("http") else f"{_base()}{path}"
    if params:
        from urllib.parse import urlencode

        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    return http_json("GET", url, token, extra_headers={"X-Business": business_id})


def _post(token: str, business_id: str, path: str, payload: Dict[str, Any]) -> tuple:
    url = path if path.startswith("http") else f"{_base()}{path}"
    return http_json(
        "POST",
        url,
        token,
        extra_headers={"X-Business": business_id},
        data=json.dumps(payload).encode("utf-8"),
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("displayed_as") or value.get("name") or value.get("id") or "")
    return str(value)


def _items(
    token: str,
    business_id: str,
    path: str,
    params: Optional[Dict[str, str]] = None,
    *,
    max_pages: int = 50,
) -> tuple:
    """Follow Sage $next pages. Returns (items, error)."""
    query = dict(params or {})
    query.setdefault("items_per_page", "200")
    collected: List[Dict[str, Any]] = []
    url = path
    first = True
    for _ in range(max_pages):
        ok, data, err = _get(token, business_id, url, query if first else None)
        first = False
        if not ok:
            return collected, err or "Sage request failed."
        if not isinstance(data, dict):
            return collected, "Unexpected Sage response."
        batch = data.get("$items") or data.get("items") or []
        if isinstance(batch, list):
            collected.extend(item for item in batch if isinstance(item, dict))
        nxt = data.get("$next") or data.get("next") or ""
        if not nxt or not batch:
            return collected, ""
        url = str(nxt)
    return collected, ""


def _contact_types(item: Dict[str, Any]) -> str:
    types = item.get("contact_types") or item.get("contact_type") or []
    if isinstance(types, dict):
        types = [types]
    labels = []
    for row in types or []:
        if isinstance(row, dict):
            labels.append(str(row.get("id") or row.get("displayed_as") or ""))
        elif row:
            labels.append(str(row))
    return ", ".join(x for x in labels if x)


def _invoice_row(item: Dict[str, Any]) -> Dict[str, Any]:
    contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
    return {
        "InvoiceNumber": item.get("invoice_number") or item.get("displayed_as") or "",
        "Date": item.get("date") or "",
        "DueDate": item.get("due_date") or "",
        "Contact": _as_text(contact) or item.get("contact_name") or "",
        "ContactId": (contact or {}).get("id") or "",
        "Status": _as_text(item.get("status")),
        "Reference": item.get("reference") or "",
        "Net": item.get("net_amount") or item.get("net") or "",
        "VAT": item.get("tax_amount") or item.get("tax") or "",
        "Gross": item.get("total_amount") or item.get("total") or "",
        "Outstanding": item.get("outstanding_amount") or "",
        "Currency": _as_text(item.get("currency")) or "GBP",
        "Id": item.get("id") or "",
    }


def _invoice_line_rows(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    header = _invoice_row(item)
    lines = item.get("invoice_lines") or item.get("credit_note_lines") or item.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return []
    rows = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        ledger = line.get("ledger_account") if isinstance(line.get("ledger_account"), dict) else {}
        tax = line.get("tax_rate") if isinstance(line.get("tax_rate"), dict) else {}
        rows.append(
            {
                "InvoiceNumber": header["InvoiceNumber"],
                "Date": header["Date"],
                "Contact": header["Contact"],
                "Description": line.get("description") or "",
                "LedgerAccount": _as_text(ledger) or ledger.get("nominal_code") or "",
                "Quantity": line.get("quantity") or "",
                "UnitPrice": line.get("unit_price") or line.get("actual_quantity") or "",
                "Net": line.get("net_amount") or "",
                "VAT": line.get("tax_amount") or "",
                "TaxRate": _as_text(tax),
                "Gross": line.get("total_amount") or "",
            }
        )
    return rows


def _contact_row(item: Dict[str, Any]) -> Dict[str, Any]:
    person = item.get("main_contact_person") if isinstance(item.get("main_contact_person"), dict) else {}
    return {
        "Name": item.get("name") or item.get("displayed_as") or "",
        "Reference": item.get("reference") or "",
        "Type": _contact_types(item),
        "Email": item.get("email") or person.get("email") or "",
        "Phone": item.get("telephone") or item.get("mobile") or person.get("telephone") or "",
        "Balance": item.get("balance") or "",
        "TaxNumber": item.get("tax_number") or "",
        "Id": item.get("id") or "",
    }


def _payment_row(item: Dict[str, Any]) -> Dict[str, Any]:
    contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
    return {
        "Date": item.get("date") or "",
        "Contact": _as_text(contact),
        "ContactId": (contact or {}).get("id") or "",
        "Amount": item.get("total_amount") or item.get("amount") or "",
        "Method": _as_text(item.get("payment_method")),
        "Reference": item.get("reference") or item.get("displayed_as") or "",
        "Type": _as_text(item.get("transaction_type")),
        "Id": item.get("id") or "",
    }


def _write_sales_ledger(
    token: str,
    bid: str,
    source,
    stamp: str,
    start,
) -> tuple:
    """Pull invoices, credit notes, customers, receipts. Returns (files, counts)."""
    files: List[str] = []
    counts: Dict[str, Any] = {}
    period = {"from_date": start.isoformat(), "to_date": stamp}

    invoices, err = _items(token, bid, "/sales_invoices", {**period, "attributes": "all"})
    if err and not invoices:
        invoices, err = _items(token, bid, "/sales_invoices", period)
    if invoices:
        rows = [_invoice_row(item) for item in invoices]
        name = f"{stamp} Sage Sales Invoices.csv"
        _write_csv(
            source / name,
            [
                "InvoiceNumber",
                "Date",
                "DueDate",
                "Contact",
                "ContactId",
                "Status",
                "Reference",
                "Net",
                "VAT",
                "Gross",
                "Outstanding",
                "Currency",
                "Id",
            ],
            rows,
        )
        files.append(name)
        counts["sales_invoices"] = len(rows)
        line_rows: List[Dict[str, Any]] = []
        for item in invoices:
            line_rows.extend(_invoice_line_rows(item))
        if line_rows:
            lname = f"{stamp} Sage Sales Invoice Lines.csv"
            _write_csv(
                source / lname,
                [
                    "InvoiceNumber",
                    "Date",
                    "Contact",
                    "Description",
                    "LedgerAccount",
                    "Quantity",
                    "UnitPrice",
                    "Net",
                    "VAT",
                    "TaxRate",
                    "Gross",
                ],
                line_rows,
            )
            files.append(lname)
            counts["sales_invoice_lines"] = len(line_rows)
    elif err:
        counts["sales_invoices_error"] = err

    credits, err = _items(token, bid, "/sales_credit_notes", {**period, "attributes": "all"})
    if err and not credits:
        credits, err = _items(token, bid, "/sales_credit_notes", period)
    if credits:
        rows = [_invoice_row(item) for item in credits]
        name = f"{stamp} Sage Sales Credit Notes.csv"
        _write_csv(
            source / name,
            [
                "InvoiceNumber",
                "Date",
                "DueDate",
                "Contact",
                "ContactId",
                "Status",
                "Reference",
                "Net",
                "VAT",
                "Gross",
                "Outstanding",
                "Currency",
                "Id",
            ],
            rows,
        )
        files.append(name)
        counts["sales_credit_notes"] = len(rows)
    elif err:
        counts["sales_credit_notes_error"] = err

    contacts, err = _items(token, bid, "/contacts", {"contact_type_id": "CUSTOMER", "attributes": "all"})
    if err and not contacts:
        contacts, err = _items(token, bid, "/contacts", {"contact_type_id": "CUSTOMER"})
    if err and not contacts:
        all_contacts, err2 = _items(token, bid, "/contacts")
        contacts = [
            c
            for c in all_contacts
            if "CUSTOMER" in _contact_types(c).upper() or "customer" in _contact_types(c).lower()
        ]
        if not contacts:
            contacts = all_contacts
            err = err or err2
    if contacts:
        rows = [_contact_row(item) for item in contacts]
        name = f"{stamp} Sage Customers.csv"
        _write_csv(
            source / name,
            ["Name", "Reference", "Type", "Email", "Phone", "Balance", "TaxNumber", "Id"],
            rows,
        )
        files.append(name)
        counts["contacts"] = len(rows)
    elif err:
        counts["contacts_error"] = err

    payments, err = _items(token, bid, "/contact_payments", period)
    if payments:
        rows = [_payment_row(item) for item in payments]
        name = f"{stamp} Sage Contact Payments.csv"
        _write_csv(
            source / name,
            ["Date", "Contact", "ContactId", "Amount", "Method", "Reference", "Type", "Id"],
            rows,
        )
        files.append(name)
        counts["contact_payments"] = len(rows)
    elif err:
        counts["contact_payments_error"] = err

    aged_written = False
    for path, params in (
        ("/reports/aged_debtors", {"from_date": stamp, "to_date": stamp}),
        ("/aged_debtors", {"date": stamp, "to_date": stamp}),
        ("/reports/aged_debtors_analyser", {"to_date": stamp}),
    ):
        ok, data, err = _get(token, bid, path, params)
        if not ok or not data:
            if err:
                counts.setdefault("aged_debtors_error", err)
            continue
        name = f"{stamp} Sage Aged Debtors.json"
        (source / name).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        files.append(name)
        rows = []
        batch = []
        if isinstance(data, dict):
            batch = data.get("$items") or data.get("items") or data.get("contacts") or []
        if isinstance(batch, list):
            for item in batch:
                if not isinstance(item, dict):
                    continue
                rows.append(
                    {
                        "Contact": _as_text(item.get("contact") or item),
                        "Current": item.get("current") or item.get("period_current") or "",
                        "Period1": item.get("period_1") or item.get("days_30") or "",
                        "Period2": item.get("period_2") or item.get("days_60") or "",
                        "Period3": item.get("period_3") or item.get("days_90") or "",
                        "Older": item.get("older") or item.get("days_older") or "",
                        "Total": item.get("total") or item.get("balance") or item.get("outstanding") or "",
                    }
                )
        if rows:
            cname = f"{stamp} Sage Aged Debtors.csv"
            _write_csv(
                source / cname,
                ["Contact", "Current", "Period1", "Period2", "Period3", "Older", "Total"],
                rows,
            )
            files.append(cname)
            counts["aged_debtors"] = len(rows)
        else:
            counts["aged_debtors"] = 1
        aged_written = True
        break
    if not aged_written:
        outstanding = [
            _invoice_row(item)
            for item in invoices
            if str(item.get("outstanding_amount") or "0") not in ("0", "0.0", "0.00", "")
        ]
        if outstanding:
            name = f"{stamp} Sage Outstanding Invoices (current).csv"
            _write_csv(
                source / name,
                [
                    "InvoiceNumber",
                    "Date",
                    "DueDate",
                    "Contact",
                    "ContactId",
                    "Status",
                    "Reference",
                    "Net",
                    "VAT",
                    "Gross",
                    "Outstanding",
                    "Currency",
                    "Id",
                ],
                outstanding,
            )
            files.append(name)
            counts["outstanding_invoices"] = len(outstanding)
            counts["aged_debtors_note"] = (
                "Sage aged-debtors report as at year end was not available. "
                "Outstanding column is current Sage balance, not historical YE."
            )

    return files, counts


def resolve_business(db: Session, client: Client) -> tuple:
    pb = get_or_create_playbook(db, client.id)
    tid = (pb.source_org_id or "").strip()
    if tid:
        return tid, ""
    conn = client_connections.get_connection(db, client.id, "sage")
    if conn and conn.enabled and conn.external_id:
        return conn.external_id.strip(), ""
    token, err, _ = get_valid_access_token(db, PROVIDER)
    if err:
        return "", err or "Sage is not connected."
    businesses, berr = fetch_sage_businesses(token or "")
    if len(businesses) == 1:
        return businesses[0]["tenantId"], ""
    if berr:
        return "", berr
    return "", "Choose the Sage business on the Playbook tab."


def assign_business(db: Session, client: Client, business_id: str, name: str = "") -> None:
    pb = get_or_create_playbook(db, client.id)
    pb.source_org_id = (business_id or "").strip() or None
    pb.bookkeeping_source = "sage_cloud"
    pb.updated_at = datetime.utcnow()
    client_connections.set_connection(
        db, client.id, "sage", enabled=bool(business_id), external_id=business_id or None, notes=name or None
    )
    db.commit()


def pull_client_books(db: Session, client: Client, *, as_at: Optional[date] = None) -> Dict[str, Any]:
    pb = get_or_create_playbook(db, client.id)
    as_at = as_at or default_as_at(db, client, pb)
    start = date(as_at.year - 1, as_at.month, as_at.day) + timedelta(days=1)
    bid, err = resolve_business(db, client)
    result: Dict[str, Any] = {
        "ok": False,
        "as_at": as_at.isoformat(),
        "tenant_id": bid,
        "files": [],
        "counts": {},
        "error": "",
        "source_dir": "",
    }
    if err:
        result["error"] = err
        return result
    token, terr, _ = get_valid_access_token(db, PROVIDER)
    if not token:
        result["error"] = terr or "Sage is not connected."
        return result
    source = _source_dir(db, client)
    result["source_dir"] = str(source)
    stamp = as_at.isoformat()
    files: List[str] = []
    counts: Dict[str, Any] = {}
    sales_only = is_sales_ledger_only(pb)
    result["sales_ledger_only"] = sales_only

    sales_files, sales_counts = _write_sales_ledger(token, bid, source, stamp, start)
    files.extend(sales_files)
    counts.update(sales_counts)

    if sales_only:
        meta = {
            "pulled_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "provider": "sage",
            "scope": "sales_ledger_only",
            "client_id": client.id,
            "business_id": bid,
            "as_at": stamp,
            "from_date": start.isoformat(),
            "counts": counts,
            "files": files,
            "note": (
                "Sage is the sales ledger only. Trial balance, bank and ledger "
                "accounts were not pulled. Year-end journals must not be posted to Sage."
            ),
        }
        (source / f"{stamp} sage pull.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        files.append(f"{stamp} sage pull.json")
        got = int(counts.get("sales_invoices") or 0) + int(counts.get("contacts") or 0)
        if not got:
            first_err = next((v for k, v in counts.items() if str(k).endswith("_error") and v), "")
            result["error"] = first_err or "Sage returned no sales invoices or customers."
            result.update(ok=False, files=files, counts=counts)
            return result
        result.update(ok=True, files=files, counts=counts)
        return result

    ok, tb, e = _get(
        token,
        bid,
        "/trial_balance",
        {"from_date": start.isoformat(), "to_date": stamp},
    )
    if ok and isinstance(tb, dict):
        rows = []
        for item in tb.get("$items") or tb.get("items") or tb.get("trial_balance") or []:
            if not isinstance(item, dict):
                continue
            ledger = item.get("ledger_account") or {}
            rows.append(
                {
                    "Code": (ledger.get("nominal_code") if isinstance(ledger, dict) else "")
                    or item.get("nominal_code")
                    or "",
                    "Name": (ledger.get("displayed_as") if isinstance(ledger, dict) else "")
                    or item.get("displayed_as")
                    or "",
                    "Debit": item.get("debit") or "",
                    "Credit": item.get("credit") or "",
                    "Balance": item.get("balance") or "",
                }
            )
        name = f"{stamp} Sage Trial Balance.csv"
        _write_csv(source / name, ["Code", "Name", "Debit", "Credit", "Balance"], rows)
        files.append(name)
        counts["trial_balance"] = len(rows)
    elif e:
        counts["trial_balance_error"] = e

    ok, acc, e = _get(token, bid, "/ledger_accounts")
    if ok and isinstance(acc, dict):
        rows = []
        for item in acc.get("$items") or acc.get("items") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "Code": item.get("nominal_code") or "",
                    "Name": item.get("displayed_as") or item.get("name") or "",
                    "Type": (item.get("ledger_account_type") or {}).get("displayed_as")
                    if isinstance(item.get("ledger_account_type"), dict)
                    else item.get("ledger_account_type") or "",
                    "Status": "hidden" if item.get("is_hidden") else "active",
                }
            )
        name = f"{stamp} Sage Ledger Accounts.csv"
        _write_csv(source / name, ["Code", "Name", "Type", "Status"], rows)
        files.append(name)
        counts["accounts"] = len(rows)
    elif e:
        counts["accounts_error"] = e

    ok, banks, e = _get(token, bid, "/bank_accounts")
    if ok and isinstance(banks, dict):
        rows = []
        for item in banks.get("$items") or banks.get("items") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "Name": item.get("displayed_as") or "",
                    "AccountNumber": item.get("account_number") or "",
                    "SortCode": item.get("sort_code") or "",
                    "Balance": item.get("balance") or "",
                    "Id": item.get("id") or "",
                }
            )
        name = f"{stamp} Sage Bank Accounts.csv"
        _write_csv(source / name, ["Name", "AccountNumber", "SortCode", "Balance", "Id"], rows)
        files.append(name)
        counts["bank_accounts"] = len(rows)
    elif e:
        counts["bank_error"] = e

    meta = {
        "pulled_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "provider": "sage",
        "scope": "full_books",
        "client_id": client.id,
        "business_id": bid,
        "as_at": stamp,
        "from_date": start.isoformat(),
        "counts": counts,
        "files": files,
    }
    (source / f"{stamp} sage pull.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    files.append(f"{stamp} sage pull.json")
    result.update(ok=True, files=files, counts=counts)
    return result


def post_journal_draft(db: Session, client: Client, filename: str, *, status: str = "DRAFT") -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "filename": filename, "posted": [], "error": ""}
    pb = get_or_create_playbook(db, client.id)
    if is_sales_ledger_only(pb):
        result["error"] = (
            "This client uses Sage for the sales ledger only. "
            "Year-end journals are not posted to Sage."
        )
        return result
    path, journals, err = load_journal_draft(db, client, filename)
    if err:
        result["error"] = err
        return result
    if any(not j["Balanced"] for j in journals):
        result["error"] = "One or more journals do not balance."
        return result
    bid, berr = resolve_business(db, client)
    if berr:
        result["error"] = berr
        return result
    token, terr, _ = get_valid_access_token(db, PROVIDER)
    if not token:
        result["error"] = terr or "Sage is not connected."
        return result
    posted = []
    for j in journals:
        payload = {
            "transaction_type_id": "JOURNAL",
            "date": j["Date"],
            "reference": j["Narration"][:25],
            "description": j["Narration"],
            "lines": [
                {
                    "ledger_account_id": ln["AccountCode"],
                    "debit": f"{ln['LineAmount']:.2f}" if ln["LineAmount"] > 0 else "0.00",
                    "credit": f"{abs(ln['LineAmount']):.2f}" if ln["LineAmount"] < 0 else "0.00",
                    "description": ln["Description"] or j["Narration"],
                }
                for ln in j["Lines"]
            ],
        }
        # Sage often wants ledger_account: {id} rather than nominal code.
        # We send nominal-looking codes; user may need to use Sage account ids.
        ok, data, perr = _post(token, bid, "/journals", payload)
        if not ok:
            result["error"] = perr or "Sage rejected a journal."
            result["posted"] = posted
            return result
        posted.append({"Narration": j["Narration"], "id": (data or {}).get("id")})
    path.with_suffix(".posted.json").write_text(
        json.dumps(
            {
                "posted_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "provider": "sage",
                "journals": posted,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    result.update(ok=True, posted=posted)
    return result
